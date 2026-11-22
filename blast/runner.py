"""Run a commit's candidate tests on the patched code, to learn which of them really fail.

The static selection (blast/analyze.py) says which tests *could* be hit.  Many of
them run the changed line and still pass, and a commit whose candidates all pass
needs no worker at all (2 s saved, plus the load).  Running them costs CPU, which
is charged second for second, so this is done only where blast/verify.py expects
a net saving.

Three pieces:

  materialize()  the patch as {path: new bytes | None}, or None when the patch is
                 not one this module can reproduce byte for byte (binary parts,
                 mode changes, hunks that needed fuzz, files outside sqlglot/ and
                 tests/ ...).  Such commits keep their static selection.
  Tree           one real copy of the pinned checkout in /tmp (real files and real
                 directories, compiled files included).  A job writes the patched
                 files into it and puts the pinned ones back afterwards, with their
                 time stamps, so the compiled files of the pinned commit stay valid.
  Runner         the zygote process (blast/runner_child.py) and its protocol.

Nothing here decides anything: `Runner.run` returns, for every test it was given,
"pass", "fail" or nothing.  Only a "pass" of a job that ran to its end lets the
caller drop a test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from . import config as C
from .diffparse import ApplyError, apply_hunks, parse_patch

# The only parts of the checkout a runnable patch may touch.
_ROOTS = ("sqlglot/", "tests/")


def materialize(data: bytes, read_pinned) -> tuple[dict[str, bytes | None] | None, str]:
    """-> ({path: new content, None for a removed file}, "") or (None, why not)."""
    try:
        patch = parse_patch(data)
    except Exception as exc:
        return None, f"unreadable patch ({type(exc).__name__})"
    if patch.problems:
        return None, patch.problems[0]
    if not patch.files:
        return None, "no file in the patch"
    files: dict[str, bytes | None] = {}

    def put(path: str, content: bytes | None) -> bool:
        if path in files or not path.startswith(_ROOTS) or ".." in path.split("/") or "__pycache__" in path.split("/"):
            return False
        files[path] = content
        return True

    for fc in patch.files:
        path = fc.path
        if not path or fc.problems:
            return None, f"{path}: {fc.problems[0] if fc.problems else 'no path'}"
        if fc.binary or fc.copied or fc.mode_change:
            return None, f"{path}: binary, copied or mode change"
        if any((mode or "").startswith(("12", "16")) for mode in (fc.old_mode, fc.new_mode)):
            return None, f"{path}: symbolic link or submodule"
        if any(not h.complete for h in fc.hunks):
            return None, f"{path}: truncated hunk"
        old_path = fc.old_path if fc.renamed and fc.old_path else path
        old_text = read_pinned(old_path)
        if fc.new_file:
            if old_text is not None or len(fc.hunks) != 1 or any(tag != "+" for tag, _ in fc.hunks[0].lines):
                return None, f"{path}: new file that is not one block of added lines"
            old_text = ""
        elif old_text is None:
            return None, f"{old_path}: not in the pinned commit"
        if fc.deleted:
            if not put(path, None):
                return None, f"{path}: outside sqlglot/ and tests/"
            continue
        if fc.hunks:
            try:
                applied = apply_hunks(old_text, fc.hunks, 0)
            except ApplyError as exc:
                return None, f"{path}: {exc}"
            if applied.fuzz or not _exact_fit(applied.old_lines, fc.hunks):
                return None, f"{path}: hunk does not sit where its header says, or fits only loosely"
            new_text = applied.new_text()
        else:
            new_text = old_text
        if fc.renamed and fc.old_path and fc.new_path and fc.old_path != fc.new_path:
            if read_pinned(fc.new_path) is not None or not put(fc.old_path, None) or not put(fc.new_path, _encode(new_text)):
                return None, f"{path}: rename this module cannot reproduce"
            continue
        if not fc.hunks:
            return None, f"{path}: no content change"
        if not put(path, _encode(new_text)):
            return None, f"{path}: outside sqlglot/ and tests/"
    return files, ""


def _exact_fit(old_lines: list[str], hunks) -> bool:
    """Every context and removed line equals the pinned line at the stated place (what `git apply` asks for)."""
    for h in hunks:
        want = [text for tag, text in h.lines if tag != "+"]
        at = h.old_first - 1
        if want and old_lines[at:at + len(want)] != want:
            return False
    return True


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


class Tree:
    """A writable copy of the pinned checkout."""

    def __init__(self, src: str, tmp: str):
        self.src = src
        self.root = os.path.join(tmp, f"blast-tree-{os.getpid()}")
        self.built = False

    def build(self) -> None:
        if self.built:
            return
        shutil.rmtree(self.root, ignore_errors=True)
        os.mkdir(self.root)
        with os.scandir(self.src) as it:
            for entry in it:
                target = os.path.join(self.root, entry.name)
                if entry.name in ("sqlglot", "tests"):
                    self._copy(entry.path, target)
                elif entry.name != ".git":
                    os.symlink(entry.path, target)
        self.built = True

    def _copy(self, src: str, dst: str) -> None:
        """Real directories and real files with the pinned time stamps.  Only the __pycache__
        directories are links: a compiled file is used when the size and time stamp it records
        equal those of the source beside it, so the pinned ones serve every unpatched file and
        are ignored for a patched one (nothing is ever written there: the checkout is read-only
        and the children run with bytecode writing off)."""
        os.mkdir(dst)
        with os.scandir(src) as it:
            entries = list(it)
        for entry in entries:
            target = os.path.join(dst, entry.name)
            if entry.name == "__pycache__":
                os.symlink(entry.path, target)
            elif entry.is_symlink():
                os.symlink(os.readlink(entry.path), target)
            elif entry.is_dir():
                self._copy(entry.path, target)
            else:
                shutil.copyfile(entry.path, target)
                st = entry.stat()
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))

    def apply(self, files: dict[str, bytes | None]) -> list:
        undo = []
        for rel, content in files.items():
            full = os.path.join(self.root, rel)
            if os.path.islink(full) or os.path.isdir(full):
                self.restore(undo)
                raise OSError(f"{rel}: not a plain file in the tree")
            if os.path.exists(full):
                st = os.stat(full)
                with open(full, "rb") as fh:
                    undo.append((full, fh.read(), (st.st_atime_ns, st.st_mtime_ns), st.st_mode, []))
            else:
                made = []
                d = os.path.dirname(full)
                while not os.path.isdir(d):
                    made.append(d)
                    d = os.path.dirname(d)
                undo.append((full, None, None, None, made))
            if content is None:
                os.unlink(full)
            else:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as fh:
                    fh.write(content)
        return undo

    def restore(self, undo: list) -> None:
        for full, content, stamps, mode, made in reversed(undo):
            if content is None:
                try:
                    os.unlink(full)
                except OSError:
                    pass
                for d in made:
                    shutil.rmtree(d, ignore_errors=True)
            else:
                with open(full, "wb") as fh:
                    fh.write(content)
                os.chmod(full, mode & 0o7777)
                os.utime(full, ns=stamps)

    def remove(self) -> None:
        if self.built:
            shutil.rmtree(self.root, ignore_errors=True)
            self.built = False


class Runner:
    def __init__(self, src: str | None = None, tmp: str | None = None, python: str | None = None):
        self.tree = Tree(src or C.SRC_DIR, tmp or C.TMP_DIR)
        self.python = python or C.RUNNER_PYTHON or sys.executable
        self.proc: subprocess.Popen | None = None
        self.jobs = 0
        self.cpu = 0.0          # CPU of the test processes, as the zygote measured it
        self.dead: str | None = None

    def start(self) -> None:
        if self.proc is not None or self.dead:
            return
        child = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner_child.py")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHASHSEED", None)   # the selector fixes it for its own comparisons; the labeling run did not
        # The run is given 2 cores, whatever the host has: process pools that tests create size
        # themselves by os.process_cpu_count(), which Python 3.13 takes from this variable.
        env["PYTHON_CPU_COUNT"] = "2"
        try:
            self.tree.build()
            self.proc = subprocess.Popen([self.python, child], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=env, cwd=self.tree.root, text=True, bufsize=1)
        except Exception as exc:
            self.dead = f"{type(exc).__name__}: {exc}"

    def run(self, files: dict[str, bytes | None], tests: list[tuple[int, str, float]], preimport=(), spawn=None,
            start_limit: float = 10.0, job_limit: float | None = None, trace: bool = False) -> dict:
        """tests: (number, pytest id, time limit in seconds).  -> the zygote's answer (see runner_child.py),
        or {"void": reason, "status": {}, "complete": False} when the job could not be run."""
        nothing = {"status": {}, "complete": False, "cpu": 0.0, "wall": 0.0}
        self.start()
        if self.dead or self.proc is None:
            return dict(nothing, void=f"runner unavailable ({self.dead})")
        try:
            undo = self.tree.apply(files)
        except Exception as exc:
            return dict(nothing, void=f"could not write the patched files ({type(exc).__name__}: {exc})")
        answer = None
        try:
            if job_limit is None:
                job_limit = start_limit + sum(limit for _n, _i, limit in tests) + 5.0
            job = {"id": self.jobs, "root": self.tree.root, "tests": [list(t) for t in tests], "preimport": list(preimport),
                   "spawn": spawn, "start_limit": start_limit, "job_limit": job_limit, "trace": trace}
            self.jobs += 1
            self.proc.stdin.write(json.dumps(job) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            if not line:
                raise OSError("the runner process ended")
            answer = json.loads(line)
            self.cpu += float(answer.get("cpu", 0.0))
        except Exception as exc:
            self.dead = f"{type(exc).__name__}: {exc}"
            self.close()
            answer = dict(nothing, void=f"runner failed ({self.dead})")
        finally:
            try:
                self.tree.restore(undo)
            except Exception as exc:      # the tree is no longer the pinned commit: never use it again
                self.dead = f"could not restore the tree ({exc})"
                self.close()
                answer = dict(nothing, void=self.dead)
        return answer

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            try:
                proc.stdout.close()
            except Exception:
                pass
        self.tree.remove()

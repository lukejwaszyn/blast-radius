"""The exact answer for code that runs at import.

A class-level or module-level edit changes values that exist once per process
and that other definitions copy, invert or compile into tries while sqlglot is
being imported.  Instead of guessing how far that goes, the patched tree is
imported in a child process and every class-level / module-level value is
fingerprinted and compared with the pinned tree's fingerprint (taken when the
image was built).  The changed names and dict keys are then looked up in
maps/reads.json; a module that no longer imports is looked up in
maps/modules.json.  No test runs.  Costs about half a second of CPU.

The patched tree is an overlay in /tmp: symlinks to the read-only pinned
checkout, real files only for what the patch changes, and the pinned
__pycache__ files for everything untouched.
"""

from __future__ import annotations

import json
import os
import sys
import time

from . import config as C


def available() -> bool:
    return os.path.isfile(os.path.join(C.BASE_DIR, "base.json")) and os.path.isdir(os.path.join(C.SRC_DIR, "sqlglot"))


def build_overlay(files: dict[str, str | None]) -> str:
    """files: {path under the checkout: new text, or None for a removed file}. -> overlay root."""
    import tempfile

    root = tempfile.mkdtemp(prefix="blast-", dir=C.TMP_DIR)
    files = {rel: text for rel, text in files.items() if rel.startswith("sqlglot/")}
    changed_dirs = set()
    for rel in files:
        d = os.path.dirname(rel)
        while d:
            changed_dirs.add(d)
            d = os.path.dirname(d)
    changed_dirs.add("sqlglot")

    def mirror(rel_dir: str):
        os.mkdir(os.path.join(root, rel_dir))
        src_dir = os.path.join(C.SRC_DIR, rel_dir)
        if not os.path.isdir(src_dir):
            return
        with os.scandir(src_dir) as it:
            entries = list(it)
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}"
            target = os.path.join(root, rel)
            if entry.name == "__pycache__":
                # compiled files of untouched modules only: a stale one must never be picked up
                os.mkdir(target)
                with os.scandir(entry.path) as pit:
                    for pyc in pit:
                        if f"{rel_dir}/{pyc.name.split('.')[0]}.py" not in files:
                            os.symlink(pyc.path, os.path.join(target, pyc.name))
            elif rel in files:
                continue
            elif entry.is_dir(follow_symlinks=False) and rel in changed_dirs:
                mirror(rel)
            else:
                os.symlink(entry.path, target)

    mirror("sqlglot")
    for rel, text in files.items():
        if text is None:
            continue
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(text.encode("utf-8", "surrogateescape"))
    return root


class Job:
    def __init__(self, analysis, root: str, proc, out_path: str):
        self.analysis = analysis
        self.root = root
        self.proc = proc
        self.out_path = out_path
        self.started = time.monotonic()
        self.result: dict | None = None
        self.error: str | None = None


def start(analysis) -> Job:
    import subprocess

    root = build_overlay(analysis.exact.files)
    out_path = os.path.join(root, "diff.json")
    child = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot_child.py")
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    out = open(out_path, "wb")
    try:
        proc = subprocess.Popen([sys.executable, "-S", child, "diff", root, os.path.join(C.BASE_DIR, "base.json")],
                                stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env,
                                cwd=root, start_new_session=True)
    finally:
        out.close()
    return Job(analysis, root, proc, out_path)


def finish(job: Job, timeout: float) -> None:
    """Wait for the child (at most `timeout` more seconds), read its answer, remove the overlay."""
    import shutil
    import subprocess

    try:
        code = job.proc.wait(timeout=max(timeout, 0.01))
        if code != 0:
            job.error = f"the import of the patched tree ended with status {code}"
        else:
            with open(job.out_path, "rb") as fh:
                job.result = json.load(fh)
    except subprocess.TimeoutExpired:
        job.error = "the import of the patched tree did not finish"
        try:
            os.killpg(job.proc.pid, 9)
        except OSError:
            job.proc.kill()
        job.proc.wait()
    except (OSError, ValueError) as exc:
        job.error = f"no answer from the import of the patched tree ({exc})"
    finally:
        shutil.rmtree(job.root, ignore_errors=True)


def groups_from(job: Job, maps, Group) -> list:
    """Candidate groups for what the comparison found."""
    out = []
    result = job.result or {}
    known = job.analysis.exact.names
    for name, keys in sorted(result.get("changed", {}).items()):
        if keys == "order" and name not in known:
            # Same members in another order, in a value the edit does not feed (maps/derive.json):
            # such tuples are built by iterating sets and differ from process to process.
            continue
        if name.endswith("|<bases>"):
            mask = maps.class_users(name.split("|")[0])
            if mask:
                out.append(Group(C.P_CLASS_USER, mask, f"exact: bases of {name.split('|')[0]} changed"))
            continue
        if isinstance(keys, list):
            by_key, whole = maps.readers(name, keys)
            if by_key:
                shown = keys if len(keys) <= 4 else keys[:4] + ["..."]
                out.append(Group(C.P_VALUE_EXACT, by_key, f"exact: {name}{shown} changed: tests that looked the keys up"))
            if whole:
                out.append(Group(C.P_VALUE_WHOLE, whole, f"exact: {name} changed: tests that took it as a whole"))
        else:
            _, any_reader = maps.readers(name)
            if any_reader:
                out.append(Group(C.P_VALUE_EXACT, any_reader, f"exact: {name} changed: tests that read it"))
    for module, message in sorted(result.get("errors", {}).items()):
        mask = maps.module_tests(module)
        rel = maps.module_path(module)
        if rel:
            mask |= maps.file_line_tests(rel)
            if rel.endswith("__init__.py"):
                mask |= maps.package_tests(module)
        if mask:
            out.append(Group(C.P_MODULE_BROKEN, mask, f"exact: {module} no longer imports ({message[:80]})"))
    return out

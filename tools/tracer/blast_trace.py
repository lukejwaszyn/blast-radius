"""Line tracer used to build the maps (never used at decision time).

One instance lives in every Python process of a tracing run (the pytest
process of one test file, the forked child of one test, and every subprocess a
test starts: multiprocessing "spawn" workers, ``python -c`` ...).  It is
switched on by ``sitecustomize.py`` when ``$BLAST_OUT`` is set.

What it records, for files under ``$BLAST_ROOT`` (the sqlglot checkout):

* executed lines, with ``sys.monitoring`` LINE events, plus the first line of
  every code object that is entered (PY_START): a LINE event only fires when
  the line changes, so a one-line lambda or def would otherwise never be seen
  being called.  Each location is switched off after its first hit and all
  locations are switched on again when the context changes, so the cost is
  one callback per line per context.
* three buckets per context: ``imp`` while a module of ``sqlglot`` is being
  imported (module bodies, class bodies, and every function they call, e.g.
  metaclasses); ``top`` while a module of ``tests`` is being imported and no
  sqlglot import is in progress (the test module's own top-level code and what
  it calls); ``body`` for everything else (the test itself).
* files opened / directories listed (audit events), in the same two buckets.
* which ``sqlglot``/``tests`` modules got imported.

A line is stored as ``file_id << 16 | lineno``; file ids come from
``$BLAST_OUT/files.json`` so that all processes agree on them.
"""

from __future__ import annotations

import json
import os
import sys
from threading import get_ident

ROOT = os.environ.get("BLAST_ROOT", "")
OUT = os.environ.get("BLAST_OUT", "")
TRACKED_PACKAGES = ("sqlglot", "tests")

_mon = sys.monitoring
TOOL = 3  # any free tool id
_DISABLE = _mon.DISABLE

_file_ids: dict[str, int] = {}  # relative path -> id
_code_file: dict[str, int | None] = {}  # co_filename -> id or None

body: set[int] = set()
imp: set[int] = set()
top: set[int] = set()  # run by a test module's own top-level code (no sqlglot import in progress)
opens_body: set = set()
opens_imp: set = set()
_depth = 0  # number of tracked imports in progress, all threads
_importing: dict[int, int] = {}  # thread id -> its own import depth
_importing_sq: dict[int, int] = {}  # thread id -> depth of imports of sqlglot's own modules
_started = False
_prefix = ""


def _resolve(filename: str):
    fid = None
    try:
        if filename and filename[0] != "<":
            real = os.path.realpath(filename)
            if real.startswith(_prefix):
                fid = _file_ids.get(real[len(_prefix) :])
    except Exception:
        fid = None
    _code_file[filename] = fid
    return fid


def _on_line(code, lineno):
    fid = _code_file.get(code.co_filename, -1)
    if fid == -1:
        fid = _resolve(code.co_filename)
    if fid is None:
        return _DISABLE
    # "importing" is per thread: a pool's helper thread may import a module (while
    # unpickling a result) at the same moment the test's own thread runs test code.
    if _depth and _importing.get(get_ident()):
        if _importing_sq.get(get_ident()):
            imp.add(fid << 16 | lineno)
        else:
            # a module of tests/ is being imported and this is its own code (or code it
            # calls) rather than the import of a sqlglot module: every test of the files
            # that import that test module depends on it
            top.add(fid << 16 | lineno)
    else:
        body.add(fid << 16 | lineno)
    return _DISABLE


def _on_start(code, _offset):
    # sys.monitoring reports a LINE only when the line *changes* inside a code object, so
    # a function whose whole body sits on its first line (`"X": lambda args: f(args)`,
    # `def f(x): return x`) would never show up when it is called.  Its entry counts as
    # the execution of its first line.
    return _on_line(code, code.co_firstlineno)


def _audit(event, args):
    # Keep this tiny: it runs for every audit event of the interpreter.
    if event == "open":
        path, mode = args[0], args[1]
        if type(path) is str or isinstance(path, (bytes, os.PathLike)):
            (opens_imp if _depth and _importing.get(get_ident()) else opens_body).add((_abs(path), mode or "r"))
    elif event == "os.listdir" or event == "os.scandir":
        path = args[0]
        if type(path) is str or isinstance(path, (bytes, os.PathLike)):
            (opens_imp if _depth and _importing.get(get_ident()) else opens_body).add((_abs(path), "dir"))
    elif event == "glob.glob" or event == "glob.glob/2":
        (opens_imp if _depth and _importing.get(get_ident()) else opens_body).add((_abs(args[0]), "glob"))


def _abs(path):
    try:
        path = os.fsdecode(path)
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        return path
    except Exception:
        return "?"


def _patch_import():
    import importlib._bootstrap as bootstrap

    original = bootstrap._load_unlocked

    def _load_unlocked(spec):
        global _depth
        name = spec.name
        package = name.split(".", 1)[0]
        if package in TRACKED_PACKAGES:
            tid = get_ident()
            mine = _importing.get(tid, 0)
            mine_sq = _importing_sq.get(tid, 0)
            is_sq = package == "sqlglot"
            # switch every line back on whenever the bucket changes
            if mine == 0 or (is_sq and mine_sq == 0):
                _mon.restart_events()
            _importing[tid] = mine + 1
            if is_sq:
                _importing_sq[tid] = mine_sq + 1
            _depth += 1
            try:
                return original(spec)
            finally:
                _depth -= 1
                if is_sq:
                    if mine_sq:
                        _importing_sq[tid] = mine_sq
                    else:
                        _importing_sq.pop(tid, None)
                if mine:
                    _importing[tid] = mine
                else:
                    _importing.pop(tid, None)
                if mine == 0 or (is_sq and mine_sq == 0):
                    _mon.restart_events()
        return original(spec)

    bootstrap._load_unlocked = _load_unlocked


def start():
    """Idempotent.  Called once per process, as early as possible."""
    global _started, _prefix
    if _started or not OUT or not ROOT:
        return
    _started = True
    _prefix = os.path.realpath(ROOT).rstrip(os.sep) + os.sep
    if os.environ.get("BLAST_READS") or os.environ.get("BLAST_CMP"):
        return  # the "reads" and "comparisons" passes record through the rewritten sources, not through lines
    with open(os.path.join(OUT, "files.json")) as fh:
        for i, rel in enumerate(json.load(fh)):
            _file_ids[rel] = i
    _mon.use_tool_id(TOOL, "blast")
    _mon.register_callback(TOOL, _mon.events.LINE, _on_line)
    _mon.register_callback(TOOL, _mon.events.PY_START, _on_start)
    _mon.set_events(TOOL, _mon.events.LINE | _mon.events.PY_START)
    sys.addaudithook(_audit)
    _patch_import()


def reset():
    """Start a new context: forget everything recorded so far."""
    global _depth
    _depth = 0  # a fork keeps only the forking thread; no import is in progress in it
    _importing.clear()
    _importing_sq.clear()
    top.clear()
    body.clear()
    imp.clear()
    opens_body.clear()
    opens_imp.clear()
    if not (os.environ.get("BLAST_READS") or os.environ.get("BLAST_CMP")):
        _mon.restart_events()


def tracked_modules() -> set[str]:
    return {
        name
        for name in list(sys.modules)
        if name.split(".", 1)[0] in TRACKED_PACKAGES and sys.modules.get(name) is not None
    }


def _rel_opens(entries):
    out = set()
    for path, mode in entries:
        try:
            real = os.path.realpath(path)
        except Exception:
            continue
        if not real.startswith(_prefix):
            continue
        rel = real[len(_prefix) :]
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        kind = "d" if mode in ("dir", "glob") else ("w" if any(c in mode for c in "wax+") else "r")
        out.add(f"{kind}:{rel}")
    return sorted(out)


def snapshot(**extra) -> dict:
    data = {
        "pid": os.getpid(),
        "body": sorted(body),
        "imp": sorted(imp),
        "top": sorted(top),
        "opens_body": _rel_opens(opens_body),
        "opens_imp": _rel_opens(opens_imp),
        "modules": sorted(tracked_modules()),
    }
    data.update(extra)
    return data


def dump(name: str, **extra) -> None:
    path = os.path.join(OUT, name)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(snapshot(**extra), fh, separators=(",", ":"))
    os.replace(tmp, path)

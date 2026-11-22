"""Runs sqlglot tests on a patched tree, in a process of its own.

    python runner_child.py            (started once by blast/runner.py)

This process (the "zygote") imports what never changes between commits -- the
standard library modules the tests use, and numpy / pandas / duckdb when a job
asks for them -- and then, for every job it reads on stdin, forks a child.  The
child imports sqlglot and the test files *fresh* from the job's tree (nothing of
sqlglot or of the tests is ever imported in the zygote), runs the asked tests in
the order pytest would, and reports each outcome as soon as it is known.

A job is one JSON line:
    {"id": .., "root": tree, "tests": [[number, "tests/x.py::Class::test", limit_s], ..],
     "preimport": [module, ..], "start_limit": s, "spawn": "fork" | null}
and the answer is one JSON line:
    {"id": .., "status": {number: "pass" | "fail" | "skip" | "odd" | "timeout"},
     "complete": bool, "void": reason | null, "cpu": seconds, "wall": seconds}

What the caller may conclude:
  "pass"      the test ran to its end, clean-ups included, with no failure, error or failed subtest;
  "fail"      it failed (the child stops a test at its first failed subtest);
  anything else, a missing number, "complete": false or a "void" reason
              means nothing is known and the test must be kept.
"void" is set when something failed outside a test (setUpClass, tearDownClass, a class clean-up,
setUpModule): pytest reports such an error on a test that this runner may already have called
passed, so no "pass" of the job can be trusted.
"""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time

# What a child needs whatever the commit.  The rest of the standard library that sqlglot and its
# tests import comes with each job ("preimport", from maps/runner.json) and is loaded here once.
STDLIB = ("unittest", "importlib", "signal")


def _emit(fd: int, text: str) -> None:
    os.write(fd, text.encode())


def _run_tests(job: dict, fd: int) -> None:
    """In the child: import, run, report.  Ends the process."""
    import unittest

    root = job["root"]
    os.environ["SKIP_INTEGRATION"] = "0"       # the labeling environment (kit README)
    os.environ["PYTHONPATH"] = root
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(root)
    sys.path.insert(0, root)
    sys.argv = ["pytest"]

    limits = {number: limit for number, _id, limit in job["tests"]}
    state = {"number": None}

    def on_alarm(_signum, _frame):
        # The running test (or the import before it) is over its limit.  Nothing after this point
        # could be trusted, so the process ends here; the caller keeps every test without an answer.
        if state["number"] is not None:
            _emit(fd, f"R {state['number']} timeout\n")
        _emit(fd, "T\n")
        os._exit(3)

    signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, float(job.get("start_limit", 10.0)))

    if job.get("spawn") == "fork":
        # Tests that hand work to a process pool ask for the "spawn" start method: every worker is a
        # new interpreter that imports sqlglot, the test file, pandas and duckdb again (about 2 s of
        # CPU each).  A forked worker starts from this process instead, which has imported exactly
        # the same patched files.
        import multiprocessing

        real_get_context = multiprocessing.get_context

        def get_context(method=None):
            return real_get_context("fork" if method == "spawn" else method)

        multiprocessing.get_context = get_context     # the test files do `from multiprocessing import get_context`

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.failfast = True       # a test stops at its first failed subtest
            self.numbers = {}
            self.bad = self.skip = self.odd = False

        def startTest(self, test):
            super().startTest(test)
            self.bad = self.skip = self.odd = False
            number = self.numbers.get(id(test))
            state["number"] = number
            _emit(fd, f"S {number}\n")
            signal.setitimer(signal.ITIMER_REAL, float(limits.get(number, 5.0)))

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            number = state["number"]
            state["number"] = None
            status = "fail" if self.bad else "skip" if self.skip else "odd" if self.odd else "pass"
            _emit(fd, f"R {number} {status}\n")
            super().stopTest(test)
            self.shouldStop = False    # failfast is per test here, not for the run
            signal.setitimer(signal.ITIMER_REAL, float(job.get("start_limit", 10.0)))

        def _problem(self, test):
            if isinstance(test, unittest.TestCase) and id(test) in self.numbers and state["number"] == self.numbers[id(test)]:
                self.bad = True
            else:
                # setUpClass, tearDownClass, class clean-ups, setUpModule ...: outside any test
                _emit(fd, "X " + f"{type(test).__name__}: {str(test)[:120]}".replace("\n", " ") + "\n")

        def addError(self, test, err):
            super().addError(test, err)
            self._problem(test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._problem(test)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self._problem(test)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            if state["number"] is not None:
                self.skip = True
            else:
                _emit(fd, "X skip outside a test\n")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self.odd = True

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self.odd = True

    import importlib

    if job.get("trace"):
        _trace_processes(fd, state)
    result = Result()
    cases = []
    modules: dict[str, object] = {}
    for number, test_id, _limit in sorted(job["tests"]):
        try:
            path, cls_name, method = test_id.split("::")
        except ValueError:
            _emit(fd, f"R {number} odd\n")
            continue
        name = path[:-3].replace("/", ".")
        if name not in modules:
            try:
                modules[name] = importlib.import_module(name)
            except BaseException as exc:  # the file cannot be imported: each of its tests fails
                if isinstance(exc, KeyboardInterrupt):
                    raise
                modules[name] = None
        module = modules[name]
        if module is None:
            _emit(fd, f"R {number} fail\n")
            continue
        try:
            case = getattr(module, cls_name)(method)
        except Exception:
            _emit(fd, f"R {number} odd\n")      # no such class or test any more: not for this runner to judge
            continue
        result.numbers[id(case)] = number
        cases.append(case)
    if job.get("trace"):
        prefixes = tuple(p for p in sys.path if p.endswith(("site-packages", "dist-packages")))
        external = sorted({name.split(".")[0] for name, mod in list(sys.modules.items())
                           if (getattr(mod, "__file__", None) or "").startswith(prefixes)}) if prefixes else []
        _emit(fd, "M " + ",".join(external) + "\n")
    _emit(fd, "I\n")
    if cases:
        suite = unittest.TestSuite(cases)
        suite.run(result)
    signal.setitimer(signal.ITIMER_REAL, 0)
    _emit(fd, "D\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _trace_processes(fd: int, state: dict) -> None:
    """Map building only (tools/runner_map.py): report every process a test starts."""
    import _posixsubprocess
    import subprocess

    def wrap(owner, name):
        real = getattr(owner, name, None)
        if real is None:
            return

        def traced(*args, **kwargs):
            _emit(fd, f"P {state['number']}\n")
            return real(*args, **kwargs)

        setattr(owner, name, traced)

    wrap(_posixsubprocess, "fork_exec")
    wrap(subprocess, "_fork_exec")
    wrap(os, "posix_spawn")
    wrap(os, "posix_spawnp")
    wrap(os, "fork")
    wrap(os, "forkpty")
    wrap(os, "system")
    wrap(os, "popen")


def _child(job: dict, fd: int) -> None:
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for target in (0, 1, 2):
            os.dup2(null, target)
        _run_tests(job, fd)
    except BaseException:
        pass
    finally:
        os._exit(4)


def serve() -> None:
    import gc
    import importlib

    for name in STDLIB:
        try:
            importlib.import_module(name)
        except Exception:
            pass
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        job = json.loads(line)
        before = len(sys.modules)
        for name in job.get("preimport", ()):
            if name not in sys.modules:
                try:
                    importlib.import_module(name)
                except Exception:
                    pass
        if len(sys.modules) != before:
            gc.collect()
            gc.freeze()     # what was imported here is never freed: keep the children from touching it
        started = time.monotonic()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            _child(job, write_fd)
        os.close(write_fd)
        # The child ends itself when a test is over its limit, but its alarm is never delivered
        # while C code loops (`sum(iter(int, 1))`).  So the same limits are kept here, a little
        # later: `start_limit` for everything outside a test (imports, class set-up), each test's
        # own limit while it runs, `job_limit` for the whole job.
        grace = float(job.get("grace", 0.5))
        start_limit = float(job.get("start_limit", 10.0))
        limits = {str(number): float(limit) for number, _id, limit in job["tests"]}
        end = started + float(job.get("job_limit", 60.0))
        deadline = started + start_limit + grace
        buf = b""
        seen = 0
        while True:
            left = min(deadline, end) - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([read_fd], [], [], left)
            if not ready:
                break
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            buf += chunk
            last = buf.rfind(b"\n")
            for raw in buf[seen:last + 1].decode("utf-8", "replace").splitlines():
                kind, _, rest = raw.partition(" ")
                if kind == "S":
                    deadline = time.monotonic() + limits.get(rest, start_limit) + grace
                elif kind in ("R", "I"):
                    deadline = time.monotonic() + start_limit + grace
            seen = last + 1
        os.close(read_fd)
        try:
            os.killpg(pid, signal.SIGKILL)     # the child and whatever its tests started
        except OSError:
            pass
        try:
            _pid, _status, usage = os.wait4(pid, 0)
            cpu = usage.ru_utime + usage.ru_stime
        except OSError:
            cpu = 0.0
        status: dict[str, str] = {}
        spawned: list[str] = []
        external = None
        complete = False
        void = None
        imported = False
        for raw in buf.decode("utf-8", "replace").splitlines():
            kind, _, rest = raw.partition(" ")
            if kind == "R":
                number, _, value = rest.partition(" ")
                status[number] = value
            elif kind == "D":
                complete = True
            elif kind == "I":
                imported = True
            elif kind == "P":
                spawned.append(rest)
            elif kind == "M":
                external = [name for name in rest.split(",") if name]
            elif kind == "X" and void is None:
                void = rest or "error outside a test"
            elif kind == "T" and void is None:
                void = "over the time limit"
        answer = {"id": job.get("id"), "status": status, "complete": complete, "void": void,
                  "imported": imported, "cpu": round(cpu, 4), "wall": round(time.monotonic() - started, 4)}
        if job.get("trace"):
            answer["spawned"] = spawned
            answer["external"] = external
        out.write(json.dumps(answer) + "\n")
        out.flush()


if __name__ == "__main__":
    serve()

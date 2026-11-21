"""pytest plugin for the tracing runs (``-p blast_pytest``), one test file per
pytest process.

* After collection it writes what importing the test file executed
  ("file.<slug>.json").
* Every test then runs in its own fork of that process, so nothing a test
  leaves behind (lazily imported dialects, caches, class-level setUp) can hide
  work from a later test: each test is recorded as if it ran alone right after
  its file was imported.  setUpClass and friends run inside the fork and are
  therefore part of the test.  The fork writes "t.<slug>.<n>.json".

With $BLAST_READS set the same plugin records attribute/key reads instead of
lines (see blast_reads.py).
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

import blast_trace as bt

READS = bool(os.environ.get("BLAST_READS"))
if READS:
    import blast_reads as br

_slug = None
_index = 0
_base_modules: set[str] = set()


def _slugify(path: str) -> str:
    return path.replace("/", "__").replace(".py", "")


def pytest_collection_finish(session):
    global _slug, _base_modules
    files = sorted({item.nodeid.split("::")[0] for item in session.items})
    _slug = _slugify(files[0]) if files else "none"
    _base_modules = bt.tracked_modules()
    extra = dict(
        kind="file",
        files=files,
        tests=[item.nodeid for item in session.items],
    )
    if READS:
        extra["reads"] = br.snapshot()
    bt.dump(f"file.{_slug}.json", **extra)


class _Outcome:
    def __init__(self):
        self.failed = []
        self.skipped = []

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.append([report.when, type(report).__name__, str(report.longrepr)[-600:]])
        elif report.skipped:
            self.skipped.append(report.when)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global _index
    index = _index
    _index += 1
    name = f"t.{_slug}.{index}"
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid:
        _, status = os.waitpid(pid, 0)
        if status != 0:
            with open(os.path.join(bt.OUT, name + ".crash"), "w") as fh:
                fh.write(f"{item.nodeid}\nstatus {status}\n")
        return True

    # ---- child: this process runs exactly one test and exits ----
    code = 1
    try:
        os.environ["BLAST_CTX"] = name
        outcome = _Outcome()
        item.config.pluginmanager.register(outcome, "blast-outcome")
        bt.reset()
        if READS:
            br.reset()
        start = time.time()
        from _pytest.runner import runtestprotocol

        runtestprotocol(item, nextitem=None, log=True)
        wall = time.time() - start
        # Subprocesses of the test write their own files when they exit.  The tests
        # join their pools / subprocess.run themselves, so this is only a short grace
        # period (multiprocessing's resource tracker never exits before we do).
        deadline = time.time() + 0.3
        while time.time() < deadline:
            try:
                done, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if not done:
                time.sleep(0.01)
        extra = dict(
            kind="test",
            ctx=name,
            test=item.nodeid,
            failed=outcome.failed,
            skipped=outcome.skipped,
            wall=round(wall, 3),
            new_modules=sorted(bt.tracked_modules() - _base_modules),
        )
        if READS:
            extra["reads"] = br.snapshot()
        bt.dump(name + ".json", **extra)
        code = 0
    except BaseException as exc:  # never return into pytest's loop from the child
        try:
            with open(os.path.join(bt.OUT, name + ".crash"), "w") as fh:
                fh.write(f"{item.nodeid}\n{exc!r}\n")
        except Exception:
            pass
    finally:
        os._exit(code)

#!/usr/bin/env python3
"""Image build step: the checkout must be the commit the maps were made from,
and the selector must answer a few made-up patches as user-readable files.

    python check_image.py <checkout> <maps dir> <selector.py>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


def main() -> None:
    checkout, maps_dir, selector = sys.argv[1:4]
    with open(os.path.join(maps_dir, "static.json")) as fh:
        static = json.load(fh)["files"]
    wrong = []
    for rel, info in static.items():
        full = os.path.join(checkout, rel)
        if not os.path.isfile(full):
            wrong.append(f"{rel}: missing")
            continue
        with open(full, "rb") as fh:
            lines = fh.read().count(b"\n")
        if abs(lines - info["lines"]) > 1:
            wrong.append(f"{rel}: {lines} lines, the maps say {info['lines']}")
    if wrong:
        raise SystemExit("checkout does not match the maps:\n  " + "\n  ".join(wrong[:20]))
    with open(os.path.join(maps_dir, "tests.json")) as fh:
        tests = json.load(fh)
    with tempfile.TemporaryDirectory() as data:
        os.mkdir(os.path.join(data, "commits"))
        with open(os.path.join(data, "tests.json"), "w") as fh:
            json.dump({"tests": [{"number": i, "id": tid, "file": tid.split("::")[0], "seconds": s}
                                 for i, (tid, s) in enumerate(zip(tests["ids"], tests["seconds"]))]}, fh)
        patches = {
            "comment": "--- a/sqlglot/trie.py\n+++ b/sqlglot/trie.py\n@@ -1,2 +1,3 @@\n import typing as t\n+# note\n from enum import Enum, auto\n",
            "syntax": "--- a/sqlglot/trie.py\n+++ b/sqlglot/trie.py\n@@ -1,2 +1,3 @@\n import typing as t\n+def (:\n from enum import Enum, auto\n",
            "value": "--- a/sqlglot/dialects/tsql.py\n+++ b/sqlglot/dialects/tsql.py\n@@ -17 +17 @@\n-    WEEK_OFFSET = -1\n+    WEEK_OFFSET = -3\n",
        }
        for name, text in patches.items():
            with open(os.path.join(data, "commits", name + ".patch"), "w") as fh:
                fh.write(text)
        # the static answer (the runner is checked below), with time limits a slow build machine cannot hit
        env = dict(os.environ, BLAST_DATA=data, BLAST_VERIFY="off", BLAST_SNAPSHOT_TIMEOUT="300", BLAST_WALL_BUDGET="3000")
        out = subprocess.run([sys.executable, "-S", selector], env=env, stdout=subprocess.PIPE, check=True).stdout
        got = {}
        for line in out.decode().splitlines():
            row = json.loads(line)
            got[row["commit"]] = sum(len(w) for w in row["workers"])
        if got.get("comment") != 0 or got.get("syntax", 0) < 1000 or not 1 <= got.get("value", 0) <= 50:
            raise SystemExit(f"selector self-check failed: {got}")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from blast.analyze import PAIR_FIXTURE_SOURCE

    for rel, wanted in PAIR_FIXTURE_SOURCE.items():
        with open(os.path.join(checkout, rel), encoding="utf-8") as fh:
            have = set(fh.read().split("\n"))
        gone = [line for line in wanted if line not in have]
        if gone:
            raise SystemExit(f"{rel} is not the file the fixture rules were read from: {gone}")
    imports_check(checkout, sorted({tid.split("::")[0] for tid in tests["ids"]}))
    runner_check(checkout, tests["ids"])
    print(f"image check passed: {len(static)} files match the maps; self-check {got}")


def imports_check(checkout: str, files: list[str]) -> None:
    """Every test file of the pinned commit must import in this image: a missing package would
    make the runner call its tests failing (harmless) but would also mean the environment is not
    the one the tests were labeled in."""
    code = ("import importlib, sys\nsys.path.insert(0, sys.argv[1])\n"
            "for name in sys.argv[2:]:\n    importlib.import_module(name)\n")
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", code, checkout] + [f[:-3].replace("/", ".") for f in files],
                          env=env, cwd=checkout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise SystemExit("a test file does not import in this image:\n" + proc.stderr.decode(errors="replace")[-3000:])


def runner_check(checkout: str, ids: list[str]) -> None:
    """The decision-time runner must call a passing test passing and a broken one failing, in this image."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from blast.runner import Runner, materialize

    def read(rel):
        try:
            with open(os.path.join(checkout, rel), "rb") as fh:
                return fh.read().decode("utf-8", "surrogateescape")
        except OSError:
            return None

    wanted = ["tests/test_time.py::TestTime::test_format_time", "tests/dialects/test_tsql.py::TestTSQL::test_datepart",
              "tests/test_executor.py::TestExecutor::test_py_dialect"]
    numbers = [(ids.index(t), t, 60.0) for t in wanted if t in ids]
    if len(numbers) != len(wanted):
        raise SystemExit("runner self-check: the tests it uses are not in the maps")
    patch = b"--- a/sqlglot/time.py\n+++ b/sqlglot/time.py\n@@ -2,2 +2,3 @@\n import datetime\n+raise RuntimeError('x')\n import typing as t\n"
    with tempfile.TemporaryDirectory() as tmp:
        runner = Runner(src=checkout, tmp=tmp, python=sys.executable)
        try:
            good = runner.run({}, numbers, preimport=["numpy", "pandas", "duckdb"], start_limit=120.0)
            files, why = materialize(patch, read)
            if files is None:
                raise SystemExit(f"runner self-check: made-up patch not reproducible ({why})")
            bad = runner.run(files, numbers[:2], start_limit=120.0)
            again = runner.run({}, numbers[:2], start_limit=120.0)
        finally:
            runner.close()
    def verdict(answer):
        return [answer["status"].get(str(n)) for n, _t, _l in numbers]
    if not good.get("complete") or good.get("void") or verdict(good) != ["pass"] * 3:
        raise SystemExit(f"runner self-check: pinned tests do not pass: {good}")
    if verdict(bad)[:2] != ["fail", "fail"]:
        raise SystemExit(f"runner self-check: a module that raises at import was not seen: {bad}")
    if verdict(again)[:2] != ["pass", "pass"]:
        raise SystemExit(f"runner self-check: the tree was not restored: {again}")


if __name__ == "__main__":
    main()

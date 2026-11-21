#!/usr/bin/env python3
"""Run sqlglot's test suite under the map-building tracer.

    python tools/run_trace.py --src <sqlglot checkout> --python <python with requirements.lock> \
        --out <raw output dir> [--jobs 4] [--reads --instrumented <dir>]

One pytest process per test file (so each file's import is recorded alone);
inside it every test runs in its own fork (tools/tracer/blast_pytest.py).
Raw results are JSON files in --out; tools/aggregate.py turns them into maps/.

--reads runs the second pass: the tests import an instrumented copy of sqlglot
(made by tools/instrument.py) that reports which class-level / module-level
attributes and dict keys are read.

--cmp runs the comparisons pass on the copy made by tools/instrument_cmp.py:
for every `is` / `is not` / `<` / `<=` / `>` / `>=` inside a function, did the
neighbouring operator ever give another result in this test?
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TRACER = os.path.join(HERE, "tracer")

# slowest files first so the pool finishes evenly
BIG_FIRST = ["tests/test_parser.py", "tests/test_executor.py", "tests/test_optimizer.py"]


def list_py(root: str) -> list[str]:
    out = []
    for top in ("sqlglot", "tests", "sqlglotc"):
        for base, dirs, files in os.walk(os.path.join(root, top)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(out)


def test_files(root: str) -> list[str]:
    files = [f for f in list_py(root) if f.startswith("tests/") and os.path.basename(f).startswith("test_")]
    files.sort(key=lambda f: (BIG_FIRST.index(f) if f in BIG_FIRST else len(BIG_FIRST), f))
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--reads", action="store_true")
    ap.add_argument("--cmp", action="store_true", help="comparisons pass (tree made by tools/instrument_cmp.py)")
    ap.add_argument("--instrumented", help="instrumented tree (with --reads or --cmp)")
    ap.add_argument("--only", nargs="*", help="only these test files")
    ap.add_argument("--seed", default="0", help="PYTHONHASHSEED of the test processes (set order can change paths)")
    args = ap.parse_args()

    src = os.path.realpath(args.src)
    args.python = os.path.abspath(args.python)  # not realpath: keep the venv's own launcher
    out = os.path.realpath(args.out)
    os.makedirs(out, exist_ok=True)
    files = list_py(src)
    with open(os.path.join(out, "files.json"), "w") as fh:
        json.dump(files, fh)

    run_root = os.path.realpath(args.instrumented) if (args.reads or args.cmp) else src
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([run_root, TRACER]),
        PATH=os.path.dirname(os.path.abspath(args.python)) + os.pathsep + env.get("PATH", ""),
        SKIP_INTEGRATION="0",
        TZ="UTC",
        BLAST_OUT=out,
        BLAST_ROOT=run_root,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED=args.seed,
        PYTHON_CPU_COUNT="2",  # size of the tests' own process pools
        OBJC_DISABLE_INITIALIZE_FORK_SAFETY="YES",  # macOS: allow fork() after numpy/duckdb
    )
    env.pop("BLAST_CTX", None)
    env.pop("BLAST_READS", None)
    env.pop("BLAST_CMP", None)
    if args.reads:
        env["BLAST_READS"] = "1"
    if args.cmp:
        env["BLAST_CMP"] = "1"

    todo = args.only or test_files(src)

    def run(test_file: str):
        start = time.time()
        log = os.path.join(out, "log." + test_file.replace("/", "__") + ".txt")
        with open(log, "w") as fh:
            proc = subprocess.run(
                [args.python, "-m", "pytest", "-p", "blast_pytest", "-p", "no:cacheprovider",
                 "-q", "-o", "addopts=", test_file],
                cwd=run_root, env=env, stdout=fh, stderr=subprocess.STDOUT,
            )
        return test_file, proc.returncode, time.time() - start

    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        for test_file, code, wall in pool.map(run, todo):
            print(f"{test_file}: exit {code}, {wall:.1f}s", file=sys.stderr, flush=True)
    print(f"done in {time.time() - start:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()

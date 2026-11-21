#!/usr/bin/env python3
"""Lines a test can execute only because of what earlier tests left behind -> maps/warm.json.

    python tools/warm_trace.py --src <checkout> --python <python with the lock> --work <dir> --maps maps \
        [--orders forward reverse shuffle:1 ... shuffle:6] [--reuse]

cov.json records each test alone (a fork per test).  In a real run tests share a
process: `tests/test_dialect_entry_points.py` clears the dialect registry in its
setUp/tearDown, and every later test in that process that looks a dialect up
runs the "module already imported, class not registered" branch of
`Dialect._try_load`, which no test runs alone.  An edit of such a line breaks
tests that the isolated coverage does not list.

This tool runs the whole suite in ONE process under the line tracer, once per
order, and collects every (line, test) pair that the isolated coverage lacks.
Which tests reach such a line depends on the order the suite happens to run
in, so the map does not store the observed tests alone: for each such line it
stores them united with every test that runs the enclosing function alone
(any of those can be the one that comes after the test that left the state).
No patch is involved; the input is the pinned commit and its own test suite.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from mapfmt import enc  # noqa: E402

TRACER = os.path.join(HERE, "tracer")


def list_py(root: str) -> list[str]:
    out = []
    for top in ("sqlglot", "tests", "sqlglotc"):
        for base, dirs, files in os.walk(os.path.join(root, top)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(out)


def run_order(src: str, python: str, out: str, order: str) -> None:
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "files.json"), "w") as fh:
        json.dump(list_py(src), fh)
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([src, TRACER]),
        PATH=os.path.dirname(os.path.abspath(python)) + os.pathsep + env.get("PATH", ""),
        SKIP_INTEGRATION="0", TZ="UTC", BLAST_OUT=out, BLAST_ROOT=src, BLAST_ORDER=order,
        PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", PYTHON_CPU_COUNT="2",
    )
    env.pop("BLAST_CTX", None)
    start = time.time()
    with open(os.path.join(out, "log.txt"), "w") as fh:
        proc = subprocess.run([python, "-m", "pytest", "-p", "blast_warm", "-p", "no:cacheprovider", "-q", "-o", "addopts=", "tests"],
                              cwd=src, env=env, stdout=fh, stderr=subprocess.STDOUT)
    print(f"order {order}: pytest exit {proc.returncode}, {time.time() - start:.0f}s", file=sys.stderr, flush=True)
    if proc.returncode not in (0, 1) or (proc.returncode and order == "forward"):
        raise SystemExit(f"the suite did not run in order {order}: see {out}/log.txt")
    # In another order a few tests fail at the pinned commit itself (they depend on what ran
    # before them); their traces still say which lines left-over state can lead to.


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--work", required=True)
    ap.add_argument("--maps", required=True)
    ap.add_argument("--orders", nargs="+", default=["forward", "reverse"] + [f"shuffle:{i}" for i in range(1, 7)])
    ap.add_argument("--reuse", action="store_true", help="use the traces already in --work")
    args = ap.parse_args()
    src = os.path.realpath(args.src)

    from blast.maps import Maps, bits

    maps = Maps(args.maps)
    number = {tid: i for i, tid in enumerate(maps.ids)}
    extra: dict[tuple[str, int], int] = {}   # (file, line) -> tests seen on it only in a shared process
    seen_orders = []
    for order in args.orders:
        out = os.path.join(args.work, "warm." + order.replace(":", "_"))
        if not (args.reuse and glob.glob(os.path.join(out, "w.*.json"))):
            run_order(src, os.path.abspath(args.python), out, order)
        files = json.load(open(os.path.join(out, "files.json")))
        pairs = 0
        for path in glob.glob(os.path.join(out, "w.*.json")):
            with open(path) as fh:
                row = json.load(fh)
            i = number.get(row["test"])
            if i is None:
                continue
            for code in row["body"]:
                rel, line = files[code >> 16], code & 0xFFFF
                if not rel.startswith("sqlglot/"):
                    continue
                if not ((maps.call_tests(rel, line, line, warm=False) | maps.import_tests(rel, line, line)) >> i) & 1:
                    extra[(rel, line)] = extra.get((rel, line), 0) | (1 << i)
                    pairs += 1
        seen_orders.append({"order": order, "pairs_not_in_isolated_coverage": pairs})
        print(f"order {order}: {pairs} (line, test) pairs that the isolated coverage lacks", file=sys.stderr, flush=True)

    sets: list[str] = []
    index: dict[str, int] = {}
    lines: dict[str, dict[str, int]] = {}
    observed: dict[str, dict[str, str]] = {}
    for (rel, line), mask in sorted(extra.items()):
        # the enclosing def: any test that runs it alone can be the one that meets the left-over state
        info = maps.static(rel)
        best = None
        for func in (info["funcs"] if info else []):
            if func[6] == "def" and func[2] <= line <= func[4] and (best is None or func[4] - func[2] < best[4] - best[2]):
                best = func
        wide = mask
        if best is not None:
            wide |= maps.call_tests(rel, best[2], best[4], warm=False)
        text = enc(bits(wide))
        if text not in index:
            index[text] = len(sets)
            sets.append(text)
        lines.setdefault(rel, {})[str(line)] = index[text]
        observed.setdefault(rel, {})[str(line)] = enc(bits(mask))
    with open(os.path.join(args.maps, "warm.json"), "w") as fh:
        json.dump({"orders": seen_orders, "lines": lines, "sets": sets, "observed": observed}, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    print(f"warm.json: {sum(len(v) for v in lines.values())} lines in {len(lines)} files", file=sys.stderr)


if __name__ == "__main__":
    main()

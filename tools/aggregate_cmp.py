#!/usr/bin/env python3
"""Turn the raw output of the comparisons pass into maps/cmp.json.

    python tools/aggregate_cmp.py --tests <tests.json> --sites <instrumented>/blast_cmp_sites.json \
        --raw <raw dir> [<raw dir> ...] --out maps/

maps/cmp.json:
    {"neighbour": {"is": "==", ...},
     "files": {"sqlglot/x.py": [[line, column, operator, set id], ...]},
     "sets": ["0-5,9", ...]}

For every instrumented comparison (line and column of the original source,
its operator) the set is the tests in which the neighbouring operator gave
another result at least once -- in the test itself, in a subprocess it
started, or while its test file was imported.  Several raw dirs (different
hash seeds) are united.  A test that failed or crashed during the pass has no
trustworthy record and is put into every set.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aggregate import MEASURES_THE_TRACER, Sets, write  # noqa: E402

NEIGHBOUR = {"is": "==", "isnot": "!=", "lt": "<=", "le": "<", "gt": ">=", "ge": ">"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--sites", required=True)
    ap.add_argument("--raw", required=True, nargs="+")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    tests_list = json.load(open(args.tests))["tests"]
    test_index = {t["id"]: t["number"] for t in tests_list}
    tests_of_file: dict[str, list[int]] = {}
    for t in tests_list:
        tests_of_file.setdefault(t["file"], []).append(t["number"])
    sites = json.load(open(args.sites))
    bad = [0] * len(sites)
    untrusted = 0
    for raw in args.raw:
        seen: set[int] = set()
        ctx_of: dict[str, int] = {}
        pending = []
        for path in glob.glob(os.path.join(raw, "file.*.json")):
            rec = json.load(open(path))
            mask = 0
            for f in rec["files"]:
                for n in tests_of_file.get(f, ()):
                    mask |= 1 << n
            for s in rec.get("cmp", {}).get("bad", ()):
                bad[s] |= mask
        for path in glob.glob(os.path.join(raw, "t.*.json")):
            rec = json.load(open(path))
            if rec["kind"] == "sub":
                pending.append(rec)
                continue
            number = test_index.get(rec["test"])
            if number is None:
                continue
            seen.add(number)
            ctx_of[rec["ctx"]] = number
            if rec["failed"]:
                why = MEASURES_THE_TRACER.get(rec["test"])
                if why is None or not all(why in f[2] for f in rec["failed"]):
                    print(f"note: {rec['test']} failed in the comparisons pass ({raw}): never pruned. {rec['failed'][0][2][-200:]!r}", file=sys.stderr)
                    untrusted |= 1 << number
            for s in rec["cmp"]["bad"]:
                bad[s] |= 1 << number
        for rec in pending:
            number = ctx_of.get(rec["ctx"])
            if number is None:
                continue
            for s in rec.get("cmp", {}).get("bad", ()):
                bad[s] |= 1 << number
        missing = [n for n in range(len(tests_list)) if n not in seen]
        for n in missing:
            untrusted |= 1 << n
        crashes = glob.glob(os.path.join(raw, "*.crash"))
        print(f"{raw}: {len(seen)} tests recorded, {len(missing)} without a record, {len(crashes)} crash files", file=sys.stderr)
    sets = Sets()
    files: dict[str, list] = {}
    n_bad = 0
    for (rel, line, col, op), mask in zip(sites, bad):
        mask |= untrusted
        n_bad += bool(mask & ~untrusted)
        files.setdefault(rel, []).append([line, col, op, sets.add(mask)])
    for rows in files.values():
        rows.sort()
    write(os.path.join(args.out, "cmp.json"), {"neighbour": NEIGHBOUR, "files": files, "sets": sets.items}, sort_keys=True)
    print(f"{len(sites)} sites, {n_bad} with a test in which the neighbouring operator differed, "
          f"{bin(untrusted).count('1')} tests never pruned", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Development check: run the selector on patches with known failing tests and
show, per commit, how many tests it selected against how many fail.

    python tools/dev_eval.py --commits DIR [DIR ...] --answers answers.json|labels.jsonl [...] \
        --tests tests.json --src <pinned checkout> --base <dir with base.json> [--python python3.13]

Answers come from the kit (sample/answers.json) or from a labeler that ran the
suite (one JSON object per line with "commit" and "failing").  Nothing here is
used by the selector; it only measures it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from blast.pack import pack  # noqa: E402


def load_answers(paths):
    out = {}
    for path in paths:
        with open(path) as fh:
            text = fh.read()
        if path.endswith(".jsonl"):
            for line in text.splitlines():
                if line.strip():
                    row = json.loads(line)
                    out[row["commit"]] = {"failing": row.get("failing", []), "kind": row.get("status", "")}
        else:
            out.update(json.loads(text)["commits"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", nargs="+", required=True)
    ap.add_argument("--answers", nargs="*", default=[])
    ap.add_argument("--tests", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--policy", default=None)
    ap.add_argument("--log", help="write the selector's stderr here")
    args = ap.parse_args()
    answers = load_answers(args.answers)
    with open(args.tests) as fh:
        tests = json.load(fh)["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    with tempfile.TemporaryDirectory() as data:
        os.mkdir(os.path.join(data, "commits"))
        os.mkdir(os.path.join(data, "tmp"))
        shutil.copy(args.tests, os.path.join(data, "tests.json"))
        for d in args.commits:
            names = [d] if d.endswith(".patch") else [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".patch")]
            for full in names:
                shutil.copy(full, os.path.join(data, "commits", os.path.basename(full)))
        env = dict(os.environ, BLAST_DATA=data, BLAST_SRC=os.path.abspath(args.src), BLAST_BASE=os.path.abspath(args.base),
                   BLAST_TMP=os.path.join(data, "tmp"))
        if args.policy:
            env["BLAST_SNAPSHOT"] = args.policy
        before = os.times()
        proc = subprocess.run([args.python, "-S", os.path.join(os.path.dirname(HERE), "selector.py")], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        after = os.times()
        cpu = (after.children_user + after.children_system) - (before.children_user + before.children_system)
        if args.log:
            with open(args.log, "wb") as fh:
                fh.write(proc.stderr)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode(errors="replace")[-3000:])
            return 1
    total = perfect_total = misses = 0
    print(f"{'commit':14} {'kind':10} {'failing':>7} {'selected':>8} {'cost s':>8} {'perfect s':>9}  note")
    for line in proc.stdout.decode().splitlines():
        row = json.loads(line)
        selected = {n for w in row["workers"] for n in w}
        loads = [sum(ms[n] for n in w) for w in row["workers"] if w]
        cost = (max(loads) + 2000 * len(loads)) if loads else 0
        total += cost
        ans = answers.get(row["commit"])
        if ans is None:
            print(f"{row['commit']:14} {'?':10} {'?':>7} {len(selected):8d} {cost / 1000:8.3f} {'':>9}")
            continue
        failing = set(ans["failing"])
        perfect = pack({n: ms[n] for n in failing})[0]
        perfect_total += perfect
        missed = failing - selected
        note = ""
        if missed:
            misses += 1
            total += 300_000
            note = f"MISSED {sorted(missed)[:8]}"
        print(f"{row['commit']:14} {str(ans.get('kind', ''))[:10]:10} {len(failing):7d} {len(selected):8d} "
              f"{cost / 1000:8.3f} {perfect / 1000:9.3f}  {note}")
    print(f"simulated {total / 1000:.3f} s (perfect knowledge {perfect_total / 1000:.3f} s), misses {misses}, "
          f"selector CPU {cpu:.2f} s")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())

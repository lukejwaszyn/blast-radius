#!/usr/bin/env python3
"""Development check, not part of the build: do the maps hold for edits of
definition-time lines that the sample set barely covers?

    python tools/dev_check_mutations.py --maps maps --src <checkout> --work <dir> \
        --label "<python> <path to a labeler>" [--count 20] [--seed 7]

It writes small single-line patches (flip a class-level True/False, change a
class-level number, flip a flag inside a class-level dict entry or an
``arg_types`` entry), asks an external labeler for the tests that really fail
(full suite, one JSON line per patch with a "failing" list of test numbers),
and checks that they are inside what the maps predict.  The labeler is the
lab's label.py; any program with the same output works.

The patches are generated from the pinned source by rule and are throw-away
test inputs for the maps; nothing about them is stored in the maps or used by
the selector.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import random
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_samples as vs  # noqa: E402

FLAG = re.compile(r"^(\s+[A-Za-z_][A-Za-z0-9_]*(?:: [^=]+)? = )(True|False)(\s*)$")
NUMBER = re.compile(r"^(\s+[A-Z][A-Z0-9_]*(?:: [^=]+)? = )(-?\d+)(\s*)$")
ENTRY_FLAG = re.compile(r"^(\s+.*[:=(,] ?.*?\b\w+=)(True|False)(\b.*)$")
ARG = re.compile(r'^(\s+"[a-z_]+": )(True|False)(,\s*)$')


def candidates(maps: vs.Maps, src: str):
    out = []
    for rel, info in maps.static.items():
        if not rel.startswith("sqlglot/"):
            continue
        lines = open(os.path.join(src, rel), encoding="utf-8").read().split("\n")
        for st in info["stmts"]:
            if st[2] == "" or st[3] not in ("assign", "annassign"):
                continue
            for lineno in range(st[0], st[1] + 1):
                if maps.stmt_at(rel, lineno) is not st:
                    continue
                text = lines[lineno - 1]
                kind = new = None
                if st[0] == st[1] and (m := FLAG.match(text)):
                    kind, new = "flag", m.group(1) + ("False" if m.group(2) == "True" else "True") + m.group(3)
                elif st[0] == st[1] and (m := NUMBER.match(text)):
                    kind, new = "number", m.group(1) + str(int(m.group(2)) + 1) + m.group(3)
                elif st[4] == ["arg_types"] and (m := ARG.match(text)):
                    kind, new = "arg_types", m.group(1) + ("False" if m.group(2) == "True" else "True") + m.group(3)
                elif st[0] != st[1] and "lambda" not in text and (m := ENTRY_FLAG.match(text)):
                    kind, new = "entry", m.group(1) + ("False" if m.group(2) == "True" else "True") + m.group(3)
                if kind:
                    readers = len(maps.def_tests(rel, st, closure=False))
                    out.append((kind, rel, lineno, text, new, readers))
    return out


def make_patch(src: str, rel: str, lineno: int, new: str) -> str:
    old_lines = open(os.path.join(src, rel), encoding="utf-8").read().split("\n")
    new_lines = list(old_lines)
    new_lines[lineno - 1] = new
    diff = difflib.unified_diff(old_lines, new_lines, f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(diff) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--min-readers", type=int, default=1)
    ap.add_argument("--max-readers", type=int, default=120)
    ap.add_argument("--python", help="python with requirements.lock: also check the exact fingerprint diff")
    args = ap.parse_args()
    maps = vs.Maps(args.maps)
    src = os.path.realpath(args.src)
    os.makedirs(args.work, exist_ok=True)
    exact = vs.Exact(maps, src, args.python, os.path.join(args.work, "exact")) if args.python else None
    rng = random.Random(args.seed)
    pool = [c for c in candidates(maps, src) if args.min_readers <= c[5] <= args.max_readers]
    by_kind = {}
    for c in pool:
        by_kind.setdefault(c[0], []).append(c)
    print({k: len(v) for k, v in by_kind.items()}, file=sys.stderr)
    share = {"flag": 0.5, "number": 0.1, "arg_types": 0.2, "entry": 0.2}
    chosen = []
    for kind, items in sorted(by_kind.items()):
        rng.shuffle(items)
        chosen.extend(items[: max(1, round(args.count * share[kind]))])
    ok = True
    for i, (kind, rel, lineno, text, new, readers) in enumerate(chosen):
        name = f"m{i:02d}"
        path = os.path.join(args.work, name + ".patch")
        patch = make_patch(src, rel, lineno, new)
        with open(path, "w") as fh:
            fh.write(patch)
        res = vs.analyse(maps, src, patch)
        layers = res["layers"]
        proc = subprocess.run(shlex.split(args.label) + [path], capture_output=True, text=True)
        try:
            label = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            print(f"{name}: labeler failed: {proc.stderr[-300:]}")
            ok = False
            continue
        failing = set(label.get("failing", []))
        direct = layers["lines"] | layers["defs"]
        full = direct | layers["derived"]
        verdict = "ok" if failing <= direct else ("ok-derived" if failing <= full else "MISSED")
        if verdict == "MISSED":
            ok = False
        missed = sorted(failing - direct)
        exact_note = ""
        if exact:
            ex_tests, ex_diff = exact.tests(patch) if res["def_time"] else (set(), {})
            ex_tests |= layers["lines"]
            exact_note = f" exact={len(ex_tests)}:{'ok' if failing <= ex_tests else 'MISSED'}"
            if not failing <= ex_tests:
                ok = False
                exact_note += f" {[maps.tests['ids'][t] for t in sorted(failing - ex_tests)][:4]} {ex_diff.get('changed')}"
        print(f"{name} {verdict:10} {kind:9} {rel}:{lineno} {text.strip()[:60]!r} status={label.get('status')} "
              f"failing={len(failing)} direct={len(direct)} derived={len(full)} modules={len(layers['modules'])}{exact_note}"
              + (f" not-direct={[maps.tests['ids'][t] for t in missed][:6]}" if missed else ""), flush=True)
    print("all inside the maps:", ok)


if __name__ == "__main__":
    main()

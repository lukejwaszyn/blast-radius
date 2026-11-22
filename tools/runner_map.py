#!/usr/bin/env python3
"""Build maps/runner.json: what blast/verify.py must know about each test before it
lets the decision-time runner (blast/runner.py) judge it.

    BLAST_SRC=<pinned checkout, compiled> python tools/runner_map.py [--out maps/runner.json] [--raw raw.json]

Every test of the pinned commit is run alone, through the same runner the
selector uses, on the unpatched tree.  Recorded per test:

  not_alone       it does not pass when it is the only test in the process.  Such a
                  test is never judged by the runner.
  starts_process  it starts another process while it runs (a `spawn` process pool,
                  `subprocess.run`).  Such a test costs seconds of CPU that its recorded
                  duration does not show (every pool worker is a new interpreter that
                  imports sqlglot, pandas and duckdb), so the runner leaves it alone.
  class_starts_process  its class set-up starts one (TestOptimizer creates a `spawn`
                  pool, which starts multiprocessing's small resource tracker; no worker
                  is started until a test submits work).  Informative only.

  class_setup_s   per test class, when it is more than 0.05 s: the CPU one test of the
                  class costs alone beyond its recorded duration and beyond what a job
                  costs anyway (job_overhead_s: fork, fresh import of sqlglot and the
                  test file).  TestExecutor loads the TPC-H tables in setUpClass (1.4 s).
                  Seconds on the machine the map was built on; blast/verify.py scales them.

And per test file, from importing it in a fresh interpreter:

  external        the third-party packages loaded once the file is imported;
  preload         the standard library modules that at least half of the test files
                  load (unittest, what sqlglot itself imports ...).  The zygote imports
                  these once instead of every child; the rest is left to the child that
                  needs it.

    --probe-only   redo just this second part and put it into an existing map (--out).

Nothing here depends on any patch: it is a property of the pinned test suite, like
the coverage maps.  The tests longer than --max-seconds are not run (the runner
never runs them: a long test costs more CPU than it can save).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from blast.runner import Runner  # noqa: E402

HEAVY = ("numpy", "pandas", "duckdb")

_PROBE = r"""
import importlib, json, os, sys, sysconfig
sys.path.insert(0, sys.argv[1])
os.environ["SKIP_INTEGRATION"] = "0"
import unittest
importlib.import_module(sys.argv[2])
stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"])
third, std = set(), set()
for name, mod in list(sys.modules.items()):
    top = name.split(".")[0]
    if top in ("sqlglot", "tests", "sqlglotc", "__main__", "__mp_main__"):
        continue
    origin = os.path.realpath(getattr(mod, "__file__", None) or "")
    if "site-packages" in origin or "dist-packages" in origin:
        third.add(top)
    elif not origin or origin.startswith(stdlib):
        if top in sys.stdlib_module_names and not name.startswith("_") and "._" not in name:
            std.add(name)
print(json.dumps({"third": sorted(t for t in third if not t.startswith("_")), "std": sorted(std)}))
"""


def probe_imports(src: str, python: str, files: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    import subprocess

    external: dict[str, list[str]] = {}
    loaded: dict[str, int] = {}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    for path in files:
        proc = subprocess.run([python, "-c", _PROBE, src, path[:-3].replace("/", ".")], capture_output=True, text=True, env=env, cwd=src)
        if proc.returncode != 0:
            raise SystemExit(f"{path} does not import at the pinned commit:\n{proc.stderr[-2000:]}")
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        external[path] = got["third"]
        for name in got["std"]:
            loaded[name] = loaded.get(name, 0) + 1
    return external, sorted(name for name, count in loaded.items() if 2 * count >= len(files))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", default=os.path.join(os.path.dirname(HERE), "maps"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--raw", default=None, help="also write every job's answer (timings) here")
    ap.add_argument("--max-seconds", type=float, default=5.0)
    ap.add_argument("--tmp", default=os.environ.get("BLAST_TMP", "/tmp"))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--only", default="", help="substring of the test ids to run (debugging)")
    ap.add_argument("--probe-only", action="store_true")
    args = ap.parse_args()
    out_path = args.out or os.path.join(args.maps, "runner.json")

    with open(os.path.join(args.maps, "tests.json")) as fh:
        listed = json.load(fh)
    ids, seconds = listed["ids"], listed["seconds"]
    files = sorted({test_id.split("::")[0] for test_id in ids if not args.only or args.only in test_id})
    external, preload = probe_imports(os.environ["BLAST_SRC"], args.python, files)
    print(f"{len(files)} test files probed; {sum(1 for f in files if any(m in external[f] for m in HEAVY))} need numpy / pandas / duckdb; "
          f"{len(preload)} standard library modules to preload", file=sys.stderr, flush=True)
    if args.probe_only:
        with open(out_path) as fh:
            result = json.load(fh)
        result["external"], result["preload"] = external, preload
        with open(out_path, "w") as fh:
            json.dump(result, fh, indent=0, sort_keys=True)
            fh.write("\n")
        return 0
    runner = Runner(src=os.environ["BLAST_SRC"], tmp=args.tmp, python=args.python)
    alone_fail, starts, class_starts, skipped_long = [], [], [], []
    raw = []
    started = time.time()
    for number, test_id in enumerate(ids):
        if args.only and args.only not in test_id:
            continue
        if seconds[number] > args.max_seconds:
            skipped_long.append(number)
            continue
        path = test_id.split("::")[0]
        pre = preload + [m for m in HEAVY if m in external.get(path, ())]
        ans = runner.run({}, [(number, test_id, max(30.0, 20 * seconds[number]))], preimport=pre, trace=True, start_limit=60.0)
        ok = ans.get("complete") and not ans.get("void") and ans["status"].get(str(number)) == "pass"
        if not ok:
            alone_fail.append(number)
        spawned = ans.get("spawned") or []
        if str(number) in spawned:
            starts.append(number)
        elif spawned:
            class_starts.append(number)
        raw.append({"number": number, "id": test_id, "seconds": seconds[number], "cpu": ans.get("cpu"), "wall": ans.get("wall"),
                    "status": ans["status"].get(str(number)), "void": ans.get("void"), "spawned": str(number) in spawned,
                    "class_spawned": bool(spawned)})
        if len(raw) % 100 == 0:
            print(f"{len(raw)} tests, {time.time() - started:.0f} s", file=sys.stderr, flush=True)
    runner.close()
    by_class: dict[str, list[float]] = {}
    for r in raw:
        if not r["spawned"] and r["cpu"] is not None:
            by_class.setdefault(r["id"].rsplit("::", 1)[0], []).append(r["cpu"] - r["seconds"])
    medians = {cls: sorted(v)[len(v) // 2] for cls, v in by_class.items()}
    every = sorted(x for v in by_class.values() for x in v)
    overhead = every[len(every) // 2] if every else 0.0
    class_setup = {cls: round(m - overhead, 3) for cls, m in sorted(medians.items()) if m - overhead > 0.05}
    result = {
        "about": "tools/runner_map.py: each test of the pinned commit run alone through blast/runner.py on the unpatched tree",
        "job_overhead_s": round(overhead, 3),
        "class_setup_s": class_setup,
        "not_alone": alone_fail,
        "starts_process": starts,
        "class_starts_process": class_starts,
        "not_run_long": skipped_long,
        "external": {path: mods for path, mods in sorted(external.items())},
        "preload": preload,
    }
    if not args.only:
        with open(out_path, "w") as fh:
            json.dump(result, fh, indent=0, sort_keys=True)
            fh.write("\n")
    if args.raw:
        with open(args.raw, "w") as fh:
            json.dump(raw, fh)
    print(f"{len(raw)} tests run alone: {len(alone_fail)} do not pass, {len(starts)} start a process while they run, "
          f"{len(skipped_long)} too long to run; CPU {sum(r['cpu'] or 0 for r in raw):.1f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Blast Radius selector: for every patch in /data/commits print which tests to
run and on which worker.  One JSON line per commit on stdout, logs on stderr.
The method is described in README.md; every constant is in blast/config.py.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blast import config as C  # noqa: E402
from blast import exact  # noqa: E402
from blast.analyze import Analysis, Analyzer, Group, Source  # noqa: E402
from blast.choose import choose  # noqa: E402
from blast.maps import Maps, bits  # noqa: E402
from blast.pack import pack  # noqa: E402

VERBOSE = os.environ.get("BLAST_VERBOSE", "1") != "0"


def log(*parts):
    if VERBOSE:
        print(*parts, file=sys.stderr)


def duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)  # as score.py rounds


class Run:
    def __init__(self):
        self.t0 = time.monotonic()
        self.maps = Maps(C.MAPS_DIR)
        try:
            with open(os.path.join(C.DATA_DIR, "tests.json"), "rb") as fh:
                tests = json.load(fh)["tests"]
            tests = [t for t in tests if isinstance(t.get("number"), int) and isinstance(t.get("id"), str)]
            if not tests:
                raise ValueError("no tests listed")
        except Exception as exc:  # unreadable: the maps hold the same list (ids and seconds of the pinned commit)
            log(f"tests.json unreadable ({type(exc).__name__}: {exc}); using the list in the maps")
            seconds = self.maps.load("tests")["seconds"]
            tests = [{"number": i, "id": tid, "seconds": seconds[i]} for i, tid in enumerate(self.maps.ids)]
        self.number_ms = {t["number"]: duration_ms(float(t.get("seconds") or 0.0)) for t in tests}
        by_id = {t["id"]: t for t in tests}
        self.number_of: list[int | None] = []   # maps numbering -> the run's numbering
        self.ms: list[int] = []
        valid = 0
        for i, tid in enumerate(self.maps.ids):
            t = by_id.pop(tid, None)
            if t is None:
                self.number_of.append(None)
                self.ms.append(0)
            else:
                self.number_of.append(t["number"])
                self.ms.append(self.number_ms[t["number"]])
                valid |= 1 << i
        self.valid = valid
        # tests the maps have never seen (a different tests.json): nothing is known about them
        self.unmapped = sorted(t["number"] for t in by_id.values())
        if self.unmapped or valid != self.maps.all_mask:
            log(f"tests.json differs from the maps: {len(self.unmapped)} unknown tests, "
                f"{self.maps.n - len(bits(valid))} mapped tests missing")
        self.analyzer = Analyzer(self.maps, Source(C.SRC_DIR))
        self.all_workers = None

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def everything(self) -> list[list[int]]:
        if self.all_workers is None:
            self.all_workers = pack(dict(self.number_ms))[1]
        return self.all_workers

    # ------------------------------------------------------------------
    def analyze(self, commit: str, path: str) -> Analysis:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            return self.analyzer.analyze(commit, data)
        except Exception as exc:
            an = Analysis(commit)
            an.notes.append(f"analysis failed: {type(exc).__name__}: {exc}")
            an.add(C.P_INTERNAL_ERROR, self.maps.all_mask, "the analysis of this patch failed: run everything")
            return an

    def cost(self, groups) -> int:
        mask, _ = choose(groups, self.ms, self.valid)
        total = sum(self.ms[i] for i in bits(mask))
        from blast.pack import best_possible

        return best_possible(total, max((self.ms[i] for i in bits(mask)), default=0), len(bits(mask)))

    def wants_exact(self, an: Analysis) -> bool:
        req = an.exact
        if req is None or not req.files or C.SNAPSHOT_POLICY == "never" or not exact.available():
            return False
        if C.SNAPSHOT_POLICY == "always" or not req.const_only:
            return True
        return self.cost(an.groups + req.fallback) - self.cost(an.groups) > C.SNAPSHOT_MIN_SAVING_MS

    def resolve_exact(self, analyses: list[Analysis]):
        """Run the import comparison for the commits that need it, two at a time."""
        waiting = [an for an in analyses if self.wants_exact(an)]
        for an in analyses:
            if an.exact is not None and an not in waiting:
                an.groups.extend(an.exact.fallback)
                if an.exact.fallback:
                    an.notes.append("definition-time change: static closure used (no import of the patched tree)")
        running: list[exact.Job] = []
        while waiting or running:
            while waiting and len(running) < C.SNAPSHOT_PARALLEL:
                an = waiting.pop(0)
                if self.elapsed() > C.WALL_BUDGET_S:
                    an.groups.extend(an.exact.fallback)
                    an.notes.append("out of time: static closure used instead of importing the patched tree")
                    continue
                try:
                    running.append(exact.start(an))
                except Exception as exc:
                    an.groups.extend(an.exact.fallback)
                    self.import_failed(an, f"could not start the import of the patched tree ({exc})")
            if not running:
                continue
            job = running.pop(0)
            exact.finish(job, C.SNAPSHOT_TIMEOUT_S - (time.monotonic() - job.started))
            an = job.analysis
            if job.result is not None:
                found = exact.groups_from(job, self.maps, Group)
                an.groups.extend(found)
                an.notes.append(f"imported the patched tree: {len(job.result.get('changed', {}))} changed values, "
                                f"{len(job.result.get('errors', {}))} modules that no longer import")
            else:
                an.groups.extend(an.exact.fallback)
                self.import_failed(an, job.error or "no answer")

    def import_failed(self, an: Analysis, why: str):
        """The patched tree could not be imported to the end (hang, crash): assume the touched modules are broken."""
        an.notes.append(why)
        for module in sorted(an.exact.modules):
            mask = self.maps.module_tests(module)
            rel = self.maps.module_path(module)
            if rel:
                mask |= self.maps.file_line_tests(rel)
            an.add(C.P_MODULE_BROKEN, mask, f"{why}: every test that loads {module}")

    def answer(self, an: Analysis) -> list[list[int]]:
        mask, left_out = choose(an.groups, self.ms, self.valid)
        numbers = {self.number_of[i]: self.ms[i] for i in bits(mask)}
        if self.unmapped and any(g.p >= C.ALWAYS_KEEP for g in an.groups):
            for number in self.unmapped:
                numbers[number] = self.number_ms[number]
        cost, workers, gap = pack(numbers)
        if VERBOSE:
            log(f"{an.commit}: {len(numbers)} tests, {len(workers)} workers, {cost / 1000:.3f} s"
                + (f" (packing {gap} ms above the bound)" if gap else ""))
            for g in sorted(an.groups, key=lambda g: -g.p):
                log(f"    P={g.p:<5g} {len(bits(g.mask & self.valid)):5d} tests  {g.why}")
            for line in left_out:
                log(f"    {line}")
            for note in an.notes:
                log(f"    note: {note}")
        return workers


def main() -> int:
    run = Run()
    commits_dir = os.path.join(C.DATA_DIR, "commits")
    try:
        names = sorted(n for n in os.listdir(commits_dir) if n.endswith(".patch"))
    except OSError as exc:
        log(f"no commits to answer: {exc}")
        names = []
    analyses = [run.analyze(name[:-6], os.path.join(commits_dir, name)) for name in names]
    try:
        run.resolve_exact(analyses)
    except Exception as exc:
        log(f"exact phase failed: {type(exc).__name__}: {exc}")
        for an in analyses:
            if an.exact is not None:
                an.groups.extend(an.exact.fallback)
    out = []
    for an in analyses:
        try:
            workers = run.answer(an)
        except Exception as exc:
            log(f"{an.commit}: selection failed ({type(exc).__name__}: {exc}); running everything")
            workers = run.everything()
        out.append(json.dumps({"commit": an.commit, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    usage = os.times()
    log(f"{len(names)} commits in {run.elapsed():.2f} s wall, "
        f"{usage.user + usage.system + usage.children_user + usage.children_system:.2f} s CPU")
    return 0


if __name__ == "__main__":
    sys.exit(main())

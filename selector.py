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
            an = self.analyzer.analyze(commit, data)
            an.data = data
            return an
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
                an.exact_done = True
                if an.exact.fallback:
                    an.notes.append("definition-time change: static closure used (no import of the patched tree)")
        running: list[exact.Job] = []
        while waiting or running:
            while waiting and len(running) < C.SNAPSHOT_PARALLEL:
                an = waiting.pop(0)
                if self.elapsed() > C.WALL_BUDGET_S:
                    an.groups.extend(an.exact.fallback)
                    self.import_failed(an, "out of time: the patched tree was not imported")
                    an.exact_done = True
                    continue
                try:
                    running.append(exact.start(an))
                except Exception as exc:
                    an.groups.extend(an.exact.fallback)
                    self.import_failed(an, f"could not start the import of the patched tree ({exc})")
                    an.exact_done = True
            if not running:
                continue
            job = running.pop(0)
            exact.finish(job, C.SNAPSHOT_TIMEOUT_S - (time.monotonic() - job.started))
            an = job.analysis
            if job.result is not None:
                try:
                    warm_before = self.maps.warm_hits
                    found = exact.groups_from(job, self.maps, Group, self.analyzer)
                    if self.maps.warm_hits != warm_before:
                        an.shared.append("a changed value that some tests read only after other tests ran (maps/warm.json)")
                except Exception as exc:  # never let one odd answer cost a commit its candidates
                    an.groups.extend(an.exact.fallback)
                    self.import_failed(an, f"the import comparison could not be read ({type(exc).__name__}: {exc})")
                else:
                    an.groups.extend(found)
                    an.notes.append(f"imported the patched tree: {len(job.result.get('changed', {}))} changed values, "
                                    f"{len(job.result.get('errors', {}))} modules that no longer import")
            else:
                an.groups.extend(an.exact.fallback)
                self.import_failed(an, job.error or "no answer")
            an.exact_done = True

    def import_failed(self, an: Analysis, why: str):
        """The patched tree could not be imported to the end (hang, crash): assume the touched modules are broken."""
        an.notes.append(why)
        touched = {self.maps.module_of(rel) or "" for rel in an.exact.files}
        if not an.exact.modules or not touched <= an.exact.modules:
            # a new or removed module: nothing narrower is known about what the import ran into
            an.add(C.P_MODULE_BROKEN, self.maps.package_tests("sqlglot"), f"{why}: every test that loads sqlglot")
        for module in sorted(an.exact.modules):
            mask = self.maps.module_tests(module)
            rel = self.maps.module_path(module)
            if rel:
                mask |= self.maps.file_line_tests(rel)
            an.add(C.P_MODULE_BROKEN, mask, f"{why}: every test that loads {module}")

    def verify(self, analyses: list[Analysis]) -> None:
        """Run the candidates of the commits where that is expected to pay (blast/verify.py)."""
        if C.VERIFY != "on" or self.unmapped or self.valid != self.maps.all_mask:
            return
        from blast.verify import Verifier

        verifier = Verifier(self.maps, self.ms, self.valid, self.analyzer.src.read, log)
        if not verifier.ok:
            return
        verifier.budget += C.VERIFY_CPU_BUDGET_PER_COMMIT_S * len(analyses)
        before = os.times()
        cost_before = {}
        try:
            plans = []
            for an in analyses:
                if getattr(an, "selected", None):
                    plan = verifier.plan(an, an.selected[0], an.groups)
                    plans.append(plan)
            todo = verifier.schedule(plans)
            for plan in plans:
                if plan.why:
                    plan.an.notes.append(f"verify: not run: {plan.why}")
            for plan in todo:
                an = plan.an
                cost_before[an.commit] = self.cost_of_mask(an.selected[0])
                try:
                    an.selected = (verifier.run(plan, self.elapsed()), an.selected[1])
                except Exception as exc:
                    an.notes.append(f"verify: failed ({type(exc).__name__}: {exc}): static selection kept")
        finally:
            verifier.close()   # reaps the runner, so its CPU and its children's is in os.times() from here on
            after = os.times()
            cpu = sum(getattr(after, k) - getattr(before, k) for k in ("user", "system", "children_user", "children_system"))
            saved = sum(cost_before[an.commit] - self.cost_of_mask(an.selected[0]) for an in analyses if an.commit in cost_before)
            log(f"verify: {len(cost_before)} of {len(analyses)} commits run, {saved / 1000:.3f} s of simulated cost saved "
                f"for {cpu:.2f} s CPU (of which {verifier.spent:.2f} s in the test processes)")

    def cost_of_mask(self, mask: int) -> int:
        from blast.pack import best_possible

        indices = bits(mask)
        return best_possible(sum(self.ms[i] for i in indices), max((self.ms[i] for i in indices), default=0), len(indices))

    def select(self, an: Analysis) -> None:
        an.selected = choose(an.groups, self.ms, self.valid)

    def answer(self, an: Analysis) -> list[list[int]]:
        mask, left_out = an.selected if getattr(an, "selected", None) is not None else choose(an.groups, self.ms, self.valid)
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
            if an.exact is not None and not getattr(an, "exact_done", False):
                an.groups.extend(an.exact.fallback)
                try:
                    run.import_failed(an, "the import comparison did not run")
                except Exception:
                    an.add(C.P_INTERNAL_ERROR, run.maps.all_mask, "the import comparison did not run: run everything")
    for an in analyses:
        try:
            run.select(an)
        except Exception as exc:
            an.selected = None
            log(f"{an.commit}: selection failed ({type(exc).__name__}: {exc})")
    try:
        run.verify(analyses)
    except Exception as exc:  # the static selection stands
        log(f"verify phase failed: {type(exc).__name__}: {exc}")
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

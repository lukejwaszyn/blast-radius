"""Whether to run a commit's candidate tests before answering, and what to conclude.

The static selection of a commit is the set of tests that *could* fail.  Running
them on the patched code (blast/runner.py) tells which really do; the ones that
ran to their end and passed are dropped.  A commit whose candidates all pass then
costs nothing instead of 2 s for a worker plus its load.  Every CPU second spent
running is charged, so a commit is run only when

    P(no candidate fails) x cost of the selection
  + P(some fail) x what dropping the passing ones still saves
  - CPU of the run
  > VERIFY_MARGIN_S

and only when a test's result in a process of its own is what the labeling run
would have seen.  That excludes, per commit:

  - a patch the runner cannot reproduce byte for byte (runner.materialize),
    or one that touches more than Python files of sqlglot/ and tests/ and fixtures:
    configuration, conftest.py, new test files and extension modules change what
    pytest itself does;
  - a selection that exists because of state shared by the whole process
    (Analysis.shared: writes to shared state, lines reached only after other tests);
  - a selection with a test the runner must not judge (maps/runner.json): one that
    does not pass alone at the pinned commit, one that starts other processes,
    a long one.

A test is dropped only on a "pass" from a job that ran to its end with no error
outside a test.  Everything else keeps the static selection.
"""

from __future__ import annotations

import os

from . import config as C
from .maps import bits
from .pack import best_possible

HEAVY = ("numpy", "pandas", "duckdb")


class Plan:
    __slots__ = ("an", "mask", "files", "tests", "preimport", "heavy", "gain", "cpu", "p_none", "why")

    def __init__(self, an, mask):
        self.an = an
        self.mask = mask
        self.files = None
        self.tests: list[tuple[int, str, float]] = []
        self.preimport: list[str] = []
        self.heavy = False
        self.gain = 0.0
        self.cpu = 0.0
        self.p_none = 0.0
        self.why = ""


class Verifier:
    def __init__(self, maps, ms: list[int], valid: int, read_pinned, log):
        self.maps = maps
        self.ms = ms
        self.valid = valid
        self.read_pinned = read_pinned
        self.log = log
        self.runner = None
        self.ok = False
        self.spent = 0.0
        self.budget = C.VERIFY_CPU_BUDGET_S
        try:
            table = maps.load("runner")
            banned = set(table["not_alone"]) | set(table["not_run_long"])
            if C.VERIFY_SPAWN != "fork":
                banned |= set(table["starts_process"])
            self.banned = 0
            for i in banned:
                self.banned |= 1 << i
            self.external = table["external"]
            self.preload = list(table.get("preload", ()))
            self.job_overhead = float(table.get("job_overhead_s", 0.1))
            self.class_setup = {cls: float(s) for cls, s in table.get("class_setup_s", {}).items()}
            self.ok = True
        except Exception as exc:
            log(f"verify: no runner map ({type(exc).__name__}: {exc}); static selection only")

    # ------------------------------------------------------------ planning
    def path_ok(self, path: str, content, existed: bool) -> bool:
        base = os.path.basename(path)
        if path.startswith("sqlglot/"):
            return path.endswith(".py")
        if path.startswith("tests/fixtures/"):
            return not path.endswith(".py")
        if path.startswith("tests/") and path.endswith(".py"):
            if base == "conftest.py":
                return False
            if not existed:
                return False   # a new file in the test tree: pytest may collect it, the runner would not
            if base == "__init__.py" and content is None:
                return False   # without the package marker pytest imports the test files under other names
            return True
        return False

    def plan(self, an, mask: int, groups) -> Plan:
        """Always returns a Plan; `plan.why` says why it will not be run, or is empty."""
        from .runner import materialize

        plan = Plan(an, mask)
        if not self.ok or not mask:
            plan.why = "nothing selected" if not mask else "no runner"
            return plan
        if getattr(an, "shared", None):
            plan.why = f"depends on state shared between tests ({an.shared[0]})"
            return plan
        if mask & self.banned:
            plan.why = f"{len(bits(mask & self.banned))} selected test(s) the runner does not judge (start processes, long, or not passing alone)"
            return plan
        indices = bits(mask)
        total = sum(self.ms[i] for i in indices)
        if total > C.VERIFY_MAX_RUN_S * 1000:
            plan.why = f"{total / 1000:.1f} s of tests: more CPU than it can save"
            return plan
        data = getattr(an, "data", None)
        if data is None:
            plan.why = "patch not kept"
            return plan
        files, why = materialize(data, self.read_pinned)
        if files is None:
            plan.why = f"patch not reproducible ({why})"
            return plan
        for path, content in files.items():
            if not self.path_ok(path, content, self.read_pinned(path) is not None):
                plan.why = f"{path}: not a kind of file a test's own run settles"
                return plan
        plan.files = files

        longest = max(self.ms[i] for i in indices)
        cost = best_possible(total, longest, len(indices))
        relevant = [g for g in groups if g.mask & mask]
        p_some = max((g.p for g in relevant), default=1.0)
        p_some = min(max(p_some, 0.0), 1.0)
        plan.p_none = 1.0 - p_some
        # when something does fail, the passing candidates are still dropped if that saves enough
        # (VERIFY_MIN_DROP_SAVING_S): assume half the load stays
        partial = max(0, cost - best_possible(-(-total // 2), longest, max(1, len(indices) // 2)))
        if partial < C.VERIFY_MIN_DROP_SAVING_S * 1000:
            partial = 0
        test_files = sorted({self.maps.ids[i].split("::")[0] for i in indices})
        externals = {m for f in test_files for m in self.external.get(f, ())}
        plan.heavy = any(m in externals for m in HEAVY)
        classes = {self.maps.ids[i].rsplit("::", 1)[0] for i in indices}
        fixed = (self.job_overhead + C.VERIFY_FILE_IMPORT_S * (len(test_files) - 1)
                 + sum(self.class_setup.get(cls, 0.0) for cls in classes))
        plan.cpu = C.VERIFY_MACHINE_FACTOR * fixed + C.VERIFY_SPEED * total / 1000
        plan.gain = (plan.p_none * cost + p_some * partial * C.VERIFY_PARTIAL_WEIGHT) / 1000 - plan.cpu
        plan.preimport = self.preload + ([m for m in HEAVY if m in externals] if plan.heavy else [])
        plan.tests = [(i, self.maps.ids[i], C.VERIFY_TEST_LIMIT_FACTOR * self.ms[i] / 1000 + C.VERIFY_TEST_LIMIT_S)
                      for i in indices]
        if plan.gain <= C.VERIFY_MARGIN_S:
            plan.why = (f"expected saving {plan.gain + plan.cpu:.2f} s (P(nothing fails) {plan.p_none:.2f} x {cost / 1000:.2f} s) "
                        f"does not exceed the run's {plan.cpu:.2f} s by {C.VERIFY_MARGIN_S:g} s")
        return plan

    def schedule(self, plans: list[Plan]) -> list[Plan]:
        """The plans worth running, cheap ones first.  The one-off costs (starting the runner, importing
        numpy / pandas / duckdb) are paid only if the commits that need them repay them together."""
        good = [p for p in plans if not p.why]
        light = [p for p in good if not p.heavy]
        heavy = [p for p in good if p.heavy]
        heavy_cost = C.VERIFY_HEAVY_IMPORT_S * C.VERIFY_MACHINE_FACTOR
        start_cost = C.VERIFY_START_S * C.VERIFY_MACHINE_FACTOR
        if heavy and sum(p.gain for p in heavy) <= heavy_cost + C.VERIFY_MARGIN_S:
            for p in heavy:
                p.why = (f"needs numpy / pandas / duckdb imported ({heavy_cost:.2f} s once); the commits that do "
                         f"would save {sum(q.gain for q in heavy):.2f} s together")
            heavy = []
        chosen = light + heavy
        if chosen and sum(p.gain for p in chosen) <= start_cost + C.VERIFY_MARGIN_S:
            for p in chosen:
                p.why = f"starting the runner costs {start_cost:.2f} s; all runs together would save {sum(q.gain for q in chosen):.2f} s"
            return []
        light.sort(key=lambda p: -p.gain)
        heavy.sort(key=lambda p: -p.gain)
        return light + heavy

    # ------------------------------------------------------------- running
    def run(self, plan: Plan, elapsed: float) -> int:
        """-> the selection after the run (a subset of plan.mask)."""
        from .runner import Runner

        an = plan.an
        job_wall = 2 * C.VERIFY_START_LIMIT_S + sum(limit for _i, _id, limit in plan.tests)
        if elapsed + job_wall > C.WALL_BUDGET_S:
            an.notes.append("verify: not run, out of time")
            return plan.mask
        if self.spent + plan.cpu > self.budget:
            an.notes.append("verify: not run, CPU budget for running tests is used up")
            return plan.mask
        if self.runner is None:
            self.runner = Runner()
        answer = self.runner.run(plan.files, plan.tests, preimport=plan.preimport,
                                 spawn="fork" if C.VERIFY_SPAWN == "fork" else None,
                                 start_limit=C.VERIFY_START_LIMIT_S, job_limit=job_wall + 2.0)
        self.spent += float(answer.get("cpu") or 0.0)
        status = answer.get("status") or {}
        if not answer.get("complete") or answer.get("void"):
            an.notes.append(f"verify: ran {len(plan.tests)} test(s), {answer.get('cpu', 0):.2f} s CPU, no verdict "
                            f"({answer.get('void') or 'the run did not finish'}): static selection kept")
            return plan.mask
        passed = 0
        for i, _id, _limit in plan.tests:
            if status.get(str(i)) == "pass":
                passed |= 1 << i
        kept = plan.mask & ~passed
        saving = (self.cost(plan.mask) - self.cost(kept)) / 1000
        head = f"verify: ran {len(plan.tests)} test(s) on the patched tree, {answer.get('cpu', 0):.2f} s CPU: "
        tail = f" (expected net saving {plan.gain:.2f} s, P(nothing fails) {plan.p_none:.2f})"
        if saving < C.VERIFY_MIN_DROP_SAVING_S:
            an.notes.append(head + f"{len(bits(passed))} passed, {len(bits(kept))} did not; dropping the passing ones would save "
                            f"{saving:.3f} s, less than {C.VERIFY_MIN_DROP_SAVING_S:g} s: static selection kept" + tail)
            return plan.mask
        an.notes.append(head + f"{len(bits(passed))} passed and are dropped, {len(bits(kept))} kept, {saving:.3f} s saved" + tail)
        return kept

    def cost(self, mask: int) -> int:
        indices = bits(mask)
        if not indices:
            return 0
        return best_possible(sum(self.ms[i] for i in indices), max(self.ms[i] for i in indices), len(indices))

    def close(self):
        if self.runner is not None:
            self.runner.close()
            self.runner = None

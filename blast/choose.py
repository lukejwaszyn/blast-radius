"""Which candidate groups are worth running, by expected cost.

Running a group adds  cost(selection + group) - cost(selection)  to the commit;
leaving it out risks  P(group) x 300 s.  Groups at or above ALWAYS_KEEP are
taken as they are (running the whole suite costs about 37 s, less than
0.15 x 300 s).  The others are added, most valuable first, while they pay for
themselves; inside such a group a long test is weighed on its own, because
it alone can set the busiest worker's load.
"""

from __future__ import annotations

from . import config as C
from .maps import bits
from .pack import best_possible


class Tally:
    """total / longest / count of a selection, kept incrementally."""

    __slots__ = ("mask", "total", "longest", "count")

    def __init__(self):
        self.mask = 0
        self.total = 0
        self.longest = 0
        self.count = 0

    def cost(self) -> int:
        return best_possible(self.total, self.longest, self.count)

    def cost_with(self, extra: list[int], ms: list[int]) -> int:
        if not extra:
            return self.cost()
        total = self.total + sum(ms[i] for i in extra)
        longest = max(self.longest, max(ms[i] for i in extra))
        return best_possible(total, longest, self.count + len(extra))

    def add(self, extra: list[int], ms: list[int]):
        for i in extra:
            self.mask |= 1 << i
            self.total += ms[i]
            if ms[i] > self.longest:
                self.longest = ms[i]
            self.count += 1


def choose(groups, ms: list[int], valid_mask: int) -> tuple[int, list[str]]:
    """groups: objects with .p, .mask, .why;  ms[i]: duration of test i (maps numbering).
    -> (selected mask, log lines)."""
    log: list[str] = []
    tally = Tally()
    sure = 0
    for g in groups:
        if g.p >= C.ALWAYS_KEEP:
            sure |= g.mask
    sure &= valid_mask
    tally.add(bits(sure), ms)
    low = [g for g in groups if g.p < C.ALWAYS_KEEP and (g.mask & valid_mask & ~sure)]
    pending = []
    for g in low:
        extra = bits(g.mask & valid_mask & ~sure)
        small = [i for i in extra if ms[i] < C.BIG_TEST_MS]
        if small:
            pending.append((g, small))
        for i in extra:
            if ms[i] >= C.BIG_TEST_MS:
                pending.append((g, [i]))
    # A group may add at most P x 300 s in total, however it is split into units.
    spent: dict[int, int] = {}
    changed = True
    while changed and pending:
        changed = False
        best = None
        for k, (g, unit) in enumerate(pending):
            unit = [i for i in unit if not (tally.mask >> i) & 1]
            if not unit:
                best = (float("inf"), k, unit, 0)
                break
            added = tally.cost_with(unit, ms) - tally.cost()
            gain = g.p * C.MISS_PENALTY_MS - spent.get(id(g), 0) - added
            if gain > 0 and (best is None or gain > best[0]):
                best = (gain, k, unit, added)
        if best is not None:
            _gain, k, unit, added = best
            g, _ = pending.pop(k)
            if unit:
                tally.add(unit, ms)
                spent[id(g)] = spent.get(id(g), 0) + added
            changed = True
    for g, unit in pending:
        unit = [i for i in unit if not (tally.mask >> i) & 1]
        if unit:
            added = tally.cost_with(unit, ms) - tally.cost()
            log.append(f"left out {len(unit)} test(s), P={g.p:g} x 300 s < +{added / 1000:.2f} s: {g.why}")
    return tally.mask, log

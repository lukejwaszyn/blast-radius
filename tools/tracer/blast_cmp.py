"""Run-time half of the "comparisons" pass (map building only).

tools/instrument_cmp.py rewrites `a is b` into `_bc.is_(site, a, b)` and so on.
Each function returns exactly what the original comparison returns and, on the
side, evaluates the neighbouring operator on the same operands:

    is -> ==      is not -> !=      <  -> <=      <= -> <      >  -> >=      >= -> >

`bad` collects the sites where the neighbour gave another result (or raised) at
least once in this process.  A site that a test evaluates and that never lands
in `bad` computes the same value with either operator, every time, in that
test: swapping the operator there cannot change what the test does.

"Another result" is strict: the neighbour must return the very same object
(True or False); a truthy non-bool does not count as True.

A site evaluated more than LIMIT times in one process is declared bad and no
longer checked: the check must not turn a test into a different, much slower
test (`==` between two large expression trees hashes both).
"""

from __future__ import annotations

LIMIT = 200_000

bad: set = set()
_count: dict = {}


def reset() -> None:
    bad.clear()
    _count.clear()


def snapshot() -> dict:
    return {"bad": sorted(bad)}


def _tick(s) -> bool:
    n = _count.get(s, 0) + 1
    _count[s] = n
    if n > LIMIT:
        bad.add(s)
        return False
    return True


def is_(s, a, b):
    r = a is b
    if s not in bad and _tick(s):
        try:
            if (a == b) is not r:
                bad.add(s)
        except BaseException:
            bad.add(s)
    return r


def isnot(s, a, b):
    r = a is not b
    if s not in bad and _tick(s):
        try:
            if (a != b) is not r:
                bad.add(s)
        except BaseException:
            bad.add(s)
    return r


def lt(s, a, b):
    r = a < b
    if s not in bad and _tick(s):
        try:
            if (a <= b) is not r:
                bad.add(s)
        except BaseException:
            bad.add(s)
    return r


def le(s, a, b):
    r = a <= b
    if s not in bad and _tick(s):
        try:
            if (a < b) is not r:
                bad.add(s)
        except BaseException:
            bad.add(s)
    return r


def gt(s, a, b):
    r = a > b
    if s not in bad and _tick(s):
        try:
            if (a >= b) is not r:
                bad.add(s)
        except BaseException:
            bad.add(s)
    return r


def ge(s, a, b):
    r = a >= b
    if s not in bad and _tick(s):
        try:
            if (a > b) is not r:
                bad.add(s)
        except BaseException:
            bad.add(s)
    return r

"""Split a set of tests over workers so that  (busiest worker + 2 s per worker)
is as small as possible.

For every worker count k the cost cannot be below
    max(longest test, ceil(total / k)) + 2 s * k.
Tests are packed longest first onto the least loaded worker (LPT) and, when
that does not reach the bound, by first-fit-decreasing into bins whose
capacity is searched between the bound and the LPT result (MULTIFIT).  With
one long test and hundreds of tiny ones, which is what this suite looks like,
both reach the bound; `pack` reports the gap so a caller can see when not.
"""

from __future__ import annotations

import heapq

from .config import MAX_WORKERS, WORKER_COST_MS


def lower_bound(total: int, longest: int, k: int) -> int:
    return max(longest, -(-total // k)) + WORKER_COST_MS * k


def best_possible(total: int, longest: int, count: int) -> int:
    """Lower bound of the cost of a commit, over all worker counts."""
    if count == 0:
        return 0
    return min(lower_bound(total, longest, k) for k in range(1, min(MAX_WORKERS, count) + 1))


def _lpt(items: list[tuple[int, int]], k: int) -> tuple[int, list[list[int]]]:
    heap = [(0, w) for w in range(k)]
    workers: list[list[int]] = [[] for _ in range(k)]
    for ms, number in items:
        load, w = heapq.heappop(heap)
        workers[w].append(number)
        heapq.heappush(heap, (load + ms, w))
    return max(load for load, _ in heap), workers


def _ffd(items: list[tuple[int, int]], k: int, capacity: int):
    loads = [0] * k
    workers: list[list[int]] = [[] for _ in range(k)]
    for ms, number in items:
        for w in range(k):
            if loads[w] + ms <= capacity:
                loads[w] += ms
                workers[w].append(number)
                break
        else:
            return None
    return max(loads), workers


def pack(durations: dict[int, int]) -> tuple[int, list[list[int]], int]:
    """durations: {test number: milliseconds}.  -> (cost in ms, workers, gap to the lower bound)."""
    if not durations:
        return 0, [], 0
    items = sorted(((ms, number) for number, ms in durations.items()), key=lambda it: (-it[0], it[1]))
    total = sum(ms for ms, _ in items)
    longest = items[0][0]
    best_cost, best_workers = None, None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        bound = lower_bound(total, longest, k)
        if best_cost is not None and bound >= best_cost:
            continue
        load, workers = _lpt(items, k)
        if load + WORKER_COST_MS * k > bound and len(items) <= 4000:
            lo, hi = bound - WORKER_COST_MS * k, load
            for _ in range(24):
                if lo >= hi:
                    break
                mid = (lo + hi) // 2
                got = _ffd(items, k, mid)
                if got is None:
                    lo = mid + 1
                else:
                    hi = got[0]
                    load, workers = got
        cost = load + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    workers = [sorted(w) for w in best_workers if w]
    return best_cost, workers, best_cost - best_possible(total, longest, len(items))


def cost_of(workers: list[list[int]], durations: dict[int, int]) -> int:
    loads = [sum(durations[n] for n in w) for w in workers if w]
    return (max(loads) + WORKER_COST_MS * len(loads)) if loads else 0

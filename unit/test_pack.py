import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blast.pack import best_possible, cost_of, pack  # noqa: E402


def brute(durations: dict[int, int]) -> int:
    """Exact optimum by trying every assignment (small inputs only)."""
    items = list(durations.items())
    best = None
    for k in range(1, min(8, len(items)) + 1):
        for assign in itertools.product(range(k), repeat=len(items)):
            if len(set(assign)) != k:
                continue
            loads = [0] * k
            for (number, ms), w in zip(items, assign):
                loads[w] += ms
            cost = max(loads) + 2000 * k
            if best is None or cost < best:
                best = cost
    return best


class PackTests(unittest.TestCase):
    def test_empty_costs_nothing(self):
        self.assertEqual(pack({}), (0, [], 0))

    def test_single_test(self):
        cost, workers, gap = pack({7: 31})
        self.assertEqual((cost, workers, gap), (2031, [[7]], 0))

    def test_tiny_tests_share_one_worker(self):
        durations = {i: 10 for i in range(100)}
        cost, workers, _ = pack(durations)
        self.assertEqual(len(workers), 1)
        self.assertEqual(cost, 3000)

    def test_long_test_sets_the_load_and_the_rest_rides_along(self):
        durations = {0: 21454, **{i: 50 for i in range(1, 101)}}   # 5 s of small tests
        cost, workers, gap = pack(durations)
        self.assertEqual(cost, 21454 + 2 * 2000)
        self.assertEqual(gap, 0)
        self.assertEqual(cost_of(workers, durations), cost)

    def test_worker_count_is_a_trade_off(self):
        # 8 tests of 10 s: 1 worker 82 s, 2 -> 44, 4 -> 28, 8 -> 26
        cost, workers, _ = pack({i: 10000 for i in range(8)})
        self.assertEqual(cost, 26000)
        self.assertEqual(len(workers), 8)
        # 8 tests of 1 s: 1 worker 10 s, 2 -> 8 s, 3 -> 9 s
        cost, workers, _ = pack({i: 1000 for i in range(8)})
        self.assertEqual(cost, 8000)
        self.assertEqual(len(workers), 2)

    def test_matches_brute_force_on_small_inputs(self):
        rng = random.Random(7)
        for _ in range(60):
            n = rng.randint(1, 7)
            durations = {i: rng.choice([1, 20, 300, 1500, 2500, 4000, 9000, 28817]) for i in range(n)}
            cost, workers, _ = pack(durations)
            self.assertEqual(sorted(x for w in workers for x in w), sorted(durations))
            self.assertEqual(cost_of(workers, durations), cost)
            self.assertEqual(cost, brute(durations), durations)

    def test_never_below_the_bound_and_close_to_it_on_realistic_sets(self):
        rng = random.Random(11)
        for _ in range(40):
            n = rng.randint(1, 400)
            durations = {i: rng.choice([1, 2, 5, 9, 30, 80, 400, 1300]) for i in range(n)}
            if rng.random() < 0.5:
                durations[10_000] = rng.choice([3863, 6686, 21454, 28817])
            cost, workers, gap = pack(durations)
            bound = best_possible(sum(durations.values()), max(durations.values()), len(durations))
            self.assertGreaterEqual(cost, bound)
            self.assertLessEqual(gap, 25, (cost, bound))
            self.assertLessEqual(len(workers), 8)
            self.assertEqual(sorted(x for w in workers for x in w), sorted(durations))

    def test_whole_suite_shape(self):
        durations = {0: 28817, 1: 21454, 2: 6686, 3: 3863, 4: 3537, **{i: 24 for i in range(5, 1620)}}
        cost, workers, gap = pack(durations)
        self.assertEqual(gap, 0)
        self.assertEqual(cost, 28817 + 2000 * len(workers))


if __name__ == "__main__":
    unittest.main()

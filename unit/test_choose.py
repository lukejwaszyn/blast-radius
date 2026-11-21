import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blast import config as C  # noqa: E402
from blast.choose import choose  # noqa: E402
from blast.maps import bits, mask_of  # noqa: E402


class G:
    def __init__(self, p, numbers, why="g"):
        self.p = p
        self.mask = sum(1 << n for n in numbers)
        self.why = why


class ChooseTests(unittest.TestCase):
    def setUp(self):
        self.ms = [10] * 100
        self.ms[0] = 21454
        self.ms[1] = 28817
        self.valid = (1 << 100) - 1

    def test_likely_groups_are_always_run(self):
        mask, _ = choose([G(C.P_LINE_HIT, [0, 5, 6])], self.ms, self.valid)
        self.assertEqual(bits(mask), [0, 5, 6])

    def test_unlikely_lonely_test_is_not_worth_a_worker(self):
        mask, log = choose([G(C.P_TEXT_READER_COMMENT, [7])], self.ms, self.valid)
        self.assertEqual(mask, 0)
        self.assertTrue(log)

    def test_unlikely_test_rides_along_once_a_worker_is_open(self):
        mask, _ = choose([G(C.P_FIXTURE_CONSUMER, [5]), G(C.P_TEXT_READER_COMMENT, [7])], self.ms, self.valid)
        self.assertEqual(bits(mask), [5, 7])

    def test_long_test_of_an_unlikely_group_is_weighed_alone(self):
        # 0.02 x 300 s = 6 s: the small openers are worth it, the 21 s one is not
        mask, _ = choose([G(C.P_FIXTURE_OPENER, [0, 10, 11, 12])], self.ms, self.valid)
        self.assertEqual(bits(mask), [10, 11, 12])

    def test_a_group_never_spends_more_than_its_expected_penalty(self):
        ms = [2500] * 10
        mask, _ = choose([G(0.03, range(10))], ms, (1 << 10) - 1)   # 9 s to spend, each test >= 2.5 s
        from blast.pack import best_possible

        chosen = bits(mask)
        self.assertLessEqual(best_possible(sum(ms[i] for i in chosen), 2500 if chosen else 0, len(chosen)), 9000)

    def test_tests_missing_from_the_run_are_ignored(self):
        mask, _ = choose([G(0.9, [3, 4])], self.ms, self.valid & ~(1 << 4))
        self.assertEqual(bits(mask), [3])

    def test_mask_helpers(self):
        self.assertEqual(bits(mask_of("0-2,5,9-10")), [0, 1, 2, 5, 9, 10])
        self.assertEqual(mask_of(""), 0)


if __name__ == "__main__":
    unittest.main()

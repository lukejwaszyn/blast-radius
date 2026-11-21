"""The operator of one comparison replaced by its neighbour (`is` -> `==`, `<` -> `<=` ...).

maps/cmp.json records, for every such comparison inside a function of sqlglot and for every
test, whether the neighbouring operator ever gave another result on the operands the test
produced.  These tests check the detection of the edit (no map needed) and, with the pinned
checkout and the map, that the split is used and that anything the map cannot vouch for
falls back to plain line coverage.
"""

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from blast import config as C  # noqa: E402
from blast.analyze import Analyzer, Source  # noqa: E402
from blast.maps import Maps, bits  # noqa: E402
from blast.pydiff import operator_swap  # noqa: E402

from test_generalization import HAVE_SRC, patch, read  # noqa: E402

HAVE_MAP = os.path.exists(os.path.join(ROOT, "maps", "cmp.json"))


def stmt(source):
    return ast.parse(source).body[0]


class DetectionTests(unittest.TestCase):
    def swap(self, old, new):
        return operator_swap([(stmt(old), stmt(new))])

    def test_single_operator_swaps(self):
        self.assertEqual(self.swap("if x is None:\n    pass\n", "if x == None:\n    pass\n")[2:], ("is", "=="))
        self.assertEqual(self.swap("y = a.b is not c\n", "y = a.b != c\n")[2:], ("isnot", "!="))
        self.assertEqual(self.swap("return_ = n < len(s)\n", "return_ = n <= len(s)\n")[2:], ("lt", "<="))
        line, col, _old, _new = self.swap("y = f(1,\n      a >= b)\n", "y = f(1,\n      a > b)\n")
        self.assertEqual((line, col), (2, 6))      # where the comparison starts in the old text

    def test_anything_more_is_not_a_swap(self):
        self.assertIsNone(self.swap("y = a is b\n", "y = a is b\n"))                 # nothing changed
        self.assertIsNone(self.swap("y = a is b\n", "y = c == b\n"))                 # an operand changed too
        self.assertIsNone(self.swap("y = a is b and c is d\n", "y = a == b and c == d\n"))   # two sites
        self.assertIsNone(self.swap("y = a < b < c\n", "y = a <= b < c\n"))          # chained comparison
        self.assertIsNone(self.swap("y = a + b\n", "y = a - b\n"))                   # not a comparison
        self.assertIsNone(self.swap("y = a == b\n", "y = a is b\n"))                 # `==` is not recorded

    def test_runtime_half_is_transparent_and_strict(self):
        sys.path.insert(0, os.path.join(ROOT, "tools", "tracer"))
        import blast_cmp as bc

        class Loose:
            def __eq__(self, other):
                return True

            __hash__ = None

        class Raises:
            def __eq__(self, other):
                raise RuntimeError("no")

        bc.reset()
        self.assertIs(bc.is_(1, None, None), True)
        self.assertIs(bc.is_(2, [], []), False)            # equal but not identical
        self.assertIs(bc.is_(3, Loose(), None), False)     # `==` says True
        self.assertIs(bc.is_(4, Raises(), None), False)    # `==` raises
        self.assertIs(bc.is_(5, 1, None), False)
        self.assertIs(bc.isnot(6, "a", None), True)
        self.assertIs(bc.lt(7, 1, 2), True)
        self.assertIs(bc.lt(8, 2, 2), False)               # `<=` says True
        self.assertIs(bc.ge(9, 2, 2), True)                # `>` says False
        self.assertIs(bc.gt(10, 3, 2), True)
        self.assertEqual(bc.snapshot()["bad"], [2, 3, 4, 8, 9])
        with self.assertRaises(TypeError):
            bc.lt(11, 1, "a")                               # the original comparison raises: so does this


@unittest.skipUnless(HAVE_SRC and HAVE_MAP, "needs the pinned checkout (BLAST_SRC) and maps/cmp.json")
class SwapSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_generalization import SRC

        cls.maps = Maps(os.path.join(ROOT, "maps"))
        cls.az = Analyzer(cls.maps, Source(SRC))

    def sites(self, op, min_ran=30):
        """(file, statement node, compare node, tests that ran the line, tests where the neighbour differed)
        for comparisons of the pinned source that are a statement's only content of interest."""
        table = self.maps.load("cmp")["files"]
        for rel in sorted(table):
            tree = ast.parse(read(rel))
            index = {(row[0], row[1]): row for row in table[rel]}
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and node.test.lineno == node.test.end_lineno:
                    row = index.get((node.test.lineno, node.test.col_offset))
                    if row is None or row[2] != op:
                        continue
                    if self.maps.ran_at_import(rel, node.lineno, node.lineno) and self.maps._set("cmp", row[3]):
                        continue      # runs at import and the operators disagreed somewhere: not split
                    ran = self.maps.line_tests(rel, node.lineno, node.test.end_lineno)
                    if len(bits(ran)) >= min_ran:
                        yield rel, node, node.test, ran, self.maps.neighbour_differs(rel, row[0], row[1], op, self.maps.load("cmp")["neighbour"][op])

    def groups(self, rel, text, new):
        an = self.az.analyze("t", patch(rel, text, new))
        return {g.p: g.mask for g in an.groups if g.mask}, an

    def rewrite(self, rel, compare, old, new):
        text = read(rel)
        lines = text.split("\n")
        line = lines[compare.left.end_lineno - 1]
        gap_start, gap_end = compare.left.end_col_offset, compare.comparators[0].col_offset
        raw = line.encode()
        self.assertEqual(raw[gap_start:gap_end].decode().strip(), old)
        lines[compare.left.end_lineno - 1] = (raw[:gap_start] + f" {new} ".encode() + raw[gap_end:]).decode()
        return text, "\n".join(lines)

    def test_is_to_eq_splits_the_tests_that_ran_the_line(self):
        checked = 0
        for rel, node, compare, ran, differs in self.sites("is"):
            text, new = self.rewrite(rel, compare, "is", "==")
            groups, an = self.groups(rel, text, new)
            alone = self.maps.call_tests(rel, node.lineno, node.test.end_lineno, warm=False)
            self.assertEqual(groups.get(C.P_LINE_HIT, 0), (ran & differs) | (ran & ~alone), rel)
            self.assertEqual(groups.get(C.P_OPERATOR_AGREED, 0), alone & ~differs, rel)
            self.assertEqual(groups.get(C.P_LINE_HIT, 0) | groups.get(C.P_OPERATOR_AGREED, 0), ran)   # nobody is dropped silently
            checked += 1
            if checked == 5:
                break
        self.assertEqual(checked, 5)

    def test_other_operator_changes_keep_plain_line_coverage(self):
        rel, node, compare, ran, differs = next(iter(self.sites("is")))
        text, new = self.rewrite(rel, compare, "is", "is not")       # a negation: never equivalent
        groups, _ = self.groups(rel, text, new)
        self.assertEqual(groups.get(C.P_LINE_HIT, 0), ran)
        self.assertNotIn(C.P_OPERATOR_AGREED, groups)
        text, new = self.rewrite(rel, compare, "is", "!=")
        groups, _ = self.groups(rel, text, new)
        self.assertEqual(groups.get(C.P_LINE_HIT, 0), ran)

    def test_map_is_about_the_pinned_operator_at_that_place(self):
        rel, node, compare, ran, differs = next(iter(self.sites("is")))
        op = self.maps.load("cmp")["neighbour"]
        self.assertIsNone(self.maps.neighbour_differs(rel, compare.lineno, compare.col_offset, "is", "!="))
        self.assertIsNone(self.maps.neighbour_differs(rel, compare.lineno, compare.col_offset, "lt", op["lt"]))
        self.assertIsNone(self.maps.neighbour_differs(rel, compare.lineno, compare.col_offset + 1, "is", "=="))
        self.assertIsNone(self.maps.neighbour_differs("sqlglot/no_such_file.py", 1, 0, "is", "=="))


if __name__ == "__main__":
    unittest.main()

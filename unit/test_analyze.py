"""End-to-end checks of the analysis against the pinned checkout.

Need sqlglot at the pinned commit: set BLAST_SRC to the checkout (inside the
image it is /opt/sqlglot).  Skipped when it is not there.  The patches are
made up here from the pinned text; the assertions are about *which kind of
reasoning* applies, never about recorded answers.
"""

import difflib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from blast import config as C  # noqa: E402
from blast.analyze import Analyzer, Source  # noqa: E402
from blast.choose import choose  # noqa: E402
from blast.maps import Maps, bits  # noqa: E402

SRC = os.environ.get("BLAST_SRC", C.SRC_DIR)
HAVE_SRC = os.path.isfile(os.path.join(SRC, "sqlglot", "__init__.py"))


def read(rel):
    with open(os.path.join(SRC, rel), encoding="utf-8") as fh:
        return fh.read()


def patch(rel, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/" + rel, "b/" + rel))
    return (f"diff --git a/{rel} b/{rel}\n" + body).encode()


def edit(rel, a, b):
    old = read(rel)
    assert a in old, a
    return patch(rel, old, old.replace(a, b, 1))


@unittest.skipUnless(HAVE_SRC, "pinned sqlglot checkout not available (set BLAST_SRC)")
class AnalyzeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.maps = Maps(os.path.join(ROOT, "maps"))
        cls.az = Analyzer(cls.maps, Source(SRC))
        cls.ms = [int(s * 1000 + 0.5) for s in cls.maps.load("tests")["seconds"]]

    def run_patch(self, data, with_fallback=True):
        an = self.az.analyze("t", data)
        groups = list(an.groups)
        if with_fallback and an.exact is not None:
            groups += an.exact.fallback
        mask, _ = choose(groups, self.ms, self.maps.all_mask)
        return an, set(bits(mask))

    def ids(self, numbers):
        return {self.maps.ids[n] for n in numbers}

    def test_comment_in_code_selects_nothing(self):
        an, sel = self.run_patch(edit("sqlglot/serde.py", "            payload[CLASS] = klass\n",
                                      "            # a comment\n            payload[CLASS] = klass\n"))
        self.assertEqual(sel, set())
        self.assertIsNone(an.exact)

    def test_changed_statement_selects_the_tests_that_ran_it(self):
        an, sel = self.run_patch(edit("sqlglot/serde.py", "if node.type and node.type is not node:",
                                      "if node.type or node.type is not node:"))
        self.assertTrue(sel)
        self.assertEqual(sel, set(bits(self.maps.line_tests("sqlglot/serde.py", 53, 53))))
        self.assertTrue(sel < set(bits(self.maps.module_tests("sqlglot.serde"))) or
                        sel == set(bits(self.maps.module_tests("sqlglot.serde"))))

    def test_syntax_error_selects_every_test_that_loads_the_module(self):
        an, sel = self.run_patch(edit("sqlglot/dialects/tsql.py", "class TSQL(Dialect):\n", "class TSQL(Dialect):\n    def (:\n"))
        self.assertEqual(sel, set(bits(self.maps.module_tests("sqlglot.dialects.tsql"))))
        an, sel = self.run_patch(edit("sqlglot/trie.py", "import typing as t\n", "import typing as t\n\tx = (\n"))
        self.assertGreaterEqual(len(sel), 1600)

    def test_class_level_value_selects_its_readers_and_asks_for_the_import_comparison(self):
        an, sel = self.run_patch(edit("sqlglot/dialects/tsql.py", "    WEEK_OFFSET = -1\n", "    WEEK_OFFSET = -2\n"), with_fallback=False)
        _, readers = self.maps.readers("sqlglot.dialects.tsql:TSQL|WEEK_OFFSET")
        self.assertEqual(sel, set(bits(readers)))
        self.assertTrue(0 < len(sel) < 20)
        self.assertIsNotNone(an.exact)
        self.assertIn("sqlglot/dialects/tsql.py", an.exact.files)
        self.assertTrue(an.exact.const_only)

    def test_lambda_in_class_level_table_is_looked_up_by_coverage(self):
        an, sel = self.run_patch(edit("sqlglot/parsers/dremio.py", "            this=seq_get(args, 0),\n            expression=seq_get(args, 1),\n            mode=seq_get(args, 2),",
                                      "            this=seq_get(args, 1),\n            expression=seq_get(args, 1),\n            mode=seq_get(args, 2),"))
        self.assertTrue(0 < len(sel) <= 5)
        self.assertIsNone(an.exact)

    def test_fixture_edit_selects_its_consumer_but_dashdash_lines_do_not(self):
        rel = "tests/fixtures/optimizer/simplify.sql"
        an, sel = self.run_patch(edit(rel, "x + 3;", "x + 4;"))
        self.assertEqual(self.ids(sel), {"tests/test_optimizer.py::TestOptimizer::test_simplify"})
        old = read(rel)
        an, sel = self.run_patch(patch(rel, old, "-- a note\n" + old))
        self.assertEqual(sel, set())
        an, sel = self.run_patch(patch(rel, old, "# a note\n" + old))   # `#` lines are directives: a real change
        self.assertEqual(len(sel), 1)

    def test_test_body_edit_selects_that_test(self):
        an, sel = self.run_patch(edit("tests/test_helper.py", 'self.assertEqual(tsort({"a": set()}), ["a"])',
                                      'self.assertEqual(tsort({"a": set()}), ["b"])'))
        self.assertEqual(self.ids(sel), {"tests/test_helper.py::TestHelper::test_tsort"})

    def test_shared_test_helper_reaches_its_users(self):
        an, sel = self.run_patch(edit("tests/dialects/test_dialect.py",
                                      "            write_sql or sql, expression.sql(dialect=self.dialect, pretty=pretty, identify=identify)",
                                      "            write_sql or sql, expression.sql(dialect=self.dialect, pretty=not pretty, identify=identify)"))
        files = {i.split("::")[0] for i in self.ids(sel)}
        self.assertGreater(len(files), 20)
        self.assertTrue(all(f.startswith("tests/dialects/") for f in files))

    def test_new_test_and_docs_select_nothing(self):
        new_test = b"diff --git a/tests/test_zz.py b/tests/test_zz.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_zz.py\n@@ -0,0 +1,2 @@\n+def test_new():\n+    assert False\n"
        self.assertEqual(self.run_patch(new_test)[1], set())
        old = read("README.md")
        self.assertEqual(self.run_patch(patch("README.md", old, "Edited.\n" + old))[1], set())
        self.assertEqual(self.run_patch(b"diff --git a/sqlglot/trie.py b/sqlglot/trie.py\nold mode 100644\nnew mode 100755\n")[1], set())
        self.assertEqual(self.run_patch(b"")[1], set())

    def test_refactors_that_bind_the_same_things_select_nothing_statically(self):
        rel = "sqlglot/optimizer/eliminate_joins.py"
        one = "from sqlglot.optimizer.scope import Scope, find_in_scope, traverse_scope\n"
        an, sel = self.run_patch(edit(rel, one, "from sqlglot.optimizer.scope import Scope\nfrom sqlglot.optimizer.scope import find_in_scope, traverse_scope\n"),
                                 with_fallback=False)
        self.assertEqual(sel, set())
        self.assertIsNotNone(an.exact)       # the import of the patched tree still checks that it imports
        an, sel = self.run_patch(edit(rel, "from sqlglot.optimizer.normalize import normalized\n" + one,
                                      one + "from sqlglot.optimizer.normalize import normalized\n"), with_fallback=False)
        self.assertEqual(sel, set())
        an, sel = self.run_patch(edit("sqlglot/generators/drill.py", "    def ilike_sql(self, expression: exp.ILike) -> str:\n",
                                      '    def ilike_sql(self, expression: exp.ILike) -> str:\n        """Render ILIKE."""\n'))
        self.assertEqual(sel, set())
        self.assertIsNone(an.exact)

    def test_broken_import_selects_the_tests_that_load_the_module(self):
        rel = "sqlglot/optimizer/eliminate_joins.py"
        an, sel = self.run_patch(edit(rel, "from sqlglot.optimizer.normalize import normalized", "from sqlglot.optimizer.normalize import normalised"),
                                 with_fallback=False)
        self.assertEqual(sel, set(bits(self.maps.module_tests("sqlglot.optimizer.eliminate_joins"))))

    def test_one_line_lambda_in_a_table_is_found_through_function_entry(self):
        # the lambda's whole body is on its first line: only the entry event records its callers
        an, sel = self.run_patch(edit("sqlglot/generators/teradata.py", 'exp.Use: lambda self, e: f"DATABASE {self.sql(e, \'this\')}",',
                                      'exp.Use: lambda self, e: f"SCHEMA {self.sql(e, \'this\')}",'))
        self.assertTrue(sel)
        self.assertTrue(all(i.startswith("tests/dialects/test_teradata.py") for i in self.ids(sel)))
        self.assertIsNone(an.exact)

    def test_pytest_configuration(self):
        old = read("pyproject.toml")
        self.assertEqual(self.run_patch(patch("pyproject.toml", old, old.replace("easily", "very easily", 1)))[1], set())
        an, sel = self.run_patch(patch("pyproject.toml", old, old + '\n[tool.pytest.ini_options]\naddopts = "-x"\n'))
        self.assertEqual(len(sel), self.maps.n)
        an, sel = self.run_patch(patch("pyproject.toml", old, old + "\n[broken\n"))
        self.assertEqual(len(sel), self.maps.n)
        conftest = b"--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+raise RuntimeError\n"
        self.assertEqual(len(self.run_patch(conftest)[1]), self.maps.n)

    def test_deleted_module_and_unreadable_patch_fall_back_wide(self):
        old = read("sqlglot/trie.py")
        gone = ("diff --git a/sqlglot/trie.py b/sqlglot/trie.py\ndeleted file mode 100644\n" +
                "".join(difflib.unified_diff(old.splitlines(True), [], "a/sqlglot/trie.py", "/dev/null"))).encode()
        self.assertGreaterEqual(len(self.run_patch(gone)[1]), 1600)
        self.assertEqual(len(self.run_patch(b"not a patch\n")[1]), self.maps.n)
        mismatch = b"--- a/sqlglot/trie.py\n+++ b/sqlglot/trie.py\n@@ -5,3 +5,3 @@\n nothing like\n-the pinned\n+file\n at all\n"
        self.assertGreaterEqual(len(self.run_patch(mismatch)[1]), 1600)


if __name__ == "__main__":
    unittest.main()

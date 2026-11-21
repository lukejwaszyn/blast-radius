"""Regression tests for unsound assumptions found with made-up patches of kinds
the sample does not have.  Each test builds its patch from the pinned text and
derives what must be selected from first principles (who imports the file, who
names the symbol, who loads the module), never from a recorded answer.

Need sqlglot at the pinned commit (BLAST_SRC); skipped otherwise.
"""

import ast
import difflib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from blast import config as C  # noqa: E402
from blast import exact  # noqa: E402
from blast.analyze import Analyzer, Group, Source, declared_encoding  # noqa: E402
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


def new_file(rel, text):
    lines = text.splitlines(True)
    return (f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n"
            + "".join("+" + line for line in lines)).encode()


class EncodingTests(unittest.TestCase):
    def test_declared_encoding_follows_pep_263(self):
        self.assertEqual(declared_encoding("x = 1\n"), "utf-8")
        self.assertEqual(declared_encoding("# -*- coding: latin-1 -*-\nx = 1\n"), "iso8859-1")
        self.assertEqual(declared_encoding("#!/usr/bin/python\n# vim: set fileencoding=utf8 :\n"), "utf-8")
        self.assertEqual(declared_encoding("x = 1\n# coding: latin-1\n"), "utf-8")      # second line only after a comment
        self.assertEqual(declared_encoding("\n\n# coding: latin-1\n"), "utf-8")           # third line never
        self.assertTrue(declared_encoding("# coding: no-such-codec\n").startswith("?"))


class SnapshotTests(unittest.TestCase):
    def test_a_package_that_no_longer_imports_is_reported_not_raised(self):
        import snapshot

        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "zzpkg_blast"))
            with open(os.path.join(tmp, "zzpkg_blast", "__init__.py"), "w") as fh:
                fh.write("raise RuntimeError('boom')\n")
            sys.path.insert(0, tmp)
            try:
                snap = snapshot.take("zzpkg_blast")
            finally:
                sys.path.remove(tmp)
        self.assertTrue(snap.get("fatal"))
        self.assertIn("zzpkg_blast", snap["errors"])
        self.assertIn("RuntimeError", snap["errors"]["zzpkg_blast"])

    def test_a_logger_is_fingerprinted_by_name_not_by_state(self):
        import logging

        import snapshot

        log = logging.getLogger("blast.unit.logger")
        before = snapshot.Canon().text(log)
        log.isEnabledFor(logging.ERROR)      # fills the logger's level cache
        log.setLevel(logging.WARNING)
        self.assertEqual(before, snapshot.Canon().text(log))


@unittest.skipUnless(HAVE_SRC, "pinned sqlglot checkout not available (set BLAST_SRC)")
class GeneralizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.maps = Maps(os.path.join(ROOT, "maps"))
        cls.az = Analyzer(cls.maps, Source(SRC))
        cls.ms = [int(s * 1000 + 0.5) for s in cls.maps.load("tests")["seconds"]]

    def run_patch(self, data, with_fallback=False):
        an = self.az.analyze("t", data)
        groups = list(an.groups)
        if with_fallback and an.exact is not None:
            groups += an.exact.fallback
        mask, _ = choose(groups, self.ms, self.maps.all_mask)
        return an, set(bits(mask))

    def files_tests(self, pred):
        return {i for i, tid in enumerate(self.maps.ids) if pred(tid.split("::")[0])}

    # ---- the test package itself -------------------------------------------------
    def test_code_in_the_test_package_marker_reaches_every_test_below_it(self):
        # `import tests.x` runs tests/__init__.py first; the file is empty at the pinned commit,
        # so no test has line coverage in it
        _, sel = self.run_patch(patch("tests/__init__.py", "", "raise RuntimeError('boom')\n"))
        self.assertEqual(len(sel), self.maps.n)
        _, sel = self.run_patch(patch("tests/__init__.py", "", "def broken(:\n"))
        self.assertEqual(len(sel), self.maps.n)
        _, sel = self.run_patch(patch("tests/dialects/__init__.py", "", "raise RuntimeError('boom')\n"))
        self.assertTrue(sel >= self.files_tests(lambda f: f.startswith("tests/dialects/")))
        # a comment there still selects nothing
        _, sel = self.run_patch(patch("tests/__init__.py", "", "# the test package\n"))
        self.assertEqual(sel, set())

    # ---- names that maps/reads.json does not track --------------------------------
    def importers(self, module, name):
        """Test files with `from <module> import <name>` at module level, found in the pinned tree."""
        out = set()
        for rel in self.az.src.python_files():
            if not rel.startswith("tests/"):
                continue
            for node in ast.parse(read(rel)).body:
                if isinstance(node, ast.ImportFrom) and node.module == module and any(a.name == name for a in node.names):
                    out.add(rel)
        return out

    def test_removed_reexport_reaches_the_files_that_import_it(self):
        # `to_table` is bound in sqlglot/__init__.py by an import, not by an assignment: no read map
        files = self.importers("sqlglot", "to_table")
        self.assertTrue(files)
        an, sel = self.run_patch(edit("sqlglot/__init__.py", "    to_table as to_table,\n", ""))
        self.assertTrue(sel >= self.files_tests(lambda f: f in files))
        self.assertLess(len(sel), 400)      # ... and not everything that mentions the word

    def test_name_users_resolves_aliases_and_string_targets(self):
        # mock.patch("sqlglot.dialects.dialect.entry_points", ...) names the symbol in a string
        mask, _ = self.az.name_users("sqlglot.dialects.dialect", "entry_points")
        want = self.files_tests(lambda f: f == "tests/test_dialect_entry_points.py")
        self.assertTrue(want & set(bits(mask)))
        # `exp.to_table` is sqlglot.expressions.to_table, not sqlglot.to_table
        aliases = self.az.module_aliases("tests/test_expressions.py")
        self.assertEqual(aliases.get("exp"), "sqlglot.expressions")

    def test_untracked_names_are_not_taken_as_unread(self):
        maps = self.maps
        # bound by an import: the read map cannot answer, the pinned source can
        got = exact.untracked_users("sqlglot:|to_table", "removed", maps, self.az)
        self.assertIsNotNone(got)
        self.assertTrue(got[0])
        # the same function with another body is not a rebinding: line coverage finds its callers
        self.assertIsNone(exact.untracked_users("sqlglot:|to_table", "changed", maps, self.az))
        # a new name has no reader at the pinned commit
        self.assertIsNone(exact.untracked_users("sqlglot:|brand_new_name", "added", maps, self.az))
        # bound by an assignment and read by tests: the read map answers
        self.assertIsNone(exact.untracked_users("sqlglot.dialects.tsql:TSQL|WEEK_OFFSET", "changed", maps, self.az))
        # consumed by the interpreter when the class is created
        got = exact.untracked_users("sqlglot.optimizer.pushdown_projections:DependencyNode|__slots__", "changed", maps, self.az)
        self.assertIsNotNone(got)
        self.assertTrue(got[0])

    def test_slots_and_dataclass_fields_reach_the_users_of_the_class(self):
        rel = "sqlglot/optimizer/pushdown_projections.py"
        an, sel = self.run_patch(edit(rel, '__slots__ = ("dependencies", "required_by")', '__slots__ = ("dependencies",)'))
        st = self.maps.static(rel)
        lo, hi = next((sc[2], sc[3]) for sc in st["scopes"] if sc[0] == "DependencyNode")
        ran_the_class = set(bits(self.maps.line_tests(rel, lo, hi)))
        self.assertTrue(ran_the_class)
        self.assertTrue(sel >= ran_the_class)
        # a field of a @dataclass: its default goes into the generated __init__, nobody loads it by name
        rel = "sqlglot/lineage.py"
        old = read(rel)
        tree = ast.parse(old)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.decorator_list)
        field = next(s for s in cls.body if isinstance(s, ast.AnnAssign) and s.value is not None)
        lines = old.split("\n")
        seg = ast.get_source_segment(old, field.value)
        lines[field.lineno - 1] = lines[field.lineno - 1].replace(seg, "'changed'" if seg != "'changed'" else "'other'")
        an, sel = self.run_patch(patch(rel, old, "\n".join(lines)))
        users = set(bits(self.maps.line_tests(rel, cls.lineno, cls.end_lineno)))
        self.assertTrue(users)
        self.assertTrue(sel >= users)

    # ---- source encoding ----------------------------------------------------------
    def non_ascii_literals(self, rel):
        """(strings used as values, strings that stand alone as statements) holding non-ASCII text."""
        tree = ast.parse(read(rel))
        alone = set()
        for n in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(n, field, None)
                if isinstance(stmts, list):
                    alone |= {id(s.value) for s in stmts if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)}
        used = docs = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and not node.value.isascii():
                if id(node) in alone:
                    docs += 1
                else:
                    used += 1
        return used, docs

    def test_coding_cookie_on_a_file_with_non_ascii_text_is_a_change_of_the_whole_file(self):
        files = [r for r in self.az.src.python_files() if r.startswith(("sqlglot/", "tests/test_")) and not read(r).isascii()]
        with_literals = next(r for r in files if self.non_ascii_literals(r)[0])
        comments_only = next(r for r in files if r.startswith("sqlglot/") and self.non_ascii_literals(r)[0] == 0)
        # a non-ASCII string literal now decodes to other characters, in a statement the hunk does not touch
        old = read(with_literals)
        _, sel = self.run_patch(patch(with_literals, old, "# -*- coding: latin-1 -*-\n" + old))
        if with_literals.startswith("sqlglot/"):
            affected = set(bits(self.maps.module_tests(self.maps.module_of(with_literals))))
        else:
            affected = set(bits(self.maps.tests_in_file(with_literals)))
        self.assertTrue(affected and sel >= affected)
        # non-ASCII text only in comments and in strings that stand alone as statements: nothing executes differently
        old = read(comments_only)
        _, sel = self.run_patch(patch(comments_only, old, "# -*- coding: latin-1 -*-\n" + old))
        self.assertEqual(len(sel), 0)
        # the same cookie on a pure-ASCII file decodes to the same text: nothing
        rel = "sqlglot/trie.py"
        old = read(rel)
        self.assertTrue(old.isascii())
        _, sel = self.run_patch(patch(rel, old, "# -*- coding: latin-1 -*-\n" + old))
        self.assertEqual(len(sel), 0)

    # ---- state left behind by other tests -------------------------------------------
    def test_branch_reached_only_after_another_test_cleared_the_registry(self):
        # Dialect._try_load: `if key not in cls._classes:` is false for every test that runs alone
        # (importing the dialect registers it).  tests/test_dialect_entry_points.py clears the
        # registry, so in a shared process later tests run the body of that `if`.
        rel = "sqlglot/dialects/dialect.py"
        old = read(rel)
        tree = ast.parse(old)
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_try_load")
        guard = next(n for n in ast.walk(func) if isinstance(n, ast.If) and ast.unparse(n.test) == "key not in cls._classes"
                     and isinstance(n.body[0], ast.For))
        loop = guard.body[0]
        alone = self.maps.call_tests(rel, loop.lineno, loop.end_lineno, warm=False)
        self.assertEqual(alone, 0)                        # the premise: no test runs it alone
        reach_guard = set(bits(self.maps.call_tests(rel, guard.lineno, guard.lineno, warm=False)))
        self.assertGreater(len(reach_guard), 500)
        line = old.split("\n")[loop.body[-1].lineno - 1]
        self.assertIn("if", old.split("\n")[loop.body[-1].lineno - 1] + line)
        target = loop.body[0]                               # attr = getattr(module, attr_name, None)
        text = old.split("\n")[target.lineno - 1]
        an, sel = self.run_patch(edit(rel, text + "\n", text.replace("None", "False") + "\n"))
        self.assertTrue(sel >= reach_guard)

    # ---- import of the patched tree -----------------------------------------------
    def test_fatal_import_result_selects_everything(self):
        class Job:
            pass

        job = Job()
        job.result = {"changed": {}, "errors": {"sqlglot": "RuntimeError: boom"}, "fatal": True}
        job.analysis = self.az.analyze("t", new_file("sqlglot/_version.py", "raise RuntimeError('boom')\n"))
        self.assertIsNotNone(job.analysis.exact)     # a new module is checked by importing the tree
        groups = exact.groups_from(job, self.maps, Group, self.az)
        mask, _ = choose(groups, self.ms, self.maps.all_mask)
        self.assertEqual(len(bits(mask)), self.maps.n)


if __name__ == "__main__":
    unittest.main()

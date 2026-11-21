"""Regression tests for the second round of made-up patches (kinds the sample does not have).

As in test_generalization.py, every patch is built from the pinned text and what must be
selected is derived from the maps' own primitives (who runs the first line of a function,
who loads a module, which tests a file holds), never from a recorded answer.

The tests that need sqlglot at the pinned commit (BLAST_SRC) are skipped without it.
"""

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from blast import config as C  # noqa: E402
from blast.analyze import Analyzer, Source, collected_by_pytest, is_pytest_config, same_tree_as_text  # noqa: E402
from blast.choose import choose  # noqa: E402
from blast.maps import Maps, bits  # noqa: E402
from blast.pydiff import scope_shift  # noqa: E402
from blast.state import changed_writers, new_write_targets, writers  # noqa: E402

from test_generalization import HAVE_SRC, edit, new_file, patch, read  # noqa: E402


def fn(source):
    return ast.parse(source).body[0]


class ScopeShiftTests(unittest.TestCase):
    """One statement can change what other, untouched lines of its function do."""

    def test_assignment_makes_a_global_local_everywhere_in_the_function(self):
        old = fn("def f(x):\n    if x:\n        return helper(x)\n    return 0\n")
        new = fn("def f(x):\n    if x:\n        return helper(x)\n    helper = None\n    return 0\n")
        self.assertIn("helper", scope_shift(old, new))
        # run it: the untouched line 3 now raises
        ns = {"helper": lambda v: v}
        exec(compile(ast.Module(body=[new], type_ignores=[]), "<t>", "exec"), ns)
        with self.assertRaises(UnboundLocalError):
            ns["f"](1)

    def test_yield_turns_the_function_into_a_generator(self):
        old = fn("def f(x):\n    if x > 100:\n        return None\n    return x\n")
        new = fn("def f(x):\n    if x > 100:\n        yield\n        return None\n    return x\n")
        self.assertIn("generator", scope_shift(old, new))

    def test_global_declaration_and_closure_capture(self):
        old = fn("def f():\n    count = 0\n    return count\n")
        new = fn("def f():\n    global count\n    count = 0\n    return count\n")
        self.assertIsNotNone(scope_shift(old, new))
        old = fn("def f(items):\n    def g():\n        return total\n    return g()\n")
        new = fn("def f(items):\n    def g():\n        return total\n    total = 1\n    return g()\n")
        self.assertIsNotNone(scope_shift(old, new))       # `total` in g: global -> closure variable

    def test_ordinary_edits_are_not_scope_shifts(self):
        old = fn("def f(x):\n    y = x + 1\n    return helper(y)\n")
        self.assertIsNone(scope_shift(old, fn("def f(x):\n    y = x - 1\n    return helper(y)\n")))
        self.assertIsNone(scope_shift(old, fn("def f(x):\n    y = x + 1\n    z = 2\n    return helper(y)\n")))    # a new local
        self.assertIsNone(scope_shift(old, fn("def f(x):\n    y = x + 1\n    return helper(y).name\n")))
        # an attribute called like a local is not that local
        self.assertIsNone(scope_shift(fn("def f(x):\n    return x.this\n"), fn("def f(x):\n    this = x\n    return this.this\n")))


class StateWriterTests(unittest.TestCase):
    """Statements whose effect outlives their frame (blast/state.py)."""

    def what(self, source, **kw):
        return [scope for scope, _what, _dump in writers(ast.parse(source), **kw)]

    def test_writes_to_imported_objects_are_shared_state(self):
        src = (
            "import os\nimport sqlglot\nfrom sqlglot.parsers.snowflake import SnowflakeParser\n\n"
            "CACHE = {}\nCACHE['a'] = 1\n"                                   # the module's own table, at its own level
            "sqlglot.pretty = True\n"
            "class T:\n"
            "    def setUp(self):\n"
            "        self.x = 1\n"                                            # instance state
            "        local = {}\n        local['k'] = 2\n"                    # a local
            "    def test_a(self):\n"
            "        SnowflakeParser.FUNCTIONS['X'] = None\n"
            "        try:\n            pass\n        finally:\n            del SnowflakeParser.FUNCTIONS['X']\n"
            "    def test_b(self):\n"
            "        os.environ['A'] = '1'\n"
            "        __import__('sqlglot').pretty = False\n"
            "        sqlglot.Dialect.classes.clear()\n"
            "        setattr(sqlglot, 'pretty', True)\n"
            "    def test_c(self, node):\n"
            "        node.args['this'] = 1\n        node.expressions.append(2)\n        CACHE['b'] = 2\n"
        )
        self.assertEqual(self.what(src), ["", "T.test_a", "T.test_a", "T.test_b", "T.test_b", "T.test_b", "T.test_b", "T.test_c"])
        self.assertEqual(self.what(src, functions_only=True),
                         ["T.test_a", "T.test_a", "T.test_b", "T.test_b", "T.test_b", "T.test_b", "T.test_c"])

    def test_global_declaration_but_not_nonlocal(self):
        src = (
            "_schema = None\n"
            "def initializer(schema):\n    global _schema\n    _schema = schema\n"
            "def outer():\n    i = 0\n    def step():\n        nonlocal i\n        i += 1\n    step()\n    return i\n"
        )
        self.assertEqual(self.what(src), ["initializer"])

    def test_changed_and_new_writers(self):
        old = ast.parse("import m\ndef f(x):\n    m.CACHE[x] = 1\n    return x + 1\n")
        same_target = ast.parse("import m\ndef f(x):\n    m.CACHE[x] = 2\n    return x + 1\n")
        other_line = ast.parse("import m\ndef f(x):\n    m.CACHE[x] = 1\n    return x + 2\n")
        new_target = ast.parse("import m\ndef f(x):\n    m.CACHE[x] = 1\n    m.flag = True\n    return x + 1\n")
        self.assertEqual(changed_writers(old, other_line), [])
        self.assertEqual(changed_writers(old, same_target), ["f"])
        self.assertEqual(new_write_targets(old, same_target), [])     # line coverage already covers it
        self.assertEqual(new_write_targets(old, new_target), ["f"])


class NamesTests(unittest.TestCase):
    def test_pytest_configuration_files(self):
        for path in ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg", "pyproject.toml",
                     "tests/pytest.ini", "tests/pytest.toml"):
            self.assertTrue(is_pytest_config(path), path)
        for path in ("tests/fixtures/pytest.ini", "sqlglotc/pyproject.toml", "docs/pytest.toml", "pytest.yaml"):
            self.assertFalse(is_pytest_config(path), path)

    def test_files_pytest_collects(self):
        self.assertTrue(collected_by_pytest("tests/test_new.py"))
        self.assertTrue(collected_by_pytest("benchmarks/parse_test.py"))
        self.assertTrue(collected_by_pytest("test_root.py"))
        self.assertFalse(collected_by_pytest("tests/helpers_new.py"))
        self.assertFalse(collected_by_pytest(".github/test_ci.py"))
        self.assertFalse(collected_by_pytest("tests/fixtures/test_data.sql"))

    def test_a_byte_order_mark_is_invisible_to_the_interpreter_but_not_to_a_text_reader(self):
        self.assertTrue(same_tree_as_text("x = 1\n", "x = 1  # note\n"))
        self.assertFalse(same_tree_as_text("x = 1\n", "\ufeffx = 1\n"))
        ast.parse("\ufeffx = 1\n".encode("utf-8"))      # as source bytes it is fine


@unittest.skipUnless(HAVE_SRC, "pinned sqlglot checkout not available (set BLAST_SRC)")
class Round2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_generalization import SRC

        cls.maps = Maps(os.path.join(ROOT, "maps"))
        cls.az = Analyzer(cls.maps, Source(SRC))
        cls.ms = [int(s * 1000 + 0.5) for s in cls.maps.load("tests")["seconds"]]

    def run_patch(self, data):
        an = self.az.analyze("t", data)
        mask, _ = choose(list(an.groups), self.ms, self.maps.all_mask)
        return an, set(bits(mask))

    def files_tests(self, pred):
        return {i for i, tid in enumerate(self.maps.ids) if pred(tid.split("::")[0])}

    # ---- scope ------------------------------------------------------------------
    def function_with_dead_tail(self):
        """(file, text, def, global name, tests entering it): a module-level function that many tests
        enter, that calls a module-level name, and whose last statement is a `return` that not all
        of them reach: code appended after that return never runs."""
        for rel in ("sqlglot/helper.py", "sqlglot/time.py", "sqlglot/transforms.py", "sqlglot/schema.py",
                    "sqlglot/optimizer/simplify.py", "sqlglot/optimizer/scope.py", "sqlglot/dialects/dialect.py"):
            text = read(rel)
            tree = ast.parse(text)
            globals_ = {a.asname or a.name.split(".")[0] for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom)) for a in s.names}
            globals_ |= {s.name for s in tree.body if isinstance(s, (ast.FunctionDef, ast.ClassDef))}
            for f in tree.body:
                if not isinstance(f, ast.FunctionDef) or not isinstance(f.body[-1], ast.Return):
                    continue
                stored = {n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                stored |= {a.arg for a in f.args.args + f.args.kwonlyargs}
                if any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.FunctionDef, ast.Lambda)) for n in ast.walk(f) if n is not f):
                    continue
                # (a name the function *calls*: one that only appears in annotations is never loaded)
                loads = sorted({n.func.id for b in f.body for n in ast.walk(b)
                                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)} & globals_ - stored)
                first = f.body[1] if isinstance(f.body[0], ast.Expr) and len(f.body) > 1 else f.body[0]
                entered = set(bits(self.maps.line_tests(rel, first.lineno, first.lineno)))
                tail = set(bits(self.maps.line_tests(rel, f.body[-1].lineno, f.body[-1].end_lineno)))
                if loads and len(entered) >= 20 and entered - tail:
                    return rel, text, f, loads[0], entered
        self.fail("no suitable function")

    def test_dead_assignment_that_shadows_a_global_reaches_everyone_who_enters_the_function(self):
        rel, text, f, name, entered = self.function_with_dead_tail()
        lines = text.split("\n")
        indent = " " * f.body[-1].col_offset
        new = "\n".join(lines[:f.end_lineno] + [f"{indent}{name} = None"] + lines[f.end_lineno:])
        an, sel = self.run_patch(patch(rel, text, new))
        self.assertTrue(sel >= entered, (f.name, name, len(entered - sel)))

    def test_dead_yield_reaches_everyone_who_enters_the_function(self):
        rel, text, f, _name, entered = self.function_with_dead_tail()
        lines = text.split("\n")
        indent = " " * f.body[-1].col_offset
        new = "\n".join(lines[:f.end_lineno] + [f"{indent}yield"] + lines[f.end_lineno:])
        an, sel = self.run_patch(patch(rel, text, new))
        self.assertTrue(sel >= entered, (f.name, len(entered - sel)))

    # ---- shared state -------------------------------------------------------------
    def test_test_file_that_writes_to_an_imported_module_reaches_every_test(self):
        rel = "tests/test_time.py"
        text = read(rel)
        _, sel = self.run_patch(patch(rel, text, text + "\nimport sqlglot  # noqa: E402\n\nsqlglot.pretty = True\n"))
        self.assertEqual(len(sel), self.maps.n)
        # inside one test: every test that runs after it in the same process
        first = next(n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))
        lines = text.split("\n")
        indent = " " * first.body[0].col_offset
        new = "\n".join(lines[:first.body[0].lineno - 1] + [f"{indent}Dialect.NULL_ORDERING = 'nulls_are_large'"] + lines[first.body[0].lineno - 1:])
        self.assertIn("Dialect", {a.asname or a.name for s in ast.parse(text).body if isinstance(s, ast.ImportFrom) for a in s.names})
        _, sel = self.run_patch(patch(rel, text, new))
        self.assertEqual(len(sel), self.maps.n)
        # an ordinary statement added to the same test stays with the tests of the file
        new = "\n".join(lines[:first.body[0].lineno - 1] + [f"{indent}self.assertTrue(True)"] + lines[first.body[0].lineno - 1:])
        _, sel = self.run_patch(patch(rel, text, new))
        self.assertTrue(sel <= self.files_tests(lambda f: f == rel))

    def test_new_test_file_that_writes_shared_state_reaches_every_test(self):
        _, sel = self.run_patch(new_file("tests/test_zz_state.py", "import sqlglot\n\nsqlglot.pretty = True\n\n\ndef test_x():\n    assert True\n"))
        self.assertEqual(len(sel), self.maps.n)
        _, sel = self.run_patch(new_file("tests/test_zz_state.py", "import sqlglot\n\n\ndef test_x():\n    sqlglot.pretty = True\n"))
        self.assertEqual(len(sel), self.maps.n)
        _, sel = self.run_patch(new_file("tests/test_zz_plain.py", "import sqlglot\n\n\ndef test_x():\n    assert sqlglot.parse_one('1')\n"))
        self.assertEqual(sel, set())

    def test_new_write_to_shared_state_in_library_code_reaches_every_test(self):
        rel, text, f, _name, entered = self.function_with_dead_tail()
        lines = text.split("\n")
        first = f.body[1] if isinstance(f.body[0], ast.Expr) and len(f.body) > 1 else f.body[0]
        indent = " " * first.col_offset
        new = "\n".join(lines[:first.lineno - 1] + [f"{indent}__import__('sqlglot').pretty = True"] + lines[first.lineno - 1:])
        _, sel = self.run_patch(patch(rel, text, new))
        self.assertEqual(len(sel), self.maps.n)

    # ---- files the import system or pytest pick up by name ------------------------------
    def binary_new_file(self, rel):
        return (f"diff --git a/{rel} b/{rel}\nnew file mode 100644\nindex 0000000..1111111\n"
                f"Binary files /dev/null and b/{rel} differ\n").encode()

    def test_extension_module_shadows_the_python_module_next_to_it(self):
        _, sel = self.run_patch(self.binary_new_file("sqlglot/time.abi3.so"))
        self.assertTrue(sel >= set(bits(self.maps.module_tests("sqlglot.time"))))
        self.assertGreater(len(sel), 1000)
        _, sel = self.run_patch(self.binary_new_file("sqlglot/dialects/prql.cpython-313-x86_64-linux-gnu.so"))
        self.assertTrue(sel >= self.files_tests(lambda f: f == "tests/dialects/test_prql.py"))
        self.assertTrue(sel >= set(bits(self.maps.module_tests("sqlglot.dialects.prql"))))
        self.assertLess(len(sel), 200)
        _, sel = self.run_patch(self.binary_new_file("tests/helpers.so"))
        self.assertTrue(sel >= self.files_tests(lambda f: f == "tests/test_optimizer.py"))
        # a name nothing imports, and a data file that merely ends in .so somewhere unrelated
        _, sel = self.run_patch(self.binary_new_file("sqlglot/zz_unrelated.so"))
        self.assertLess(len(sel), 10)
        _, sel = self.run_patch(self.binary_new_file("csv.so"))
        self.assertEqual(len(sel), self.maps.n)       # shadows the standard library from PYTHONPATH

    def test_pytest_9_configuration_files_reach_every_test(self):
        for rel, text in (("pytest.toml", '[pytest]\npython_classes = ["Check*"]\n'), (".pytest.toml", "[pytest]\nxfail_strict = true\n"),
                          ("tests/pytest.ini", "[pytest]\npython_functions = check_*\n"), ("tests/pytest.ini", ""),
                          ("pytest.toml", "not toml at all [\n")):
            _, sel = self.run_patch(new_file(rel, text))
            self.assertEqual(len(sel), self.maps.n, rel)
        _, sel = self.run_patch(new_file("tests/fixtures/pytest.ini", "[pytest]\npython_functions = check_*\n"))
        self.assertEqual(sel, set())

    def test_byte_order_mark_in_a_file_a_test_parses_as_text(self):
        # tests/test_typing.py reads every sqlglot/typing/*.py with read_text() and ast.parse()
        rel = "sqlglot/typing/duckdb.py"
        readers = set(bits(self.maps.text_reader_tests(rel)))
        self.assertTrue(readers)
        text = read(rel)
        _, sel = self.run_patch(patch(rel, text, "\ufeff" + text))
        self.assertTrue(sel >= readers)
        _, sel = self.run_patch(patch(rel, text, "# a comment\n" + text))
        self.assertEqual(sel, set())
        other = read("sqlglot/serde.py")
        _, sel = self.run_patch(patch("sqlglot/serde.py", other, "\ufeff" + other))
        self.assertEqual(sel, set())


if __name__ == "__main__":
    unittest.main()

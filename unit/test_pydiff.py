import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blast.diffparse import Applied, split_lines  # noqa: E402
from blast.pydiff import diff_python  # noqa: E402

import difflib  # noqa: E402


def applied(old: str, new: str) -> Applied:
    """What apply_hunks would return, built from the two texts."""
    a, nl_a = split_lines(old)
    b, nl_b = split_lines(new)
    new_to_old = [0] * len(b)
    deleted, insert_after = set(), set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for d in range(i2 - i1):
                new_to_old[j1 + d] = i1 + d + 1
        else:
            deleted.update(range(i1 + 1, i2 + 1))
            if j2 > j1:
                insert_after.add(i2)
    return Applied(a, b, new_to_old, deleted, insert_after, nl_b, 0)


def diff(old: str, new: str):
    old, new = textwrap.dedent(old), textwrap.dedent(new)
    return diff_python(old, applied(old, new))


BASE = '''\
    import os
    from helper import seq_get

    LIMIT = 10


    class Thing(Base):
        FLAG = True
        TABLE = {
            "a": 1,
            "b": lambda args: seq_get(args, 0),
            **Base.TABLE,
        }

        def method(self, x):
            """Doc."""
            if x > LIMIT:
                return self.other(
                    x,
                    1,
                )
            # trailing
            return x

        def other(self, x, y):
            return x + y


    def helper(value):
        for item in value:
            yield item
'''


def kinds(d):
    return [(m.kind, m.lo, m.hi) for m in d.marks]


class PyDiffTests(unittest.TestCase):
    def test_comment_blank_and_layout_changes_are_the_same_ast(self):
        for new in (
            BASE.replace("LIMIT = 10", "LIMIT = 10  # ten"),
            BASE.replace("    class Thing(Base):", "    # a class\n    class Thing(Base):"),
            BASE.replace("            # trailing\n", ""),
            BASE.replace('"a": 1,', '"a":   1,'),
            BASE.replace("return x + y", "return (x + y)"),
            BASE.replace("            return x\n", "            return x\n\n\n"),
            BASE.replace("        FLAG = True", "        FLAG = True  # type: ignore"),
        ):
            d = diff(BASE, new)
            self.assertTrue(d.same_ast, new)
            self.assertIsNone(d.broken)
            self.assertEqual(d.marks, [])

    def test_comment_inside_a_string_is_a_real_change(self):
        d = diff(BASE, BASE.replace('"""Doc."""', '"""Doc.\n            # not a comment\n            """'))
        self.assertFalse(d.same_ast)
        self.assertEqual(kinds(d), [("call", 16, 16)])

    def test_statement_in_function_is_call_time_with_its_whole_span(self):
        d = diff(BASE, BASE.replace("                    1,\n", "                    2,\n"))
        self.assertEqual(kinds(d), [("call", 18, 21)])     # the multi-line return statement
        d = diff(BASE, BASE.replace("if x > LIMIT:", "if x >= LIMIT:"))
        self.assertEqual(kinds(d), [("call", 17, 17)])     # only the header of the if
        self.assertEqual(d.marks[0].info["why"], "header")

    def test_insertion_and_deletion_inside_function(self):
        d = diff(BASE, BASE.replace("            return x\n", "            x = x + 1\n            return x\n"))
        self.assertEqual({(m.kind, m.lo, m.hi, m.info["why"]) for m in d.marks},
                         {("call", 17, 21, "neighbour"), ("call", 23, 23, "neighbour")})
        d = diff(BASE, BASE.replace("            return x\n", ""))
        self.assertEqual(kinds(d), [("call", 23, 23)])
        self.assertEqual(d.marks[0].info["why"], "deleted")

    def test_class_level_scalar_and_dict_entry(self):
        d = diff(BASE, BASE.replace("FLAG = True", "FLAG = False"))
        (m,) = d.marks
        self.assertEqual((m.kind, m.scope, m.info["targets"], m.info["keys"], m.info["const_only"]),
                         ("value", "Thing", ["FLAG"], None, True))
        d = diff(BASE, BASE.replace('"a": 1,', '"a": 2,'))
        (m,) = d.marks
        self.assertEqual((m.kind, m.scope, m.info["targets"], m.info["const_only"]), ("value", "Thing", ["TABLE"], True))
        self.assertEqual([k.value for k in m.info["keys"]], ["a"])
        d = diff(BASE, BASE.replace('"a": 1,', '"a": 1,\n            "c": 3,'))
        (m,) = d.marks
        self.assertEqual([k.value for k in m.info["keys"]], ["c"])
        self.assertFalse(m.info["const_only"])
        d = diff(BASE, BASE.replace("**Base.TABLE,", "**Other.TABLE,"))
        (m,) = d.marks
        self.assertIsNone(m.info["keys"])

    def test_lambda_body_in_a_class_level_table_is_call_time(self):
        d = diff(BASE, BASE.replace("seq_get(args, 0)", "seq_get(args, 1)"))
        self.assertEqual(kinds(d), [("lambda", 11, 11)])
        d = diff(BASE, BASE.replace("lambda args: seq_get(args, 0)", "lambda args, n=1: seq_get(args, 0)"))
        self.assertEqual([m.kind for m in d.marks], ["value"])   # a default is evaluated at definition time

    def test_module_level_and_imports(self):
        d = diff(BASE, BASE.replace("LIMIT = 10", "LIMIT = 11"))
        (m,) = d.marks
        self.assertEqual((m.kind, m.scope, m.info["targets"]), ("value", "", ["LIMIT"]))
        d = diff(BASE, BASE.replace("from helper import seq_get", "from helper import seq_get2"))
        (m,) = d.marks
        self.assertEqual((m.kind, m.info["removed"], m.info["added"]), ("import", ["seq_get"], ["seq_get2"]))
        d = diff(BASE, BASE.replace("    import os\n", ""))
        (m,) = d.marks
        self.assertEqual((m.kind, m.info["op"], m.info["removed"]), ("import", "delete", ["os"]))

    def test_function_header_removed_added_and_renamed_defs(self):
        d = diff(BASE, BASE.replace("def other(self, x, y):", "def other(self, x, y=2):"))
        (m,) = d.marks
        self.assertEqual((m.kind, m.info["name"], m.info["body"], m.info["annotations_only"]), ("defhdr", "other", (26, 26), False))
        d = diff(BASE, BASE.replace("def other(self, x, y):", "def other(self, x: int, y) -> int:"))
        (m,) = d.marks
        self.assertTrue(m.info["annotations_only"])
        d = diff(BASE, BASE.replace("        def other(self, x, y):\n            return x + y\n", ""))
        (m,) = d.marks
        self.assertEqual((m.kind, m.scope, m.info["name"]), ("defdel", "Thing", "other"))
        d = diff(BASE, BASE + "\n\n    def extra():\n        return 1\n")
        (m,) = d.marks
        self.assertEqual((m.kind, m.scope, m.info["name"]), ("defins", "", "extra"))
        d = diff(BASE, BASE.replace("class Thing(Base):", "class Thing(Other):"))
        self.assertEqual([m.kind for m in d.marks], ["classhdr"])

    def test_indentation_change_moves_a_statement_between_blocks(self):
        # dedenting the comment-adjacent return out of nothing is a syntax matter; dedenting a
        # statement out of the `if` changes who executes it
        new = BASE.replace("            return x\n", "            x = 0\n            return x\n")
        moved = new.replace("            x = 0\n", "                x = 0\n")
        d = diff(new, moved)
        self.assertFalse(d.same_ast)
        self.assertTrue(all(m.kind == "call" for m in d.marks))
        self.assertTrue(any(m.lo <= 17 <= m.hi for m in d.marks))   # reaches the `if` it moved into

    def test_broken_sources(self):
        for new, what in (
            (BASE.replace("def other(self, x, y):", "def other(self, x, y:"), "syntax"),
            (BASE.replace("            return x + y", "        return x + y"), "indentation"),
            (BASE.replace("            return x + y", "\t\t\treturn x + y\n            return 1"), "tabs"),
            (BASE.replace("        FLAG = True", "        FLAG = True\n        return"), "return outside function"),
            (BASE.replace("            yield item", "            break\n        break"), "break outside loop"),
            (BASE.replace("def other(self, x, y):", "def other(self, x, x):"), "duplicate argument"),
            ("# -*- coding: no-such-codec -*-\n" + textwrap.dedent(BASE), "encoding"),
            (BASE.replace('"""Doc."""', '"""Doc.'), "unterminated string"),
        ):
            d = diff(BASE, new) if what != "encoding" else diff_python(textwrap.dedent(BASE), applied(textwrap.dedent(BASE), new))
            self.assertIsNotNone(d.broken, what)

    def test_docstrings_and_pass_inside_functions_execute_nothing(self):
        d = diff(BASE, BASE.replace("        def other(self, x, y):\n", '        def other(self, x, y):\n            """Adds."""\n'))
        self.assertEqual(d.marks, [])
        self.assertFalse(d.same_ast)          # the tree did change ...
        self.assertTrue(d.no_op_changes)      # ... by statements that execute nothing
        d = diff(BASE, BASE.replace('            """Doc."""\n', ""))
        self.assertEqual(d.marks, [])
        d = diff(BASE, BASE.replace("            return x + y", "            pass\n            return x + y"))
        self.assertEqual(d.marks, [])

    def test_moved_definition_is_not_a_removal_plus_an_addition(self):
        method = "        def method(self, x):"
        other = "        def other(self, x, y):\n            return x + y\n"
        moved = BASE.replace("\n\n" + other, "").replace(method, other + "\n" + method)
        d = diff(BASE, moved)
        self.assertEqual([(m.kind, m.info["names"], m.info["is_def"]) for m in d.marks], [("moved", ["other"], True)])
        swapped = BASE.replace("    import os\n    from helper import seq_get\n", "    from helper import seq_get\n    import os\n")
        d = diff(BASE, swapped)
        self.assertEqual([(m.kind, m.info["is_import"]) for m in d.marks], [("moved", True)])
        # inside a function the order of statements is behaviour
        body = BASE.replace("            return x + y", "            a = x\n            b = y\n            return a + b")
        d = diff(body, body.replace("            a = x\n            b = y\n", "            b = y\n            a = x\n"))
        self.assertTrue(d.marks and all(m.kind == "call" for m in d.marks))

    def test_same_bytecode_is_no_change(self):
        src = BASE.replace("            return x + y", "            total = x + y\n            for i in (1, 2):\n                total += i * 60 * 60\n            return total")
        renamed = src.replace("total", "acc").replace(" i ", " k ").replace("+= i", "+= k")
        d = diff(src, renamed)
        self.assertEqual(d.marks, [])
        self.assertEqual(d.equivalent, ["Thing.other"])
        self.assertFalse(d.same_ast)
        self.assertEqual(diff(src, src.replace("(1, 2)", "(1, 1 + 1)")).marks, [])                 # folded by the compiler
        self.assertEqual(diff(src, src.replace("total = x + y", "total: int = x + y")).marks, [])  # annotation of a local
        # not equivalent: (i * 60) * 60 is not i * 3600 for the interpreter, another operator,
        # another constant, swapped operands, a call added
        for changed in (src.replace("i * 60 * 60", "i * 3600"), src.replace("total += i", "total -= i"),
                        src.replace("(1, 2)", "(1, 3)"), src.replace("x + y", "y + x"), src.replace("x + y", "abs(x) + y")):
            self.assertTrue(diff(src, changed).marks, changed)
        d = diff(src, src.replace("def other(self, x, y):", "def other(self, a, y):").replace("total = x + y", "total = a + y"))
        self.assertTrue(d.marks)
        # a function that inspects its locals is never called equivalent
        peek = src.replace("            return total", "            return locals()")
        self.assertTrue(diff(peek, peek.replace("total", "acc")).marks)

    def test_untouched_statements_are_not_compared(self):
        # a 3000-statement module with one changed line: only that statement is dumped
        big = "".join(f"V{i} = {i}\n" for i in range(3000))
        new = big.replace("V1500 = 1500\n", "V1500 = 0\n")
        d = diff_python(big, applied(big, new))
        (m,) = d.marks
        self.assertEqual((m.kind, m.lo, m.info["targets"]), ("value", 1501, ["V1500"]))


if __name__ == "__main__":
    unittest.main()

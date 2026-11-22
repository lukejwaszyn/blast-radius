"""The decision-time runner: reproducing a patch, the working tree, what a run may conclude,
and when blast/verify.py decides to run at all.

The tests that run sqlglot's own tests need the pinned checkout (BLAST_SRC) and are skipped
without it.  They use only test files that import nothing outside the standard library.
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from blast import config as C  # noqa: E402
from blast.analyze import Analysis, Group  # noqa: E402
from blast.runner import Runner, Tree, materialize  # noqa: E402
from blast.verify import Verifier  # noqa: E402

from test_generalization import HAVE_SRC, SRC, edit, new_file, patch, read  # noqa: E402

PINNED = {
    "sqlglot/a.py": "import os\n\n\ndef f(x):\n    return x + 1\n\n\ndef g(x):\n    return x * 2\n",
    "tests/test_a.py": "from sqlglot.a import f\n\n\ndef test_f():\n    assert f(1) == 2\n",
    "tests/fixtures/q.sql": "SELECT 1;\nSELECT 1;\n",
    "README.md": "hello\n",
}


def pinned(rel):
    return PINNED.get(rel)


def diff(rel, a, b):
    old = PINNED[rel]
    assert a in old
    return patch(rel, old, old.replace(a, b, 1))


class MaterializeTests(unittest.TestCase):
    def test_a_plain_edit_gives_the_new_bytes(self):
        files, why = materialize(diff("sqlglot/a.py", "x + 1", "x + 2"), pinned)
        self.assertEqual(why, "")
        self.assertEqual(files, {"sqlglot/a.py": PINNED["sqlglot/a.py"].replace("x + 1", "x + 2").encode()})

    def test_new_deleted_and_renamed_files(self):
        files, _ = materialize(new_file("sqlglot/b.py", "X = 1\n"), pinned)
        self.assertEqual(files, {"sqlglot/b.py": b"X = 1\n"})
        gone = (b"diff --git a/sqlglot/a.py b/sqlglot/a.py\ndeleted file mode 100644\n--- a/sqlglot/a.py\n+++ /dev/null\n"
                b"@@ -1,9 +0,0 @@\n" + "".join("-" + line for line in PINNED["sqlglot/a.py"].splitlines(True)).encode())
        files, _ = materialize(gone, pinned)
        self.assertEqual(files, {"sqlglot/a.py": None})
        moved = b"diff --git a/sqlglot/a.py b/sqlglot/c.py\nsimilarity index 100%\nrename from sqlglot/a.py\nrename to sqlglot/c.py\n"
        files, _ = materialize(moved, pinned)
        self.assertEqual(files, {"sqlglot/a.py": None, "sqlglot/c.py": PINNED["sqlglot/a.py"].encode()})

    def test_what_cannot_be_reproduced_exactly_is_refused(self):
        # a hunk whose header points elsewhere (git apply would search; the runner does not guess)
        shifted = diff("sqlglot/a.py", "x * 2", "x * 3").replace(b"@@ -6,4 +6,4 @@", b"@@ -2,4 +2,4 @@")
        self.assertIn(b"@@ -2,4 +2,4 @@", shifted)
        self.assertIsNone(materialize(shifted, pinned)[0])
        # context that fits only when trailing blanks are ignored
        loose = diff("sqlglot/a.py", "x + 1", "x + 2").replace(b" def f(x):\n", b" def f(x):  \n")
        self.assertIsNone(materialize(loose, pinned)[0])
        # binary content, a mode change, a file outside sqlglot/ and tests/, an unreadable patch
        binary = b"diff --git a/sqlglot/a.py b/sqlglot/a.py\nindex 1..2 100644\nBinary files a/sqlglot/a.py and b/sqlglot/a.py differ\n"
        self.assertIsNone(materialize(binary, pinned)[0])
        mode = b"diff --git a/sqlglot/a.py b/sqlglot/a.py\nold mode 100644\nnew mode 100755\n"
        self.assertIsNone(materialize(mode, pinned)[0])
        self.assertIsNone(materialize(diff("README.md", "hello", "bye"), pinned)[0])
        self.assertIsNone(materialize(b"not a patch at all\n", pinned)[0])
        # a new file that the pinned commit already has
        self.assertIsNone(materialize(new_file("sqlglot/a.py", "X = 1\n"), pinned)[0])

    def test_the_final_newline_is_kept_as_the_patch_says(self):
        data = (b"--- a/tests/fixtures/q.sql\n+++ b/tests/fixtures/q.sql\n@@ -1,2 +1,2 @@\n SELECT 1;\n-SELECT 1;\n+SELECT 2;\n"
                b"\\ No newline at end of file\n")
        files, _ = materialize(data, pinned)
        self.assertEqual(files, {"tests/fixtures/q.sql": b"SELECT 1;\nSELECT 2;"})


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        for rel, text in PINNED.items():
            full = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(text)
            os.utime(full, (1_600_000_000, 1_600_000_000))
        os.mkdir(os.path.join(self.src, "sqlglot", "__pycache__"))
        with open(os.path.join(self.src, "sqlglot", "__pycache__", "a.cpython-313.pyc"), "wb") as fh:
            fh.write(b"compiled")
        self.tree = Tree(self.src, self.tmp)
        self.tree.build()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snapshot(self):
        out = {}
        for base, dirs, names in os.walk(self.tree.root):
            for name in names + dirs:
                full = os.path.join(base, name)
                st = os.lstat(full)
                content = None
                if not os.path.isdir(full):
                    with open(full, "rb") as fh:
                        content = fh.read()
                out[os.path.relpath(full, self.tree.root)] = (os.path.islink(full), content,
                                                              None if os.path.isdir(full) else st.st_mtime_ns)
        return out

    def test_the_copy_has_real_files_with_the_pinned_time_stamps(self):
        a = os.path.join(self.tree.root, "sqlglot", "a.py")
        self.assertFalse(os.path.islink(a))
        self.assertFalse(os.path.islink(os.path.join(self.tree.root, "tests", "fixtures")))
        self.assertEqual(os.stat(a).st_mtime, 1_600_000_000)                      # the compiled file stays valid
        self.assertTrue(os.path.islink(os.path.join(self.tree.root, "sqlglot", "__pycache__")))
        self.assertTrue(os.path.islink(os.path.join(self.tree.root, "README.md")))  # outside sqlglot/ and tests/: never patched

    def test_apply_then_restore_gives_back_the_pinned_tree_exactly(self):
        before = self.snapshot()
        undo = self.tree.apply({"sqlglot/a.py": b"X = 1\n", "tests/test_a.py": None, "sqlglot/new/deep/m.py": b"Y = 2\n",
                                "tests/fixtures/q.sql": b"SELECT 3;\n"})
        root = self.tree.root
        with open(os.path.join(root, "sqlglot", "a.py"), "rb") as fh:
            self.assertEqual(fh.read(), b"X = 1\n")
        self.assertNotEqual(os.stat(os.path.join(root, "sqlglot", "a.py")).st_mtime, 1_600_000_000)  # stale .pyc is ignored
        self.assertFalse(os.path.exists(os.path.join(root, "tests", "test_a.py")))
        self.assertTrue(os.path.isfile(os.path.join(root, "sqlglot", "new", "deep", "m.py")))
        self.tree.restore(undo)
        self.assertEqual(self.snapshot(), before)
        # the pinned checkout itself was never written to
        with open(os.path.join(self.src, "sqlglot", "a.py")) as fh:
            self.assertEqual(fh.read(), PINNED["sqlglot/a.py"])


class FakeMaps:
    """Six tests in two files; the runner map bans number 5."""

    ids = [f"tests/dialects/test_x.py::TestX::test_{i}" for i in range(4)] + [
        "tests/test_executor.py::TestExecutor::test_a", "tests/test_optimizer.py::TestOptimizer::test_pool"]

    def load(self, name):
        assert name == "runner"
        return {"not_alone": [], "not_run_long": [], "starts_process": [5], "preload": ["re"], "job_overhead_s": 0.1,
                "class_setup_s": {"tests/test_executor.py::TestExecutor": 1.4},
                "external": {"tests/test_executor.py": ["numpy", "pandas", "duckdb"], "tests/dialects/test_x.py": []}}


class PolicyTests(unittest.TestCase):
    """When a run is planned.  The numbers are the scoring rule's: a worker costs 2 s."""

    def setUp(self):
        self.ms = [10, 20, 30, 3000, 50, 400]
        self.verifier = Verifier(FakeMaps(), self.ms, 0b111111, pinned, lambda *a: None)
        self.data = diff("sqlglot/a.py", "x + 1", "x + 2")

    def plan(self, mask, p=C.P_LINE_HIT, data=None, shared=None, groups=None):
        an = Analysis("c")
        an.data = self.data if data is None else data
        if shared:
            an.shared.append(shared)
        return self.verifier.plan(an, mask, groups or [Group(p, mask, "why")])

    def test_a_few_quick_candidates_that_may_all_pass_are_run(self):
        plan = self.plan(0b0111)
        self.assertEqual(plan.why, "")
        self.assertEqual([n for n, _id, _limit in plan.tests], [0, 1, 2])
        self.assertAlmostEqual(plan.p_none, 1 - C.P_LINE_HIT)
        # expected: P(nothing fails) x (2 s worker + 0.06 s load) minus the run
        self.assertGreater(plan.gain, C.VERIFY_MARGIN_S)
        self.assertLess(plan.gain, plan.p_none * 2.06 + 0.1)
        for _n, _id, limit in plan.tests:
            self.assertGreaterEqual(limit, C.VERIFY_TEST_LIMIT_S)

    def test_candidates_that_almost_surely_fail_are_not_run(self):
        self.assertIn("does not exceed", self.plan(0b0111, p=C.P_FIXTURE_CONSUMER).why)
        self.assertIn("does not exceed", self.plan(0b0111, p=C.P_MODULE_BROKEN).why)

    def test_a_long_candidate_costs_more_cpu_than_the_worker_it_might_save(self):
        self.assertNotEqual(self.plan(0b1111).why, "")

    def test_state_shared_between_tests_keeps_the_static_selection(self):
        self.assertIn("shared", self.plan(0b0111, shared="a write to state shared by the whole process").why)

    def test_a_test_the_runner_does_not_judge_keeps_the_whole_commit_static(self):
        # dropping the others would not close the worker: nothing to gain
        self.assertIn("does not judge", self.plan(0b100011).why)

    def test_patches_that_change_what_pytest_itself_does_are_not_run(self):
        for path, text in (("tests/conftest.py", "import pytest\n"), ("tests/test_new.py", "def test_x():\n    assert 0\n"),
                           ("sqlglot/time.abi3.so", "x\n"), ("tests/pytest.ini", "[pytest]\naddopts = -x\n")):
            self.assertNotEqual(self.plan(0b0111, data=new_file(path, text)).why, "", path)
        self.assertIn("not reproducible", self.plan(0b0111, data=diff("README.md", "hello", "bye")).why)

    def test_class_setup_and_heavy_imports_are_priced(self):
        plan = self.plan(0b10000)
        self.assertTrue(plan.heavy)
        self.assertGreater(plan.cpu, 1.4 * C.VERIFY_MACHINE_FACTOR)   # TestExecutor's setUpClass
        self.assertNotEqual(plan.why, "")

    def test_passing_tests_are_dropped_only_when_that_closes_a_worker(self):
        class FakeRunner:
            def __init__(self, status):
                self.status = status

            def run(self, files, tests, **kwargs):
                return {"status": self.status, "complete": True, "void": None, "cpu": 0.1}

            def close(self):
                pass

        plan = self.plan(0b0111)
        self.verifier.runner = FakeRunner({"0": "pass", "1": "pass", "2": "pass"})
        self.assertEqual(self.verifier.run(plan, 0.0), 0)                       # nothing left: 2.06 s saved
        self.verifier.runner = FakeRunner({"0": "pass", "1": "fail", "2": "pass"})
        self.assertEqual(self.verifier.run(plan, 0.0), 0b0111)                  # the worker stays: 0.04 s is not worth a bet
        self.verifier.runner = FakeRunner({"0": "pass", "1": "pass"})           # no answer for test 2: it is kept
        self.assertEqual(self.verifier.run(plan, 0.0), 0b0111)
        answer = {"status": {"0": "pass", "1": "pass", "2": "pass"}, "complete": True, "void": "error outside a test", "cpu": 0.1}
        self.verifier.runner = FakeRunner(None)
        self.verifier.runner.run = lambda *a, **k: answer
        self.assertEqual(self.verifier.run(plan, 0.0), 0b0111)                  # a void job proves nothing
        self.verifier.runner = None

    def test_one_off_costs_are_paid_only_when_the_batch_repays_them(self):
        light = [self.plan(0b0001), self.plan(0b0010)]
        self.assertEqual(self.verifier.schedule(light), sorted(light, key=lambda p: -p.gain))
        lone = self.plan(0b0001)
        lone.gain = 0.2          # less than starting the runner costs
        self.assertEqual(self.verifier.schedule([lone]), [])
        self.assertIn("starting the runner", lone.why)


@unittest.skipUnless(HAVE_SRC, "needs the pinned sqlglot checkout (BLAST_SRC)")
class RunTests(unittest.TestCase):
    """What a run reports, on sqlglot's own tests/test_time.py."""

    FILE = "tests/test_time.py"
    TESTS = ["test_canonical_time_format_vocabulary", "test_format_time", "test_subsecond_precision"]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.runner = Runner(src=SRC, tmp=cls.tmp, python=sys.executable)

    @classmethod
    def tearDownClass(cls):
        cls.runner.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_patch(self, data, limit=20.0, names=None):
        files = {}
        if data is not None:
            files, why = materialize(data, lambda rel: read(rel) if os.path.isfile(os.path.join(SRC, rel)) else None)
            self.assertIsNotNone(files, why)
        tests = [(i, f"{self.FILE}::TestTime::{name}", limit) for i, name in enumerate(names or self.TESTS)]
        answer = self.runner.run(files, tests, start_limit=60.0)
        return answer, [answer["status"].get(str(i)) for i, _id, _l in tests]

    def test_the_pinned_tree_passes_and_stays_pinned(self):
        answer, verdict = self.run_patch(None)
        self.assertTrue(answer["complete"])
        self.assertIsNone(answer["void"])
        self.assertEqual(verdict, ["pass", "pass", "pass"])

    def test_a_broken_function_fails_only_the_test_that_checks_it(self):
        answer, verdict = self.run_patch(edit("sqlglot/time.py", "    if not string:\n        return None\n",
                                              "    if not string:\n        return ''\n"))
        self.assertTrue(answer["complete"])
        self.assertEqual(verdict, ["pass", "fail", "pass"])
        # the next job sees the pinned file again
        self.assertEqual(self.run_patch(None)[1], ["pass", "pass", "pass"])

    def test_a_failing_subtest_fails_its_test(self):
        data = edit(self.FILE, '        self.assertEqual(format_time("", {}), None)\n',
                    '        self.assertEqual(format_time("", {}), None)\n        for i in range(3):\n'
                    '            with self.subTest(i=i):\n                self.assertNotEqual(i, 1)\n')
        self.assertEqual(self.run_patch(data)[1], ["pass", "fail", "pass"])

    def test_a_module_that_no_longer_imports_fails_every_test_of_the_file(self):
        answer, verdict = self.run_patch(edit("sqlglot/time.py", "import datetime\n", "import datetime\nraise RuntimeError('x')\n"))
        self.assertEqual(verdict, ["fail", "fail", "fail"])

    def test_a_hang_gives_no_verdict(self):
        answer, verdict = self.run_patch(edit("sqlglot/time.py", "    if not string:\n        return None\n",
                                              "    while not string:\n        pass\n"), limit=1.0)
        self.assertFalse(answer["complete"] and not answer["void"])
        self.assertEqual(verdict[1], "timeout")
        self.assertNotIn("pass", verdict[1:])       # nothing after the hang is reported at all
        self.assertEqual(self.run_patch(None)[1], ["pass", "pass", "pass"])

    def test_an_error_outside_any_test_voids_the_job(self):
        data = edit(self.FILE, "class TestTime(unittest.TestCase):\n",
                    "class TestTime(unittest.TestCase):\n    @classmethod\n    def tearDownClass(cls):\n        raise RuntimeError('late')\n\n")
        answer, verdict = self.run_patch(data)
        # every test passed, yet pytest reports the error on the last test of the class
        self.assertEqual(verdict, ["pass", "pass", "pass"])
        self.assertIsNotNone(answer["void"])

    def test_a_test_that_is_gone_or_skipped_is_not_called_passing(self):
        gone = edit(self.FILE, "    def test_format_time(self):\n", "    def test_format_time_renamed(self):\n")
        self.assertEqual(self.run_patch(gone)[1], ["pass", "odd", "pass"])
        skipped = edit(self.FILE, "    def test_format_time(self):\n", "    @unittest.skip('x')\n    def test_format_time(self):\n")
        self.assertEqual(self.run_patch(skipped)[1], ["pass", "skip", "pass"])

    def test_state_left_by_an_earlier_candidate_is_seen_by_a_later_one(self):
        # tests of one job share a process and run in pytest's order, as in the labeling run
        old = read(self.FILE)
        new = old.replace('        self.assertEqual(format_time("", {}), None)\n',
                          '        self.assertEqual(format_time("", {}), None)\n        self.assertFalse(getattr(sys, "_blast_mark", False))\n', 1)
        new = new.replace("    def test_canonical_time_format_vocabulary(self):\n",
                          "    def test_canonical_time_format_vocabulary(self):\n        sys._blast_mark = True\n", 1)
        both = patch(self.FILE, old, new)
        self.assertEqual(self.run_patch(both)[1], ["pass", "fail", "pass"])
        self.assertEqual(self.run_patch(both, names=["test_format_time"])[1], ["pass"])   # alone it passes


if __name__ == "__main__":
    unittest.main()

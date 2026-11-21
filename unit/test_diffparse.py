import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blast.diffparse import ApplyError, apply_hunks, parse_patch  # noqa: E402

OLD = "".join(f"line {i}\n" for i in range(1, 21))


def patch_of(body: str, path: str = "pkg/mod.py") -> str:
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n{body}"


class ParseTests(unittest.TestCase):
    def test_single_replacement(self):
        p = parse_patch(patch_of("@@ -4,3 +4,3 @@ ctx\n line 4\n-line 5\n+LINE 5\n line 6\n"))
        self.assertEqual(len(p.files), 1)
        fc = p.files[0]
        self.assertEqual((fc.old_path, fc.new_path, fc.path), ("pkg/mod.py",) * 3)
        self.assertFalse(fc.new_file or fc.deleted or fc.renamed or fc.binary)
        ap = apply_hunks(OLD, fc.hunks)
        self.assertEqual(ap.deleted, {5})
        self.assertEqual(ap.new_lines[4], "LINE 5")
        self.assertEqual(ap.new_to_old[3:6], [4, 0, 6])
        self.assertEqual(ap.changed_old_ranges(), [(5, 5)])
        self.assertTrue(ap.old_touched(5, 5))
        self.assertFalse(ap.old_touched(6, 20))
        self.assertTrue(ap.new_touched(5, 5))
        self.assertFalse(ap.new_touched(6, 20))

    def test_pure_insertion_marks_neighbours(self):
        p = parse_patch(patch_of("@@ -4,2 +4,3 @@\n line 4\n+added\n line 5\n"))
        ap = apply_hunks(OLD, p.files[0].hunks)
        self.assertEqual(ap.deleted, set())
        self.assertEqual(ap.insert_after, {4})
        self.assertEqual(ap.changed_old_ranges(), [(4, 5)])
        self.assertTrue(ap.old_touched(4, 5))      # a statement spanning the insertion point
        self.assertFalse(ap.old_touched(1, 4))     # one ending right before it
        self.assertFalse(ap.old_touched(5, 9))     # one starting right after it
        self.assertEqual(len(ap.new_lines), 21)

    def test_insert_at_start_and_end(self):
        p = parse_patch(patch_of("@@ -1,2 +1,3 @@\n+first\n line 1\n line 2\n@@ -19,2 +20,3 @@\n line 19\n line 20\n+last\n"))
        ap = apply_hunks(OLD, p.files[0].hunks)
        self.assertEqual(ap.new_lines[0], "first")
        self.assertEqual(ap.new_lines[-1], "last")
        self.assertEqual(ap.insert_after, {0, 20})
        self.assertEqual(ap.changed_old_ranges(), [(1, 1), (20, 20)])

    def test_zero_context_and_omitted_counts(self):
        p = parse_patch("--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -5 +5 @@\n-line 5\n+five\n@@ -9,0 +10,2 @@\n+x\n+y\n@@ -12,2 +14,0 @@\n-line 12\n-line 13\n")
        fc = p.files[0]
        self.assertEqual(fc.path, "pkg/mod.py")
        self.assertEqual([(h.old_start, h.old_count, h.new_start, h.new_count) for h in fc.hunks],
                         [(5, 1, 5, 1), (9, 0, 10, 2), (12, 2, 14, 0)])
        ap = apply_hunks(OLD, fc.hunks)
        self.assertEqual(ap.new_lines[4], "five")
        self.assertEqual(ap.new_lines[9:11], ["x", "y"])
        self.assertEqual(ap.deleted, {5, 12, 13})
        self.assertEqual(ap.insert_after, {5, 9})
        self.assertNotIn("line 12", ap.new_lines)

    def test_new_deleted_renamed_mode_binary(self):
        text = (
            "diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n"
            "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\nindex 1111111..0000000\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a = 1\n-b = 2\n"
            "diff --git a/old name.py b/new name.py\nsimilarity index 90%\nrename from old name.py\nrename to new name.py\n--- a/old name.py\n+++ b/new name.py\n@@ -1 +1 @@\n-x\n+y\n"
            "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
            "diff --git a/data.gz b/data.gz\nindex 1111111..2222222 100644\nGIT binary patch\nliteral 5\nMcmZQzU|?im00961\n\nliteral 0\nHcmV?d00001\n\n"
            "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
        )
        p = parse_patch(text)
        self.assertEqual(len(p.files), 6)
        new, gone, ren, mode, binary, binary2 = p.files
        self.assertTrue(new.new_file and new.path == "new.py" and new.old_path is None)
        self.assertTrue(gone.deleted and gone.path == "gone.py" and gone.new_path is None)
        self.assertTrue(ren.renamed and (ren.old_path, ren.new_path) == ("old name.py", "new name.py"))
        self.assertEqual(len(ren.hunks), 1)
        self.assertTrue(mode.mode_change and not mode.content_changed and mode.path == "run.sh")
        self.assertTrue(binary.binary and binary.path == "data.gz" and not binary.hunks)
        self.assertTrue(binary2.binary and binary2.path == "img.png")

    def test_no_newline_markers(self):
        p = parse_patch(patch_of("@@ -19,2 +19,2 @@\n line 19\n-line 20\n+line twenty\n\\ No newline at end of file\n"))
        hunk = p.files[0].hunks[0]
        self.assertTrue(hunk.new_no_nl and not hunk.old_no_nl)
        ap = apply_hunks(OLD, [hunk])
        self.assertEqual(ap.new_text()[-11:], "line twenty")
        p = parse_patch(patch_of("@@ -2,2 +2,2 @@\n line 2\n-line 3\n\\ No newline at end of file\n+line 3\n"))
        hunk = p.files[0].hunks[0]
        self.assertTrue(hunk.old_no_nl and not hunk.new_no_nl)
        ap = apply_hunks("line 1\nline 2\nline 3", [hunk])
        self.assertEqual(ap.new_text(), "line 1\nline 2\nline 3\n")
        self.assertEqual(ap.new_lines, ap.old_lines)

    def test_removed_line_that_looks_like_a_header(self):
        # a removed SQL comment "-- x" is written "--- x"; an added "++ y" is "+++ y"
        old = "a\n-- x\n++ y\nb\n"
        p = parse_patch("--- a/f.sql\n+++ b/f.sql\n@@ -1,4 +1,4 @@\n a\n--- x\n+++ z\n ++ y\n b\n")
        self.assertEqual(len(p.files), 1)
        ap = apply_hunks(old, p.files[0].hunks)
        self.assertEqual(ap.new_lines, ["a", "++ z", "++ y", "b"])

    def test_crlf_patch_and_stripped_blank_context(self):
        body = "@@ -4,3 +4,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n"
        p = parse_patch(patch_of(body).replace("\n", "\r\n"))
        self.assertEqual(p.files[0].path, "pkg/mod.py")
        self.assertEqual(apply_hunks(OLD, p.files[0].hunks).new_lines[4], "LINE 5")
        old = "a\n\nb\n"
        p = parse_patch("--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n")   # the blank context line lost its space
        self.assertEqual(apply_hunks(old, p.files[0].hunks).new_lines, ["a", "", "c"])

    def test_crlf_file_content_is_kept(self):
        old = "a\r\nb\r\nc\r\n"
        p = parse_patch("--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\r\n-b\r\n+B\r\n c\r\n")
        self.assertEqual(apply_hunks(old, p.files[0].hunks).new_text(), "a\r\nB\r\nc\r\n")

    def test_quoted_path_and_timestamps(self):
        p = parse_patch('diff --git "a/t\\303\\244st dir/x.py" "b/t\\303\\244st dir/x.py"\n--- "a/t\\303\\244st dir/x.py"\n+++ "b/t\\303\\244st dir/x.py"\n@@ -1 +1 @@\n-a\n+b\n')
        self.assertEqual(p.files[0].path, "täst dir/x.py")
        p = parse_patch("--- sqlglot/x.py\t2024-01-01 00:00:00.000000000 +0000\n+++ sqlglot/x.py\t2024-01-02 00:00:00.000000000 +0000\n@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(p.files[0].path, "sqlglot/x.py")

    def test_several_files_and_noise_between(self):
        text = ("From 1234 Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n---\n f.py | 2 +-\n\n"
                + patch_of("@@ -1,2 +1,2 @@\n-line 1\n+LINE 1\n line 2\n", "a.py")
                + patch_of("@@ -3,1 +3,1 @@\n-line 3\n+LINE 3\n", "b.py") + "-- \n2.40.0\n")
        p = parse_patch(text)
        self.assertEqual([f.path for f in p.files], ["a.py", "b.py"])
        self.assertEqual([len(f.hunks) for f in p.files], [1, 1])

    def test_fuzzy_position_and_mismatch(self):
        p = parse_patch(patch_of("@@ -8,3 +8,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n"))   # stated 4 lines too low
        ap = apply_hunks(OLD, p.files[0].hunks)
        self.assertEqual(ap.deleted, {5})
        self.assertEqual(ap.fuzz, 4)
        p = parse_patch(patch_of("@@ -4,3 +4,3 @@\n line 4\n-something else\n+LINE 5\n line 6\n"))
        with self.assertRaises(ApplyError):
            apply_hunks(OLD, p.files[0].hunks)

    def test_garbage_and_truncation_do_not_raise(self):
        self.assertEqual(parse_patch("").files, [])
        p = parse_patch("hello\nworld\n")
        self.assertEqual(p.files, [])
        self.assertTrue(p.problems)
        p = parse_patch(patch_of("@@ -4,3 +4,3 @@\n line 4\n-line 5\n"))  # cut short
        self.assertFalse(p.files[0].hunks[0].complete)
        p = parse_patch(b"\xff\xfe\x00garbage\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-\xff\n+\xfe\n")
        self.assertEqual(p.files[0].path, "x")


if __name__ == "__main__":
    unittest.main()

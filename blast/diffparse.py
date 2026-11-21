"""A tolerant reader of unified diffs (git style or plain) and an in-memory
applier.

Handles: several files, new / deleted / renamed / copied files, mode-only
changes, binary patches (`Binary files ... differ`, `GIT binary patch`),
`\\ No newline at end of file`, hunks at the first and last line, zero-length
ranges (`-12,0`), omitted counts (`@@ -3 +3 @@`), quoted paths with C escapes,
CRLF patches, blank context lines whose single space was stripped, and
garbage between files (mail headers, `index` lines, commit messages).
Nothing here raises on malformed input: what cannot be read is reported in
`FileChange.problems` / `Patch.problems` and the caller falls back.
"""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OCTAL = re.compile(rb"\\([0-7]{1,3})")
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


class Hunk:
    __slots__ = ("old_start", "old_count", "new_start", "new_count", "lines", "old_no_nl", "new_no_nl", "complete")

    def __init__(self, old_start, old_count, new_start, new_count):
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.lines: list[tuple[str, str]] = []  # (" " | "-" | "+", text without the marker)
        self.old_no_nl = False
        self.new_no_nl = False
        self.complete = True

    @property
    def old_first(self) -> int:
        """1-based old line the first line of the hunk body refers to."""
        return self.old_start + 1 if self.old_count == 0 else self.old_start


class FileChange:
    __slots__ = ("old_path", "new_path", "new_file", "deleted", "renamed", "copied", "mode_change",
                 "binary", "hunks", "problems", "old_mode", "new_mode")

    def __init__(self):
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.new_file = False
        self.deleted = False
        self.renamed = False
        self.copied = False
        self.mode_change = False
        self.binary = False
        self.hunks: list[Hunk] = []
        self.problems: list[str] = []
        self.old_mode: str | None = None
        self.new_mode: str | None = None

    @property
    def path(self) -> str | None:
        """The path this change is about in the pinned tree (the new path for a new file)."""
        if self.new_file and not self.old_path:
            return self.new_path
        return self.old_path or self.new_path

    @property
    def content_changed(self) -> bool:
        return bool(self.hunks) or self.binary

    def __repr__(self):
        flags = [f for f in ("new_file", "deleted", "renamed", "copied", "mode_change", "binary") if getattr(self, f)]
        return f"<FileChange {self.old_path!r} -> {self.new_path!r} {' '.join(flags)} hunks={len(self.hunks)}>"


class Patch:
    def __init__(self):
        self.files: list[FileChange] = []
        self.problems: list[str] = []


def _unquote(text: str) -> str:
    """Undo git's C-style quoting of a path ("a/t\\303\\244st.py")."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body = text[1:-1]
    out = bytearray()
    i = 0
    raw = body.encode("utf-8", "surrogateescape")
    while i < len(raw):
        ch = raw[i:i + 1]
        if ch == b"\\" and i + 1 < len(raw):
            m = _OCTAL.match(raw, i)
            if m:
                out.append(int(m.group(1), 8) & 0xFF)
                i = m.end()
                continue
            nxt = chr(raw[i + 1])
            out.extend(_SIMPLE.get(nxt, nxt).encode())
            i += 2
            continue
        out.extend(ch)
        i += 1
    return out.decode("utf-8", "surrogateescape")


def _clean_path(text: str, strip_prefix: bool) -> str | None:
    """Path from a `---` / `+++` line: drop a trailing timestamp, quotes, and the a/ b/ prefix."""
    text = text.split("\t")[0]
    text = _unquote(text.strip()) if text.strip().startswith('"') else text.rstrip()
    if text == "/dev/null":
        return None
    if strip_prefix and len(text) > 2 and text[1] == "/" and text[0] in "abciwo":
        text = text[2:]
    while text.startswith("./"):
        text = text[2:]
    return text


def _split_git_header(rest: str) -> tuple[str | None, str | None]:
    """`a/x b/y` from a `diff --git` line (paths may be quoted or hold spaces)."""
    rest = rest.strip()
    if rest.startswith('"'):
        end = 1
        while end < len(rest) and (rest[end] != '"' or rest[end - 1] == "\\"):
            end += 1
        a, b = rest[:end + 1], rest[end + 1:].strip()
        return _clean_path(a, True), _clean_path(b, True)
    # unquoted: "a/P b/P" - when both sides are the same path the split is in the middle
    mid = len(rest) // 2
    if rest[mid:mid + 1] == " " and rest[:mid][2:] == rest[mid + 1:][2:]:
        return _clean_path(rest[:mid], True), _clean_path(rest[mid + 1:], True)
    idx = rest.find(" b/")
    if idx > 0:
        return _clean_path(rest[:idx], True), _clean_path(rest[idx + 1:], True)
    parts = rest.split(" ")
    if len(parts) == 2:
        return _clean_path(parts[0], True), _clean_path(parts[1], True)
    return None, None


def parse_patch(data: bytes | str) -> Patch:
    if isinstance(data, bytes):
        text = data.decode("utf-8", "surrogateescape")
    else:
        text = data
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    # A patch saved with CRLF line ends: every structural line ends in \r.
    structural = [ln for ln in lines if ln.startswith(("diff --git ", "--- ", "+++ ", "@@ "))]
    if structural and all(ln.endswith("\r") for ln in structural):
        lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]

    patch = Patch()
    cur: FileChange | None = None
    i, n = 0, len(lines)

    def start() -> FileChange:
        fc = FileChange()
        patch.files.append(fc)
        return fc

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = start()
            cur.old_path, cur.new_path = _split_git_header(line[11:])
            i += 1
            continue
        if line.startswith("diff ") and not line.startswith("diff --git"):
            cur = None  # `diff -ruN a b`: the ---/+++ lines that follow name the file
            i += 1
            continue
        if cur is not None and not cur.hunks:
            if line.startswith("old mode "):
                cur.mode_change, cur.old_mode = True, line[9:].strip()
            elif line.startswith("new mode "):
                cur.mode_change, cur.new_mode = True, line[9:].strip()
            elif line.startswith("deleted file mode"):
                cur.deleted = True
            elif line.startswith("new file mode"):
                cur.new_file, cur.new_mode = True, line[13:].strip()
            elif line.startswith("rename from "):
                cur.renamed, cur.old_path = True, _clean_path(line[12:], False)
            elif line.startswith("rename to "):
                cur.renamed, cur.new_path = True, _clean_path(line[10:], False)
            elif line.startswith("copy from "):
                cur.copied, cur.old_path = True, _clean_path(line[10:], False)
            elif line.startswith("copy to "):
                cur.copied, cur.new_path = True, _clean_path(line[8:], False)
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur.binary = True
                if line.startswith("GIT binary patch"):
                    i += 1
                    while i < n and not lines[i].startswith(("diff --git ", "--- ", "@@ ")):
                        i += 1
                    continue
            elif line.startswith("index ") and cur.mode_change is False:
                m = re.match(r"index [0-9a-fA-F]+\.\.[0-9a-fA-F]+ (\d+)", line)
                if m:
                    cur.old_mode = cur.old_mode or m.group(1)
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            old_raw, new_raw = line[4:], lines[i + 1][4:]
            git_style = cur is not None and not cur.hunks and (cur.old_path is not None or cur.new_path is not None
                                                                or cur.new_file or cur.deleted)
            if cur is None or cur.hunks:
                cur = start()
            both_prefixed = (old_raw.lstrip('"')[:2] in ("a/", "i/", "c/", "o/") or old_raw.startswith("/dev/null")) and \
                            (new_raw.lstrip('"')[:2] in ("b/", "w/", "i/") or new_raw.startswith("/dev/null"))
            strip = git_style or both_prefixed
            old_p, new_p = _clean_path(old_raw, strip), _clean_path(new_raw, strip)
            if old_p is None:
                cur.new_file = True
                cur.old_path = None
            elif not cur.renamed and not cur.copied:
                cur.old_path = old_p
            if new_p is None:
                cur.deleted = True
                cur.new_path = None
            else:
                cur.new_path = new_p
            i += 2
            continue
        m = _HUNK.match(line) if line.startswith("@@") else None
        if m and cur is None:
            patch.problems.append(f"hunk before any file header at line {i + 1}")
            cur = start()
        if m:
            hunk = Hunk(int(m.group(1)), 1 if m.group(2) is None else int(m.group(2)),
                        int(m.group(3)), 1 if m.group(4) is None else int(m.group(4)))
            cur.hunks.append(hunk)
            i += 1
            old_seen = new_seen = 0
            last = ""
            while i < n and (old_seen < hunk.old_count or new_seen < hunk.new_count):
                body = lines[i]
                tag = body[:1]
                if tag == " " or body == "":
                    # a blank line is a context line whose single space was stripped
                    hunk.lines.append((" ", body[1:]))
                    old_seen += 1
                    new_seen += 1
                elif tag == "-":
                    hunk.lines.append(("-", body[1:]))
                    old_seen += 1
                elif tag == "+":
                    hunk.lines.append(("+", body[1:]))
                    new_seen += 1
                elif tag == "\\":
                    if last == "-":
                        hunk.old_no_nl = True
                    elif last == "+":
                        hunk.new_no_nl = True
                    else:
                        hunk.old_no_nl = hunk.new_no_nl = True
                    i += 1
                    continue
                else:
                    hunk.complete = False
                    cur.problems.append(f"hunk cut short at line {i + 1}")
                    break
                last = tag if tag in "-+" else " "
                i += 1
            if i < n and lines[i].startswith("\\"):
                if last == "-":
                    hunk.old_no_nl = True
                elif last == "+":
                    hunk.new_no_nl = True
                else:
                    hunk.old_no_nl = hunk.new_no_nl = True
                i += 1
                # "-old\n\ No newline\n+new\n\ No newline": the + lines may follow the marker
            if old_seen < hunk.old_count or new_seen < hunk.new_count:
                if hunk.complete:
                    hunk.complete = False
                    cur.problems.append("hunk shorter than its header says")
            continue
        i += 1

    for fc in patch.files:
        if fc.old_path is None and fc.new_path is None:
            fc.problems.append("no path")
        if fc.old_path and fc.new_path and fc.old_path != fc.new_path and not fc.copied:
            fc.renamed = True
        if fc.old_mode and fc.new_mode and fc.old_mode != fc.new_mode and not fc.new_file:
            fc.mode_change = True
    if not patch.files and text.strip():
        patch.problems.append("no file header found")
    return patch


# --------------------------------------------------------------------- apply


class ApplyError(Exception):
    pass


class Applied:
    """Result of applying one file's hunks to the pinned text.

    old_lines / new_lines: lists of lines without terminators (1-based access
    through the helpers).  `new_to_old[i]` is the old line number of new line
    i+1, or 0 for an added line.  `deleted` is the set of old line numbers that
    are gone or replaced, `insert_after` the old line numbers (0 = before the
    first line) directly after which new lines were added.
    """

    __slots__ = ("old_lines", "new_lines", "new_to_old", "deleted", "insert_after", "new_ends_nl", "fuzz",
                 "_old_del_prefix", "_new_add_prefix", "_ins_sorted")

    def __init__(self, old_lines, new_lines, new_to_old, deleted, insert_after, new_ends_nl, fuzz):
        self.old_lines = old_lines
        self.new_lines = new_lines
        self.new_to_old = new_to_old
        self.deleted = deleted
        self.insert_after = insert_after
        self.new_ends_nl = new_ends_nl
        self.fuzz = fuzz
        self._old_del_prefix = None
        self._new_add_prefix = None
        self._ins_sorted = None

    def new_text(self) -> str:
        return "\n".join(self.new_lines) + ("\n" if self.new_ends_nl and self.new_lines else "")

    def old_touched(self, first: int, last: int) -> bool:
        """Does the patch delete a line of old[first..last] or insert strictly inside it?"""
        if self._old_del_prefix is None:
            pre = [0] * (len(self.old_lines) + 2)
            for k in range(1, len(self.old_lines) + 1):
                pre[k] = pre[k - 1] + (1 if k in self.deleted else 0)
            pre[len(self.old_lines) + 1] = pre[len(self.old_lines)]
            self._old_del_prefix = pre
            self._ins_sorted = sorted(self.insert_after)
        pre = self._old_del_prefix
        hi = min(last, len(self.old_lines))
        lo = max(first, 1)
        if hi >= lo and pre[hi] - pre[lo - 1]:
            return True
        import bisect

        k = bisect.bisect_left(self._ins_sorted, first)
        return k < len(self._ins_sorted) and self._ins_sorted[k] < last

    def new_touched(self, first: int, last: int) -> bool:
        """Does new[first..last] hold an added line, or were old lines removed strictly inside it?"""
        if self._new_add_prefix is None:
            pre = [0] * (len(self.new_lines) + 1)
            for k, o in enumerate(self.new_to_old, 1):
                pre[k] = pre[k - 1] + (0 if o else 1)
            self._new_add_prefix = pre
        pre = self._new_add_prefix
        hi = min(last, len(self.new_lines))
        lo = max(first, 1)
        if hi < lo:
            return True
        if pre[hi] - pre[lo - 1]:
            return True
        return self.new_to_old[hi - 1] - self.new_to_old[lo - 1] != hi - lo

    def changed_old_ranges(self) -> list[tuple[int, int]]:
        """Old line ranges the hunks rewrite; an insertion counts as the two lines around it."""
        marks = set(self.deleted)
        total = len(self.old_lines)
        for a in self.insert_after:
            if a in self.deleted:
                continue  # new text standing in for removed lines: a replacement
            if a >= 1:
                marks.add(a)
            if a + 1 <= total:
                marks.add(a + 1)
        out = []
        for ln in sorted(marks):
            if out and out[-1][1] == ln - 1:
                out[-1] = (out[-1][0], ln)
            else:
                out.append((ln, ln))
        return out


def split_lines(text: str) -> tuple[list[str], bool]:
    """Lines without their \\n, and whether the text ended with one."""
    if text == "":
        return [], True
    ends = text.endswith("\n")
    lines = text.split("\n")
    if ends:
        lines.pop()
    return lines, ends


def _fits(old: list[str], pos: int, want: list[str]) -> int:
    """0 = exact, 1 = equal ignoring trailing \\r / whitespace, -1 = no."""
    if pos < 0 or pos + len(want) > len(old):
        return -1
    loose = 0
    for k, text in enumerate(want):
        have = old[pos + k]
        if have == text:
            continue
        if have.rstrip() == text.rstrip():
            loose = 1
            continue
        return -1
    return loose


def apply_hunks(old_text: str, hunks: list[Hunk], fuzz_lines: int = 300) -> Applied:
    old, old_nl = split_lines(old_text)
    new: list[str] = []
    new_to_old: list[int] = []
    deleted: set[int] = set()
    insert_after: set[int] = set()
    pos = 0  # 0-based index into old of the next line not yet copied
    fuzz = 0
    new_nl = old_nl
    for hunk in sorted(hunks, key=lambda h: (h.old_first, h.new_start)):
        want = [text for tag, text in hunk.lines if tag != "+"]
        at = hunk.old_first - 1
        if hunk.old_count == 0 and not want:
            at = min(max(hunk.old_start, 0), len(old))
        else:
            fit = _fits(old, at, want)
            if fit < 0 or at < pos:
                found = None
                for delta in range(1, fuzz_lines + 1):
                    for cand in (at - delta, at + delta):
                        if cand >= pos and _fits(old, cand, want) >= 0:
                            found = cand
                            break
                    if found is not None:
                        break
                if found is None:
                    raise ApplyError(f"hunk at old line {hunk.old_start} does not match the pinned file")
                fuzz = max(fuzz, abs(found - at))
                at = found
        while pos < at:
            new.append(old[pos])
            new_to_old.append(pos + 1)
            pos += 1
        for tag, text in hunk.lines:
            if tag == " ":
                new.append(old[pos])
                new_to_old.append(pos + 1)
                pos += 1
            elif tag == "-":
                deleted.add(pos + 1)
                pos += 1
            else:
                new.append(text)
                new_to_old.append(0)
                insert_after.add(pos)
        if pos >= len(old) or hunk.new_no_nl or hunk.old_no_nl:
            # the hunk reaches the end of the file: it decides the final newline
            if pos >= len(old):
                new_nl = not hunk.new_no_nl
    while pos < len(old):
        new.append(old[pos])
        new_to_old.append(pos + 1)
        pos += 1
    return Applied(old, new, new_to_old, deleted, insert_after, new_nl, fuzz)

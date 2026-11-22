"""Read access to maps/*.json (formats: maps/README.md).

Test sets are handled as Python ints used as bit masks over the maps' own
test numbering (position in maps/tests.json -> ids); `Maps.numbers(mask)`
translates to the numbering of the tests.json the run was given, by test id.
Every file is loaded on first use, so a batch of fixture edits never pays for
the big coverage tables.
"""

from __future__ import annotations

import bisect
import json
import os


def mask_of(text: str) -> int:
    """'0-5,9' -> bit mask."""
    mask = 0
    if not text:
        return mask
    for part in text.split(","):
        a, _, b = part.partition("-")
        lo = int(a)
        hi = int(b) if b else lo
        mask |= ((1 << (hi - lo + 1)) - 1) << lo
    return mask


def ints_of(text: str) -> list[int]:
    out: list[int] = []
    if not text:
        return out
    for part in text.split(","):
        a, _, b = part.partition("-")
        if b:
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(a))
    return out


def bits(mask: int) -> list[int]:
    out = []
    while mask:
        low = mask & -mask
        out.append(low.bit_length() - 1)
        mask ^= low
    return out


class Maps:
    def __init__(self, root: str):
        self.root = root
        self._json: dict[str, object] = {}
        self._set_cache: dict[tuple[str, int], int] = {}
        t = self.load("tests")
        self.ids: list[str] = t["ids"]
        self.n = len(self.ids)
        self.all_mask = (1 << self.n) - 1
        self.test_files: list[str] = t["files"]
        self.file_of: list[int] = t["file_of"]
        self.test_lines = t["lines"]
        self._file_masks: dict[str, int] | None = None
        self._cov_starts: dict[tuple[str, str], list[int]] = {}
        self._import_readers = None
        self._subclasses = None
        self._module_index = None
        self._stmt_index: dict[str, dict] = {}
        self._warm = None
        self.warm_hits = 0   # times a lookup returned tests that reach code only after other tests
        self._assigned_index: dict[str, set] = {}

    # ------------------------------------------------------------- loading
    def load(self, name: str):
        got = self._json.get(name)
        if got is None:
            with open(os.path.join(self.root, name + ".json"), "rb") as fh:
                got = json.load(fh)
            self._json[name] = got
        return got

    def _set(self, table: str, index: int) -> int:
        key = (table, index)
        got = self._set_cache.get(key)
        if got is None:
            got = mask_of(self.load(table)["sets"][index])
            self._set_cache[key] = got
        return got

    # --------------------------------------------------------- test files
    @property
    def file_masks(self) -> dict[str, int]:
        if self._file_masks is None:
            out: dict[str, int] = {}
            for number, f in enumerate(self.file_of):
                name = self.test_files[f]
                out[name] = out.get(name, 0) | (1 << number)
            self._file_masks = out
        return self._file_masks

    def tests_in_file(self, rel: str) -> int:
        return self.file_masks.get(rel, 0)

    # ------------------------------------------------------ line coverage
    def _runs(self, table: str, rel: str):
        runs = self.load(table)["files"].get(rel)
        if not runs:
            return None, None
        key = (table, rel)
        starts = self._cov_starts.get(key)
        if starts is None:
            starts = [r[0] for r in runs]
            self._cov_starts[key] = starts
        return runs, starts

    def _overlapping(self, table: str, rel: str, lo: int, hi: int):
        runs, starts = self._runs(table, rel)
        if not runs:
            return
        k = bisect.bisect_right(starts, lo) - 1
        if k < 0:
            k = 0
        while k < len(runs) and runs[k][0] <= hi:
            if runs[k][1] >= lo:
                yield runs[k][2]
            k += 1

    def call_tests(self, rel: str, lo: int, hi: int, warm: bool = True) -> int:
        """Tests that executed a line of rel[lo..hi] at call time (cov.json), and the tests that can
        execute it in a process where other tests ran before them (warm.json)."""
        mask = 0
        for sid in set(self._overlapping("cov", rel, lo, hi)):
            mask |= self._set("cov", sid)
        if warm:
            mask |= self.warm_tests(rel, lo, hi)
        return mask

    # --------------------------------------------- neighbouring operators
    def neighbour_differs(self, rel: str, line: int, col: int, old_op: str, new_op: str) -> int | None:
        """Tests in which the comparison at rel:line:col (operator `old_op`, e.g. "is") gave, at least
        once, another result than `new_op` (e.g. "==") would have on the same operands (cmp.json).
        None when the map has nothing to say: no such site, another operator pair, no map."""
        if not os.path.exists(os.path.join(self.root, "cmp.json")):
            return None
        table = self.load("cmp")
        if table["neighbour"].get(old_op) != new_op:
            return None
        index = self._json.get("cmp:index")
        if index is None:
            index = {name: {(row[0], row[1]): row for row in rows} for name, rows in table["files"].items()}
            self._json["cmp:index"] = index
        row = index.get(rel, {}).get((line, col))
        if row is None or row[2] != old_op:
            return None
        return self._set("cmp", row[3])

    def warm_lines(self) -> list[tuple[str, int, int]]:
        """[(file, line, tests)] of maps/warm.json."""
        self.warm_tests("", 0, 0)
        out = []
        for rel, lines in self._warm["lines"].items():
            for key, sid in lines.items():
                out.append((rel, int(key), mask_of(self._warm["sets"][sid])))
        return out

    def warm_tests(self, rel: str, lo: int, hi: int) -> int:
        """Tests that can reach a line of rel[lo..hi] only through state an earlier test left behind
        (maps/warm.json: lines seen in shared-process runs that no test executes alone)."""
        if self._warm is None:
            try:
                self._warm = self.load("warm")
            except (OSError, ValueError):
                self._warm = {"lines": {}, "sets": []}
        lines = self._warm["lines"].get(rel)
        if not lines:
            return 0
        if lo > hi:
            return 0
        mask = 0
        for key, sid in lines.items():
            if lo <= int(key) <= hi:
                mask |= mask_of(self._warm["sets"][sid])
        if mask or any(r == rel and first <= hi and lo <= last for r, first, last in self.warm_scopes()):
            self.warm_hits += 1
        return mask

    def warm_scopes(self) -> list[tuple[str, int, int]]:
        """(file, first, last) of every function holding a line of maps/warm.json: code that is
        known to take another path once other tests have run in the process.  What a test does
        there when it runs alone says little about what it does in a full run."""
        got = self.__dict__.get("_warm_scopes")
        if got is None:
            got = []
            if self._warm is None:
                self.warm_tests("", 0, 0)
            for rel, lines in self._warm["lines"].items():
                info = self.static(rel) or {}
                for key in lines:
                    line = int(key)
                    holding = [(f[4] - f[1], f[1], f[4]) for f in info.get("funcs", ()) if f[6] == "def" and f[1] <= line <= f[4]]
                    span = (rel, min(holding)[1], min(holding)[2]) if holding else (rel, line, line)
                    if span not in got:
                        got.append(span)
            self.__dict__["_warm_scopes"] = got
        return got

    def import_tests(self, rel: str, lo: int, hi: int) -> int:
        """Tests of the test files whose import executes a line of rel[lo..hi] (filecov.json)."""
        mask = 0
        fc = self.load("filecov")
        for sid in set(self._overlapping("filecov", rel, lo, hi)):
            for f in ints_of(fc["sets"][sid]):
                mask |= self.tests_in_file(fc["test_files"][f])
        return mask

    def line_tests(self, rel: str, lo: int, hi: int) -> int:
        return self.call_tests(rel, lo, hi) | self.import_tests(rel, lo, hi)

    def file_line_tests(self, rel: str) -> int:
        """Every test that executes any line of the file, at call time or by importing it."""
        return self.line_tests(rel, 1, 10 ** 9)

    def covered(self, rel: str) -> bool:
        return rel in self.load("cov")["files"] or rel in self.load("filecov")["files"]

    # ---------------------------------------------- import-time function lines
    def import_sites(self, rel: str, lo: int, hi: int) -> set[str]:
        """Definition-time lines ("path:line") on whose behalf function lines rel[lo..hi] ran at import."""
        dd = self.load("defdeps")
        deps = dd["deps"].get(rel)
        out: set[str] = set()
        if not deps:
            return out
        if hi - lo > 4000:
            keys = [k for k in deps if lo <= int(k) <= hi]
        else:
            keys = [str(k) for k in range(lo, hi + 1) if str(k) in deps]
        for k in keys:
            out.update(s for s in dd["site_sets"][deps[k]] if s != "?")
        return out

    def ran_at_import(self, rel: str, lo: int, hi: int) -> bool:
        deps = self.load("defdeps")["deps"].get(rel)
        if not deps:
            return False
        if hi - lo > 4000:
            return any(lo <= int(k) <= hi for k in deps)
        return any(str(k) in deps for k in range(lo, hi + 1))

    # ---------------------------------------------------------------- reads
    def readers(self, name: str, keys=None) -> tuple[int, int]:
        """-> (tests that looked up one of `keys` directly, tests that took the value whole).

        keys=None: everything is returned as "whole" (any reader)."""
        entry = self.load("reads")["reads"].get(name)
        if not entry:
            return 0, 0
        if keys is None:
            return 0, self._set("reads", entry["any"])
        whole = self._set("reads", entry["whole"]) if "whole" in entry else 0
        table = entry.get("keys", {})
        by_key = 0
        for key in keys:
            if key.startswith("?attr:"):
                # `Owner.NAME` in the source: an enum member ("Owner.NAME") or a class ("<pkg.mod.NAME>")
                owner_attr = key[6:]
                suffix = "." + owner_attr.rsplit(".", 1)[-1] + ">"
                for k, sid in table.items():
                    if k == owner_attr or k.endswith(suffix):
                        by_key |= self._set("reads", sid)
                continue
            sid = table.get(key)
            if sid is not None:
                by_key |= self._set("reads", sid)
        return by_key, whole

    def read_keys(self, name: str):
        entry = self.load("reads")["reads"].get(name)
        return entry.get("keys", {}) if entry else {}

    def has_reads(self, name: str) -> bool:
        return name in self.load("reads")["reads"]

    @property
    def subclasses(self) -> dict[str, set[str]]:
        if self._subclasses is None:
            out: dict[str, set[str]] = {}
            for name, info in self.load("classes")["classes"].items():
                for base in info["mro"]:
                    out.setdefault(base, set()).add(name)
            self._subclasses = out
        return self._subclasses

    def class_users(self, cls: str) -> int:
        """Tests that read a tracked value from an object of this class or of a subclass."""
        used = self.load("reads")["used_classes"]
        mask = 0
        for c in {cls} | self.subclasses.get(cls, set()):
            sid = used.get(c)
            if sid is not None:
                mask |= self._set("reads", sid)
        return mask

    def class_info(self, cls: str):
        return self.load("classes")["classes"].get(cls)

    # ---------------------------------------------------------------- derive
    @property
    def import_readers(self) -> dict[str, list[tuple[str, str, object]]]:
        """name -> [(file, "first-last", keys or None)]: definition statements that read it at import."""
        if self._import_readers is None:
            out: dict[str, list] = {}
            for rel, stmts in self.load("derive")["reads_at_import"].items():
                for span, rows in stmts.items():
                    for row in rows:
                        if isinstance(row, str):
                            out.setdefault(row, []).append((rel, span, None))
                        else:
                            out.setdefault(row[0], []).append((rel, span, row[1:]))
            self._import_readers = out
        return self._import_readers

    def reads_of_statement(self, rel: str, first: int, last: int) -> list[str]:
        rows = self.load("derive")["reads_at_import"].get(rel, {}).get(f"{first}-{last}", [])
        return [row if isinstance(row, str) else row[0] for row in rows]

    # ---------------------------------------------------------------- static
    def static(self, rel: str):
        return self.load("static")["files"].get(rel)

    def statement_at(self, rel: str, first: int, last: int | None = None):
        """The definition-time statement of the pinned file that starts at `first`
        (or, with last=None, the innermost one holding the line)."""
        info = self.static(rel)
        if not info:
            return None
        best = None
        for st in info["stmts"]:
            if last is not None:
                if st[0] == first and st[1] == last:
                    return st
            elif st[0] <= first <= st[1] and (best is None or st[1] - st[0] <= best[1] - best[0]):
                best = st
        return best

    def assigned(self, rel: str, scope: str, name: str) -> bool:
        """Is `name` bound by an assignment statement directly in `scope` ("" = module level) of the
        pinned file?  Those are the names maps/reads.json tracks loads of."""
        info = self.static(rel)
        if not info:
            return False
        index = self._assigned_index.get(rel)
        if index is None:
            index = set()
            for st in info["stmts"]:
                if st[3].startswith(("assign", "annassign", "augassign")):
                    for target in st[4]:
                        index.add((st[2], target))
            self._assigned_index[rel] = index
        if (scope, name) in index:
            return True
        cls = self.class_info(f"{info.get('module', '')}:{scope}") if scope else None
        return bool(cls and name in cls.get("set_at_class_creation", ()))

    def in_function_body(self, rel: str, line: int) -> bool:
        info = self.static(rel)
        if not info:
            return False
        for func in info["funcs"]:
            if func[6] == "def" and func[3] <= line <= func[4]:
                return True
            if func[6] == "lambda" and func[1] < line <= func[4]:
                return True
        return False

    def method_body(self, module: str, qualname: str):
        """(file, first body line, last line) of a def, by module and qualified name."""
        rel = self.module_path(module)
        info = self.static(rel) if rel else None
        if not info:
            return None
        for func in info["funcs"]:
            if func[0] == qualname and func[6] == "def":
                return rel, func[3], func[4]
        return None

    # --------------------------------------------------------------- modules
    @property
    def module_index(self) -> dict[str, int]:
        if self._module_index is None:
            self._module_index = {m: i for i, m in enumerate(self.load("modules")["modules"])}
        return self._module_index

    def module_of(self, rel: str) -> str | None:
        mo = self.load("modules")
        try:
            return mo["modules"][mo["path"].index(rel)]
        except ValueError:
            pass
        if rel.endswith(".py"):
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts.pop()
            return ".".join(parts)
        return None

    def module_path(self, module: str) -> str | None:
        mo = self.load("modules")
        i = self.module_index.get(module)
        return mo["path"][i] if i is not None else None

    def module_tests(self, module: str) -> int:
        """Tests that have the module loaded by any route (import of the test file, lazily, in a subprocess)."""
        return mask_of(self.load("modules")["loaded_by_tests"].get(module, ""))

    def package_tests(self, module: str) -> int:
        """module_tests of a package and of everything below it."""
        mask = 0
        prefix = module + "."
        for name, text in self.load("modules")["loaded_by_tests"].items():
            if name == module or name.startswith(prefix):
                mask |= mask_of(text)
        return mask

    def imports_transitively(self, module: str, target: str) -> bool:
        """Does `module` (statically, transitively) import `target`?"""
        mo = self.load("modules")
        names = mo["modules"]
        want = self.module_index.get(target)
        start = self.module_index.get(module)
        if want is None or start is None:
            return False
        seen = {start}
        todo = [start]
        while todo:
            m = todo.pop()
            for dep in ints_of(mo["imports_static"].get(names[m], "")):
                if dep == want:
                    return True
                if dep not in seen:
                    seen.add(dep)
                    todo.append(dep)
        return False

    # ----------------------------------------------------------------- opens
    def opens(self):
        return self.load("opens")

    def opener_tests(self, rel: str) -> int:
        o = self.opens()
        mask = mask_of(o["by_tests"].get(rel, ""))
        for f in ints_of(o["by_test_file_imports"].get(rel, "")):
            mask |= self.tests_in_file(o["test_files"][f])
        return mask

    def consumer_tests(self, rel: str) -> int | None:
        narrowed = self.opens()["consumers"].get(rel)
        return mask_of(narrowed["tests"]) if narrowed else None

    def text_reader_tests(self, rel: str) -> int:
        return mask_of(self.opens()["py_read_as_data"].get(rel, ""))

    def dir_listing_tests(self, rel: str, ignore=()) -> int:
        """Tests that list the directory the path sits in."""
        d = os.path.dirname(rel) + "/"
        if d in ignore:
            return 0
        return mask_of(self.opens()["dirs_listed"].get(d, ""))

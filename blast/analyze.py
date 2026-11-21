"""From one patch to groups of candidate tests.

A group is (probability that some test in it fails, set of tests, reason).
Nothing here knows any patch in advance: every group comes from looking up
what the patch touches in the maps of the pinned commit (maps/README.md):

  code inside a function or lambda  -> tests that executed those statements
  code that runs at import          -> tests that read the values it binds,
                                       directly and through derived values
  a module that cannot be imported  -> tests that load it
  a test file                       -> tests that execute the changed lines,
                                       in their body or when the file is imported
  a data file                       -> tests that open it
  anything else                     -> what pytest and the import system would
                                       do with it, or nothing
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys

from . import config as C
from .diffparse import ApplyError, FileChange, apply_hunks, parse_patch
from .maps import Maps
from .pydiff import Mark, bound_names, diff_python

_PYTEST_INI_FILES = ("pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")
_PYTEST_WORDS = re.compile(
    r"\[tool\.pytest|\[pytest\]|\[tool:pytest\]|\baddopts\b|\btestpaths\b|\bfilterwarnings\b|\bpython_files\b|"
    r"\bpython_classes\b|\bpython_functions\b|\bminversion\b|\brequired_plugins\b|\bxfail_strict\b|\bnorecursedirs\b|"
    r"\busefixtures\b|\bpythonpath\b|\btimeout\b|\bmarkers\b|\blog_cli\b|\bconsider_namespace_packages\b|\bdoctest_")
# top-level names that the labeling environment imports from site-packages
_THIRD_PARTY = {
    "duckdb", "numpy", "pandas", "dateutil", "pytz", "tzdata", "pytest", "_pytest", "pluggy", "iniconfig", "packaging",
    "xdist", "execnet", "pytest_timeout", "typing_extensions", "mypy_extensions", "setuptools", "pkg_resources",
    "jinja2", "markupsafe", "yaml", "psutil", "pygments", "six", "sqlglotc", "sqlglotrs",
}
_DISPATCH_NAME = re.compile(r".*_sql$|^_parse_|^visit_|^_annotate_|^_to_|^__")


class Group:
    __slots__ = ("p", "mask", "why")

    def __init__(self, p: float, mask: int, why: str):
        self.p = p
        self.mask = mask
        self.why = why


class ExactRequest:
    """Ask for the patched tree to be imported and its definition-time values compared."""

    def __init__(self):
        self.files: dict[str, str | None] = {}   # sqlglot path -> new text (None = deleted)
        self.reasons: list[str] = []
        self.const_only = True
        self.fallback: list[Group] = []          # used instead when the comparison cannot run
        self.names: set[str] = set()             # values the edit binds or feeds, per the static maps
        self.modules: set[str] = set()           # modules whose import the patch may break


class Analysis:
    def __init__(self, commit: str):
        self.commit = commit
        self.groups: list[Group] = []
        self.notes: list[str] = []
        self.exact: ExactRequest | None = None

    def add(self, p: float, mask: int, why: str):
        if mask:
            self.groups.append(Group(p, mask, why))

    def want_exact(self, reason: str, const_only: bool = False) -> ExactRequest:
        if self.exact is None:
            self.exact = ExactRequest()
        self.exact.reasons.append(reason)
        if not const_only:
            self.exact.const_only = False
        return self.exact

    def fallback(self, p: float, mask: int, why: str):
        """A group that only counts when the exact comparison does not run."""
        if mask:
            if self.exact is None:
                self.exact = ExactRequest()
            self.exact.fallback.append(Group(p, mask, why))


class Source:
    """The pinned checkout, read on demand."""

    def __init__(self, root: str):
        self.root = root
        self._text: dict[str, str | None] = {}
        self._py_files: list[str] | None = None
        self.ast_cache: dict = {}

    def read(self, rel: str) -> str | None:
        if rel not in self._text:
            full = os.path.join(self.root, rel)
            try:
                with open(full, "rb") as fh:
                    self._text[rel] = fh.read().decode("utf-8", "surrogateescape")
            except (OSError, ValueError):
                self._text[rel] = None
        return self._text[rel]

    def exists(self, rel: str) -> bool:
        return os.path.lexists(os.path.join(self.root, rel))

    def isdir(self, rel: str) -> bool:
        return os.path.isdir(os.path.join(self.root, rel))

    def python_files(self) -> list[str]:
        if self._py_files is None:
            out = []
            for top in ("sqlglot", "tests", "sqlglotc"):
                for base, dirs, files in os.walk(os.path.join(self.root, top)):
                    dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                    for name in sorted(files):
                        if name.endswith(".py"):
                            out.append(os.path.relpath(os.path.join(base, name), self.root).replace(os.sep, "/"))
            self._py_files = out
        return self._py_files


def category(path: str) -> str:
    if path.endswith(".py"):
        if path.startswith("sqlglot/"):
            return "src"
        if path.startswith("tests/"):
            return "test"
        return "py"
    return "data"


class Analyzer:
    def __init__(self, maps: Maps, source: Source):
        self.maps = maps
        self.src = source

    # ================================================================ entry
    def analyze(self, commit: str, data: bytes) -> Analysis:
        an = Analysis(commit)
        patch = parse_patch(data)
        if patch.problems and not patch.files:
            an.add(C.P_UNREADABLE_PATCH, self.maps.all_mask, "patch has no readable file header")
            return an
        for problem in patch.problems:
            an.notes.append(problem)
        for fc in patch.files:
            try:
                self.file(an, fc)
            except Exception as exc:  # one odd file must not take the commit down: run what could depend on it
                an.notes.append(f"{fc.path}: analysis failed ({type(exc).__name__}: {exc})")
                self.broad(an, fc.path or "", C.P_INTERNAL_ERROR, "analysis of this file failed")
        return an

    def broad(self, an: Analysis, path: str, p: float, why: str):
        """Everything that could depend on a file, when nothing finer is known."""
        maps = self.maps
        cat = category(path)
        if cat == "src":
            module = maps.module_of(path) or ""
            mask = maps.module_tests(module) | maps.file_line_tests(path) | maps.text_reader_tests(path)
            if path.endswith("__init__.py"):
                mask |= maps.package_tests(module)
            an.add(p, mask, f"{path}: {why}: tests that load the module")
        elif cat == "test" or (cat == "py" and maps.covered(path)):
            mask = maps.tests_in_file(path) | maps.file_line_tests(path) | maps.text_reader_tests(path)
            if os.path.basename(path) in ("conftest.py", "__init__.py"):
                mask |= self.tests_under(os.path.dirname(path))
            an.add(p, mask, f"{path}: {why}: tests in or depending on the file")
        elif cat == "py":
            if os.path.basename(path) == "conftest.py" or "/" not in path:
                an.add(p, maps.all_mask, f"{path}: {why}")
        else:
            mask = maps.opener_tests(path)
            if os.path.basename(path) in _PYTEST_INI_FILES and "/" not in path:
                mask = maps.all_mask
            an.add(p, mask, f"{path}: {why}: tests that open the file")

    def tests_under(self, directory: str) -> int:
        prefix = directory.rstrip("/") + "/" if directory else ""
        mask = 0
        for name, m in self.maps.file_masks.items():
            if name.startswith(prefix):
                mask |= m
        return mask

    # ============================================================== one file
    def file(self, an: Analysis, fc: FileChange):
        path = fc.path
        if not path:
            an.add(C.P_UNREADABLE_PATCH, self.maps.all_mask, "a file entry without a path")
            return
        for problem in fc.problems:
            an.notes.append(f"{path}: {problem}")
        if fc.copied and fc.new_path:
            text = self.src.read(fc.old_path) if fc.old_path else None
            self.new_file(an, fc.new_path, self.patched_text(text or "", fc), fc)
            return
        if fc.renamed and fc.old_path and fc.new_path and fc.old_path != fc.new_path:
            old_text = self.src.read(fc.old_path)
            self.deleted_file(an, fc.old_path)
            self.new_file(an, fc.new_path, self.patched_text(old_text or "", fc), fc)
            return
        if fc.deleted:
            self.deleted_file(an, path)
            return
        if fc.new_file or not self.src.exists(path):
            if fc.new_file and self.src.exists(path):
                an.notes.append(f"{path}: patch adds a file that the pinned commit already has")
                self.broad(an, path, C.P_HUNK_MISMATCH, "patch does not fit the pinned file")
                return
            if not fc.new_file:
                an.notes.append(f"{path}: not in the pinned commit, treated as a new file")
            self.new_file(an, path, self.patched_text("", fc), fc)
            return
        if not fc.content_changed:
            an.notes.append(f"{path}: mode change only" if fc.mode_change else f"{path}: no content change")
            return
        self.modified_file(an, path, fc)

    def patched_text(self, old_text: str, fc: FileChange) -> str | None:
        if fc.binary:
            return None
        if not fc.hunks:
            return old_text
        try:
            return apply_hunks(old_text, fc.hunks, C.HUNK_FUZZ_LINES).new_text()
        except ApplyError:
            return "\n".join(text for h in fc.hunks for tag, text in h.lines if tag != "-") + "\n"

    def modified_file(self, an: Analysis, path: str, fc: FileChange):
        maps = self.maps
        cat = category(path)
        if fc.binary:
            if cat == "data":
                an.add(C.P_FIXTURE_GONE, maps.opener_tests(path), f"{path}: binary content replaced: tests that open it")
            else:
                self.broad(an, path, C.P_MODULE_BROKEN, "Python file replaced by binary content")
            return
        old_text = self.src.read(path)
        if old_text is None or self.src.isdir(path):
            self.broad(an, path, C.P_HUNK_MISMATCH, "pinned file unreadable")
            return
        try:
            applied = apply_hunks(old_text, fc.hunks, C.HUNK_FUZZ_LINES)
        except ApplyError as exc:
            an.notes.append(f"{path}: {exc}")
            self.broad(an, path, C.P_HUNK_MISMATCH, "hunk does not fit the pinned file")
            return
        if any(not h.complete for h in fc.hunks):
            self.broad(an, path, C.P_HUNK_MISMATCH, "truncated hunk")
        if applied.new_lines == applied.old_lines:
            an.notes.append(f"{path}: no line changes")
            return
        if cat == "data":
            self.data_file(an, path, old_text, applied.new_text())
        else:
            self.python_file(an, path, cat, old_text, applied)

    # ============================================================ data files
    def data_file(self, an: Analysis, path: str, old_text: str, new_text: str):
        maps = self.maps
        base = os.path.basename(path)
        if "/" not in path and base in _PYTEST_INI_FILES:
            self.pytest_config(an, path, old_text, new_text)
            return
        if path.startswith("sqlglotc/"):
            an.add(C.P_TEXT_READER_CODE, maps.text_reader_tests("sqlglotc/setup.py"), f"{path}: sqlglotc build input")
            return
        openers = maps.opener_tests(path)
        if not openers:
            an.notes.append(f"{path}: no test opens it")
            return
        consumers = maps.consumer_tests(path)
        ignored = None
        if path.startswith("tests/fixtures/") and path.endswith(".sql"):
            # tests/helpers.py reads every .sql fixture through _filter_comments
            if _filter_comments(old_text) == _filter_comments(new_text):
                ignored = "only blank lines and `--` lines changed, which tests/helpers.py::_filter_comments drops"
        elif path.endswith(".json"):
            try:
                if json.loads(old_text) == json.loads(new_text):
                    ignored = "the JSON document is unchanged"
            except ValueError:
                pass
        if ignored:
            an.add(C.P_FIXTURE_IGNORED_LINES, openers, f"{path}: {ignored}")
            return
        readable = True
        try:
            new_text.encode("utf-8")
        except UnicodeEncodeError:
            readable = False  # the loaders open fixtures as UTF-8
        if consumers is None or not readable:
            an.add(C.P_FIXTURE_CONSUMER, openers, f"{path}: tests that open the fixture")
        else:
            an.add(C.P_FIXTURE_CONSUMER, consumers, f"{path}: tests that consume the fixture")
            an.add(C.P_FIXTURE_OPENER, openers & ~consumers, f"{path}: tests that open the fixture in a shared set-up")

    def pytest_config(self, an: Analysis, path: str, old_text: str | None, new_text: str | None):
        """pyproject.toml, setup.cfg, pytest.ini, tox.ini: pytest parses them at start-up for every test."""
        maps = self.maps
        base = os.path.basename(path)
        if new_text is None:
            an.add(0.05, maps.all_mask, f"{path}: removed; pytest looked at it when choosing its rootdir")
            return
        problem = None
        if base == "pyproject.toml":
            try:
                import tomllib

                doc = tomllib.loads(new_text)
                has_section = "pytest" in doc.get("tool", {}) if isinstance(doc.get("tool"), dict) else False
            except Exception as exc:
                problem = f"not valid TOML ({exc})"
                has_section = False
        else:
            problem, has_section = _ini_problem(new_text, base)
        if problem:
            an.add(C.P_MODULE_BROKEN, maps.all_mask, f"{path}: {problem}: pytest cannot start")
            return
        old_lines = set((old_text or "").split("\n"))
        new_lines = set(new_text.split("\n"))
        changed = [ln for ln in (old_lines ^ new_lines) if ln.strip()]
        old_has = bool(old_text) and bool(re.search(r"\[tool\.pytest|\[pytest\]|\[tool:pytest\]", old_text))
        if base in ("pytest.ini", ".pytest.ini") and old_text is None and any(
                ln.strip() and not ln.strip().startswith(("#", ";", "[")) for ln in new_text.split("\n")):
            has_section = True
        if has_section or old_has or any(_PYTEST_WORDS.search(ln) for ln in changed):
            an.add(C.P_PYTEST_CONFIG, maps.all_mask, f"{path}: pytest configuration changed")
        else:
            an.add(C.P_PROJECT_METADATA, maps.all_mask, f"{path}: project metadata outside any pytest section")

    # ========================================================== whole files
    def deleted_file(self, an: Analysis, path: str):
        maps = self.maps
        cat = category(path)
        listing = maps.dir_listing_tests(path, C.DIR_LISTINGS_BY_IMPORT_SYSTEM)
        if cat == "src":
            module = maps.module_of(path) or ""
            mask = maps.module_tests(module) | maps.file_line_tests(path) | maps.text_reader_tests(path)
            if path.endswith("__init__.py"):
                mask |= maps.package_tests(module)
            an.add(C.P_MODULE_BROKEN, mask, f"{path}: module removed: tests that load it")
            an.add(C.P_DIR_LISTED, listing, f"{path}: removed from a directory these tests list")
            if module:
                # modules that import it break at import; the exact comparison finds them
                req = an.want_exact(f"module {module} removed")
                req.files[path] = None
        elif cat == "test" or cat == "py":
            mask = maps.tests_in_file(path) | maps.file_line_tests(path) | maps.text_reader_tests(path)
            if os.path.basename(path) in ("__init__.py", "conftest.py"):
                an.add(0.3, self.tests_under(os.path.dirname(path)), f"{path}: package marker / conftest removed")
            an.add(C.P_TEST_FILE_BROKEN, mask, f"{path}: file removed: its tests and the tests that import it")
        else:
            if "/" not in path and os.path.basename(path) in _PYTEST_INI_FILES:
                self.pytest_config(an, path, self.src.read(path), None)
                return
            an.add(C.P_FIXTURE_GONE, maps.opener_tests(path), f"{path}: file removed: tests that open it")
            an.add(C.P_DIR_LISTED, listing, f"{path}: removed from a directory these tests list")

    def new_file(self, an: Analysis, path: str, text: str | None, fc: FileChange):
        maps = self.maps
        base = os.path.basename(path)
        cat = category(path)
        parts = path.split("/")
        an.add(C.P_DIR_LISTED, maps.dir_listing_tests(path, C.DIR_LISTINGS_BY_IMPORT_SYSTEM), f"{path}: new entry in a directory these tests list")
        # ---- names that the interpreter or pytest pick up without anyone importing them
        if base == "conftest.py":
            an.add(C.P_PYTEST_CONFIG, self.tests_under(os.path.dirname(path)), f"{path}: new conftest.py")
            return
        if len(parts) == 1 and base in _PYTEST_INI_FILES:
            self.pytest_config(an, path, None, text or "")
            return
        top = parts[0][:-3] if len(parts) == 1 and parts[0].endswith(".py") else parts[0]
        if len(parts) == 1 and top in ("sitecustomize", "usercustomize"):
            an.add(C.P_SHADOWS_MODULE, maps.all_mask, f"{path}: imported at interpreter start-up from PYTHONPATH")
            return
        shadows_top = (len(parts) == 1 and path.endswith(".py")) or (len(parts) == 2 and base == "__init__.py")
        if shadows_top and top not in ("sqlglot", "tests", "setup", "sqlglotc") and (
                top in getattr(sys, "stdlib_module_names", ()) or top in _THIRD_PARTY):
            an.add(C.P_SHADOWS_MODULE, maps.all_mask,
                   f"{path}: the checkout is on PYTHONPATH, so this shadows the module `{top}`")
            return
        if base == "__init__.py" and self.src.exists(os.path.dirname(path) + ".py"):
            # a package directory wins over a module file of the same name
            sibling = os.path.dirname(path) + ".py"
            module = maps.module_of(sibling) or ""
            an.add(C.P_MODULE_BROKEN,
                   maps.module_tests(module) | maps.file_line_tests(sibling) | maps.tests_in_file(sibling),
                   f"{path}: new package shadows the module {sibling}")
            return
        if cat == "src":
            module = maps.module_of(path)
            if text is not None and module:
                req = an.want_exact(f"new module {module}", const_only=True)
                req.files[path] = text
            an.notes.append(f"{path}: new module, nothing imports it at the pinned commit")
            return
        if cat == "test":
            if base == "__init__.py":
                an.add(0.2, self.tests_under(os.path.dirname(path)), f"{path}: new package marker in the test tree")
            else:
                an.notes.append(f"{path}: new test file; its tests have no number")
            return
        an.notes.append(f"{path}: new file nothing reads")

    # ========================================================== python files
    def python_file(self, an: Analysis, path: str, cat: str, old_text: str, applied):
        maps = self.maps
        diff = diff_python(old_text, applied, self.src.ast_cache, path, filename=path)
        readers = maps.text_reader_tests(path)
        if diff.broken:
            an.notes.append(f"{path}: does not compile after the patch ({diff.broken})")
            if cat == "src":
                module = maps.module_of(path) or ""
                mask = maps.module_tests(module) | maps.file_line_tests(path) | readers
                if path.endswith("__init__.py"):
                    mask |= maps.package_tests(module)
                an.add(C.P_MODULE_BROKEN, mask, f"{path}: no longer compiles: every test that loads the module")
            elif cat == "test" or maps.covered(path):
                mask = maps.tests_in_file(path) | maps.file_line_tests(path) | readers
                an.add(C.P_TEST_FILE_BROKEN, mask, f"{path}: no longer compiles: its tests and the tests that import it")
            return
        if diff.same_ast:
            an.notes.append(f"{path}: comments / blank lines / layout only (same AST)")
            an.add(C.P_TEXT_READER_COMMENT, readers, f"{path}: read as text by these tests; only comments changed")
            return
        an.add(C.P_TEXT_READER_CODE, readers, f"{path}: these tests read the file as text or run it by path")
        if cat == "src":
            ctx = _SrcFile(self, an, path, old_text, applied, diff)
            for mark in diff.marks:
                ctx.mark(mark)
            ctx.finish()
        elif cat == "test" or maps.covered(path):
            ctx = _TestFile(self, an, path, old_text, applied, diff)
            for mark in diff.marks:
                ctx.mark(mark)
        else:
            an.notes.append(f"{path}: Python file no test executes")

    # ------------------------------------------------------ shared helpers
    def mention_tests(self, name: str, only_prefix: tuple[str, ...] = ("sqlglot/", "tests/", "sqlglotc/"),
                      skip: str | None = None) -> tuple[int, set[str]]:
        """Tests that execute a line mentioning `name` as a word, anywhere in the pinned tree;
        and the sqlglot modules that mention it in definition-time code (they break at import
        if the name is gone)."""
        maps = self.maps
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        mask = 0
        def_time_modules: set[str] = set()
        for rel in self.src.python_files():
            if rel == skip or not rel.startswith(only_prefix):
                continue
            text = self.src.read(rel)
            if not text or name not in text:
                continue
            for number, line in enumerate(text.split("\n"), 1):
                if name in line and pattern.search(line):
                    mask |= maps.line_tests(rel, number, number)
                    if rel.startswith("sqlglot/") and not maps.in_function_body(rel, number):
                        def_time_modules.add(maps.module_of(rel) or "")
        return mask, def_time_modules

    def local_mentions(self, path: str, old_text: str, name: str, skip_range=None) -> tuple[int, bool]:
        """Tests executing lines of this file that mention `name`; and whether a definition-time line does."""
        maps = self.maps
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        mask = 0
        def_time = False
        for number, line in enumerate(old_text.split("\n"), 1):
            if skip_range and skip_range[0] <= number <= skip_range[1]:
                continue
            if name in line and pattern.search(line) and not line.lstrip().startswith("#"):
                mask |= maps.line_tests(path, number, number)
                if not maps.in_function_body(path, number):
                    def_time = True
        return mask, def_time


def _filter_comments(s: str) -> str:
    """tests/helpers.py::_filter_comments at the pinned commit."""
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _ini_problem(text: str, base: str) -> tuple[str | None, bool]:
    """The checks iniconfig makes when pytest parses an ini file; -> (problem, has a pytest section)."""
    section = None
    names: set[tuple[str, str]] = set()
    sections: set[str] = set()
    last_was_name = False
    wanted = "tool:pytest" if base == "setup.cfg" else "pytest"
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if line[0] == "[":
            end = line.find("]")
            if end < 0:
                return "unterminated section header", False
            section = line[1:end]
            if section in sections:
                return f"duplicate section [{section}]", False
            sections.add(section)
            last_was_name = False
            continue
        if line[0].isspace():
            if not last_was_name:
                return "unexpected continuation line", False
            continue
        cut = min((i for i in (line.find("="), line.find(":")) if i >= 0), default=-1)
        if cut < 0:
            return f"line without `=`: {stripped[:40]!r}", False
        if section is None:
            return "value before any section header", False
        name = line[:cut].strip()
        if (section, name) in names:
            return f"duplicate name {name!r} in [{section}]", False
        names.add((section, name))
        last_was_name = True
    return None, wanted in sections


class _FileCtx:
    def __init__(self, analyzer: Analyzer, an: Analysis, path: str, old_text: str, applied, diff):
        self.az = analyzer
        self.maps = analyzer.maps
        self.an = an
        self.path = path
        self.old_text = old_text
        self.applied = applied
        self.diff = diff
        self.module = self.maps.module_of(path) or ""

    def old_def(self, scope: str, name: str):
        """(first body line, last line) of the def `name` directly in class `scope` of the old file."""
        body = self.diff.old_tree.body
        for part in [p for p in scope.split(".") if p]:
            nxt = None
            for s in body:
                if isinstance(s, ast.ClassDef) and s.name == part:
                    nxt = s.body
            if nxt is None:
                return None
            body = nxt
        found = None
        for s in body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == name:
                found = (s.body[0].lineno, s.end_lineno)
        return found

    def old_scope_names(self, scope: str) -> list[str]:
        body = self.diff.old_tree.body
        for part in [p for p in scope.split(".") if p]:
            nxt = None
            for s in body:
                if isinstance(s, ast.ClassDef) and s.name == part:
                    nxt = s.body
            if nxt is None:
                return []
            body = nxt
        return bound_names(body)


class _TestFile(_FileCtx):
    """A test file, a helper of the tests, or any other Python file tests execute:
    line coverage of tests/** answers both "whose body is this" and "whose import runs this"."""

    def lines(self, lo: int, hi: int) -> int:
        return self.maps.line_tests(self.path, lo, hi)

    def anchors(self, mark: Mark) -> int:
        mask = 0
        for lo, hi in mark.info.get("anchors", ()):
            # the first line of a neighbouring statement is enough: who executes a class body or
            # a module executes all its statement headers
            mask |= self.maps.import_tests(self.path, lo, lo) | self.maps.import_tests(self.path, hi, hi)
            mask |= self.maps.call_tests(self.path, lo, lo)
        if not mask:
            mask = self.maps.tests_in_file(self.path)
        return mask

    def mark(self, m: Mark):
        an, path = self.an, self.path
        kind = m.kind
        if kind in ("call", "lambda"):
            p = C.P_INSERT_NEIGHBOUR if m.info.get("why") == "neighbour" else C.P_TEST_BODY
            an.add(p, self.lines(m.lo, m.hi), f"{path}:{m.lo}-{m.hi}: tests that execute this code ({m.info.get('why', 'lambda')})")
        elif kind == "value":
            if m.info["op"] == "insert":
                if not m.info.get("trivial"):
                    an.add(C.P_TEST_FILE_LEVEL, self.anchors(m), f"{path}: new module/class-level statement")
            else:
                # A module-level constant of a test helper (a schema table, a path): the tests that
                # execute a line mentioning it.  Anything that is evaluated (and so may raise while
                # the file is imported) or sits in a class body: every test of the importing files.
                precise = 0
                targets = m.info["targets"]
                confined = bool(targets) and not m.scope and (m.info.get("const_only") or m.info.get("keys") is not None) \
                    and m.info["op"] == "change"
                if confined:
                    for name in targets:
                        precise |= self.az.local_mentions(path, self.old_text, name, (m.lo, m.hi))[0]
                        precise |= self.az.mention_tests(name, ("tests/",), skip=path)[0]
                if confined and precise:
                    an.add(C.P_TEST_FILE_LEVEL, precise,
                           f"{path}:{m.lo}-{m.hi}: {'/'.join(targets)} changed: tests executing lines that mention it")
                else:
                    an.add(C.P_TEST_FILE_LEVEL, self.lines(m.lo, m.hi) or self.maps.tests_in_file(path),
                           f"{path}:{m.lo}-{m.hi}: module/class-level statement changed: tests of the files whose import runs it")
        elif kind == "import":
            if m.info["op"] == "insert":
                an.add(C.P_TEST_FILE_LEVEL, self.anchors(m), f"{path}: new import; the file may no longer import")
            else:
                an.add(C.P_TEST_FILE_LEVEL, self.lines(m.lo, m.hi) or self.maps.tests_in_file(path),
                       f"{path}:{m.lo}: import changed: tests of the files whose import runs it")
                for name in m.info["removed"] + m.info["rebound"]:
                    mask, _ = self.az.local_mentions(path, self.old_text, name)
                    an.add(C.P_TEST_FILE_LEVEL, mask, f"{path}: tests executing lines that mention `{name}`")
        elif kind == "defhdr":
            lo, hi = m.info["body"]
            if not (m.info["annotations_only"] and self.diff.future_annotations):
                an.add(C.P_TEST_BODY, self.lines(m.lo, hi), f"{path}: def {m.info['name']} header changed: tests that run it")
            if m.info["name_changed"]:
                self.name_gone(m.info["name"], (m.lo, hi))
                self.new_def(m.scope, m.info["new_name"], self.lines(m.lo, hi) or self.maps.tests_in_file(path))
        elif kind == "defdel":
            lo, hi = m.info["body"]
            an.add(C.P_TEST_BODY, self.lines(m.lo, hi), f"{path}: def {m.info['name']} removed: tests that ran it")
            self.name_gone(m.info["name"], (m.lo, hi))
        elif kind == "defins":
            self.new_def(m.scope, m.info["name"], self.anchors(m))
        elif kind in ("classhdr", "classdel"):
            lo, hi = m.info["range"]
            an.add(C.P_TEST_FILE_LEVEL, self.lines(lo, hi), f"{path}: class {m.info['name']} changed or removed")
            if kind == "classdel" or m.info.get("new_name") != m.info["name"]:
                self.name_gone(m.info["name"], (lo, hi))
        elif kind == "classins":
            used, _ = self.az.local_mentions(path, self.old_text, m.info["name"])
            an.add(C.P_TEST_FILE_LEVEL, used, f"{path}: new class `{m.info['name']}` takes over an existing name")
            an.add(C.P_NEW_TEST, self.anchors(m), f"{path}: new class with a fresh name")
        elif kind == "block":
            mask = self.lines(m.lo, m.hi) if m.lo else self.anchors(m)
            an.add(C.P_TEST_FILE_LEVEL, mask or self.maps.tests_in_file(path), f"{path}: module/class-level block changed")
        else:
            an.add(C.P_TEST_FILE_LEVEL, self.maps.tests_in_file(path) | self.maps.file_line_tests(path), f"{path}: {m.info.get('why', kind)}")

    def name_gone(self, name: str, own_range):
        mask, _ = self.az.local_mentions(self.path, self.old_text, name, own_range)
        self.an.add(C.P_NAME_MENTION, mask, f"{self.path}: tests executing lines that mention removed `{name}`")
        if not name.startswith("test"):
            everywhere, _ = self.az.mention_tests(name, ("tests/",), skip=self.path)
            self.an.add(C.P_NAME_MENTION, everywhere, f"other test files mention removed `{name}`")

    def new_def(self, scope: str, name: str, file_tests: int):
        an, path = self.an, self.path
        previous = self.old_def(scope, name)
        if previous:
            an.add(C.P_TEST_BODY, self.lines(*previous), f"{path}: a second def {name} replaces the first: tests that ran it")
            return
        import unittest  # only here: importing it costs more than a typical commit's whole analysis

        hook = hasattr(unittest.TestCase, name) or name in (
            "setUpModule", "tearDownModule", "load_tests", "asyncSetUp", "asyncTearDown") or name.startswith("pytest_")
        inherited, _ = self.az.mention_tests(name, ("tests/",)) if scope else (0, set())
        local, _ = self.az.local_mentions(path, self.old_text, name)
        if hook:
            an.add(C.P_TEST_FILE_LEVEL, file_tests, f"{path}: new def {name} is a unittest/pytest hook")
        elif local or inherited:
            an.add(C.P_TEST_FILE_LEVEL, (local | inherited) & (file_tests | local),
                   f"{path}: new def {name} takes over a name these tests use")
        else:
            an.add(C.P_NEW_TEST, file_tests, f"{path}: new def {name} with a fresh name")


class _SrcFile(_FileCtx):
    """A module of sqlglot."""

    def __init__(self, *args):
        super().__init__(*args)
        self.exact_reasons: list[str] = []
        self.changed_names: list[tuple[str, list[str] | None]] = []

    def definer(self, scope: str) -> str:
        return f"{self.module}:{scope}"

    def call(self, lo: int, hi: int, p: float, why: str):
        maps, an, path = self.maps, self.an, self.path
        an.add(p, maps.line_tests(path, lo, hi), f"{path}:{lo}-{hi}: tests that executed this code ({why})")
        if maps.ran_at_import(path, lo, hi):
            # the same lines also ran while sqlglot was being imported: the definitions
            # they ran for may come out different, or the import may fail
            self.exact(f"{path}:{lo}-{hi} runs at import")
            for site in sorted(maps.import_sites(path, lo, hi)):
                rel, _, line = site.rpartition(":")
                st = maps.statement_at(rel, int(line))
                if st is None:
                    continue
                module = maps.module_of(rel) or ""
                names = [f"{module}:{st[2]}|{t}" for t in st[4]]
                if st[3].startswith("classhdr") and st[4]:
                    cls = f"{module}:{st[2] + '.' if st[2] else ''}{st[4][0]}"
                    info = maps.class_info(cls)
                    names = [f"{cls}|{a}" for a in (info["set_at_class_creation"] if info else [])]
                self.static_closure(names, None, f"{path}:{lo}-{hi} ran at import for {site}", fallback_only=True)

    def exact(self, reason: str, const_only: bool = False):
        req = self.an.want_exact(reason, const_only)
        req.files[self.path] = self.applied.new_text()
        req.modules.add(self.module)

    def mark(self, m: Mark):
        an, maps, path = self.an, self.maps, self.path
        kind = m.kind
        if kind == "call":
            p = C.P_INSERT_NEIGHBOUR if m.info.get("why") == "neighbour" else C.P_LINE_HIT
            self.call(m.lo, m.hi, p, m.info.get("why", ""))
        elif kind == "lambda":
            self.call(m.lo, m.hi, C.P_LINE_HIT, "lambda body")
        elif kind == "value":
            self.value(m)
        elif kind == "import":
            self.import_(m)
        elif kind == "defhdr":
            lo, hi = m.info["body"]
            if m.info["annotations_only"] and self.diff.future_annotations:
                an.notes.append(f"{path}: only annotations of def {m.info['name']} changed (never evaluated)")
                return
            self.call(m.lo, hi, C.P_SIGNATURE, f"def {m.info['name']}: signature, defaults or decorators changed")
            self.exact(f"def {m.info['name']} header changed")
            if m.info["name_changed"]:
                self.name_gone(m.scope, m.info["name"], (m.lo, hi))
                self.new_def(m.scope, m.info["new_name"])
        elif kind == "defdel":
            lo, hi = m.info["body"]
            self.call(m.lo, hi, C.P_LINE_HIT, f"def {m.info['name']} removed")
            self.name_gone(m.scope, m.info["name"], (m.lo, hi))
            self.exact(f"def {m.info['name']} removed")
        elif kind == "defins":
            self.new_def(m.scope, m.info["name"])
            self.exact(f"def {m.info['name']} added")
        elif kind in ("classhdr", "classdel"):
            lo, hi = m.info["range"]
            cls = f"{self.module}:{m.info['qual']}"
            an.add(C.P_CLASS_USER, maps.class_users(cls) | maps.line_tests(path, lo, hi),
                   f"{path}: class {m.info['qual']} header changed or class removed: tests that used it")
            self.exact(f"class {m.info['qual']} changed")
            if kind == "classdel" or m.info.get("new_name") != m.info["name"]:
                self.name_gone(m.scope, m.info["name"], (lo, hi))
            self.unconfined(f"class {m.info['qual']} changed", fallback_only=True)
        elif kind == "classins":
            name = m.info["name"]
            if name in self.old_scope_names(m.scope):
                mask, _ = self.az.local_mentions(path, self.old_text, name)
                an.add(C.P_IMPORT_REBIND, mask, f"{path}: new class `{name}` takes over an existing name")
            self.exact(f"class {name} added")
            self.unconfined(f"class {name} added", fallback_only=True)
        elif kind == "block":
            for name in m.info["names"]:
                self.value_names([f"{self.definer(m.scope)}|{name}"], None, f"`{name}` bound in a changed block")
            if m.lo:
                an.add(C.P_LINE_HIT, maps.line_tests(path, m.lo, m.hi), f"{path}:{m.lo}-{m.hi}: tests that executed the block")
            self.exact("module/class-level block changed")
            self.unconfined("module/class-level block changed", fallback_only=True)
        else:
            self.unconfined(m.info.get("why", kind))

    def unconfined(self, why: str, fallback_only: bool = False):
        mask = self.maps.module_tests(self.module) | self.maps.file_line_tests(self.path)
        if fallback_only:
            self.an.fallback(C.P_MODULE_UNCONFINED, mask, f"{self.path}: {why}: tests that load the module")
        else:
            self.an.add(C.P_MODULE_UNCONFINED, mask, f"{self.path}: {why}: tests that load the module")

    # ------------------------------------------------------------- values
    def value(self, m: Mark):
        an, maps, path = self.an, self.maps, self.path
        info = m.info
        op = info["op"]
        if op == "insert" and info.get("trivial"):
            return
        definer = self.definer(m.scope)
        targets = info["targets"]
        const_only = bool(info.get("const_only")) and op == "change"
        self.exact(f"{path}:{m.lo}: definition-time statement {op}", const_only)
        if not targets or targets == ["__doc__"]:
            # a statement that binds nothing acts on what it reads (KEYWORDS.pop(...), a call)
            names = maps.reads_of_statement(path, *info["stmt"]) if info.get("stmt") else []
            if targets == ["__doc__"]:
                names = [f"{definer}|__doc__"]
            if names:
                self.value_names(names, None, f"{path}:{m.lo}: statement without a target; values it reads")
            elif op != "insert" or not isinstance(info.get("new_node"), ast.Pass):
                self.unconfined(f"line {m.lo or '(new)'}: module/class-level statement with side effects only",
                                fallback_only=targets == ["__doc__"])
            return
        names = [f"{definer}|{t}" for t in targets]
        keys = None
        if info.get("keys") is not None and len(names) == 1:
            keys = spell_keys(info["keys"])
        self.value_names(names, keys, f"{path}:{m.lo or '(new)'}: {'/'.join(targets)} {op}")
        if op in ("insert", "delete") and m.scope:
            # the attribute now shadows (or stops shadowing) one inherited from a base class:
            # readers were charged to the base that defined it
            info_c = maps.class_info(definer)
            users = maps.class_users(definer)
            for base in (info_c["mro"] if info_c else []):
                for t in targets:
                    _, whole = maps.readers(f"{base}|{t}")
                    if whole:
                        an.add(C.P_VALUE_READ, (whole & users) if users else whole,
                               f"{path}: {t} of {m.scope} overrides {base}: its readers that used {m.scope}")
        if op == "delete" and not m.scope:
            for t in targets:
                self.name_gone("", t, (m.lo, m.hi))

    def value_names(self, names: list[str], keys: list[str] | None, why: str):
        an, maps = self.an, self.maps
        for name in names:
            by_key, whole = maps.readers(name, keys)
            if keys is None:
                an.add(C.P_VALUE_READ, whole, f"{why}: tests that read {name}")
            else:
                an.add(C.P_VALUE_READ, by_key, f"{why}: tests that looked up {keys} in {name}")
                an.add(C.P_VALUE_WHOLE, whole, f"{why}: tests that took {name} as a whole")
            self.changed_names.append((name, keys))
        self.static_closure(names, keys, why, fallback_only=True)

    def static_closure(self, names: list[str], keys, why: str, fallback_only: bool):
        """Readers of every value derived at import from `names` (maps/derive.json), transitively."""
        maps = self.maps
        derive = maps.import_readers
        seen_names = set(names)
        seen_stmts: set[tuple[str, str]] = set()
        todo = [(n, keys) for n in names]
        mask = 0
        while todo:
            name, name_keys = todo.pop()
            if name not in names:
                mask |= maps.readers(name)[1]
            for rel, span, only_keys in derive.get(name, ()):
                if (rel, span) in seen_stmts:
                    continue
                if name_keys is not None and only_keys is not None and not any(k.startswith("?") for k in name_keys) \
                        and not (set(name_keys) & set(only_keys)):
                    continue  # that definition looked up other keys only
                seen_stmts.add((rel, span))
                first, _, last = span.partition("-")
                st = maps.statement_at(rel, int(first), int(last))
                if st is None:
                    continue
                module = maps.module_of(rel) or ""
                kind, targets = st[3], st[4]
                derived: list[str] = []
                if kind.startswith("classhdr") and targets:
                    cls = f"{module}:{st[2] + '.' if st[2] else ''}{targets[0]}"
                    info = maps.class_info(cls)
                    derived = [f"{cls}|{a}" for a in (info["set_at_class_creation"] if info else [])]
                else:
                    derived = [f"{module}:{st[2]}|{t}" for t in targets]
                    if not targets or kind.startswith(("expr", "augassign", "cond")):
                        derived += maps.reads_of_statement(rel, int(first), int(last))
                for d in derived:
                    if d not in seen_names:
                        seen_names.add(d)
                        todo.append((d, None))
        if self.an.exact is None:
            self.an.want_exact("definition-time value", const_only=True)
        self.an.exact.names |= seen_names
        if fallback_only:
            self.an.fallback(C.P_VALUE_DERIVED, mask, f"{why}: readers of values derived from it at import")
        else:
            self.an.add(C.P_VALUE_DERIVED, mask, f"{why}: readers of values derived from it at import")

    # ------------------------------------------------------------ imports
    def import_(self, m: Mark):
        an, maps, path = self.an, self.maps, self.path
        info = m.info
        self.exact(f"{path}: import statement {info['op']}")
        gone = list(info["removed"]) + list(info["rebound"])
        if info["op"] == "insert":
            # a new import can take over a name the module already uses (a builtin, another import)
            taken = set(self.old_scope_names(m.scope))
            gone += [n for n in info["added"] if n in taken]
        else:
            gone += [n for n in info["added"] if n in self.old_scope_names(m.scope)]
        def_time_user = False
        for name in sorted(set(gone)):
            mask, at_def = self.az.local_mentions(path, self.old_text, name, (m.lo, m.hi) if m.lo else None)
            an.add(C.P_IMPORT_REBIND, mask, f"{path}: import of `{name}` changed: tests executing lines that mention it")
            def_time_user = def_time_user or at_def
            if not m.scope and name in info["removed"]:
                # other modules may import the name from here
                self.reexport_gone(name)
        broken = self.import_resolves(info["new"]) if info["new"] else None
        if broken:
            an.add(C.P_MODULE_BROKEN, maps.module_tests(self.module) | maps.file_line_tests(path),
                   f"{path}: {broken}: every test that loads the module")
        elif def_time_user or info["added"] or info["rebound"]:
            self.unconfined("import statement changed and the module may fail to import", fallback_only=True)

    def import_resolves(self, bindings: dict) -> str | None:
        """A reason the import certainly fails, judged from the pinned tree alone; None when it may work."""
        for bound, (module, name, level) in bindings.items():
            if level:
                base = self.module.split(".")
                if not self.path.endswith("__init__.py"):
                    base = base[:-1]
                base = base[:len(base) - (level - 1)] if level > 1 else base
                module = ".".join(base + ([module] if module else []))
            top = module.split(".")[0]
            if top in ("sqlglot", "tests"):
                rel = self.module_file(module)
                if rel is None:
                    return f"imports `{module}`, which does not exist"
                if name and name != "*":
                    if self.module_file(module + "." + name) is None and not self.binds(rel, name):
                        return f"imports `{name}` from `{module}`, which does not bind it"
            elif top == "__future__":
                import __future__

                if name and not hasattr(__future__, name):
                    return f"unknown __future__ feature {name}"
            elif top not in _THIRD_PARTY and top not in getattr(sys, "stdlib_module_names", (top,)):
                return f"imports `{module}`, which is not installed"
        return None

    def module_file(self, module: str) -> str | None:
        rel = module.replace(".", "/")
        for cand in (rel + ".py", rel + "/__init__.py"):
            if self.az.src.exists(cand):
                return cand
        return None

    def binds(self, rel: str, name: str) -> bool:
        text = self.az.src.read(rel)
        if text is None:
            return True
        try:
            tree = ast.parse(text.encode("utf-8", "surrogateescape"))
        except SyntaxError:
            return True
        if name in bound_names(tree.body):
            return True
        # star imports, __getattr__, names set through globals(): cannot tell -> assume bound
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                return True
            if isinstance(node, (ast.FunctionDef,)) and node.name == "__getattr__":
                return True
        return False

    def reexport_gone(self, name: str):
        mask, def_modules = self.az.mention_tests(name, skip=self.path)
        # restricted to files that import from this module
        self.an.fallback(C.P_NAME_MENTION, mask, f"`{name}` is no longer importable from {self.module}")

    # --------------------------------------------------------- names, defs
    def name_gone(self, scope: str, name: str, own_range):
        an, maps, path = self.an, self.maps, self.path
        if scope:
            # a method or class attribute: callers were found through the body's lines; other
            # mentions (getattr by name, super calls) are lines that mention the name
            mask, _ = self.az.local_mentions(path, self.old_text, name, own_range)
            an.add(C.P_NAME_MENTION, mask & (maps.class_users(self.definer(scope)) or mask),
                   f"{path}: tests executing lines that mention removed `{scope}.{name}`")
            return
        local, at_def = self.az.local_mentions(path, self.old_text, name, own_range)
        an.add(C.P_NAME_MENTION, local, f"{path}: tests executing lines that mention removed `{name}`")
        everywhere, def_modules = self.az.mention_tests(name, skip=path)
        an.add(C.P_NAME_MENTION, everywhere, f"tests executing lines elsewhere that mention removed `{name}`")
        broken = 0
        for module in def_modules:
            broken |= maps.module_tests(module)
        if at_def:
            broken |= maps.module_tests(self.module)
        # modules that use the name while being imported fail to import; the exact comparison
        # reports them, this is the stand-in when it cannot run
        an.fallback(C.P_MODULE_BROKEN, broken, f"modules that use removed `{name}` at import")

    def new_def(self, scope: str, name: str):
        an, maps, path = self.an, self.maps, self.path
        previous = self.old_def(scope, name)
        if previous:
            an.add(C.P_LINE_HIT, maps.line_tests(path, *previous), f"{path}: a second def {name} replaces the first: tests that ran it")
            return
        if not scope:
            if name in self.old_scope_names("") or name in ("__getattr__", "__dir__"):
                mask, _ = self.az.local_mentions(path, self.old_text, name)
                if name.startswith("__"):
                    mask |= maps.module_tests(self.module)
                an.add(C.P_IMPORT_REBIND, mask, f"{path}: new def {name} takes over a name the module uses")
            else:
                an.add(C.P_NEW_NAME, maps.file_line_tests(path), f"{path}: new function {name}, nothing refers to it")
            return
        cls = self.definer(scope)
        users = maps.class_users(cls)
        info = maps.class_info(cls)
        overridden = 0
        for base in (info["mro"] if info else []):
            base_module, _, base_qual = base.partition(":")
            body = maps.method_body(base_module, f"{base_qual}.{name}")
            if body:
                callers = maps.line_tests(body[0], body[1], body[2])
                overridden |= (callers & users) if users else callers
            _, whole = maps.readers(f"{base}|{name}")
            overridden |= (whole & users) if users else whole
        if overridden:
            an.add(C.P_SIGNATURE, overridden, f"{path}: new {scope}.{name} overrides an inherited one: its users through {scope}")
        class_tests = users
        st = maps.static(path)
        for sc in (st["scopes"] if st else []):
            if sc[0] == scope:
                class_tests |= maps.line_tests(path, sc[2], sc[3])
        if _DISPATCH_NAME.match(name):
            an.add(C.P_NEW_DISPATCH_METHOD, class_tests, f"{path}: new {scope}.{name} may be found by name: tests that used {scope}")
        elif not overridden:
            an.add(C.P_NEW_NAME, class_tests, f"{path}: new method {scope}.{name}, nothing refers to it")

    def finish(self):
        pass


def spell_keys(nodes) -> list[str] | None:
    """Key nodes of a dict/set display in the spelling of maps/reads.json, or None if one cannot be spelled.

    A class key (`exp.DateSub`) is written `<module.Class>` there; the module is not known from
    the source text, so the spelling ends with `.DateSub>` and is matched by suffix (see
    `match_keys`)."""
    out = []
    for node in nodes:
        text = spell_key(node)
        if text is None:
            return None
        out.append(text)
    return out


def spell_key(node) -> str | None:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, str):
            return "'" + v
        if isinstance(v, (bool, int, float)) or v is None:
            return repr(v)
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, (int, float)):
        return repr(-node.operand.value)
    if isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Name, ast.Attribute)):
        owner = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
        # Enum.MEMBER is spelled "Enum.MEMBER"; module.Class is spelled "<pkg.module.Class>"
        return f"?attr:{owner}.{node.attr}"
    if isinstance(node, ast.Tuple):
        parts = [spell_key(e) for e in node.elts]
        if any(p is None or p.startswith("?") for p in parts):
            return None
        return "(" + ",".join(parts) + ")"
    return None

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
from .pydiff import Mark, bound_names, diff_python, parse_source

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

    def test_dependents(self, path: str) -> int:
        """Tests that depend on a Python file of the test tree beyond what line coverage says: the
        tests it defines and, for a package marker or a conftest.py, every test below its directory
        (importing `tests.x` runs tests/__init__.py first; pytest loads a conftest.py for its directory)
        and every test whose file imports a module of that package."""
        maps = self.maps
        mask = maps.tests_in_file(path)
        if os.path.basename(path) in ("__init__.py", "conftest.py"):
            directory = os.path.dirname(path)
            mask |= self.tests_under(directory)
            prefix = directory + "/" if directory else ""
            for rel in self.src.python_files():
                if rel.startswith(prefix) and rel != path:
                    mask |= maps.file_line_tests(rel)
        return mask

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
        new_text = applied.new_text()
        if not diff.broken and not new_text.isascii() and declared_encoding(old_text) != declared_encoding(new_text):
            # Every non-ASCII literal of the file now decodes differently, also in statements the
            # hunks do not touch (those are otherwise taken as unchanged without being compared).
            an.notes.append(f"{path}: source encoding declaration changed and the file holds non-ASCII text")
            if cat == "src":
                module = maps.module_of(path) or ""
                an.add(C.P_MODULE_UNCONFINED, maps.module_tests(module) | maps.file_line_tests(path) | readers,
                       f"{path}: source encoding changed: every test that loads the module")
            else:
                an.add(C.P_TEST_FILE_LEVEL, self.test_dependents(path) | maps.file_line_tests(path) | readers,
                       f"{path}: source encoding changed: its tests and the tests that import it")
        if diff.broken:
            an.notes.append(f"{path}: does not compile after the patch ({diff.broken})")
            if cat == "src":
                module = maps.module_of(path) or ""
                mask = maps.module_tests(module) | maps.file_line_tests(path) | readers
                if path.endswith("__init__.py"):
                    mask |= maps.package_tests(module)
                an.add(C.P_MODULE_BROKEN, mask, f"{path}: no longer compiles: every test that loads the module")
            elif cat == "test" or maps.covered(path):
                mask = self.test_dependents(path) | maps.file_line_tests(path) | readers
                an.add(C.P_TEST_FILE_BROKEN, mask, f"{path}: no longer compiles: its tests and the tests that import it")
            return
        if diff.same_ast:
            an.notes.append(f"{path}: comments / blank lines / layout only (same AST)")
            an.add(C.P_TEXT_READER_COMMENT, readers, f"{path}: read as text by these tests; only comments changed")
            return
        for name in diff.equivalent:
            an.notes.append(f"{path}: def {name} compiles to the same bytecode (only names of locals / layout of constants differ)")
        if not diff.marks:
            an.notes.append(f"{path}: nothing that executes changed (docstrings, `pass`, or equivalent bytecode)")
            an.add(C.P_TEXT_READER_COMMENT, readers, f"{path}: read as text by these tests; only docstrings changed")
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
            ctx.finish()
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

    # --------------------------------------------- who uses a module-level name
    def module_file(self, module: str) -> str | None:
        rel = module.replace(".", "/")
        for cand in (rel + ".py", rel + "/__init__.py"):
            if self.src.exists(cand):
                return cand
        return None

    def _tree(self, rel: str):
        text = self.src.read(rel)
        if text is None:
            return None
        try:
            return parse_source(text, self.src.ast_cache, rel)
        except (SyntaxError, ValueError, RecursionError):
            return None

    @staticmethod
    def _import_source(node: ast.ImportFrom, this_module: str, is_package: bool) -> str:
        """The absolute module an ImportFrom statement imports from."""
        module = node.module or ""
        if not node.level:
            return module
        base = this_module.split(".") if this_module else []
        if not is_package:
            base = base[:-1]
        if node.level > 1:
            base = base[:len(base) - (node.level - 1)]
        return ".".join(base + ([module] if module else []))

    def module_aliases(self, rel: str, depth: int = 0) -> dict[str, str]:
        """{local name: dotted module} for the names a file binds to modules of the pinned tree by
        import statements (anywhere in the file), following `from pkg import alias` a few levels."""
        cache = self.__dict__.setdefault("_alias_cache", {})
        if rel in cache:
            return cache[rel]
        cache[rel] = out = {}
        tree = self._tree(rel)
        if tree is None:
            return out
        for source, names, _lo, _hi in self._name_index(rel)[1]:
            for imported, bound in names.items():
                if imported == "*":
                    continue
                if self.module_file(f"{source}.{imported}"):
                    out[bound] = f"{source}.{imported}"
                elif depth < 3:
                    src_rel = self.module_file(source)
                    target = self.module_aliases(src_rel, depth + 1).get(imported) if src_rel else None
                    if target:
                        out[bound] = target
        for node in ast.walk(tree) if "import " in (self.src.read(rel) or "") else ():
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        out[a.asname] = a.name
                    else:
                        out[a.name.split(".")[0]] = a.name.split(".")[0]
        return out

    def _dotted_module(self, expr, aliases: dict[str, str]) -> str | None:
        if isinstance(expr, ast.Name):
            return aliases.get(expr.id)
        if isinstance(expr, ast.Attribute):
            base = self._dotted_module(expr.value, aliases)
            if base is None:
                return None
            if self.module_file(f"{base}.{expr.attr}"):
                return f"{base}.{expr.attr}"
            base_rel = self.module_file(base)
            return self.module_aliases(base_rel).get(expr.attr) if base_rel else None
        return None

    def _name_index(self, rel: str):
        """Per file, once: its `from x import ...` statements and its attribute reads by attribute name."""
        cache = self.__dict__.setdefault("_name_index_cache", {})
        got = cache.get(rel)
        if got is not None:
            return got
        imports: list = []     # (source module, {imported name: bound name}, first line, last line)
        attrs: dict = {}       # attribute name -> [(expression before the dot, first line, last line)]
        tree = self._tree(rel)
        if tree is not None:
            this_module = self.maps.module_of(rel) or ""
            package = rel.endswith("__init__.py")
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports.append((self._import_source(node, this_module, package),
                                    {a.name: a.asname or a.name for a in node.names}, node.lineno, node.end_lineno))
                elif isinstance(node, ast.Attribute):
                    attrs.setdefault(node.attr, []).append((node.value, node.lineno, node.end_lineno))
        cache[rel] = got = (tree is not None, imports, attrs)
        return got

    def star_importers(self, module: str) -> list[str]:
        out = []
        for rel in self.src.python_files():
            text = self.src.read(rel)
            if not text or "import *" not in text:
                continue
            for source, names, _lo, _hi in self._name_index(rel)[1]:
                if "*" in names and source == module:
                    out.append(rel)
                    break
        return out

    def name_users(self, module: str, name: str, own_rel: str | None = None, _seen: set | None = None) -> tuple[int, set[str]]:
        """Tests that can notice that the module-level `name` of `module` is gone or bound to
        something else, by every spelling the pinned tree uses:

          from module import name      the tests that execute the statement (in a test file: the
                                       tests of every file whose import runs it);
          alias.name / pkg.mod.name    the tests that execute the line, when the object before the
                                       dot is that module;
          "pkg.mod.name" in a string   (mock.patch targets) the tests that execute the line.

        A sqlglot module that imports the name at module level passes the change on (and breaks at
        import if the name is gone): its own users are added, and it is returned in the second
        value.  The module's own file is the caller's business (`own_rel` is skipped)."""
        maps = self.maps
        seen = _seen if _seen is not None else set()
        if (module, name) in seen:
            return 0, set()
        seen.add((module, name))
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        dotted = f"{module}.{name}"
        mask = 0
        def_modules: set[str] = set()
        for rel in self.src.python_files():
            if rel == own_rel:
                continue
            text = self.src.read(rel)
            if not text:
                continue
            star = "import *" in text
            if name not in text and not star:
                continue
            lines = text.split("\n")
            hits = [i for i, line in enumerate(lines, 1) if name in line and pattern.search(line)]
            if not hits and not star:
                continue
            for number in hits:
                if dotted in lines[number - 1]:
                    mask |= maps.line_tests(rel, number, number)
            parsed, imports, attrs = self._name_index(rel)
            if not parsed:
                for number in hits:
                    mask |= maps.line_tests(rel, number, number)
                continue
            this_module = maps.module_of(rel) or ""
            for source, names, lo, hi in imports:
                if source != module or not (name in names or "*" in names):
                    continue
                mask |= maps.line_tests(rel, lo, hi)
                if maps.in_function_body(rel, lo):
                    continue
                if rel.startswith("sqlglot/"):
                    def_modules.add(this_module)
                    bound = names.get(name, name)
                    more, more_modules = self.name_users(this_module, bound, rel, seen)
                    mask |= more | self.local_mentions(rel, text, bound, (lo, hi))[0]
                    def_modules |= more_modules
                else:
                    mask |= maps.tests_in_file(rel)
            if hits and name in attrs:
                aliases = self.module_aliases(rel)
                for value, lo, hi in attrs[name]:
                    if self._dotted_module(value, aliases) == module:
                        mask |= maps.line_tests(rel, lo, hi)
        return mask, def_modules

    def local_mentions(self, path: str, old_text: str, name: str, skip_range=None, skip_ranges=()) -> tuple[int, bool]:
        """Tests executing lines of this file that mention `name`; and whether a definition-time line does."""
        maps = self.maps
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        mask = 0
        def_time = False
        for number, line in enumerate(old_text.split("\n"), 1):
            if skip_range and skip_range[0] <= number <= skip_range[1]:
                continue
            if any(lo <= number <= hi for lo, hi in skip_ranges):
                continue
            if name in line and pattern.search(line) and not line.lstrip().startswith("#"):
                mask |= maps.line_tests(path, number, number)
                if not maps.in_function_body(path, number):
                    def_time = True
        return mask, def_time


_CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def declared_encoding(text: str) -> str:
    """The source encoding Python uses for a file (PEP 263): a coding cookie on the first line, or on
    the second when the first is blank or a comment; a UTF-8 byte order mark; UTF-8 otherwise."""
    import codecs

    lines = text.split("\n", 2)[:2]
    found = None
    for i, line in enumerate(lines):
        line = line.lstrip("\ufeff") if i == 0 else line
        m = _CODING.match(line)
        if m:
            found = m.group(1)
            break
        if line.strip() and not line.lstrip().startswith("#"):
            break
    if found is None:
        return "utf-8"
    try:
        return codecs.lookup(found).name
    except LookupError:
        return "?" + found


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
        self.import_marks: list[Mark] = []

    def net_imports(self):
        """Import statements that were changed, added or removed, netted per scope: reordering
        imports or splitting one statement in two binds the same names to the same things."""
        by_scope: dict[str, list[Mark]] = {}
        for m in self.import_marks:
            by_scope.setdefault(m.scope, []).append(m)
        out = []
        for scope, marks in by_scope.items():
            old: dict = {}
            new: dict = {}
            for m in marks:
                old.update(m.info["old"])
                new.update(m.info["new"])
            ranges = [(m.lo, m.hi) for m in marks if m.lo]
            anchors = [a for m in marks for a in m.info.get("anchors", ())]
            out.append({
                "scope": scope, "old": old, "new": new, "ranges": ranges, "anchors": anchors,
                "removed": sorted(k for k in old if k not in new),
                "added": sorted(k for k in new if k not in old),
                "rebound": sorted(k for k in old if k in new and old[k] != new[k]),
            })
        return out

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

    def own_tests(self) -> int:
        """The tests of this file; for tests/**/__init__.py (and a conftest.py) every test below it."""
        return self.az.test_dependents(self.path)

    def anchors(self, mark: Mark) -> int:
        mask = 0
        for lo, hi in mark.info.get("anchors", ()):
            # the first line of a neighbouring statement is enough: who executes a class body or
            # a module executes all its statement headers
            mask |= self.maps.import_tests(self.path, lo, lo) | self.maps.import_tests(self.path, hi, hi)
            mask |= self.maps.call_tests(self.path, lo, lo)
        if not mask:
            mask = self.own_tests()
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
                    an.add(C.P_TEST_FILE_LEVEL, self.lines(m.lo, m.hi) or self.own_tests(),
                           f"{path}:{m.lo}-{m.hi}: module/class-level statement changed: tests of the files whose import runs it")
        elif kind == "import":
            self.import_marks.append(m)
        elif kind == "moved":
            if m.info["is_def"] or m.info.get("is_import"):
                an.add(C.P_NEW_TEST, self.lines(m.lo, m.hi) or self.own_tests(),
                       f"{path}: a def, class or import moved inside its file")
            else:
                an.add(C.P_TEST_FILE_LEVEL, self.maps.import_tests(path, m.lo, m.hi) or self.own_tests(),
                       f"{path}: a module/class-level statement moved; later statements may depend on it")
        elif kind == "defhdr":
            lo, hi = m.info["body"]
            if not (m.info["annotations_only"] and self.diff.future_annotations):
                an.add(C.P_TEST_BODY, self.lines(m.lo, hi), f"{path}: def {m.info['name']} header changed: tests that run it")
            if m.info["name_changed"]:
                self.name_gone(m.info["name"], (m.lo, hi))
                self.new_def(m.scope, m.info["new_name"], self.lines(m.lo, hi) or self.own_tests())
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
            an.add(C.P_TEST_FILE_LEVEL, mask or self.own_tests(), f"{path}: module/class-level block changed")
        else:
            an.add(C.P_TEST_FILE_LEVEL, self.own_tests() | self.maps.file_line_tests(path), f"{path}: {m.info.get('why', kind)}")

    def finish(self):
        an, path = self.an, self.path
        for net in self.net_imports():
            if not (net["removed"] or net["added"] or net["rebound"]):
                an.notes.append(f"{path}: imports reordered or regrouped, same bindings")
                continue
            mask = 0
            for lo, hi in net["ranges"]:
                mask |= self.lines(lo, hi)
            for lo, hi in net["anchors"]:
                mask |= self.maps.import_tests(path, lo, lo) | self.maps.import_tests(path, hi, hi)
            an.add(C.P_TEST_FILE_LEVEL, mask or self.own_tests(),
                   f"{path}: imports changed ({', '.join(net['removed'] + net['added'] + net['rebound'])}): "
                   "tests of the files whose import runs them")
            for name in net["removed"] + net["rebound"]:
                used, _ = self.az.local_mentions(path, self.old_text, name, skip_ranges=net["ranges"])
                an.add(C.P_TEST_FILE_LEVEL, used, f"{path}: tests executing lines that mention `{name}`")

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
            self.import_marks.append(m)
        elif kind == "moved":
            # same statement, other position: values and imports are checked by importing the tree
            self.exact(f"{path}:{m.lo}: definition-time statement moved")
            self.unconfined("a module/class-level statement moved", fallback_only=True)
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
        self.implicit_consumers(m, targets)
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

    def class_range_tests(self, scope: str) -> int:
        """Tests that used the class `scope` of this file (or a subclass), or ran a line of its body."""
        maps = self.maps
        mask = maps.class_users(self.definer(scope))
        st = maps.static(self.path)
        for sc in (st["scopes"] if st else []):
            if sc[0] == scope:
                mask |= maps.line_tests(self.path, sc[2], sc[3])
        return mask

    def implicit_consumers(self, m: Mark, targets: list[str]):
        """Values that are used without ever being loaded by name, so maps/reads.json cannot know
        their readers: `__slots__`, `__match_args__`, `__hash__ = None` ... are consumed by the
        interpreter when the class is created or an instance is used; `__all__` by `import *`; the
        body of a decorated class (a dataclass) or of a NamedTuple / TypedDict by whatever builds
        the real class from it.  Those reach every test that uses the class (or loads the module)."""
        an, maps, path = self.an, self.maps, self.path
        dunder = [t for t in targets if t.startswith("__") and t.endswith("__") and t != "__doc__"]
        if m.scope:
            transformed = self.class_is_transformed(m.scope)
            if dunder or transformed:
                what = "/".join(dunder) if dunder else "a field"
                why = "consumed when the class is created" if dunder else "the class is built from its body by a decorator or a special base"
                an.add(C.P_CLASS_USER, self.class_range_tests(m.scope),
                       f"{path}: {what} of {m.scope} changed ({why}): tests that used the class")
            return
        for t in dunder:
            if t == "__all__":
                mask = 0
                for rel in self.az.star_importers(self.module):
                    mask |= maps.module_tests(maps.module_of(rel) or "") | maps.file_line_tests(rel) | maps.tests_in_file(rel)
                an.add(C.P_VALUE_READ, mask, f"{path}: __all__ changed: tests that load a file doing `from {self.module} import *`")
            else:
                self.unconfined(f"module-level {t} changed (read by the interpreter, not by code)")

    def class_is_transformed(self, scope: str) -> bool:
        body = self.diff.old_tree.body
        node = None
        for part in scope.split("."):
            node = next((s for s in body if isinstance(s, ast.ClassDef) and s.name == part), None)
            if node is None:
                return False
            body = node.body
        if node.decorator_list:
            return True
        for base in node.bases:
            text = ast.unparse(base)
            if text.rsplit(".", 1)[-1] in ("NamedTuple", "TypedDict", "Protocol"):
                return True
        return False

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
    def import_(self, net: dict):
        an, maps, path = self.an, self.maps, self.path
        self.exact(f"{path}: import statements changed")
        if not (net["removed"] or net["added"] or net["rebound"]):
            an.notes.append(f"{path}: imports reordered or regrouped, same bindings")
            return
        scope = net["scope"]
        future = {k for side in ("old", "new") for k, v in net[side].items() if v[0] == "__future__"}
        if future & (set(net["removed"]) | set(net["added"])):
            # `from __future__ import annotations` (or another feature) switched on or off: the whole
            # file compiles differently.  With annotations no longer deferred, a def nested in a
            # function evaluates its annotations when it runs, and may meet names that only exist
            # for type checkers; module-level and class-level defs are covered by importing the tree.
            an.add(C.P_IMPORT_REBIND, maps.file_line_tests(path), f"{path}: __future__ import changed: tests that execute code of the file")
            net = dict(net, removed=[n for n in net["removed"] if n not in future], added=[n for n in net["added"] if n not in future])
        gone = list(net["removed"]) + list(net["rebound"])
        # a new import can take over a name the module already uses (a builtin, another binding)
        taken = set(self.old_scope_names(scope))
        gone += [n for n in net["added"] if n in taken]
        def_time_user = False
        for name in sorted(set(gone)):
            mask, at_def = self.az.local_mentions(path, self.old_text, name, skip_ranges=net["ranges"])
            an.add(C.P_IMPORT_REBIND, mask, f"{path}: import of `{name}` changed: tests executing lines that mention it")
            def_time_user = def_time_user or at_def
            if not scope and (name in net["removed"] or name in net["rebound"]):
                # other files may import the name from here, or read it as an attribute of the module
                self.reexport_gone(name)
        broken = self.import_resolves(net["new"]) if net["new"] else None
        if broken:
            an.add(C.P_MODULE_BROKEN, maps.module_tests(self.module) | maps.file_line_tests(path),
                   f"{path}: {broken}: every test that loads the module")
        elif def_time_user or net["added"] or net["rebound"]:
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
        """A module-level name bound by an import is gone or bound to something else.  maps/reads.json
        does not track imported names, so their users are found in the pinned source."""
        maps = self.maps
        mask, def_modules = self.az.name_users(self.module, name, self.path)
        self.an.add(C.P_NAME_MENTION, mask, f"`{name}` of {self.module} changed: tests executing code that imports or reads it")
        broken = 0
        for module in def_modules:
            broken |= maps.module_tests(module)
        # a sqlglot module that imports the name while being imported breaks: the import of the
        # patched tree reports it; this is the stand-in when that cannot run
        self.an.fallback(C.P_MODULE_BROKEN, broken, f"modules that import `{name}` from {self.module} at import")

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
        for net in self.net_imports():
            self.import_(net)


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

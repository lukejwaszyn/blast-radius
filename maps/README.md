# maps/

Facts about sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`,
measured once by running its own test suite under tracers (`tools/`). Nothing
here was derived from any patch: the maps say what each test executes, reads
and opens at the pinned commit, and what the source looks like. Regenerate with
`tools/build_maps.sh` (see `tools/README.md`).

All files are plain JSON, a few MB together, each loads with `json.load` in
5-25 ms.

## Conventions

- **Test numbers** are positions in `tests.json` → `ids`. That list is the kit's
  `tests.json` at build time. A consumer must translate through the test **id**
  to the numbers of the `/data/tests.json` it is given.
- **Test sets** are strings of comma-separated ranges: `"0-5,9,12-40"`
  (`tools/mapfmt.py`: `dec`). Maps with many sets keep them once in a `sets`
  list and refer to them by index.
- **Paths** are relative to the sqlglot checkout. **Lines** are 1-based lines
  of the pinned files, so a patch's *old* line numbers index the maps directly.
- A **definer** is `module:QualName` for a class (`sqlglot.dialects.tsql:TSQL`,
  `sqlglot.dialects.snowflake:Snowflake.Tokenizer`) and `module:` for a module.
  A **name** is `definer|ATTR`.

## How a test was observed

One pytest process per test file; inside it every test ran **in its own fork**
taken right after the file was imported. So each test is recorded as if it ran
alone: a dialect imported lazily, a cache filled, a `setUpClass` executed by an
earlier test cannot hide work from a later one. `setUpClass`/`setUp`/teardown
run inside the fork and count as part of the test. Subprocesses a test starts
(the `spawn` process pools of `test_optimizer.py` and `test_executor.py`,
`python -c` in `test_lazy_load`) are traced too and merged into the test.
Each pass ran twice (PYTHONHASHSEED 0 and 1) and the runs are united.

Execution is split in two kinds everywhere:

- **call time**: lines inside function (and lambda) bodies that ran while the
  test itself ran;
- **definition time**: module-level and class-level statements, and whatever
  they call (metaclasses, `__init_subclass__`, factories such as
  `_show_parser(...)` called in a class body). They run when a module is
  imported, once per process, whoever triggers the import. A test "executes"
  them only in the sense that it loads the module, so they are **not** in the
  per-test line map; they are covered by `static.json` + `reads.json` +
  `defdeps.json` + `modules.json`.

## Files

### tests.json (f)
`ids`, `seconds` (the kit's recorded seconds), `files` + `file_of` (test file
of each test), `lines` (`[first, last]` of the test function in its file,
decorators included), `skipped`, `alone_seconds` (wall time of the test alone
in its fork on the build machine, class-level set-up included; only useful to
see which tests are expensive to run one by one).

### cov.json (a) — per-test line coverage, call time
`files[path]` is a list of runs `[first_line, last_line, set]`: every line in
the run was executed, during the test or its set-up/teardown or in one of its
subprocesses, by exactly the tests of `sets[set]`. Covers `sqlglot/`, `tests/`
and `sqlglotc/`. Lines not listed were executed by no test at call time.
`subprocess_tests`: tests that ran part of their work in subprocesses.

A line is reported only if bytecode starts on it. For an edit in the middle of
a multi-line statement look up **all lines of the statement**
(`static.json` → `spans`).

The first line of a function or lambda is also recorded whenever it is
**entered**. `sys.monitoring` reports a line only when the line changes inside
a code object, so without this a call of a one-line lambda
(`"WEEKOFYEAR": lambda args: exp.WeekOfYear(...)`, of which sqlglot's dialect
tables hold about 1500) or of a one-line `def` would leave no trace. The line
of a class-level dict entry that holds such a lambda therefore lists the tests
that *called the lambda*; that the line also ran once at import is definition
time and is not in this map.

### filecov.json — what importing a test file executes
Same layout, but sets are sets of **test files** (`test_files`): the lines of
`tests/**` that run when the test file is imported (its own module level, class
bodies, `tests/helpers.py`, the `Validator` base class in
`tests/dialects/test_dialect.py` ...) and the function lines of `sqlglot/` that
ran on behalf of the test module's own top-level code. All tests of those files
depend on such a line.

### static.json (b, e) — AST description of every Python file
Per file: `module`, `lines`, and

- `scopes`: `[qualname, "class", first, last, [bases], [keywords]]`;
- `funcs`: `[qualname, first(decorators), def_line, body_first, last, owner_scope, kind, cols]`,
  kind `def` or `lambda`. A line inside `[body_first, last]` of a `def` is call
  time. For a lambda `cols = [body_line, body_col, end_line, end_col]`: the part
  of the line(s) that is call time; the rest of the line is definition time
  (e.g. the key of a class-level dict entry);
- `stmts`: definition-time statements
  `[first, last, owner_scope, kind, [targets], entries]`. `owner_scope` is `""`
  (module) or the class qualname. Kinds: `assign`, `annassign`, `augassign`,
  `annotation`, `import`, `importfrom`, `expr`, `doc` (docstring), `cond`
  (header of a module/class-level `if`/`try`/`for`/`with`), `classhdr`
  (the `class X(Base):` header), `defhdr` (decorators + signature with default
  values of a function defined in that scope). A trailing `?` means "inside a
  conditional block", `+nested` means the display found is nested in a call.
  `entries` (for dict/set/list/tuple displays) lists the elements:
  `[first, last, key_source, "kv" | "spread" | "el"]`, so a changed line maps to
  the dict key it belongs to (`"'GROUPS FOR USER'"`, `exp.DateSub`, ...);
- `spans`: `[first, last]` of every multi-line statement or statement header.

Test files are described the same way (classes, bases such as `Validator`,
test functions).

### defdeps.json (b) — function lines that run at import
`deps[path][line]` → index into `site_sets`: the definition-time lines
(`"path:line"`) on whose behalf that function line ran while sqlglot was being
imported (innermost module/class-level frame below it). Example: the lines of
`_show_parser` ← every `SHOW_PARSERS` entry that calls it; the lines of
`_Dialect.__new__` ← every dialect's `class` statement. Editing such a line is
equivalent to editing those definition lines (in addition to whatever
`cov.json` says about call-time use of the same line).
`ran[path]`: all lines executed by importing every sqlglot module. A
definition-time line that is not in `ran` never runs (e.g. under
`if t.TYPE_CHECKING:`).

### reads.json (c) — who reads which class-level / module-level value
Built by running the suite on a rewritten copy of sqlglot in which every load
of a tracked name reports itself (`tools/instrument.py`,
`tools/tracer/blast_reads.py`). Tracked names: every name bound by an
assignment in a class body or at module level anywhere in sqlglot (`FUNCTIONS`,
`TRANSFORMS`, `WEEK_OFFSET`, `arg_types`, enum members, module constants ...),
plus names a metaclass / `__init_subclass__` sets on classes. Loads in the test
files themselves are tracked too.

A read is charged to the **definer**: the class in whose `__dict__` the value
actually lives, found through the MRO of the object it was read from. A read of
`self.dialect.WEEK_OFFSET` on a `Fabric` instance is charged to `TSQL` if `TSQL`
defines it and `Fabric` does not; a read through `Dialect`'s default is charged
to `Dialect`. This is what makes "edit in class C → tests that used C's value,
directly or through a subclass" exact without enumerating subclasses.

`reads[name]` = `{"any": set, "whole": set, "keys": {key: set}}`

- `any`: tests that read the value at all (call time; reads made by importing
  the test file count for every test of the file);
- `keys`: tests that looked up that key directly (`X.NAME[k]`, `X.NAME.get(k)`,
  `k in X.NAME`). Keys are written `'text` for strings, `TokenType.COMMA` for
  enum members, `<module.Class>` for classes, `(a,b)` for tuples;
- `whole`: tests that took the value as a whole (passed it on, iterated,
  copied, used a scalar). A test not in `whole` only used the keys it is
  listed under.

`used_classes[definer]`: tests in which a tracked name was read from an object
of exactly that run-time class (or from the class itself). Use it with
`classes.json` → `mro` for "tests that used C or a subclass".
`read_at_test_file_import`: names read while importing each test file.

Not tracked: names bound by `def` (their bodies are in `cov.json`), reads made
by C code, and `vars(cls)` / `cls.__dict__` (in sqlglot only the dialect
metaclass and `Tokenizer.__init_subclass__` do that, at import time).

### derive.json (b, c) — what definition-time statements read
`reads_at_import[path]["first-last"]` = names (or `[name, key, ...]` when only
keys were looked up) read while that definition-time statement was being
evaluated during import: `SingleStoreParser.SHOW_TRIE` reads
`SingleStoreParser|SHOW_PARSERS`; a subclass's `FUNCTIONS = {**Parser.FUNCTIONS, ...}`
reads `Parser|FUNCTIONS`; a dialect's `class` statement (kind `classhdr`) lists
what the metaclass read to build `TIME_TRIE`, `INVERSE_TIME_MAPPING`,
`tokenizer_class`... A changed class-level value therefore also changes the
targets of every statement listed as reading it, transitively.

### classes.json (c)
`classes[definer]` = `{"mro": [sqlglot bases, nearest first], "meta": metaclass,
"attrs": {name: kind}, "set_at_class_creation": [names]}` for every class in
sqlglot. `attrs` are the data attributes in the class's own `__dict__` with a
value kind (`bool`, `int`, `str`, `dict:42`, `set:7`, `callable`, `class`, ...);
`set_at_class_creation` are those not written in the class body (put there by a
metaclass, `__init_subclass__` or a decorator).
`module_data[module]`: the same for module-level data names.

### modules.json (e)
`modules` (+ `path`), then, as module-index range strings:
`imports_static[m]` (imports written in the file), `loads_alone[m]` (modules in
`sys.modules` after importing only `m` in a fresh process: sqlglot imports its
dialects lazily, so this is much less than "everything"), `test_file_loads[file]`
(loaded by importing the test file), `test_lazy_loads[test]` (additionally
loaded while the test ran), `test_subprocess_loads[test]`.
`loaded_by_tests[m]`: test set that has `m` loaded by any of those routes — the
coarse fallback for "anything in this file changed" and the exact answer for
"this module no longer imports".

### opens.json (d)
- `by_tests[path]`: tests that opened the data file (set-up and subprocesses
  included). One optimizer fixture is consumed whole by one test function.
- `consumers[path]`: narrower set for fixtures that `setUpClass` loads into a
  class attribute (`cls.tpch_sqls = list(load_sql_fixture_pairs(".../tpc-h.sql"))`):
  all 60 executor tests open the file, only the tests whose executed test-file
  lines mention that attribute consume it. `why` names the assignment.
- `py_read_as_data[path]`: Python files a test reads as **text** instead of
  importing them (`test_typing.py::test_no_redundant_expression_metadata` reads
  every `sqlglot/typing/*.py`; `test_sqlglotc.py` runs `sqlglotc/setup.py`).
  Any edit of such a file, comments included, can reach those tests.
- `dirs_listed[path]`: directories a test lists (a new or removed file there
  can matter).
- `by_test_file_imports[path]`: data files read while a test file is imported.
  `pyproject.toml` and `setup.cfg` are read by pytest itself when it starts, for
  every test file: a change of pytest configuration there reaches every test.

### warm.json — lines a test reaches only through what earlier tests left behind
`cov.json` describes every test alone. A real run keeps one process for many
tests, and a test can leave state behind that sends a later test down a path it
never takes alone. At the pinned commit `tests/test_dialect_entry_points.py`
clears the dialect registry (`Dialect._classes.clear()`) in `setUp` and
`tearDown`; every later test in the same process that looks up a dialect whose
module is already imported then runs the "class not registered" branch of
`Dialect._try_load`, which no test executes alone.

`tools/warm_trace.py` runs the whole suite in **one** process under the same line
tracer, in several orders (collection order, reversed, two shuffles), and keeps
every (line, test) pair that `cov.json` and `filecov.json` do not have.
`lines[path][line]` → index into `sets`: the tests seen on that line in any of
the runs **united with every test that executes the enclosing function alone**
(which tests come after the state-leaving test depends on the order of the run,
so any caller of the function is a candidate). `observed[path][line]` keeps the
tests actually seen, `orders` the runs and how many pairs each found. The file
is tiny (a few dozen lines of sqlglot). Outside the collection order a handful
of tests fail at the pinned commit itself for the same reason; their traces are
used all the same.

`blast/maps.py::call_tests` unites `cov.json` with this map, so "a line no test
executed selects nothing" stays true only for lines that no test executes in a
shared process either.

## From a patch to candidate tests

The order in which `tools/verify_samples.py` consults the maps (it is a checker,
not the selector, but it is the reference for how the maps fit together):

1. **Not a Python file**: `opens.json` (`consumers`, else `by_tests`, plus all
   tests of the files in `by_test_file_imports`). Nobody opens it → nothing.
2. **Python file, same AST before and after** (comments, blank lines, layout):
   nothing, except the tests in `opens.json` → `py_read_as_data` for that file.
3. **Changed line inside a function body** (`static.json` → `funcs`; for a
   lambda, the columns of its body): `cov.json` ∪ `filecov.json`, looked up over
   the whole statement (`spans`). No test → the line never ran → nothing, as
   long as the edit keeps the file's structure.
   If `defdeps.json` lists the line, it also ran at import: treat the listed
   definition lines as changed too (step 4).
4. **Changed definition-time line** (`static.json` → `stmts`): the tests that
   read what the statement binds. Exact way: import the patched tree, fingerprint
   every class-level / module-level value (`tools/snapshot.py`), compare with
   the pinned tree; the changed names and dict keys, derived values included,
   are looked up in `reads.json` (`keys` + `whole`, or `any`). A module that no
   longer imports → `modules.json` → `loaded_by_tests`. Static way, without
   importing: `reads.json` for the statement's targets, closed over
   `derive.json`; it is sound but coarse once a value feeds other definitions.
5. **Anything else** (new or deleted file, an edit the AST comparison cannot
   confine to statements): `modules.json` → `loaded_by_tests`, `opens.json` →
   `dirs_listed`.

## What the sample set says

`tools/verify_samples.py` replays the kit's 30 sample patches against the maps
(results in `VERIFY.md`): every failing test is inside the candidate set, and
the candidate sets are small.

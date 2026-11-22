# Blast Radius

Given a patch against [sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, print which of its 1620 tests to
run and how to split them over up to 8 workers, so that no failing test is
left out and as little as possible is run.

```sh
make sample    # build the image, run it on sample/ under the scorer's limits, score the output
python3.13 -m unittest discover -s unit  # parser, packing, AST comparison, selection (no Docker needed; BLAST_SRC=<pinned sqlglot checkout> for the tests that read it)
```

## Method

The selector never looks a patch up. It reads the diff, works out *what kind
of thing changed* from the pinned source, and asks maps of the pinned commit
*which tests touch that thing*. The maps (`maps/`, built by `tools/` from the
pinned commit alone, formats in `maps/README.md`) record for every test the
lines it executed, the class-level and module-level values it read, the
modules it loaded and the files it opened.

1. **Read the diff** (`blast/diffparse.py`). Several files, new, deleted,
   renamed and copied files, mode-only changes, binary patches, missing final
   newlines, zero-length ranges, CRLF, quoted paths. The hunks are applied in
   memory to the pinned file, so the selector has the old text, the new text
   and the mapping between their lines.

2. **Python files: compare the two syntax trees** (`blast/pydiff.py`).
   - Equal trees: only comments, blank lines or layout changed. Nothing can
     run differently. (A test that reads the file as *text* and parses it
     itself is another matter: `tests/test_typing.py` does that with
     `sqlglot/typing/*.py`, and a byte order mark that the interpreter skips
     makes its `ast.parse` fail. Such readers are in `maps/opens.json`.) (A "comment" inside a string
     or a re-indented line are not equal trees and are treated as code.)
     One comment is not a comment: a coding cookie
     (`# -*- coding: latin-1 -*-`) changes how every non-ASCII literal of the
     file is decoded, also in statements the hunks do not touch. If the
     declared encoding changes and the file holds non-ASCII text outside
     comments and stand-alone strings, every test that loads the module is
     selected.
   - The new text does not compile (syntax, indentation, tabs, encoding,
     `return` at class level ...): the module cannot be imported, so every
     test that loads it fails (`maps/modules.json`).
   - Otherwise the walk reports which statements changed, vanished or
     appeared, and for each whether it runs **at call time** (inside a
     function or lambda body) or **at definition time** (module level or class
     body, i.e. once, when the module is imported).

3. **Call-time code → line coverage** (`maps/cov.json`, `maps/filecov.json`).
   The tests that executed any line of the changed statement (the whole
   statement, because a changed operator in the middle of a multi-line
   condition changes which later lines are evaluated; only the header for an
   `if`/`for`/`while`/`with`). A pure insertion takes the tests that executed
   the statements around it. A line no test executed selects nothing. The body
   of a lambda that sits in a class-level table (`"CHR": lambda args: ...`) is
   call-time code too: its line lists the tests that *called* it. (The maps
   record function entry as well as line events for exactly this case: a
   one-line lambda never produces a line event when called.)
   A statement can also change what *other, untouched* lines of its function
   do: an assignment makes its target a local of the whole function (every
   earlier read of the global of that name now raises `UnboundLocalError`), a
   `yield` turns the function into a generator, a `global` declaration
   redirects every use of the name. The changed line need not even run. The
   old and the new function are compiled and the scope of every name and the
   kind of function compared (`blast/pydiff.py::scope_shift`); if they differ,
   every test that runs any line of the function is a candidate.
   Coverage is recorded for each test alone; `maps/warm.json` adds the few
   lines that a test reaches only through state an earlier test left behind
   (a test file clears the dialect registry in its `setUp`), found by
   running the whole suite in one process in eight orders. For those lines
   every test that runs the enclosing function is a candidate.

   **A comparison operator replaced by its neighbour** (`is` → `==`,
   `is not` → `!=`, `<` → `<=`, `<=` → `<`, `>` → `>=`, `>=` → `>`) is the one
   edit whose effect was measured ahead for every test: `maps/cmp.json` holds,
   for each such comparison inside a function of sqlglot, the tests in which
   the neighbouring operator gave another result at least once on the operands
   the test produced. When that operator is the only thing a statement
   changes, a test outside the set computes the same value at that place every
   time, so it runs exactly as before; only the tests inside the set are taken
   as hit. (The others keep a 0.5 % weight: they ride along only when a worker
   is open anyway.) A line that also runs while sqlglot is imported is pruned
   this way only if no process of the recording saw a difference at all.

4. **Definition-time code → who reads the value.** A class-level edit such as
   `WEEK_OFFSET = -1` is executed by every test that imports the module, and
   matters only to the tests that read that value. `maps/reads.json` knows
   them, down to the dict key when the edit is one entry of a table
   (`FUNCTIONS["CHR"]`). Values are also copied, inverted and compiled into
   tries by other definitions while sqlglot is imported, so the selector
   **imports the patched tree** in a child process (an overlay of symlinks in
   `/tmp`, about half a second of CPU), fingerprints every class-level and
   module-level value and compares with the fingerprint of the pinned tree
   taken when the image was built (`blast/exact.py`, `tools/snapshot.py`).
   The values and keys that really differ are looked up in `reads.json`; a
   module that now fails to import is looked up in `modules.json`. No test is
   run. If the import cannot be done, the static closure over
   `maps/derive.json` is used instead; it is sound and much coarser.

   `reads.json` only knows names that are bound by an assignment and loaded
   by name. An empty answer for anything else does not mean "nobody uses
   it", so those are handled from the source:
   - a name bound by an **import** (a re-export such as `sqlglot.to_table`)
     that is removed or now names another object: the tests that execute a
     `from module import name`, an `alias.name` whose alias is that module,
     or a string `"pkg.module.name"` (mock.patch targets), followed through
     modules that re-export it (`blast/analyze.py::name_users`). The import
     comparison reports removed, rebound and added names separately; a
     function whose body changed keeps its identity and is left to coverage;
   - values the **interpreter consumes** without a load (`__slots__`,
     `__match_args__`, `__hash__ = None`), and the body of a class that a
     decorator or a special base turns into something else (`@dataclass`,
     `NamedTuple`, `TypedDict`): the tests that used the class or ran a line
     of it; `__all__`: the tests loading a file that star-imports the module;
   - `from __future__ import annotations` removed: annotations of a def
     nested in a function are evaluated when it runs, so the tests that run
     such a def (module-level ones show up in the import comparison).

   If `import sqlglot` itself fails in the patched tree (a new
   `sqlglot/_version.py` that raises: `sqlglot/__init__.py` imports it inside
   `try/except ImportError`), the comparison says so and every test is
   selected; a new module is always checked this way.

   Edits that provably change nothing are recognised as such: a function
   whose old and new versions compile to the same bytecode (renamed locals,
   constants the compiler folds, annotations of locals) selects nothing;
   import statements are netted per scope (reordering or splitting them binds
   the same names); a definition that reappears unchanged elsewhere in its
   class or module is a move; a docstring or `pass` added inside a function
   executes nothing. For the definition-time ones the import comparison still
   runs.

   **Writes to shared state.** Line coverage says who runs a statement. A
   statement that writes to something the whole process shares (an attribute
   or item of an imported module or class, a module global, the environment,
   a registry it pops from; `blast/state.py`) also reaches every test that
   runs *after* it. In sqlglot's own functions an existing write is already
   covered (coverage was recorded for each test alone: a test that reads such
   state finds it empty and writes it itself), so only a write through a
   target the function did not write before selects every test. In test files
   any added, removed or changed write of that kind does (all test files are
   imported into the process before the first test runs, and a test's
   leftovers are seen by the tests after it), and so does a new test file that
   contains one.

5. **Test files** are handled by the same two questions, because coverage was
   recorded for `tests/**` as well: a changed test body or helper selects the
   tests that executed it; a changed module-level or class-level line selects
   the tests of every file whose import executes it (so an edit to
   `tests/helpers.py` or to the `Validator` base class reaches its users). A
   new test function with a fresh name selects nothing: it has no number.
   `tests/__init__.py` and `tests/dialects/__init__.py` are empty at the pinned
   commit, so nothing has coverage in them; code added there (or a syntax
   error) reaches every test below the package, because importing `tests.x`
   runs them first.

6. **Data files** → the tests that open them (`maps/opens.json`). For
   `tests/fixtures/**/*.sql` the selector applies the fixture loader's own
   filter (`tests/helpers.py::_filter_comments` drops empty lines and lines
   starting with `--`): if the filtered text is unchanged no test can see the
   edit; a changed `# title:` / `# dialect:` line or SQL comment is a real
   change. `pyproject.toml`, `setup.cfg`, `pytest.ini`, `.pytest.ini`,
   `pytest.toml`, `.pytest.toml` (pytest 9), `tox.ini` and `conftest.py`, at
   the root or in `tests/` (pytest looks in the common ancestor of its
   arguments, and the labeling command is not known), are what pytest reads at
   start-up: a file that no longer
   parses or a changed pytest section reaches every test, other metadata
   reaches none. Docs, CI files and files no test opens select nothing.

7. **New, deleted and renamed files**: a deleted or renamed module breaks the
   tests that load it; a deleted fixture breaks the tests that open it; a new
   file matters if it shadows an importable module (the checkout is on
   `PYTHONPATH`; a package directory wins over a module file, and an
   extension module `x.so` / `x.abi3.so` is found before `x.py`), is picked
   up by pytest (`conftest.py`, a configuration file, a `test_*.py` that
   writes shared state), or lands in a directory some test lists.

8. **Choose by expected cost** (`blast/choose.py`). Every group of candidates
   carries the probability that a test in it fails, given only the *reason*
   it was selected. A group is run when `P × 300 s` exceeds what it adds to
   the commit's cost. In practice everything directly implicated is run, and
   the rule only removes the long shots: the one test that parses a file
   whose comments changed is not worth opening a worker for, but rides along
   for 11 ms when a worker is open anyway.

9. **Pack** (`blast/pack.py`): for each worker count the cost is at least
   `max(longest test, total / k) + 2 s × k`; longest-first packing, refined
   by a capacity search, reaches that bound on this suite.

10. **Run the candidates when that costs less than it saves**
   (`blast/verify.py`, `blast/runner.py`; `BLAST_VERIFY=off` turns it off).
   The steps above say which tests *could* fail. A commit whose candidates all
   pass needs no worker at all, and five of the twelve sample `code` commits
   with a non-empty selection are of that kind. Before answering, the
   selector therefore runs a commit's candidates on the patched code when

       P(no candidate fails) × cost of the selection − CPU of the run > 0.25 s

   (CPU is charged second for second, so only small sets of quick tests
   qualify) and drops the ones that ran to their end and passed, provided
   that closes a worker. How it runs them:
   - one long-lived process imports the standard library modules the tests
     need (and numpy, pandas, duckdb when a test file needs them) and forks a
     child per commit;
   - the patched files are written into a real copy of the checkout in `/tmp`
     and the pinned ones put back afterwards, time stamps included, so the
     compiled files of the pinned commit keep serving every untouched module;
   - the child imports sqlglot and the test files *fresh* from that tree and
     runs the candidates in pytest's order with `unittest` itself (sqlglot's
     tests are plain `unittest.TestCase`s; no pytest is installed), stopping a
     test at its first failed subtest and the whole run at the first test over
     its time limit.

   Only a "pass" from a run that finished with no error outside a test
   (`setUpClass`, `tearDownClass`, class clean-ups) lets a test go. A commit
   keeps its static selection untouched when a test's result in a process of
   its own would not settle what the labeling run saw:
   - the patch is not one the runner reproduces byte for byte (binary parts,
     mode changes, a hunk that needs fuzz), or it touches anything but Python
     files of `sqlglot/` and existing ones of `tests/` and fixtures:
     `conftest.py`, configuration, new test files, a deleted package marker
     and extension modules change what pytest itself does;
   - the selection is there because of state shared between tests: a write to
     shared state (`blast/state.py`), a line of `maps/warm.json`, or anything
     in or read by the functions around those lines (the dialect registry,
     which one test file empties, and `Expression.__deepcopy__`);
   - a candidate is a test the runner does not judge (`maps/runner.json`,
     built by running every test of the pinned commit alone through the same
     runner): the 24 that start other processes (a `spawn` process pool makes
     every worker re-import sqlglot, pandas and duckdb, about 2 s of CPU each),
     the 3 long ones, and any that would not pass alone (there is none).

11. **The two TPC fixtures are triples, and some tests use only a part**
   (`blast/analyze.py::pair_fixture`). `tpc-h.sql` and `tpc-ds.sql` hold
   `(options, sql, expected)` triples. `TestExecutor.setUpClass` loads both for
   all 59 executor tests; `test_execute_tpch` / `test_execute_tpcds` then use
   the options and the sql of their own file and never the expected output
   (`for args, sql, _ in sqls`), while the optimizer's `test_tpch` /
   `test_tpcds` and `test_optimized_tpch` use everything. The selector runs
   the loader's own logic (`tests/helpers.py::load_sql_fixture_pairs`, twelve
   lines, ported; the image build checks the pinned source lines the rule was
   read from) on the old and the new text:
   - the loader raises (a `#` line without a colon): the class set-up fails,
     every test that loads the file is selected;
   - the triples are the same (blanks after a `;`): nothing is selected;
   - only expected outputs differ: the executor's test of that file is left
     out, and so is the other file's (`maps/opens.json` lists both under both
     files because one source line names both lists). An edit to an expected
     output of `tpc-ds.sql` then costs 4.2 s (`test_tpcds`) instead of 25.5 s.

Every probability, threshold and limit is a named constant in
`blast/config.py`. Anything the analysis cannot read (a hunk that does not
fit the pinned file, an exception in the analysis itself) falls back to the
tests that could depend on the file, or to the whole suite; the selector
always prints one line per commit.

## What it costs on the sample set

`make sample`: **106.2 simulated seconds, 0 misses** on the 30 sample commits
(94.4 s is the cost with perfect knowledge; the static selection alone,
`BLAST_VERIFY=off`, gives 112.5 s, and it was 165.2 s before `maps/cmp.json`).
CPU for the batch, measured natively on a loaded arm64 laptop: 0.8 s for
everything static plus 0.5 to 0.9 s for each import comparison, 4 to 6 s in
total (with `BLAST_SNAPSHOT=needed`, import only when the static answer is not
confined to constants or is expensive, about half of that and the same
selection on this set), plus 1.3 s for running candidates (next section).

| commits | selected / failing | note |
| --- | --- | --- |
| 8 fixture, 3 multi | 1/1 each (986c 2/1, rszg 4/3) | |
| 3 comment | 0/0 | |
| ary6 10/10, 3utz 37/37, 6ze8 1/1, j6uz 2/2, 535z 3/1, bj7g 2/1, 2jrq 29/8 | | all failing tests inside |
| 8y7j 0/0 | | the line is executed by no test |
| 2haz 0/0, 66ks 0/0, 2qn5 0/0 | | `is` → `==` (86 and 51 tests ran the line, `test_execute_tpcds` among them) and `<` → `<=`: no test ever saw the two operators disagree there |
| fryt 0/0, gbav 0/0, ktz3 0/0 | | 3, 1 and 3 tests run the changed code; run on the patched tree at decision time, all pass and are dropped |
| 5y72 3/0, 4d63 273/0 | | tests run the changed code, or read the changed value, and still pass; not run at decision time (process-pool tests among them; 10 s of tests) |

## Running candidates at decision time: what it buys and why it is trusted

**Cost and benefit**, static selection against static selection plus runs
(step 10), same commits, zero misses on both sides everywhere:

| set | commits | static | with runs | saved | CPU of the runs | commits run / emptied |
| --- | --- | --- | --- | --- | --- | --- |
| sample (the image, scorer's flags) | 30 | 112.461 s | 106.222 s | 6.239 s | 1.3 s | 5 / 3 |
| labeled, round-1 kinds | 212 | 2251.7 s | 2225.3 s | 26.4 s | 12.3 s | 35 / 13 |
| labeled, round-2 kinds | 78 | 540.4 s | 535.7 s | 4.7 s | 3.9 to 5.5 s | 16 / 2 |

- The CPU column is everything the step adds (planning, the zygote, the copy
  of the checkout, the test processes), measured inside the same run with
  `os.times()`. For the sample: 1.25 to 1.36 s natively on macOS/arm64, 1.2 to
  1.7 s in a linux/arm64 container with the pinned Python, 2.1 to 2.7 s under
  amd64 emulation (where everything is about three times slower). On the
  sample that is about **5 s net of 117 s** (simulated + CPU).
- The labeled sets were built to break things (mutations chosen to hit tables,
  scopes, signatures; hangs): there only 2 of 16 and about 4 in 10 runs find
  nothing failing, against 3 of 5 on the organizers' sample, so the step
  roughly pays for itself on round 2 and gains about 14 s on round 1. Seven
  hang patches cost 0.2 to 1.0 s each (the time limit of one test).
- A fixed 0.45 s per batch (0.25 s in Linux): starting the zygote, importing
  90 standard library modules once, copying `sqlglot/` and `tests/` to `/tmp`.
  A job costs 0.095 s plus its tests (fork, fresh import of sqlglot and one
  test file); tests cost about what `tests.json` records (0.85 to 1.0 ×).

**Fidelity.** A test's verdict under this runner has to be the labeling run's.
Checked three ways:

1. *The pinned commit.* All 1617 tests up to 5 s pass alone and in per-file
   jobs, natively and in the pinned image on linux/arm64; the 1593 the runner
   may judge pass inside the submission image under the scorer's flags (user
   `nobody`, read-only disk, 64 MB `/tmp`, no network).
2. *Every labeled patch, far beyond what the policy would run*
   (`lab/runfid.py fidelity`): for each of the 320 patches labeled by a full
   pytest run, the static selection plus up to 150 of its truly failing tests
   went through the runner. 19,083 verdicts on 187 patches; 9,789 truly
   failing tests reported failing. Six patches had tests reported passing
   that truly fail, each for a reason a test's own run cannot show:
   - a new `tests/conftest.py` (an autouse fixture that raises; a syntax
     error) and a deleted `tests/__init__.py`: pytest's doing, not the test's;
   - two patches that write shared state (`sqlglot.pretty = True` in a test
     file; a test that leaves it set): the failing tests are *other* tests;
   - `"Druid"` removed from `sqlglot.dialects.DIALECTS`: one test fails only
     after another test file has emptied the dialect registry.

   All six are classes the policy leaves to the static selection (paths;
   `Analysis.shared`; the functions around the lines of `maps/warm.json`).
   The last one was not excluded when it was found: it led to the rule that a
   changed value read anywhere in such a function counts as shared state.
   Apart from those classes: no test reported passing that truly fails, and 38
   reported failing or without verdict that truly pass (hangs, removed tests),
   which costs nothing.
3. *End to end*, the selector with the step on: zero misses on the sample
   (three runs in the image) and on both labeled sets.

**What was measured and left out**

- *Process-pool tests with forked workers* (`BLAST_VERIFY_SPAWN=fork`). 22
  optimizer tests hand work to a `spawn` pool; alone each costs about 2 s of
  CPU in workers that re-import everything. With forked workers they cost
  their recorded time, but the workers then start from the test process's
  state instead of a fresh interpreter, and on the sample the one commit this
  would empty (5y72, 2.5 s) is paid for by three others whose pool tests fail
  (0.5 to 0.8 s each) and by importing pandas and duckdb once (0.55 s). Net
  about zero for an unverified change of start method: off.
- *Importing every test file first*, as pytest's collection does: 0.26 s per
  job instead of 0.095 s, plus pandas and duckdb for every batch. It would not
  have fixed the one state-dependent case found (the registry is emptied by a
  test, not by an import), so the CPU buys nothing that was measured.
- *Dropping passing tests when the worker stays open* saves milliseconds and
  carries the same small risk as an emptied commit: not done
  (`VERIFY_MIN_DROP_SAVING_S`).
- *Larger candidate sets* (4d63: 273 tests, 10 s, static cost 9.1 s): the run
  costs about what it could save.

## Checks beyond the sample set

Patches made up for the purpose and labeled by running sqlglot's suite on the
patched tree (`tools/dev_eval.py` prints selected vs failing for each):

| set | patches | misses |
| --- | ---: | ---: |
| the kit's sample | 30 | 0 |
| the maps' own checks: class-level flags, numbers, `arg_types`, dict entries; five fixture files | 19 | 0 |
| single-line mutations where key-level reads, lambda bodies and derived values decide (one-line lambdas, table entries, time mappings, `arg_types` keys) | 37 | 0 |
| random single-line mutations anywhere in `sqlglot/` | 14 | 0 |
| other kinds: test bodies, shared test helpers, test class attributes, syntax errors, re-indented lines, broken and removed imports, a module that raises at import, an added method, an added class attribute, an invalid encoding cookie, a class-level value that raises, an inserted statement | 15 | 0 |

Most of the second to fourth sets were labeled on the tests that load the
changed module rather than on the whole suite (a test that never loads the
module cannot be affected); 21 were labeled on all 1620 tests.

The first run of the third set did miss: three mutations of one-line lambdas
failed tests the coverage map did not list, which is how the missing
function-entry events in the tracer were found (fixed in the maps, see
`tools/README.md`). On 66 further patches of kinds the sample does not have
(new, deleted and renamed files, binary patches, broken TOML, CRLF, mode
changes, garbage) the selector does not raise and gives the same answer in
the image as natively.

### Kinds of change the sample does not have

A generator made 351 patches of twelve kinds against the pinned commit, with
`git diff` so that the headers are git's own, and 180 of them, spread over the
kinds, were labeled by running all 1620 tests on the patched tree. They are a
check of the reasoning above, nothing else: no patch, label or kind-to-tests
rule from them is in this repository.

| kind | labeled | misses before | misses now | cost now / perfect knowledge |
| --- | ---: | ---: | ---: | ---: |
| one-line edits inside functions, all of sqlglot (operators, constants, arguments, deleted and inserted statements) | 36 | 0 | 0 | 304 s / 192 s |
| class-level and module-level edits (dict entries, constants, defaults, decorators, imports) | 16 | 1 | 0 | 313 s / 245 s |
| comments of every flavour (directives, coding cookies, inside strings, mis-indented, in `.sql`, TOML, YAML) | 14 | 0 | 0 | 35 s / 22 s |
| refactors that change nothing (renamed local, moved method, reordered imports, docstrings) | 11 | 0 | 0 | 0 s / 0 s |
| fixtures (every `tests/fixtures` folder, `# meta` lines, JSON, binary `.csv.gz`, deleted, renamed) | 13 | 0 | 0 | 119 s / 60 s |
| test files, `tests/helpers.py`, `conftest.py`, package markers | 18 | 1 | 0 | 304 s / 188 s |
| new, deleted, renamed and shadowing files | 14 | 1 | 0 | 282 s / 220 s |
| `pyproject.toml`, `setup.cfg`, docs, CI | 13 | 0 | 0 | 151 s / 74 s |
| files that no longer compile | 9 | 0 | 0 | 135 s / 135 s |
| endless loops, sleeps, hangs in C code | 7 | 0 | 0 | 21 s / 19 s |
| scripted definition-time edits (side effects at import, `__slots__`, `__init__.py` re-exports, metaclass code, enum order, base classes) | 20 | 2 | 0 | 377 s / 136 s |
| several files, harmless and harmful parts mixed | 9 | 0 | 0 | 158 s / 84 s |
| **all** | **180** | **5** | **0** | **2200 s / 1376 s** |

**Round 2** added sixteen kinds (327 patches) and labeled 78 of them, plus 32
more of the first set, again with the full suite: 290 labeled patches in all.

| kind (round 2) | labeled | misses before | misses now | cost now / perfect knowledge |
| --- | ---: | ---: | ---: | ---: |
| scope: a line no test runs makes a name local, or the function a generator | 4 | 3 | 0 | 43 s / 32 s |
| block structure: a line dedented out of or indented into a block, `elif` → `if`, swapped statements, `break` ↔ `continue`, `while` → `if`, another exception type | 4 | 0 | 0 | 14 s / 14 s |
| signatures: parameters renamed or swapped, defaults flipped or dropped, decorators added or removed | 4 | 0 | 0 | 13 s / 7 s |
| tables: entries swapped, duplicated keys, order of lists and tuples, enum members, an attribute deleted or added in a subclass | 4 | 0 | 0 | 7 s / 0 s |
| inheritance: overrides added, methods deleted or renamed, classes renamed, bases swapped | 4 | 0 | 0 | 25 s / 4 s |
| imports: names used only inside functions, rebinding, `TYPE_CHECKING`, package `__init__` tables, eager imports, cycles | 3 | 0 | 0 | 0 s / 0 s |
| annotations and docstrings | 3 | 0 | 0 | 2 s / 0 s |
| whitespace, CRLF, byte order marks, tabs (code and fixtures) | 3 | 0 | 0 | 2 s / 2 s |
| fixtures: comment flavours the loader keeps, pair structure, less common files | 3 | 0 | 0 | 6 s / 6 s |
| test files: redefined tests and classes, `expectedFailure`, skips, base classes | 3 | 0 | 0 | 6 s / 6 s |
| writes to process-wide state, in test files and in code | 3 | 2 | 0 | 110 s / 17 s |
| files: shadowing by package, extension module or stdlib name; sqlglotc; odd files | 3 | 2 | 0 | 54 s / 54 s |
| pytest 9 configuration files, configuration in `tests/` | 3 | 0 | 0 | 110 s / 0 s |
| several files | 3 | 0 | 0 | 7 s / 5 s |
| more random one-line edits on narrowly covered code | 3 | 0 | 0 | 4 s / 4 s |
| a comparison operator replaced by its neighbour (`is` → `==`, `<` → `<=` ...) | 28 | 0 | 0 | 137 s / 53 s (452 s before `maps/cmp.json`) |
| **round 2** | **78** | **7** | **0** | **540 s / 204 s** |
| first set, 212 labeled, with the round-2 selector | 212 | 0 | 0 | 2252 s / 1389 s |

The seven misses of round 2 were four assumptions:

1. *"A line no test runs changes nothing."* An assignment on such a line made
   a module-level name a local of the whole function (three patches; the
   callers got `UnboundLocalError` on lines the patch did not touch), a
   `yield` made the function a generator. Step 3, `scope_shift`.
2. *"The tests that run a statement are the tests it can affect."* A test
   file whose module level sets `sqlglot.pretty = True` broke 223 tests of
   other files; one test that set it and left it broke the 82 tests that ran
   after it in the same process. Step "Writes to shared state".
3. *"Only `.py` files and packages are importable."* A file `sqlglot/time.abi3.so`
   is found before `sqlglot/time.py` (1600 tests failed). Step 7.
4. (found without a failing patch) pytest 9 reads `pytest.toml` and
   `.pytest.toml`; a configuration file in `tests/` changes pytest's rootdir,
   and with it every test id, when pytest is started on `tests/`; a byte
   order mark breaks a test that parses a source file as text.

In the 28 operator patches the tests that failed were always among the tests
`maps/cmp.json` lists for the comparison (for `tokenizer_core.py:747`,
`<=` → `<`: 34 listed, the same 34 failed), and never one it rules out.

Labels of patches that add a test file come from a run that names the test
files explicitly (so that ids stay comparable) and therefore does not collect
the new file; a plain `pytest tests` does, and then the module-level write of
such a file fails other tests (checked by hand). The selector assumes the
plain run.

The five misses of the first round, each an assumption that did not hold, and what replaced it:

1. *"The import of the patched tree always answers."* A new
   `sqlglot/_version.py` that raises makes `import sqlglot` fail (the pinned
   `__init__.py` imports it under `try/except ImportError`); the comparison
   died and nothing was selected. It now reports the failure and every test
   is selected; a comparison that cannot run or be read leaves the commit its
   broad candidates.
2. *"A file without coverage runs for nobody."* `tests/__init__.py` is empty
   at the pinned commit; a statement added there runs for every test.
3. *"No recorded reader means no reader"* (three patches): a re-export
   removed from `sqlglot/__init__.py` (the name is bound by an import, which
   the read map does not track), and two `__slots__` edits (consumed by the
   interpreter when the class is created, never loaded by name). See step 4.

Found the same way, without a failing patch: the 19 lines of `maps/warm.json`
(reached only through state another test leaves behind) and the coding
cookie (decodes non-ASCII literals differently in untouched statements).
`unit/test_generalization.py` has a regression test for each, built from the
pinned source.

## Honest limits

- **Coverage says a test ran the code, not that the edit changes its
  result.** An edit that no assertion notices still selects every test that
  executed the line, which is expensive when `test_execute_tpcds` (21.5 s) or
  `test_max_nodes` (28.8 s) is among them. The one family measured ahead is a
  comparison operator replaced by its neighbour (`maps/cmp.json`); a changed
  constant, a negated condition, `and` → `or`, a changed argument are not.
- **`maps/cmp.json` is a record of each test running alone**, on macOS/arm64,
  with two hash seeds. A test that evaluates the comparison with other
  operands in a shared process, on another platform or under another set
  order than the recording would be ruled out wrongly. `x is y` between equal
  strings or numbers depends on interning; the recording saw what CPython 3.13
  does. Tests that reach the line only through left-over state are not
  pruned.
- **Writes to shared state are recognised by their syntax** (`blast/state.py`):
  a write through a local alias (`p = SnowflakeParser; p.FUNCTIONS[...] = ...`),
  through a called helper, or by a mutating method not in the list is not
  seen. In sqlglot's own code an edit that changes *what* an existing write
  stores is left to line coverage.
- **A test file that no longer imports** selects its own tests and the tests
  that import it. A plain `pytest` stops at the first collection error and
  runs nothing; the rules say such a file's tests fail, not all tests, and
  the selector follows the rules. A new, unnumbered test file with a syntax
  error selects nothing for the same reason.
- **Tables that are consumed whole.** `Generator.TRANSFORMS` is copied into a
  dispatch table and `Tokenizer.KEYWORDS` goes into the tokenizer core, so a
  changed entry (other than the body of a lambda, which coverage answers)
  selects every test that used that class, not only those that reach the
  entry.
- The maps were traced on macOS/arm64 with the kit's `requirements.lock`; the
  scorer is linux/amd64. Test ids and outcomes agree; a line executed only on
  one platform would be missed.
- State left behind by other tests is modelled only as far as the pinned
  commit shows it: `maps/warm.json` holds the lines that left-over state leads
  to in eight orders of the unpatched suite. A patch that makes one test
  *leave different state* (a polluted cache, a mutated global) and thereby
  breaks a later test that never runs the changed code is not modelled. The
  suite is order-dependent already: four tests fail at the pinned commit when
  it runs in reverse order.
- Which tests fail when a module no longer imports depends on what the
  process imported before (a test file that imports `sqlglot.errors` passes
  if an earlier, failed `import sqlglot` left that submodule behind). The
  selector takes every test that loads the module, a superset.
- Values used without being loaded by name are covered for the cases named
  in step 4 (`__slots__` and other dunders, decorated classes, `NamedTuple`,
  `TypedDict`, `__all__`); another mechanism of that sort would be missed.
- A definition-time statement that binds a name *and* changes process-wide
  state on the side is compared by the value it binds only.
- New `conftest.py`, pytest ini sections, side-effect statements at module
  level and data files (`.csv.gz`) cannot be shown harmless: they select
  everything that could see them, which is expensive when nothing fails.
- Timeouts are modelled only as "the tests that execute the changed code".
- `test_generator_memory` asserts on allocation counts and is sensitive to
  what ran before it in the same process; nothing here predicts that.
- Deleted or renamed names are followed by word matching through the pinned
  tree, which over-selects for short common names.
- `maps/opens.json` lists `test_execute_tpcds` as a consumer of `tpc-h.sql`
  because one line of the test file mentions both fixtures; an edit of that
  fixture pays 21 s for it.

## Layout

| Path | What |
| --- | --- |
| `selector.py` | entry point (`CMD` of the image) |
| `blast/` | `diffparse` (diff reader), `pydiff` (AST comparison, scope shifts, operator swaps), `state` (writes to shared state), `analyze` (patch → candidate groups), `exact` + `snapshot_child` (import comparison), `choose`, `pack`, `maps` (map access), `config` (all constants) |
| `maps/` | the maps of the pinned commit, plain JSON, 7.6 MB (`maps/README.md`, `maps/VERIFY.md`) |
| `tools/` | map builders, the fingerprinting used by the import comparison, image build steps (`fetch_sqlglot.py`, `check_image.py`), `dev_eval.py` (measure the selector on labeled patches) |
| `unit/` | unit tests (`test_generalization.py`, `test_round2.py`: one regression test per unsound assumption found with made-up patches; `test_swap.py`: operator swaps and `maps/cmp.json`) |
| `Dockerfile` | pinned base image; downloads sqlglot at the pinned commit from GitHub, compiles, fingerprints, self-checks |
| `Makefile`, `score.py`, `SCORING.md`, `sample/` | the organizers' kit, for local checks (the Makefile only gained quotes around paths) |

No patch, and no list of patches or kinds of edit with the tests they break,
is stored anywhere in this repository. `sample/answers.json` is the kit's file
and is read only by `score.py` and `tools/dev_eval.py`.

## License

MIT, see `LICENSE`.

# Blast Radius

Given a patch against [sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, print which of its 1620 tests to
run and how to split them over up to 8 workers, so that no failing test is
left out and as little as possible is run.

```sh
make sample    # build the image, run it on sample/ under the scorer's limits, score the output
python3 -m unittest discover -s unit    # parser, packing, AST comparison, selection (no Docker needed)
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
     run differently. (A "comment" inside a string, a changed encoding cookie
     or a re-indented line are not equal trees and are treated as code.)
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

   Edits that provably change nothing are recognised as such: a function
   whose old and new versions compile to the same bytecode (renamed locals,
   constants the compiler folds, annotations of locals) selects nothing;
   import statements are netted per scope (reordering or splitting them binds
   the same names); a definition that reappears unchanged elsewhere in its
   class or module is a move; a docstring or `pass` added inside a function
   executes nothing. For the definition-time ones the import comparison still
   runs.

5. **Test files** are handled by the same two questions, because coverage was
   recorded for `tests/**` as well: a changed test body or helper selects the
   tests that executed it; a changed module-level or class-level line selects
   the tests of every file whose import executes it (so an edit to
   `tests/helpers.py` or to the `Validator` base class reaches its users). A
   new test function with a fresh name selects nothing: it has no number.

6. **Data files** → the tests that open them (`maps/opens.json`). For
   `tests/fixtures/**/*.sql` the selector applies the fixture loader's own
   filter (`tests/helpers.py::_filter_comments` drops empty lines and lines
   starting with `--`): if the filtered text is unchanged no test can see the
   edit; a changed `# title:` / `# dialect:` line or SQL comment is a real
   change. `pyproject.toml`, `setup.cfg`, `pytest.ini`, `tox.ini` and
   `conftest.py` are what pytest reads at start-up: a file that no longer
   parses or a changed pytest section reaches every test, other metadata
   reaches none. Docs, CI files and files no test opens select nothing.

7. **New, deleted and renamed files**: a deleted or renamed module breaks the
   tests that load it; a deleted fixture breaks the tests that open it; a new
   file matters if it shadows an importable module (the checkout is on
   `PYTHONPATH`), is picked up by pytest (`conftest.py`, `pytest.ini`), or
   lands in a directory some test lists.

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

Every probability, threshold and limit is a named constant in
`blast/config.py`. Anything the analysis cannot read (a hunk that does not
fit the pinned file, an exception in the analysis itself) falls back to the
tests that could depend on the file, or to the whole suite; the selector
always prints one line per commit.

## What it costs on the sample set

`make sample`: **165.2 simulated seconds, 0 misses** on the 30 sample commits
(94.4 s is the cost with perfect knowledge). CPU for the batch, measured
natively on a loaded arm64 laptop: 0.4 s for everything static plus 0.5 to
0.9 s for each import comparison (4 of the 30 commits), 3.9 s in total; with
`BLAST_SNAPSHOT=needed` (import only when the static answer is not confined
to constants or is expensive) 2.2 s and the same selection on this set.

| commits | selected / failing | note |
| --- | --- | --- |
| 8 fixture, 3 multi | 1/1 each (rszg 4/3) | |
| 3 comment | 0/0 | |
| ary6 10/10, 3utz 37/37, 6ze8 1/1, j6uz 2/2, 535z 3/1, bj7g 2/1, 2jrq 29/8 | | all failing tests inside |
| 8y7j 0/0 | | the line is executed by no test |
| 2qn5 6/0, 5y72 3/0, fryt 3/0, gbav 1/0, ktz3 3/0, 4d63 273/0 | | tests run the changed code and still pass |
| 2haz 86/0, 66ks 51/0 | | `is` → `==`: 46 s of the gap, see below |

## Checks beyond the sample set

Patches made up for the purpose and labeled by running sqlglot's suite on the
patched tree (`tools/dev_eval.py` prints selected vs failing for each):

| set | patches | misses |
| --- | ---: | ---: |
| the kit's sample | 30 | 0 |
| the maps' own checks: class-level flags, numbers, `arg_types`, dict entries; five fixture files | 19 | 0 |
| single-line mutations where key-level reads, lambda bodies and derived values decide (one-line lambdas, table entries, time mappings, `arg_types` keys) | 37 | 0 |
| random single-line mutations anywhere in `sqlglot/` | 14 | 0 |
| other kinds: test bodies, shared test helpers, test class attributes, syntax errors, re-indented lines, broken and removed imports, a module that raises at import, an added method, an added class attribute | 12 | 0 |

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

## Honest limits

- **Coverage says a test ran the code, not that the edit changes its
  result.** An edit that leaves behaviour unchanged (`x is None` → `x == None`)
  or that no assertion notices still selects every test that executed the
  line. When one of them is `test_execute_tpcds` (21.5 s) that is expensive:
  two sample commits account for 46 of the 71 seconds above the optimum. The
  static method cannot tell these apart; running the cheap candidates at
  decision time could, at the price of CPU seconds.
- **Tables that are consumed whole.** `Generator.TRANSFORMS` is copied into a
  dispatch table and `Tokenizer.KEYWORDS` goes into the tokenizer core, so a
  changed entry (other than the body of a lambda, which coverage answers)
  selects every test that used that class, not only those that reach the
  entry.
- The maps were traced on macOS/arm64 with the kit's `requirements.lock`; the
  scorer is linux/amd64. Test ids and outcomes agree; a line executed only on
  one platform would be missed.
- A patch that makes a test fail through state left behind by *another* test
  (a polluted cache, a mutated global) is not modelled: tests are mapped as if
  each ran alone.
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
| `blast/` | `diffparse` (diff reader), `pydiff` (AST comparison), `analyze` (patch → candidate groups), `exact` + `snapshot_child` (import comparison), `choose`, `pack`, `maps` (map access), `config` (all constants) |
| `maps/` | the maps of the pinned commit, plain JSON, 7.5 MB (`maps/README.md`, `maps/VERIFY.md`) |
| `tools/` | map builders, the fingerprinting used by the import comparison, image build steps (`fetch_sqlglot.py`, `check_image.py`), `dev_eval.py` (measure the selector on labeled patches) |
| `unit/` | unit tests |
| `Dockerfile` | pinned base image; downloads sqlglot at the pinned commit from GitHub, compiles, fingerprints, self-checks |
| `Makefile`, `score.py`, `SCORING.md`, `sample/` | the organizers' kit, for local checks (the Makefile only gained quotes around paths) |

No patch, and no list of patches or kinds of edit with the tests they break,
is stored anywhere in this repository. `sample/answers.json` is the kit's file
and is read only by `score.py` and `tools/dev_eval.py`.

## License

MIT, see `LICENSE`.

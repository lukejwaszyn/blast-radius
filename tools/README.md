# tools/ — how maps/ is made

Everything in `maps/` comes from the pinned sqlglot commit
(`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) and the kit's `tests.json`
(test ids and recorded seconds). No patch is an input to any of these scripts.

```sh
git clone https://github.com/tobymao/sqlglot && git -C sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
python3.13 -m venv venv && venv/bin/pip install -r <kit>/requirements.lock   # sqlglot itself is not installed
tools/build_maps.sh sqlglot venv/bin/python <kit>/sample/tests.json /tmp/blast-work maps
```

Needs CPython 3.13 (`sys.monitoring`), `os.fork` (Linux or macOS), about
270 MB of scratch space and about 20 minutes with 4 processes (the comparisons pass adds about 8). No package
beyond the kit's `requirements.lock` is used.

| Script | What it does |
| --- | --- |
| `build_maps.sh` | the whole sequence below |
| `static_map.py` | AST of every `.py`: definition-time statements, their targets and dict entries, function/lambda spans, multi-line statement spans → `static.json` |
| `trace_imports.py deps` | imports every sqlglot module with a line tracer that never switches off and notes, for each function line that runs at import, the module/class-level line it ran for → `defdeps.json` |
| `trace_imports.py each` | imports each module alone in a fresh process → which modules that loads |
| `run_trace.py` | lines pass: one pytest per test file, each test in its own fork, line events of `sys.monitoring`, `open`/`listdir` audit events, modules loaded |
| `instrument.py` | writes a copy of the checkout in which every load of a class-level / module-level data name calls `tracer/blast_reads.py` |
| `trace_imports.py reads` | imports the rewritten copy: what each definition-time statement reads; the run-time class table |
| `run_trace.py --reads` | reads pass: the suite on the rewritten copy, each test in its own fork |
| `aggregate.py` | unites the raw runs into `maps/*.json` |
| `instrument_cmp.py` | writes a copy of the checkout in which every two-operand `is`, `is not`, `<`, `<=`, `>`, `>=` inside a function also evaluates its neighbouring operator (`==`, `!=`, `<=`, `<`, `>=`, `>`) through `tracer/blast_cmp.py` |
| `run_trace.py --cmp` | comparisons pass: the suite on that copy, each test in its own fork; records the comparisons whose neighbour gave another result |
| `aggregate_cmp.py` | unites the comparisons runs into `maps/cmp.json` |
| `warm_trace.py` | runs the whole suite in one process (several orders) under the line tracer and writes the lines a test executes only because of state earlier tests left behind → `warm.json` (run after `aggregate.py`: it compares with `cov.json`) |
| `runner_map.py` | runs every test alone, on the unpatched tree, through the selector's own decision-time runner (`blast/runner.py`) → `runner.json`: which tests pass alone, which start other processes, what a job and a class set-up cost, which packages a test file loads. About 6 minutes of CPU; not part of `build_maps.sh` (run it in the kit's pinned Python image so that the timings and module lists are the image's). `--probe-only` redoes just the per-file import lists |
| `snapshot.py` | fingerprints every class-level / module-level value of a sqlglot tree and compares two fingerprints: names the values (and dict keys) a definition-time edit really changes, derived ones included, in the vocabulary of `reads.json` |
| `verify_samples.py` | replays patches with known answers against the maps and prints, per patch, candidate-set sizes per map and which map catches each failing test (results: `maps/VERIFY.md`) |
| `dev_check_mutations.py` | development check: generates single-line edits of definition-time lines, has a labeler run the full suite on them and checks the failing tests are inside the maps' answer |
| `fetch_sqlglot.py`, `check_image.py` | image build steps: download sqlglot at the pinned commit; check that the checkout matches the maps, that the selector answers three made-up patches sensibly, that every test file imports in the image, and that the runner calls pinned tests passing, a broken module failing, and restores its tree |
| `dev_eval.py` | development check of the selector: run it on patches with known failing tests (the kit's sample, or patches labeled by running the suite) and print selected vs failing, cost vs perfect-knowledge cost, misses |
| `mapfmt.py` | the range-string encoding of test sets |
| `tracer/` | `sitecustomize.py` (switches tracing on in every process, subprocesses of tests included), `blast_trace.py` (lines, opens), `blast_pytest.py` (fork per test), `blast_reads.py` (reads), `blast_cmp.py` (neighbouring comparison operators), `blast_warm.py` (no fork: per-test lines in a shared process, in a chosen order) |

## Design decisions that matter for correctness

- **A fork per test.** sqlglot imports dialects lazily and caches a lot
  (`_DISPATCH_CACHE`, tries, dialect instances); pytest runs `setUpClass` once
  per class. In one long-lived process the first test to need something pays
  for it and the rest look as if they never touched it. Forking from the state
  "this test file has just been imported" records every test as if it ran
  alone. What importing the test file itself did is recorded separately and
  counts for every test of the file.
- **Import time is separate, per thread.** While a module of `sqlglot` is
  being imported, lines and reads go to the "import" side; what a test module's
  own top-level code runs is a third bucket (it counts for every test of the
  files that import that module). The flag is per thread: process pools
  unpickle results (and import modules) on a helper thread while the test
  thread keeps running.
- **Subprocesses are traced.** `sitecustomize.py` is on `PYTHONPATH`, so pool
  workers and `python -c` children trace themselves and write a file the
  aggregator merges into the test that started them.
- **The reads pass never changes behaviour.** Each rewritten expression
  evaluates to the same value; all 1620 tests pass on the rewritten copy, as
  they do in the lines pass (the aggregator refuses to build maps from a run
  with a failing or crashed test).
- **The comparisons pass never changes behaviour either.** `a is b` becomes a
  call that returns `a is b` and, on the side, evaluates `a == b` on the same
  two objects. The side evaluation can run an `__eq__` (sqlglot's expressions
  hash themselves there and cache the hash), so it is capped per site and
  process; a site over the cap, or whose neighbour raised, is recorded as
  "differs". A test that fails in this pass is put into every set (never
  pruned).
- **Function entry counts as a line.** A LINE event of `sys.monitoring` fires
  only when the line changes inside a code object, so a function whose whole
  body is on its first line (one-line lambdas in the dialect tables, one-line
  `def`s) is never reported when called. The tracers also listen to PY_START
  and record the code object's first line. A check with made-up mutations of
  one-line lambdas found failing tests that the first version of the maps did
  not list; with entry lines recorded they are listed.
- **Two runs per pass** (hash seeds 0 and 1) are united. The two lines-pass
  runs recorded identical line sets for every test.
- **The official environment skips one test** (`test_variadic_iif_execution`,
  SQLite < 3.49). It has no number and is dropped by the aggregator.

## Known limits (a selector must cover these itself)

- Line coverage says a test *ran* the line, not that the edit changes its
  result. An edit to a line no test ran cannot change a result unless it
  changes the structure of the file (syntax error, different indentation,
  a moved `else`): compare the ASTs and fall back to `modules.json` when the
  file no longer compiles or the change is not confined to one statement.
- Definition-time edits: use `static.json` to find the statement and the dict
  key, `reads.json` for who reads the bound name, and either `derive.json`
  (static closure, coarse) or an import of the patched module with a
  before/after comparison of the class namespaces (exact, cheap) to find the
  derived values that changed (`TIME_TRIE`, `SHOW_TRIE`, subclass copies ...).
- `reads.json` key-level sets are exact only for direct lookups. Values passed
  around whole (`KEYWORDS` into the tokenizer core, `TRANSFORMS` copied into the
  generator's dispatch table) appear under `whole`.
- A test can depend on a file's text without importing it:
  `opens.json` → `py_read_as_data`, `dirs_listed`.
- Timeouts and edits that make a test slower are not modelled.

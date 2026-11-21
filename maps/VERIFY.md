# Verification of the maps

> The tables below were recorded with the first version of `cov.json`. Since
> then the tracers also record the first line of every function that is
> entered (see `maps/README.md`): 5745 more lines carry coverage and no line
> lost a test, so every candidate set below can only have grown. The selector
> re-checks the same patches (`tools/dev_eval.py`); its results are in the
> top-level README.

Produced with `tools/verify_samples.py` (the kit's 30 sample patches and their answers), `tools/dev_check_mutations.py` and a few hand-made fixture edits, all labeled by running the full suite. The maps were built without any of these patches; this file only records how they fared.

## The 30 sample patches

Every failing test is inside the candidate set the maps give (30 of 30 commits, 73 failing tests).

Columns: `failing` = tests that really fail. `lines` = candidates from per-test line coverage of the changed lines (`cov.json` + `filecov.json`). `defs` = readers of the names bound by changed definition-time statements (`static.json` + `reads.json`), without closure. `derived` = the same closed over `derive.json`. `data` = tests that open the changed non-Python file (`opens.json`). `exact` = `lines` + `data` + readers of the values that the fingerprint comparison of the imported patched tree reports as changed (`tools/snapshot.py`), consulted only when a definition-time line changed. `modules` = every test that loads the changed module (the fallback).

| commit | kind | failing | lines | defs | derived | data | exact | modules | failing tests found by |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2haz | code | 0 | 86 | 0 | 0 | 0 | 86 | 1617 | - |
| 2jrq | code | 8 | 29 | 0 | 0 | 0 | 29 | 149 | lines 8 |
| 2qn5 | code | 0 | 6 | 0 | 0 | 0 | 6 | 150 | - |
| 2vdj | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| 3utz | code | 37 | 37 | 0 | 0 | 0 | 37 | 37 | lines 37 |
| 4d63 | code | 0 | 0 | 273 | 355 | 0 | 273 | 1088 | - |
| 535z | code | 1 | 0 | 3 | 3 | 0 | 3 | 171 | defs 1 |
| 5y72 | code | 0 | 3 | 0 | 0 | 0 | 3 | 147 | - |
| 66ks | code | 0 | 51 | 0 | 0 | 0 | 51 | 60 | - |
| 6vvp | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| 6ze8 | code | 1 | 1 | 0 | 0 | 0 | 1 | 37 | lines 1 |
| 8y7j | code | 0 | 0 | 0 | 0 | 0 | 0 | 150 | - |
| 986c | multi | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| 9u5k | comment | 0 | 0 | 0 | 0 | 0 | 0 | 0 | - |
| ary6 | code | 10 | 10 | 0 | 0 | 0 | 10 | 1102 | lines 10 |
| bap8 | comment | 0 | 0 | 0 | 0 | 0 | 0 | 0 | - |
| bj7g | code | 1 | 2 | 0 | 0 | 0 | 2 | 150 | lines 1 |
| bvw2 | multi | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| egys | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| f4bn | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| f4xa | comment | 0 | 0 | 0 | 0 | 0 | 0 | 0 | - |
| fryt | code | 0 | 3 | 0 | 0 | 0 | 3 | 37 | - |
| gbav | code | 0 | 0 | 1 | 1 | 0 | 1 | 37 | - |
| gbhk | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| j6uz | code | 2 | 2 | 0 | 0 | 0 | 2 | 1102 | lines 2 |
| ktz3 | code | 0 | 0 | 3 | 1347 | 0 | 3 | 1617 | - |
| mthh | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| nwnx | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |
| rszg | multi | 3 | 2 | 0 | 0 | 1 | 3 | 1089 | lines 2, data 1 |
| st8f | fixture | 1 | 0 | 0 | 0 | 1 | 1 | 0 | data 1 |

### Failing tests that line coverage alone does not give

| commit | test | map that gives it | why line coverage cannot |
| --- | --- | --- | --- |
| 2vdj | `TestOptimizer::test_pushdown_predicates` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| 535z | `TestOptimizer::test_simplify` | reads.json | the changed line is a class-level assignment that runs once at import; `reads.json` records that this test read that value (sqlglot.dialects.tsql:TSQL\|WEEK_OFFSET) |
| 6vvp | `TestOptimizer::test_qualify_columns__invalid` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| 986c | `TestOptimizer::test_unnest_subqueries` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| bvw2 | `TestOptimizer::test_annotate_types` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| egys | `TestOptimizer::test_simplify` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| f4bn | `TestOptimizer::test_pushdown_cte_alias_columns` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| gbhk | `TestOptimizer::test_annotate_types` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| mthh | `TestOptimizer::test_merge_subqueries` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| nwnx | `TestOptimizer::test_pushdown_projection` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| rszg | `TestOptimizer::test_qualify_columns` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |
| st8f | `TestOptimizer::test_isolate_table_selects` | opens.json | the change is in a fixture file; `opens.json` records that this test reads it |

The three `comment` commits and the comment-only files of the `multi` commits leave the AST unchanged and give no candidates. `sqlglot/typing/snowflake.py` (touched by 986c and rszg) is read as text by `test_typing.py::TestTyping::test_no_redundant_expression_metadata` (`opens.json` → `py_read_as_data`); that test did not fail on either commit.

### Cost if every candidate were simply run

`exact` candidates, best split over up to 8 workers, 2 s per worker, recorded seconds:

```
commit kind     failing candidates perfect s all candidates s  slow candidates that do not fail
2haz   code           0         86       0.0             25.5  test_execute_tpcds 21.5s, test_scope 2.5s, test_tpcds 2.2s
2jrq   code           8         29      25.5             25.5  
2qn5   code           0          6       0.0              2.5  
2vdj   fixture        1          1       2.1              2.1  
3utz   code          37         37      25.5             25.5  
4d63   code           0        273       0.0              9.1  test_annotate_funcs 2.0s
535z   code           1          3       2.6              2.7  
5y72   code           0          3       0.0              2.5  
66ks   code           0         51       0.0             24.8  test_execute_tpcds 21.5s
6vvp   fixture        1          1       2.0              2.0  
6ze8   code           1          1       2.0              2.0  
8y7j   code           0          0       0.0              0.0  
986c   multi          1          1       2.3              2.3  
9u5k   comment        0          0       0.0              0.0  
ary6   code          10         10       4.4              4.4  
bap8   comment        0          0       0.0              0.0  
bj7g   code           1          2       2.0              2.2  
bvw2   multi          1          1       2.1              2.1  
egys   fixture        1          1       2.6              2.6  
f4bn   fixture        1          1       2.0              2.0  
f4xa   comment        0          0       0.0              0.0  
fryt   code           0          3       0.0              2.2  
gbav   code           0          1       0.0              2.0  
gbhk   fixture        1          1       2.1              2.1  
j6uz   code           2          2       4.4              4.4  
ktz3   code           0          3       0.0              2.1  
mthh   fixture        1          1       3.0              3.0  
nwnx   fixture        1          1       2.1              2.1  
rszg   multi          3          3       5.8              5.8  
st8f   fixture        1          1       2.0              2.0  
total                                   94.4            165.2
```

No misses; the 71 s above the perfect-knowledge cost are candidates that ran the changed line (or read the changed value) and still pass. Deciding those is the selector's job, not the maps'.

## Definition-time edits made up for this check

The sample set has only four edits of lines that run at import (one of them breaks a test), so `tools/dev_check_mutations.py` generated 14 more by rule (flip a class-level flag, bump a class-level number, flip a flag inside a class-level dict entry or an `arg_types` entry), and the full suite was run on each. `direct` = `lines` + `defs`; `exact` as above.

```
m00 ok         arg_types sqlglot/expressions/datatypes.py:186 '"kind": False,' failing=13 direct=18 modules=1617 exact=18:ok
m01 ok         arg_types sqlglot/expressions/temporal.py:400 '"zone": False,' failing=8 direct=10 modules=1617 exact=10:ok
m02 ok         arg_types sqlglot/expressions/dml.py:515 '"whens": True,' failing=0 direct=16 modules=1617 exact=16:ok
m03 ok         entry     sqlglot/parsers/singlestore.py:212 '"GROUPS FOR ROLE": _show_parser("GROUPS FOR ROLE", target=Tr' failing=0 direct=1 modules=37 exact=1:ok
m04 ok         entry     sqlglot/parsers/mysql.py:243 '"FULL TABLES": _show_parser("TABLES", full=True),' failing=1 direct=18 modules=532 exact=19:ok
m05 ok         entry     sqlglot/parsers/singlestore.py:207 '"FULLTEXT SERVICE METRICS FOR NODE", target=True' failing=1 direct=1 modules=37 exact=1:ok
m06 ok         flag      sqlglot/generator.py:465 'COLLATE_IS_FUNC = False' failing=7 direct=7 modules=1617 exact=7:ok
m07 ok         flag      sqlglot/generators/tsql.py:148 'SUPPORTS_MULTI_ARG_CHR = False' failing=1 direct=1 modules=171 exact=1:ok
m08 ok         flag      sqlglot/dialects/bigquery.py:33 'UUID_IS_STRING_TYPE = True' failing=1 direct=2 modules=1102 exact=2:ok
m09 ok         flag      sqlglot/dialects/trino.py:12 'CONCAT_WS_COALESCE = True' failing=2 direct=4 modules=236 exact=4:ok
m10 ok         flag      sqlglot/generators/snowflake.py:401 'ALTER_TABLE_ADD_COLUMNS_WRAPPED = False' failing=1 direct=1 modules=1088 exact=1:ok
m11 ok         flag      sqlglot/dialects/oracle.py:21 'TABLESAMPLE_SIZE_IS_PERCENT = True' failing=2 direct=2 modules=137 exact=2:ok
m12 ok         flag      sqlglot/parser.py:1902 'UNPIVOT_VALUE_COLUMNS_FIRST: t.ClassVar = False' failing=6 direct=27 modules=1617 exact=27:ok
m13 ok         number    sqlglot/optimizer/simplify.py:592 'TINYINT_MAX: t.ClassVar = 127' failing=0 direct=1 modules=813 exact=1:ok
```

All failing tests are inside both `direct` and `exact`, and the sets are close to the failing sets (e.g. `Generator.COLLATE_IS_FUNC`: 7 failing, 7 candidates).

## Fixture files outside `tests/fixtures/optimizer/*.sql`

One-line edits of other data files, full suite run on each. `opened by` = `opens.json` → `by_tests`, `consumers` = the narrowed set where one exists.

```
patch        file                                             failing opened by consumers  inside  failing tests
fx_cts       tests/fixtures/jsonpath/cts.json                       1         1         1  yes     test_cts_file
fx_identity  tests/fixtures/identity.sql                            2         2         2  yes     test_serde, test_identity
fx_pretty    tests/fixtures/pretty.sql                              1         1         1  yes     test_pretty
fx_tpcds     tests/fixtures/optimizer/tpc-ds/tpc-ds.sql             1        61         2  yes     test_tpcds
fx_tpch      tests/fixtures/optimizer/tpc-h/tpc-h.sql               2        61         4  yes     test_optimized_tpch, test_tpch
```

## Tracing runs

- `tools/build_maps.sh` was run end to end a second time (10.6 minutes, 4 processes): every map came out byte-identical except `alone_seconds` in `tests.json`, which is a timing.
- Lines pass and reads pass: all 1620 numbered tests (plus the one the official environment skips) passed in their forks, in both runs of each pass; no crashed test process.
- The two lines-pass runs (PYTHONHASHSEED 0 and 1) recorded identical line sets for every test.
- Build machine: macOS arm64, CPython 3.13.15, packages exactly as in the kit's `requirements.lock`. The scorer's image is linux/amd64 with a newer 3.13 patch release and an older SQLite; the only difference in the test list is the SQLite-dependent test that the official environment skips.


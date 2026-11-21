#!/bin/sh
# Rebuild maps/ from scratch.
#
#   tools/build_maps.sh <sqlglot checkout at eb11aec4> <python> <kit tests.json> <work dir> [<maps dir>]
#
# <python> is CPython 3.13 with the kit's requirements.lock installed (sqlglot itself
# is NOT installed; the checkout goes on PYTHONPATH).  <work dir> receives the raw
# traces (about 250 MB) and an instrumented copy of the checkout; it can be deleted
# afterwards.  JOBS (default 4) is the number of test files traced at once.
#
# About 11 minutes on a laptop with JOBS=4.  Each tracing pass runs twice (hash
# seeds 0 and 1) and the runs are united.
set -eu
SRC=$1
PY=$2
TESTS=$3
WORK=$4
OUT=${5:-$(dirname "$0")/../maps}
T=$(cd "$(dirname "$0")" && pwd)
JOBS=${JOBS:-4}
mkdir -p "$WORK" "$OUT"

# 1. static description of every file (AST)
"$PY" "$T/static_map.py" --src "$SRC" --out "$WORK/static"

# 2. what runs at import time: one process per module, then one process for all
"$PY" "$T/trace_imports.py" deps --src "$SRC" --python "$PY" --out "$WORK/rawI"
"$PY" "$T/trace_imports.py" each --src "$SRC" --python "$PY" --out "$WORK/rawI" --jobs "$JOBS"

# 3. lines pass: every test in its own fork, sys.monitoring line events
"$PY" "$T/run_trace.py" --src "$SRC" --python "$PY" --out "$WORK/rawA"    --jobs "$JOBS" --seed 0
"$PY" "$T/run_trace.py" --src "$SRC" --python "$PY" --out "$WORK/rawA_s1" --jobs "$JOBS" --seed 1

# 4. reads pass: same, on a rewritten copy that reports reads of class/module-level names
"$PY" "$T/instrument.py" --src "$SRC" --static "$WORK/static" --out "$WORK/inst"
mkdir -p "$WORK/rawB"
"$PY" "$T/trace_imports.py" reads --instrumented "$WORK/inst" --python "$PY" --out "$WORK/rawB"
"$PY" "$T/run_trace.py" --src "$SRC" --python "$PY" --out "$WORK/rawB"    --jobs "$JOBS" --reads --instrumented "$WORK/inst" --seed 0
"$PY" "$T/run_trace.py" --src "$SRC" --python "$PY" --out "$WORK/rawB_s1" --jobs "$JOBS" --reads --instrumented "$WORK/inst" --seed 1

# 5. compact maps
"$PY" "$T/aggregate.py" --tests "$TESTS" --static "$WORK/static" --src "$SRC" \
    --lines "$WORK/rawA" "$WORK/rawA_s1" --imports "$WORK/rawI" \
    --reads "$WORK/rawB" "$WORK/rawB_s1" --out "$OUT"
# 5b. comparisons pass: does the neighbouring operator (`is` / `==`, `<` / `<=` ...) ever give another result?
"$PY" "$T/instrument_cmp.py" --src "$SRC" --out "$WORK/cmp_inst"
"$PY" "$T/run_trace.py" --src "$SRC" --python "$PY" --out "$WORK/rawC"    --jobs "$JOBS" --cmp --instrumented "$WORK/cmp_inst" --seed 0
"$PY" "$T/run_trace.py" --src "$SRC" --python "$PY" --out "$WORK/rawC_s1" --jobs "$JOBS" --cmp --instrumented "$WORK/cmp_inst" --seed 1
"$PY" "$T/aggregate_cmp.py" --tests "$TESTS" --sites "$WORK/cmp_inst/blast_cmp_sites.json" --raw "$WORK/rawC" "$WORK/rawC_s1" --out "$OUT"
# 6. lines that only left-over state of earlier tests leads to (whole suite in one process, 8 orders, about 20 minutes)
"$PY" "$T/warm_trace.py" --src "$SRC" --python "$PY" --work "$WORK" --maps "$OUT"
echo "maps written to $OUT"

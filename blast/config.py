"""Every probability, threshold and limit the selector uses, in one place.

The scoring rule this is tuned against (SCORING.md): a commit costs the
recorded seconds of its busiest worker plus 2 s per worker; leaving out one
failing test costs 300 s; our own CPU seconds are added.

A probability below is the selector's belief that *some* test of a group
fails, given only the reason the group was built for (not the patch's
identity).  A group is run when  P x MISS_PENALTY  exceeds what running it
adds to the commit's cost.  Since running every test costs about 37 s, any
group with P above ALWAYS_KEEP is run whatever its size.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------- scoring
WORKER_COST_MS = 2_000
MISS_PENALTY_MS = 300_000
MAX_WORKERS = 8

# ------------------------------------------------- probabilities of groups
# A test executed, at call time, a statement the patch changes or deletes.
P_LINE_HIT = 0.45
# A test executed the statements next to a pure insertion inside a function.
P_INSERT_NEIGHBOUR = 0.40
# A test called a function whose signature, defaults or decorators changed.
P_SIGNATURE = 0.40
# A test read, by its key, the entry of a class-level / module-level table
# that the patch changes (or read the scalar that changed).
P_VALUE_READ = 0.40
# A test took such a table as a whole (iterated, copied, passed it on).
P_VALUE_WHOLE = 0.30
# A test read a value derived at import from the changed one (static closure
# over maps/derive.json, used only when the exact comparison did not run).
P_VALUE_DERIVED = 0.30
# The exact comparison (import of the patched tree) says the value changed.
P_VALUE_EXACT = 0.40
# A module can no longer be imported (syntax error, import-time exception,
# deleted file): every test that loads it.
P_MODULE_BROKEN = 0.98
# Something changed that the finer maps cannot confine; tests loading the module.
P_MODULE_UNCONFINED = 0.35
# A test used a class (or a subclass) whose header / method table changed.
P_CLASS_USER = 0.30
# A new method whose name the code may look up by convention (xxx_sql ...).
P_NEW_DISPATCH_METHOD = 0.30
# A new method / function / attribute with a fresh name nothing refers to.
P_NEW_NAME = 0.004
# Lines of other files that mention a removed or renamed module-level name.
P_NAME_MENTION = 0.50
# An import statement now binds something else: tests running the module's code.
P_IMPORT_REBIND = 0.35

# Test files -----------------------------------------------------------------
# A test's own body (or a helper it ran) changed.
P_TEST_BODY = 0.60
# Module-level or class-level code of a test file changed: tests of the files
# that execute that line when they are imported.
P_TEST_FILE_LEVEL = 0.50
# A test file cannot be imported (or is gone): its tests and its importers.
P_TEST_FILE_BROKEN = 0.98
# A new test function or helper with a fresh name: has no number, breaks nobody.
P_NEW_TEST = 0.004
# conftest.py / pytest configuration that pytest reads for every test.
P_PYTEST_CONFIG = 0.50
# pyproject.toml / setup.cfg edits outside any pytest section.
P_PROJECT_METADATA = 0.003

# Data files -----------------------------------------------------------------
# The test that consumes a fixture whose effective content changed.
P_FIXTURE_CONSUMER = 0.90
# Tests that merely open the file in a shared set-up (they fail only if the
# file can no longer be read at all).
P_FIXTURE_OPENER = 0.02
# Only lines the fixture loader throws away changed (blank lines, `--` lines
# in tests/fixtures/**/*.sql: tests/helpers.py::_filter_comments).
P_FIXTURE_IGNORED_LINES = 0.004
# A fixture was deleted, renamed, or replaced by binary content: every opener.
P_FIXTURE_GONE = 0.95
# A file appeared in / vanished from a directory some test lists.
P_DIR_LISTED = 0.03
# Directories whose listing in maps/opens.json is the import system at work
# (worker processes of the optimizer and executor tests re-import `tests.*`),
# not a test looking at the files.  A new file there matters only if it
# shadows a module, which is checked on its own.
DIR_LISTINGS_BY_IMPORT_SYSTEM = ("tests/",)

# Python source that a test reads as text or executes by path -----------------
# (maps/opens.json -> py_read_as_data).  With a changed AST:
P_TEXT_READER_CODE = 0.05
# With comments / blank lines / layout only:
P_TEXT_READER_COMMENT = 0.004

# A top-level file or directory that would shadow an importable module.
P_SHADOWS_MODULE = 0.60
# Fallbacks: a patch we cannot read at all / a hunk that does not fit the pinned file.
P_UNREADABLE_PATCH = 0.60
P_HUNK_MISMATCH = 0.50
# The per-commit safety net when the analysis itself raised.
P_INTERNAL_ERROR = 0.60

# Groups at or above this are never dropped (running all tests costs ~37 s,
# i.e. the break-even probability for *everything* is ~0.12).
ALWAYS_KEEP = 0.15
# A test at least this long is weighed on its own inside a droppable group.
BIG_TEST_MS = 1_500

# ------------------------------------------------- exact definition-time diff
# "always": import the patched tree whenever code that runs at import changed.
# "needed": only when the static answer is not confined to constants or when
#           the static closure costs SNAPSHOT_MIN_SAVING_MS more than the
#           direct readers.
# "never":  static closure only.
SNAPSHOT_POLICY = os.environ.get("BLAST_SNAPSHOT", "always")
SNAPSHOT_MIN_SAVING_MS = 1_500
SNAPSHOT_TIMEOUT_S = 8.0
SNAPSHOT_PARALLEL = 2

# ------------------------------------------------------------------ limits
# Wall-clock budget for the whole batch (the scorer allows 180 s; the final 270 s).
WALL_BUDGET_S = float(os.environ.get("BLAST_WALL_BUDGET", "120"))
# Search for a hunk whose stated position does not match, this many lines around.
HUNK_FUZZ_LINES = 300

# ------------------------------------------------------------------- paths
PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DATA_DIR = os.environ.get("BLAST_DATA", "/data")
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPS_DIR = os.environ.get("BLAST_MAPS", os.path.join(APP_DIR, "maps"))
SRC_DIR = os.environ.get("BLAST_SRC", "/opt/sqlglot")
BASE_DIR = os.environ.get("BLAST_BASE", "/opt/blast-base")
TMP_DIR = os.environ.get("BLAST_TMP", "/tmp")

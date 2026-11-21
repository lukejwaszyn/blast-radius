"""Switches the map-building tracer on in every Python process of a tracing run.

This directory is put on PYTHONPATH by tools/run_trace.py, so Python imports
this file at startup.  Nothing happens unless $BLAST_OUT is set.

A process that has $BLAST_CTX set is a subprocess started by a test (the test
child sets it before running the test): everything it executes belongs to that
test and is written to "<ctx>.sub.<pid>.json" when the process exits.
"""

import os

if os.environ.get("BLAST_OUT") and os.environ.get("BLAST_ROOT"):
    import blast_trace

    blast_trace.start()

    _ctx = os.environ.get("BLAST_CTX")
    if _ctx:
        import atexit

        _owner = os.getpid()
        _done = []

        def _dump_sub():
            # Only the process that registered this (not a fork of it), once.
            if os.getpid() != _owner or _done:
                return
            _done.append(1)
            try:
                extra = {}
                if os.environ.get("BLAST_READS"):
                    import sys

                    reads = sys.modules.get("blast_reads")
                    extra["reads"] = reads.snapshot() if reads else {"reads": {}, "used": []}
                if os.environ.get("BLAST_CMP"):
                    import sys

                    cmp_ = sys.modules.get("blast_cmp")
                    extra["cmp"] = cmp_.snapshot() if cmp_ else {"bad": []}
                blast_trace.dump(f"{_ctx}.sub.{_owner}.json", ctx=_ctx, kind="sub", **extra)
            except Exception:
                pass

        atexit.register(_dump_sub)
        try:
            # multiprocessing children leave through os._exit after running
            # these finalizers, so atexit alone is not enough for "fork" ones.
            from multiprocessing import util as _mp_util

            _mp_util.Finalize(None, _dump_sub, exitpriority=-100)
        except Exception:
            pass

"""pytest plugin for the shared-process tracing runs (``-p blast_warm``).

The maps record every test as if it ran alone (a fork per test, blast_pytest.py).
A real run keeps one process for many tests, and a test can leave state behind
that sends a *later* test down a path it never takes alone (a registry another
test cleared, a cache another test filled).  This plugin runs the whole suite
in ONE process, in the order $BLAST_ORDER asks for, and writes the call-time
lines of every test ("w.<n>.json").  tools/warm_trace.py compares them with the
isolated coverage; the difference becomes maps/warm.json.

$BLAST_ORDER: "forward" (collection order), "reverse", or "shuffle:<seed>".
Test classes stay contiguous (their setUpClass runs once); the order of the
classes and of the tests inside each class changes.
"""

from __future__ import annotations

import json
import os
import random

import pytest

import blast_trace as bt

_n = 0


def pytest_collection_modifyitems(config, items):
    order = os.environ.get("BLAST_ORDER", "forward")
    if order == "forward":
        return
    groups: dict[str, list] = {}
    for item in items:
        groups.setdefault(item.nodeid.rsplit("::", 1)[0], []).append(item)
    keys = list(groups)
    if order == "reverse":
        keys.reverse()
        for key in keys:
            groups[key].reverse()
    elif order.startswith("shuffle:"):
        rng = random.Random(int(order.split(":", 1)[1]))
        rng.shuffle(keys)
        for key in keys:
            rng.shuffle(groups[key])
    items[:] = [item for key in keys for item in groups[key]]


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global _n
    bt.reset()
    yield
    with open(os.path.join(bt.OUT, f"w.{_n}.json"), "w") as fh:
        json.dump({"test": item.nodeid, "body": sorted(bt.body)}, fh, separators=(",", ":"))
    _n += 1

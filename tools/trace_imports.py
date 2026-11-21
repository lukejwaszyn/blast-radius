#!/usr/bin/env python3
"""What happens at import time (definition time).

    python tools/trace_imports.py each  --src S --python P --out DIR [--jobs 4]
    python tools/trace_imports.py deps  --src S --python P --out DIR
    python tools/trace_imports.py reads --instrumented I --python P --out DIR

each   one fresh process per sqlglot module: import just that module and write
       which modules that pulls in and which lines ran ("mod.<name>.json").
deps   one process imports every sqlglot module with a line tracer that never
       switches a line off.  For each line of a *function* that runs during
       import (a metaclass, a factory called in a class body such as
       ``_show_parser(...)``, ``new_trie`` ...) it notes the definition-time
       line(s) on whose behalf it ran: the innermost module-level or class-level
       frame below it on the stack.  Result: "defdeps.json"
       {"deps": {function line key: [definition line keys]}, "ran": [all line keys]}.
       Editing such a function line is equivalent to editing those definition lines.
reads  one process imports every module of the instrumented copy (tools/instrument.py)
       and writes, per definition-time statement, the tracked names it read
       ("imports_reads.json"), plus the run-time class table: MRO, attributes a
       metaclass / __init_subclass__ put on the class, kinds of class-level values.

Line keys are ``file_id << 16 | line`` with file ids from DIR/files.json.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TRACER = os.path.join(HERE, "tracer")

sys.path.insert(0, HERE)
from run_trace import list_py  # noqa: E402
from static_map import module_name  # noqa: E402


def base_env(root: str, out: str, python: str) -> dict:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([root, TRACER]),
        PATH=os.path.dirname(python) + os.pathsep + env.get("PATH", ""),
        SKIP_INTEGRATION="0",
        TZ="UTC",
        BLAST_OUT=out,
        BLAST_ROOT=root,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED="0",
    )
    env.pop("BLAST_CTX", None)
    env.pop("BLAST_READS", None)
    return env


EACH = r"""
import importlib, sys, blast_trace as bt
name = sys.argv[1]
err = ""
try:
    importlib.import_module(name)
except BaseException as exc:
    err = repr(exc)
bt.dump("mod." + name + ".json", kind="module", module=name, error=err)
"""

DEPS = r"""
import importlib, json, os, pkgutil, sys
root = os.path.realpath(os.environ["BLAST_ROOT"]).rstrip(os.sep) + os.sep
out = os.environ["BLAST_OUT"]
ids = {rel: i for i, rel in enumerate(json.load(open(os.path.join(out, "files.json"))))}
cache = {}
def fid_of(filename):
    fid = cache.get(filename, -1)
    if fid == -1:
        fid = None
        if filename and filename[0] != "<":
            real = os.path.realpath(filename)
            if real.startswith(root):
                fid = ids.get(real[len(root):])
        cache[filename] = fid
    return fid
deps = {}
ran = set()
mon = sys.monitoring
TOOL = 3
getframe = sys._getframe
def on_line(code, lineno):
    fid = fid_of(code.co_filename)
    if fid is None:
        return mon.DISABLE
    key = fid << 16 | lineno
    ran.add(key)
    if code.co_flags & 1:  # a function / lambda / comprehension body
        f = getframe(1).f_back
        site = 0
        while f is not None:
            c = f.f_code
            if not (c.co_flags & 1):
                ffid = fid_of(c.co_filename)
                if ffid is not None:
                    site = ffid << 16 | f.f_lineno
                    break
                if c.co_name == "<module>":
                    break
            f = f.f_back
        s = deps.get(key)
        if s is None:
            s = deps[key] = set()
        s.add(site)
mon.use_tool_id(TOOL, "blast-deps")
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.LINE)
import sqlglot
errors = {}
for info in sorted(pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."), key=lambda i: i.name):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except BaseException as exc:
        errors[info.name] = repr(exc)
mon.set_events(TOOL, 0)
with open(os.path.join(out, "defdeps.json"), "w") as fh:
    json.dump({"deps": {str(k): sorted(v) for k, v in deps.items()}, "ran": sorted(ran), "errors": errors}, fh)
"""

READS = r"""
import enum, importlib, json, os, pkgutil, sys, types
import blast_reads as br
out = os.environ["BLAST_OUT"]
import sqlglot
errors = {}
for info in sorted(pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."), key=lambda i: i.name):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except BaseException as exc:
        errors[info.name] = repr(exc)

def kind_of(cls, name, value):
    if br._is_def(cls, name, value):
        return None
    if isinstance(value, type):
        return "class"
    if isinstance(value, (dict, set, frozenset, list, tuple)):
        return type(value).__name__ + ":" + str(len(value))
    if isinstance(value, (bool, int, float, str, bytes, type(None), enum.Enum)):
        return type(value).__name__
    if callable(value) or isinstance(value, (classmethod, staticmethod, property)):
        return "callable"
    return "object"

classes = {}
seen = set()
def visit(cls):
    if id(cls) in seen or not cls.__module__.startswith("sqlglot"):
        return
    seen.add(id(cls))
    name = f"{cls.__module__}:{cls.__qualname__}"
    attrs = {}
    for attr, value in vars(cls).items():
        if isinstance(value, type) and value.__module__ == cls.__module__ and value.__qualname__.startswith(cls.__qualname__ + "."):
            visit(value)
        kind = kind_of(cls, attr, value)
        if kind and not (attr.startswith("__") and attr.endswith("__")):
            attrs[attr] = kind
    classes[name] = {
        "mro": [f"{c.__module__}:{c.__qualname__}" for c in cls.__mro__[1:] if c.__module__.startswith("sqlglot")],
        "meta": f"{type(cls).__module__}:{type(cls).__qualname__}",
        "attrs": attrs,
    }
modules = {}
for mname, mod in sorted(sys.modules.items()):
    if not (mname == "sqlglot" or mname.startswith("sqlglot.")) or mod is None:
        continue
    data = {}
    for attr, value in list(vars(mod).items()):
        if isinstance(value, type):
            if value.__module__ == mname:
                visit(value)
            continue
        if isinstance(value, types.ModuleType) or (attr.startswith("__") and attr.endswith("__")):
            continue
        if isinstance(value, types.FunctionType) and value.__module__ == mname and value.__qualname__ == attr:
            continue
        if isinstance(value, (dict, set, frozenset, list, tuple)):
            data[attr] = type(value).__name__ + ":" + str(len(value))
        elif isinstance(value, (bool, int, float, str, bytes, type(None), enum.Enum)):
            data[attr] = type(value).__name__
        elif callable(value):
            data[attr] = "callable"
        else:
            data[attr] = "object"
    modules[mname] = data
with open(os.path.join(out, "imports_reads.json"), "w") as fh:
    json.dump({"reads": br.snapshot_imports(), "classes": classes, "modules": modules, "errors": errors}, fh)
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["each", "deps", "reads"])
    ap.add_argument("--src")
    ap.add_argument("--instrumented")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    python = os.path.abspath(args.python)
    out = os.path.realpath(args.out)
    os.makedirs(out, exist_ok=True)
    root = os.path.realpath(args.instrumented if args.mode == "reads" else args.src)
    env = base_env(root, out, python)

    if args.mode == "reads":
        env["BLAST_READS"] = "1"
        subprocess.run([python, "-c", READS], cwd=root, env=env, check=True)
        return
    files = list_py(root)
    with open(os.path.join(out, "files.json"), "w") as fh:
        json.dump(files, fh)
    if args.mode == "deps":
        env["PYTHONPATH"] = root  # no sitecustomize: this mode brings its own tracer
        subprocess.run([python, "-c", DEPS], cwd=root, env=env, check=True)
        return
    modules = [module_name(f) for f in files if f.startswith("sqlglot/") and not f.endswith("__main__.py")]
    modules += ["tests.helpers"]

    def run(name):
        proc = subprocess.run([python, "-c", EACH, name], cwd=root, env=env, capture_output=True, text=True)
        return name, proc.returncode, proc.stderr[-300:]

    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        for name, code, err in pool.map(run, modules):
            if code:
                print(f"{name}: exit {code} {err}", file=sys.stderr)
    print(f"imported {len(modules)} modules one by one", file=sys.stderr)


if __name__ == "__main__":
    main()

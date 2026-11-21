#!/usr/bin/env python3
"""Fingerprint every class-level and module-level value of a sqlglot tree, and
compare two fingerprints.

    python tools/snapshot.py take --src <tree> [--out snap.json]      (run with the lock's python)
    python tools/snapshot.py diff base.json other.json

This is the exact way to find out what a definition-time edit changes: import
the edited tree, fingerprint it, compare with the pinned tree's fingerprint.
The comparison names the changed values with the same names (and dict keys)
that maps/reads.json uses, *including the derived ones* (a metaclass-built
trie, an inverse mapping, a subclass's `{**Parent.X, ...}` copy), so the tests
to consider are simply the readers of those names.  An edit that changes no
value (a comment, a reformatted literal, an annotation) gives an empty diff.

A fingerprint is {name: {"v": digest of the whole value, "k": {key: digest}}}
with name = "module:Class|ATTR" (or "module:|ATTR"), "k" present for dicts,
sets, lists and tuples.  Functions are compared by code (bytecode, constants,
names, defaults, closure values), not by identity or line number, so moving
code does not count as a change.  Dicts and sets are compared without regard
to order.  Take both fingerprints with the same PYTHONHASHSEED: a few values
depend on set iteration order.

`take` imports sqlglot from --src in *this* process: run it in a subprocess.
"""

from __future__ import annotations

import argparse
import enum
import functools
import hashlib
import importlib
import json
import os
import pkgutil
import re
import sys
import types

_ATOM = (bool, int, float, str, bytes, type(None), complex)
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_MAX_DEPTH = 12


def key_text(k):
    """Same key spelling as tools/tracer/blast_reads.py (_ck)."""
    t = type(k)
    if t is str:
        return "'" + k
    if t is int or t is bool or t is float or k is None:
        return repr(k)
    if t is tuple:
        parts = [key_text(x) for x in k]
        return None if None in parts else "(" + ",".join(parts) + ")"
    if isinstance(k, type):
        return f"<{k.__module__}.{k.__qualname__}>"
    if isinstance(k, enum.Enum):
        return f"{t.__name__}.{k.name}"
    return None


class Canon:
    def __init__(self):
        self.memo: dict[int, str] = {}
        self.active: set[int] = set()
        self.keep = []

    def code(self, co, depth) -> str:
        consts = [self.code(c, depth + 1) if isinstance(c, types.CodeType) else self.text(c, depth + 1)
                  for c in co.co_consts]
        return "code(" + "|".join([
            co.co_name, co.co_code.hex(), ",".join(consts), ",".join(co.co_names),
            ",".join(co.co_varnames), ",".join(co.co_freevars), str(co.co_argcount),
            str(co.co_kwonlyargcount), str(co.co_flags),
        ]) + ")"

    def text(self, v, depth=0) -> str:
        if isinstance(v, _ATOM):
            return f"{type(v).__name__}:{v!r}"
        if isinstance(v, enum.Enum):
            return f"enum:{type(v).__module__}.{type(v).__qualname__}.{v.name}={v.value!r}"
        if isinstance(v, type):
            return f"class:{v.__module__}.{v.__qualname__}"
        if isinstance(v, types.ModuleType):
            return f"module:{v.__name__}"
        ident = id(v)
        hit = self.memo.get(ident)
        if hit is not None:
            return hit
        if ident in self.active or depth > _MAX_DEPTH:
            return f"cycle:{type(v).__name__}"
        self.active.add(ident)
        try:
            out = self._compound(v, depth)
        finally:
            self.active.discard(ident)
        if len(out) > 200:
            out = "h:" + hashlib.blake2b(out.encode("utf-8", "surrogatepass"), digest_size=12).hexdigest()
        self.memo[ident] = out
        self.keep.append(v)
        return out

    def _compound(self, v, depth) -> str:
        d = depth + 1
        if isinstance(v, dict):
            # order-insensitive: several of sqlglot's tables are built by iterating sets of
            # classes, whose order follows memory addresses and differs between processes
            return type(v).__name__ + "{" + ",".join(sorted(f"{self.text(k, d)}:{self.text(x, d)}" for k, x in v.items())) + "}"
        if isinstance(v, (set, frozenset)):
            return type(v).__name__ + "{" + ",".join(sorted(self.text(x, d) for x in v)) + "}"
        if isinstance(v, (list, tuple)):
            return type(v).__name__ + "[" + ",".join(self.text(x, d) for x in v) + "]"
        if isinstance(v, (classmethod, staticmethod)):
            return type(v).__name__ + "(" + self.text(v.__func__, d) + ")"
        if isinstance(v, property):
            return "property(" + ",".join(self.text(f, d) for f in (v.fget, v.fset, v.fdel)) + ")"
        if isinstance(v, types.FunctionType):
            cells = []
            for cell in v.__closure__ or ():
                try:
                    cells.append(self.text(cell.cell_contents, d))
                except ValueError:
                    cells.append("empty")
            return "func(" + "|".join([
                v.__module__ or "", v.__qualname__, self.code(v.__code__, d),
                self.text(v.__defaults__, d), self.text(v.__kwdefaults__, d), ",".join(cells),
            ]) + ")"
        if isinstance(v, types.MethodType):
            return "method(" + self.text(v.__self__, d) + "," + self.text(v.__func__, d) + ")"
        if isinstance(v, (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType)):
            owner = getattr(v, "__self__", None) or getattr(v, "__objclass__", None)
            return f"builtin:{getattr(owner, '__name__', '')}.{v.__name__}"
        if isinstance(v, functools.partial):
            return "partial(" + ",".join([self.text(v.func, d), self.text(v.args, d), self.text(v.keywords, d)]) + ")"
        if isinstance(v, re.Pattern):
            return f"re:{v.pattern!r}:{v.flags}"
        if isinstance(v, types.CodeType):
            return self.code(v, d)
        # any other object: its type plus its state
        state = None
        try:
            state = vars(v)
        except TypeError:
            slots = [s for c in type(v).__mro__ for s in getattr(c, "__slots__", ())]
            if slots:
                state = {s: getattr(v, s) for s in slots if hasattr(v, s)}
        cls = type(v)
        head = f"obj:{cls.__module__}.{cls.__qualname__}"
        if state is None:
            try:
                # addresses in a repr differ from process to process
                return head + ":" + _ADDRESS.sub("0x", repr(v)) if cls.__repr__ is not object.__repr__ else head
            except Exception:
                return head
        return head + "{" + ",".join(f"{k}:{self.text(x, d)}" for k, x in sorted(state.items(), key=lambda kv: str(kv[0]))) + "}"


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def _is_def(cls, name, value) -> bool:
    """Bound by a plain def in this class (see tools/tracer/blast_reads.py)."""
    if isinstance(value, (types.MemberDescriptorType, types.GetSetDescriptorType,
                          types.WrapperDescriptorType, types.MethodDescriptorType)):
        return True
    if isinstance(value, property):
        value = value.fget
    elif isinstance(value, (classmethod, staticmethod)):
        value = value.__func__
    if isinstance(value, types.FunctionType):
        return value.__qualname__ == f"{cls.__qualname__}.{name}" and value.__module__ == cls.__module__
    return False


def fingerprint(value, canon: Canon) -> dict:
    out = {"v": _digest(canon.text(value))}
    keys = None
    if isinstance(value, dict):
        keys = {}
        for k, x in value.items():
            kt = key_text(k)
            if kt is None:
                kt = "?" + _digest(canon.text(k))
            keys[kt] = _digest(canon.text(x))
    elif isinstance(value, (set, frozenset, list, tuple)):
        keys = {}
        for i, x in enumerate(value):
            kt = key_text(x)
            if kt is None:
                kt = "?" + _digest(canon.text(x))
            keys[kt] = "1"
        if isinstance(value, (list, tuple)):
            out["o"] = _digest(canon.text(value))  # order
    if keys is not None:
        out["k"] = keys
    return out


def take(prefix: str = "sqlglot") -> dict:
    """Import every module of the package found on sys.path and fingerprint it."""
    pkg = importlib.import_module(prefix)
    errors = {}
    for info in sorted(pkgutil.walk_packages(pkg.__path__, prefix=prefix + "."), key=lambda i: i.name):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as exc:  # a module the edit broke
            errors[info.name] = f"{type(exc).__name__}: {exc}"[:300]
    canon = Canon()
    values = {}
    seen = set()

    def visit(cls):
        if id(cls) in seen or not (cls.__module__ == prefix or cls.__module__.startswith(prefix + ".")):
            return
        seen.add(id(cls))
        definer = f"{cls.__module__}:{cls.__qualname__}"
        for attr, value in list(vars(cls).items()):
            if isinstance(value, type) and value.__module__ == cls.__module__ and value.__qualname__.startswith(cls.__qualname__ + "."):
                visit(value)
            if attr.startswith("__") and attr.endswith("__") and attr != "__slots__":
                continue
            if attr in ("_abc_impl",) or _is_def(cls, attr, value):
                continue
            values[f"{definer}|{attr}"] = fingerprint(value, canon)
        values[f"{definer}|<bases>"] = {"v": _digest(",".join(f"{b.__module__}.{b.__qualname__}" for b in cls.__mro__[1:]))}

    for mname, mod in sorted(sys.modules.items()):
        if mod is None or not (mname == prefix or mname.startswith(prefix + ".")):
            continue
        for attr, value in list(vars(mod).items()):
            if isinstance(value, type):
                if value.__module__ == mname:
                    visit(value)
                continue
            if isinstance(value, types.ModuleType) or (attr.startswith("__") and attr.endswith("__")):
                continue
            if isinstance(value, types.FunctionType) and value.__module__ == mname and value.__qualname__ == attr:
                continue  # a def of this module: its body is line-covered
            values[f"{mname}:|{attr}"] = fingerprint(value, canon)
    return {"values": values, "errors": errors}


def diff(base: dict, other: dict, base_again: dict | None = None) -> dict:
    """-> {"changed": {name: None | [keys] | "order"}, "errors": {...}}

    None: the whole value changed (a scalar, or a container whose change cannot be
    pinned to keys).  [keys]: these dict keys / set or tuple members were added,
    removed or changed.  "order": same members in another order (list / tuple).
    `base_again` is a second fingerprint of the *unchanged* tree taken in another
    process: values that differ between the two (tuples made from sets of classes
    are ordered by memory address) are unstable and their order is ignored.
    """
    a, b = base["values"], other["values"]
    unstable = set()
    if base_again is not None:
        c = base_again["values"]
        unstable = {name for name in a if a[name].get("v") != c.get(name, {}).get("v")}
    changed = {}
    for name in sorted(set(a) | set(b)):
        x, y = a.get(name), b.get(name)
        if x == y:
            continue
        if x is None or y is None or "k" not in x or "k" not in y:
            changed[name] = None
            continue
        keys = sorted(k for k in set(x["k"]) | set(y["k"]) if x["k"].get(k) != y["k"].get(k))
        if any(k.startswith("?") for k in keys):
            changed[name] = None
        elif keys:
            changed[name] = keys
        elif name not in unstable:
            changed[name] = "order"
    return {"changed": changed, "errors": other.get("errors", {})}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take")
    t.add_argument("--src", required=True)
    t.add_argument("--out")
    d = sub.add_parser("diff")
    d.add_argument("base")
    d.add_argument("other")
    d.add_argument("--base-again")
    args = ap.parse_args()
    if args.cmd == "take":
        sys.path.insert(0, os.path.realpath(args.src))
        sys.dont_write_bytecode = True
        snap = take()
        text = json.dumps(snap, separators=(",", ":"), sort_keys=True)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(text)
        else:
            sys.stdout.write(text)
    else:
        again = json.load(open(args.base_again)) if args.base_again else None
        print(json.dumps(diff(json.load(open(args.base)), json.load(open(args.other)), again), indent=1))


if __name__ == "__main__":
    main()

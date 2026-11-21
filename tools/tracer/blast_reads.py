"""Run-time half of the "reads" pass (map building only).

tools/instrument.py rewrites a copy of sqlglot so that every load of a
class-level / module-level data name goes through one of the functions below:

    X.NAME           ->  _bt.attr(X, "NAME")
    X.NAME[k]        ->  _bt.sub(X, "NAME", k)
    X.NAME.get(k)    ->  _bt.get(X, "NAME", k)
    k in X.NAME      ->  _bt.isin(k, X, "NAME")
    GLOBAL           ->  _bt.g(GLOBAL, id)        (and gsub / gget / gin)
    NAME (class body)->  _bt.c(NAME, id)
    getattr(a, b)    ->  _bt.ga(a, b)

Each returns exactly what the original expression returns and notes
(definer, name, key): *definer* is the class in whose body the name is bound
(found through the MRO of the object it was read from, so a read through a
subclass or an instance is charged to the class that defines the value) or the
module; *key* is the looked-up key when the read is one of the key forms, or
None for "the whole value was taken".

Reads made while a sqlglot module is being imported (class bodies, metaclasses,
module-level code and whatever they call) are kept apart, per definition-time
statement: they say what a class-level value was derived from.  Reads made at
any other time belong to the running test.
"""

from __future__ import annotations

import enum
import json
import os
import types
from threading import get_ident

_ModuleType = types.ModuleType
_SKIP_TYPES = (types.MemberDescriptorType, types.GetSetDescriptorType,
               types.WrapperDescriptorType, types.MethodDescriptorType)


def _is_def(cls, name, value) -> bool:
    """True when `name` is bound in `cls` by a plain `def` (possibly decorated with
    property / classmethod / staticmethod).  Such a value is code: its body is seen
    by the line tracer.  A function *assigned* in the class body (the result of a
    factory call, a lambda) is data: the line that builds it runs at import."""
    if isinstance(value, _SKIP_TYPES):
        return True
    if isinstance(value, property):
        value = value.fget
    elif isinstance(value, (classmethod, staticmethod)):
        value = value.__func__
    if isinstance(value, types.FunctionType):
        return value.__qualname__ == f"{cls.__qualname__}.{name}" and value.__module__ == cls.__module__
    return False

depth = 0  # imports in progress, all threads
_threads: dict = {}  # thread id -> [own depth, current statement id, saved statement ids]

test_reads: set = set()
used: set = set()  # run-time classes whose tracked names were read
imp_reads: dict = {}  # statement id -> set of reads

_cache: dict = {}  # (id(type or class or module), name) -> (definer, actual) | None
_keep: list = []  # keep cached objects alive so ids stay unique
_ids: list = []  # id -> (definer, name) for the g / c forms
_ck_cache: dict = {}

_root = os.environ.get("BLAST_ROOT", "")
if _root and os.path.exists(os.path.join(_root, "blast_ids.json")):
    with open(os.path.join(_root, "blast_ids.json")) as _fh:
        _ids = [tuple(x) for x in json.load(_fh)]


# ---- import bookkeeping (called from the rewritten modules) -----------------
# "Importing" is per thread: a helper thread of a process pool may import a module
# (while unpickling) at the moment the test's own thread reads something.
def enter(name):
    global depth
    tid = get_ident()
    st = _threads.get(tid)
    if st is None:
        st = _threads[tid] = [0, 0, []]
    st[2].append(st[1])
    st[0] += 1
    depth += 1


def leave(name):
    global depth
    tid = get_ident()
    st = _threads[tid]
    st[1] = st[2].pop()
    st[0] -= 1
    depth -= 1
    if st[0] == 0:
        del _threads[tid]


def at(sid):
    st = _threads.get(get_ident())
    if st is not None:
        st[1] = sid


def _import_stmt():
    """Statement id the current thread is importing for, or None when it is not importing."""
    if not depth:
        return None
    st = _threads.get(get_ident())
    return st[1] if st is not None and st[0] else None


# ---- recording --------------------------------------------------------------
def _ck(k):
    """Canonical, process-independent text of a key; None when there is none."""
    t = type(k)
    if t is str:
        return "'" + k
    if t is int or t is bool or t is float or k is None:
        return repr(k)
    if t is tuple:
        parts = [_ck(x) for x in k]
        if None in parts:
            return None
        return "(" + ",".join(parts) + ")"
    ident = id(k)
    hit = _ck_cache.get(ident)
    if hit is not None:
        return hit
    if isinstance(k, type):
        text = f"<{k.__module__}.{k.__qualname__}>"
    elif isinstance(k, enum.Enum):
        text = f"{t.__name__}.{k.name}"
    else:
        return None
    _ck_cache[ident] = text
    _keep.append(k)
    return text


def _resolve(obj, name):
    t = type(obj)
    result = None
    if t is _ModuleType:
        result = (obj.__name__ + ":", None)
    else:
        if isinstance(obj, type):
            chain = obj.__mro__ + t.__mro__
            actual = obj
        else:
            chain = t.__mro__
            actual = t
        for cls in chain:
            d = cls.__dict__
            if name in d:
                if cls.__module__ != "builtins" and not _is_def(cls, name, d[name]):
                    result = (f"{cls.__module__}:{cls.__qualname__}",
                              f"{actual.__module__}:{actual.__qualname__}")
                break
    return result


def _note(obj, name, key):
    ident = (id(obj) if isinstance(obj, (type, _ModuleType)) else id(type(obj)), name)
    hit = _cache.get(ident, 0)
    if hit == 0:
        try:
            hit = _resolve(obj, name)
        except Exception:
            hit = None
        _cache[ident] = hit
        _keep.append(obj if isinstance(obj, (type, _ModuleType)) else type(obj))
    if hit is None:
        return
    item = (hit[0], name, key)
    stmt = _import_stmt() if depth else None
    if stmt is not None:
        s = imp_reads.get(stmt)
        if s is None:
            s = imp_reads[stmt] = set()
        s.add(item)
    else:
        test_reads.add(item)
        if hit[1] is not None:
            used.add(hit[1])


def _note_id(i, key):
    definer, name = _ids[i]
    item = (definer, name, key)
    stmt = _import_stmt() if depth else None
    if stmt is not None:
        s = imp_reads.get(stmt)
        if s is None:
            s = imp_reads[stmt] = set()
        s.add(item)
    else:
        test_reads.add(item)


_KEYED = (dict, set, frozenset, list, tuple)


def attr(obj, name):
    val = getattr(obj, name)
    _note(obj, name, None)
    return val


def sub(obj, name, k):
    val = getattr(obj, name)
    _note(obj, name, _ck(k) if isinstance(val, _KEYED) else None)
    return val[k]


def get(obj, name, k, *default):
    val = getattr(obj, name)
    _note(obj, name, _ck(k) if isinstance(val, dict) else None)
    return val.get(k, *default)


def isin(k, obj, name):
    val = getattr(obj, name)
    _note(obj, name, _ck(k) if isinstance(val, _KEYED) else None)
    return k in val


def ga(obj, name, *default):
    val = getattr(obj, name, *default)
    if type(name) is str:
        _note(obj, name, None)
    return val


def g(val, i):
    _note_id(i, None)
    return val


def gsub(val, i, k):
    _note_id(i, _ck(k) if isinstance(val, _KEYED) else None)
    return val[k]


def gget(val, i, k, *default):
    _note_id(i, _ck(k) if isinstance(val, dict) else None)
    return val.get(k, *default)


def gin(k, val, i):
    _note_id(i, _ck(k) if isinstance(val, _KEYED) else None)
    return k in val


c = g


# ---- output -----------------------------------------------------------------
def reset():
    global depth
    test_reads.clear()
    used.clear()
    depth = 0  # a fork keeps only the forking thread, which is not importing
    _threads.clear()


def _pack(items):
    out: dict = {}
    for definer, name, key in items:
        out.setdefault(f"{definer}|{name}", []).append(key)
    for keys in out.values():
        keys.sort(key=lambda k: (k is not None, k or ""))
    return out


def snapshot() -> dict:
    return {"reads": _pack(test_reads), "used": sorted(used)}


def snapshot_imports() -> dict:
    return {str(sid): _pack(items) for sid, items in imp_reads.items()}

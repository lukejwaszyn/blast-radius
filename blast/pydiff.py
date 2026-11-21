"""Structural comparison of a Python file before and after a patch.

`diff_python(old_text, applied)` parses both versions and walks them in
parallel.  The result says, in terms of the *old* file's lines:

- nothing, when only comments, blank lines or layout changed (equal ASTs);
- `broken`, when the new text does not compile (syntax, indentation, encoding,
  `return` outside a function ...);
- otherwise a list of `Mark`s: which statements changed, were removed or were
  added, and whether each runs at call time (inside a function or lambda body)
  or at definition time (module level / class body, i.e. when the module is
  imported).

Statements whose lines the patch does not touch are never dumped or compared:
their text is identical, so their trees are.  Only what overlaps a hunk is
looked at closely, which keeps this at parse cost.
"""

from __future__ import annotations

import ast
import difflib

from .diffparse import Applied

_BODY_FIELDS = ("body", "orelse", "finalbody")
_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ())
_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
_CONST_UNARY = (ast.USub, ast.UAdd, ast.Not, ast.Invert)


class Mark:
    __slots__ = ("kind", "lo", "hi", "scope", "info")

    def __init__(self, kind: str, lo: int, hi: int, scope: str, **info):
        self.kind = kind
        self.lo = lo
        self.hi = hi
        self.scope = scope
        self.info = info

    def __repr__(self):
        extra = {k: v for k, v in self.info.items() if not isinstance(v, ast.AST)}
        return f"<{self.kind} {self.lo}-{self.hi} scope={self.scope!r} {extra}>"


class PyDiff:
    def __init__(self):
        self.broken: str | None = None  # why the new text cannot be compiled
        self.same_ast = False
        self.marks: list[Mark] = []
        self.future_annotations = False
        self.old_tree: ast.Module | None = None
        self.new_tree: ast.Module | None = None


def first_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        return min(node.lineno, min(d.lineno for d in decorators))
    return node.lineno


def parse_source(text: str, cache: dict | None = None, key=None):
    """ast.parse of text given as str (bytes round-tripped so a coding cookie is honoured)."""
    if cache is not None and key in cache:
        return cache[key]
    tree = ast.parse(text.encode("utf-8", "surrogateescape"))
    if cache is not None:
        cache[key] = tree
    return tree


def bound_names(stmts) -> list[str]:
    """Names a list of definition-time statements binds (not entering function bodies)."""
    out: list[str] = []

    def target(t):
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, ast.Starred):
            target(t.value)
        elif isinstance(t, (ast.Attribute, ast.Subscript)):
            base = t.value
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if isinstance(base, ast.Name):
                out.append(base.id)

    def visit(s):
        if isinstance(s, ast.Assign):
            for t in s.targets:
                target(t)
        elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
            target(s.target)
        elif isinstance(s, _FUNC + (ast.ClassDef,)):
            out.append(s.name)
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            for a in s.names:
                out.append(a.asname or a.name.split(".")[0])
        elif isinstance(s, ast.Delete):
            for t in s.targets:
                target(t)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            target(s.target)
            for f in _BODY_FIELDS:
                for c in getattr(s, f, ()):
                    visit(c)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                if item.optional_vars is not None:
                    target(item.optional_vars)
            for c in s.body:
                visit(c)
        elif isinstance(s, _COMPOUND) or (hasattr(ast, "Match") and isinstance(s, ast.Match)):
            for f in _BODY_FIELDS:
                for c in getattr(s, f, ()):
                    visit(c)
            for h in getattr(s, "handlers", ()):
                for c in h.body:
                    visit(c)
            for case in getattr(s, "cases", ()):
                for c in case.body:
                    visit(c)
        elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            out.append("__doc__")

    for s in stmts:
        visit(s)
    return out


def import_bindings(node) -> dict[str, tuple[str, str | None, int]]:
    """{bound name: (module, imported name or None, relative level)} of an import statement."""
    out = {}
    if isinstance(node, ast.Import):
        for a in node.names:
            out[a.asname or a.name.split(".")[0]] = (a.name, None, 0)
    elif isinstance(node, ast.ImportFrom):
        for a in node.names:
            out[a.asname or a.name] = (node.module or "", a.name, node.level or 0)
    return out


def _sites(a, b, trail, out):
    """Where two trees of one statement differ: (old-side ancestors + the old node, the new node)."""
    if type(a) is not type(b):
        out.append((trail + [a], b))
        return
    here = trail + [a]
    for name in a._fields:
        va = getattr(a, name, None)
        vb = getattr(b, name, None)
        if isinstance(va, list) or isinstance(vb, list):
            if not (isinstance(va, list) and isinstance(vb, list)) or len(va) != len(vb):
                out.append((here, b))
                return
            for x, y in zip(va, vb):
                if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                    _sites(x, y, here, out)
                elif x != y or type(x) is not type(y):
                    out.append((here, b))
                    return
        elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
            if isinstance(va, ast.AST) and isinstance(vb, ast.AST):
                _sites(va, vb, here, out)
            else:
                out.append((here, b))
                return
        elif va != vb or type(va) is not type(vb):
            out.append((here, b))
            return


def _constantish(node) -> bool:
    if isinstance(node, ast.Constant):
        return True
    return isinstance(node, ast.UnaryOp) and isinstance(node.op, _CONST_UNARY) and isinstance(node.operand, ast.Constant)


def _display_delta(old, new):
    """Keys / members that differ between two dict, set, list or tuple displays, or None (whole)."""
    if isinstance(old, ast.Dict) and isinstance(new, ast.Dict):
        def table(d):
            spreads, entries = [], {}
            for k, v in zip(d.keys, d.values):
                if k is None:
                    spreads.append(ast.dump(v))
                else:
                    key = ast.dump(k)
                    if key in entries:
                        return None, None
                    entries[key] = (k, ast.dump(v))
            return spreads, entries

        so, eo = table(old)
        sn, en = table(new)
        if so is None or sn is None or so != sn:
            return None
        keys = []
        for key, (node, value) in eo.items():
            if key not in en or en[key][1] != value:
                keys.append(node)
        for key, (node, _value) in en.items():
            if key not in eo:
                keys.append(node)
        return keys
    if isinstance(old, (ast.Set, ast.List, ast.Tuple)) and type(old) is type(new):
        do = {ast.dump(e): e for e in old.elts}
        dn = {ast.dump(e): e for e in new.elts}
        if any(isinstance(e, ast.Starred) for e in list(do.values()) + list(dn.values())):
            return None
        keys = [e for k, e in do.items() if k not in dn] + [e for k, e in dn.items() if k not in do]
        if not keys and not isinstance(old, ast.Set):
            return None  # same members, other order
        return keys
    return None


def _args_signature(args: ast.arguments, with_annotations: bool):
    def one(a):
        if a is None:
            return None
        return a.arg, (ast.dump(a.annotation) if with_annotations and a.annotation is not None else None)

    return (
        [one(a) for a in args.posonlyargs], [one(a) for a in args.args], one(args.vararg),
        [one(a) for a in args.kwonlyargs], one(args.kwarg),
        [ast.dump(d) for d in args.defaults], [ast.dump(d) if d is not None else None for d in args.kw_defaults],
    )


class _Ctx:
    __slots__ = ("scope", "in_func", "func")

    def __init__(self, scope: str, in_func: bool, func: str):
        self.scope = scope      # qualified name of the enclosing class chain ("" = module level)
        self.in_func = in_func  # inside a function body: call time
        self.func = func        # qualified name of the enclosing function, if any


class _Walker:
    def __init__(self, applied: Applied, result: PyDiff):
        self.ap = applied
        self.res = result
        self.to_compile: list[ast.AST] = []

    # ------------------------------------------------------------------ keys
    def _key_old(self, s):
        first, last = first_line(s), s.end_lineno
        if not self.ap.old_touched(first, last):
            return ("u", type(s).__name__, first, last)
        return self._touched_key(s)

    def _key_new(self, s):
        first, last = first_line(s), s.end_lineno
        if not self.ap.new_touched(first, last):
            return ("u", type(s).__name__, self.ap.new_to_old[first - 1], self.ap.new_to_old[last - 1])
        return self._touched_key(s)

    @staticmethod
    def _touched_key(s):
        if isinstance(s, _FUNC + (ast.ClassDef,)):
            return ("named", type(s).__name__, s.name)
        return ("d", ast.dump(s))

    def mark(self, kind, lo, hi, scope, **info):
        self.res.marks.append(Mark(kind, lo, hi, scope, **info))

    # ---------------------------------------------------------------- bodies
    def body(self, old, new, ctx: _Ctx, parent=None):
        ko = [self._key_old(s) for s in old]
        kn = [self._key_new(s) for s in new]
        # equal statements at both ends need no alignment ("named" keys only say
        # "a touched def/class of this name": those pairs are still compared)
        lo, lim = 0, min(len(ko), len(kn))
        while lo < lim and ko[lo] == kn[lo] and ko[lo][0] != "named":
            lo += 1
        end_o, end_n = len(ko), len(kn)
        while end_o > lo and end_n > lo and ko[end_o - 1] == kn[end_n - 1] and ko[end_o - 1][0] != "named":
            end_o -= 1
            end_n -= 1
        if lo == end_o and lo == end_n:
            return
        mo, mn = ko[lo:end_o], kn[lo:end_n]
        if len(mo) == 1 and len(mn) == 1:
            ops = [("replace", 0, 1, 0, 1)]
        else:
            ops = difflib.SequenceMatcher(None, mo, mn, autojunk=False).get_opcodes()
        for tag, i1, i2, j1, j2 in ops:
            if tag == "equal":
                for d in range(i2 - i1):
                    if mo[i1 + d][0] == "named":
                        self.stmt(old[lo + i1 + d], new[lo + j1 + d], ctx)
            elif tag == "replace" and i2 - i1 == j2 - j1:
                for d in range(i2 - i1):
                    self.stmt(old[lo + i1 + d], new[lo + j1 + d], ctx)
            else:
                for i in range(i1, i2):
                    self.deleted(old[lo + i], ctx)
                anchors = self._anchors(old, lo + i1 - 1, lo + i2, parent)
                for j in range(j1, j2):
                    self.inserted(new[lo + j], anchors, ctx)

    @staticmethod
    def _anchors(old, prev_index, next_index, parent):
        anchors = []
        if 0 <= prev_index < len(old):
            prev = old[prev_index]
            anchors.append((first_line(prev), prev.end_lineno))
        if 0 <= next_index < len(old):
            nxt = old[next_index]
            anchors.append((first_line(nxt), nxt.end_lineno))
        if parent is not None and (not anchors or prev_index < 0):
            # first statement of a block: whoever enters the block
            anchors.append((first_line(parent), parent.body[0].lineno if getattr(parent, "body", None) else parent.end_lineno))
        return anchors

    # ------------------------------------------------------------- one pair
    def stmt(self, o, n, ctx: _Ctx):
        if type(o) is not type(n):
            self.deleted(o, ctx)
            self.inserted(n, [(first_line(o), o.end_lineno)], ctx, replaced=True)
            return
        if isinstance(o, _FUNC):
            self._function(o, n, ctx)
        elif isinstance(o, ast.ClassDef):
            self._class(o, n, ctx)
        elif isinstance(o, _COMPOUND):
            self._compound(o, n, ctx)
        else:
            self._leaf(o, n, ctx)

    def _function(self, o, n, ctx):
        outer = not ctx.in_func
        name_changed = o.name != n.name
        deco_changed = [ast.dump(d) for d in o.decorator_list] != [ast.dump(d) for d in n.decorator_list]
        sig_changed = _args_signature(o.args, False) != _args_signature(n.args, False)
        params_changed = ast.dump(ast.Tuple(elts=list(getattr(o, "type_params", []) or []), ctx=ast.Load())) != \
            ast.dump(ast.Tuple(elts=list(getattr(n, "type_params", []) or []), ctx=ast.Load()))
        ann_changed = (_args_signature(o.args, True) != _args_signature(n.args, True)
                       or (ast.dump(o.returns) if o.returns else None) != (ast.dump(n.returns) if n.returns else None))
        body_range = (o.body[0].lineno, o.end_lineno)
        if name_changed or deco_changed or sig_changed or params_changed or ann_changed or type(o) is not type(n):
            if ctx.in_func:
                self.mark("call", first_line(o), o.end_lineno, ctx.func, why="nested def header")
            else:
                self.mark("defhdr", first_line(o), o.body[0].lineno - 1 if o.body[0].lineno > o.lineno else o.lineno,
                          ctx.scope, name=o.name, new_name=n.name, body=body_range,
                          annotations_only=not (name_changed or deco_changed or sig_changed or params_changed),
                          name_changed=name_changed)
        qual = (ctx.scope + "." if ctx.scope else "") + o.name if not ctx.in_func else ctx.func + "." + o.name
        before = len(self.res.marks)
        self.body(o.body, n.body, _Ctx(ctx.scope, True, qual), parent=o)
        if outer and len(self.res.marks) != before:
            self.to_compile.append(n)
        elif outer and (name_changed or deco_changed or sig_changed or params_changed or ann_changed):
            self.to_compile.append(n)

    def _class(self, o, n, ctx):
        header_o = (o.name, [ast.dump(b) for b in o.bases], [ast.dump(k) for k in o.keywords],
                    [ast.dump(d) for d in o.decorator_list])
        header_n = (n.name, [ast.dump(b) for b in n.bases], [ast.dump(k) for k in n.keywords],
                    [ast.dump(d) for d in n.decorator_list])
        qual = (ctx.scope + "." if ctx.scope else "") + o.name
        if header_o != header_n:
            if ctx.in_func:
                self.mark("call", first_line(o), o.end_lineno, ctx.func, why="nested class header")
            else:
                self.mark("classhdr", first_line(o), o.lineno, ctx.scope, name=o.name, new_name=n.name,
                          range=(first_line(o), o.end_lineno), qual=qual)
        if ctx.in_func:
            self.body(o.body, n.body, _Ctx(ctx.scope, True, ctx.func + "." + o.name), parent=o)
        else:
            self.body(o.body, n.body, _Ctx(qual, False, ""), parent=o)

    def _header_end(self, node) -> int:
        end = node.lineno
        for name in node._fields:
            if name in _BODY_FIELDS or name in ("handlers", "cases"):
                continue
            value = getattr(node, name, None)
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, ast.AST):
                    for sub in ast.walk(v):
                        e = getattr(sub, "end_lineno", None)
                        if e is not None and e > end:
                            end = e
        return end

    def _compound(self, o, n, ctx):
        def header(node):
            out = []
            for name in node._fields:
                if name in _BODY_FIELDS or name in ("handlers", "cases"):
                    continue
                v = getattr(node, name, None)
                if isinstance(v, ast.AST):
                    out.append((name, ast.dump(v)))
                elif isinstance(v, list):
                    out.append((name, [ast.dump(x) if isinstance(x, ast.AST) else x for x in v]))
                else:
                    out.append((name, v))
            return out

        handlers_o, handlers_n = getattr(o, "handlers", []), getattr(n, "handlers", [])
        if len(handlers_o) != len(handlers_n):
            self.deleted(o, ctx)
            self.inserted(n, [(first_line(o), o.end_lineno)], ctx, replaced=True)
            return
        if header(o) != header(n):
            if ctx.in_func:
                self.mark("call", o.lineno, self._header_end(o), ctx.func, why="header")
            else:
                self.mark("block", first_line(o), o.end_lineno, ctx.scope, op="change",
                          names=sorted(set(bound_names([o]) + bound_names([n]))))
                self.to_compile.append(n)
        for name in _BODY_FIELDS:
            if hasattr(o, name):
                bo, bn = getattr(o, name), getattr(n, name)
                if bool(bo) != bool(bn):
                    # an else / finally branch appeared or vanished
                    if ctx.in_func:
                        self.mark("call", first_line(o), o.end_lineno, ctx.func, why="branch added or removed")
                    else:
                        self.mark("block", first_line(o), o.end_lineno, ctx.scope, op="change",
                                  names=sorted(set(bound_names([o]) + bound_names([n]))))
                        self.to_compile.append(n)
                    continue
                self.body(bo, bn, ctx, parent=o)
        for ho, hn in zip(handlers_o, handlers_n):
            ho_head = (ast.dump(ho.type) if ho.type else None, ho.name)
            hn_head = (ast.dump(hn.type) if hn.type else None, hn.name)
            if ho_head != hn_head:
                if ctx.in_func:
                    self.mark("call", o.lineno, o.end_lineno, ctx.func, why="except clause")
                else:
                    self.mark("block", first_line(o), o.end_lineno, ctx.scope, op="change",
                              names=sorted(set(bound_names([o]) + bound_names([n]))))
                    self.to_compile.append(n)
            self.body(ho.body, hn.body, ctx, parent=o)

    def _leaf(self, o, n, ctx):
        if ctx.in_func:
            self.mark("call", o.lineno, o.end_lineno, ctx.func, why="statement")
            return
        self.to_compile.append(n)
        if isinstance(o, (ast.Import, ast.ImportFrom)):
            self._import(o, n, ctx, "change")
            return
        sites: list = []
        _sites(o, n, [], sites)
        if not sites:
            return
        def_sites = []
        for trail, new_node in sites:
            lam = None
            for i in range(len(trail) - 1):
                if isinstance(trail[i], ast.Lambda) and trail[i + 1] is trail[i].body:
                    lam = trail[i]
                    break
            if lam is not None:
                self.mark("lambda", lam.body.lineno, lam.body.end_lineno, ctx.scope, stmt=(o.lineno, o.end_lineno))
            else:
                def_sites.append((trail, new_node))
        if not def_sites:
            return
        targets = bound_names([o])
        new_targets = bound_names([n])
        keys = None
        value_o, value_n = getattr(o, "value", None), getattr(n, "value", None)
        if isinstance(o, (ast.Assign, ast.AnnAssign)) and targets == new_targets and \
                isinstance(value_o, (ast.Dict, ast.Set, ast.List, ast.Tuple)) and type(value_o) is type(value_n):
            keys = []
            for trail, _new in def_sites:
                if len(trail) < 2 or trail[1] is not value_o:
                    keys = None
                    break
                if len(trail) == 2:
                    delta = _display_delta(value_o, value_n)
                    if delta is None:
                        keys = None
                        break
                    keys.extend(delta)
                    continue
                child = trail[2]
                if isinstance(value_o, ast.Dict):
                    found = False
                    for idx, (k, v) in enumerate(zip(value_o.keys, value_o.values)):
                        if child is k or child is v:
                            found = True
                            if k is None:
                                keys = None
                            else:
                                keys.append(k)
                                if child is k and value_n.keys[idx] is not None:
                                    keys.append(value_n.keys[idx])
                            break
                    if not found or keys is None:
                        keys = None
                        break
                else:
                    try:
                        idx = value_o.elts.index(child)
                    except ValueError:
                        keys = None
                        break
                    keys.extend([child, value_n.elts[idx]])
        const_only = all(_constantish(trail[-1]) and _constantish(new_node) for trail, new_node in def_sites)
        self.mark("value", o.lineno, o.end_lineno, ctx.scope, op="change",
                  targets=sorted(set(targets + new_targets)), keys=keys, const_only=const_only,
                  stmt=(o.lineno, o.end_lineno), node=o, new_node=n)

    def _import(self, o, n, ctx, op):
        old = import_bindings(o) if o is not None else {}
        new = import_bindings(n) if n is not None else {}
        removed = sorted(k for k in old if k not in new)
        added = sorted(k for k in new if k not in old)
        rebound = sorted(k for k in old if k in new and old[k] != new[k])
        node = o if o is not None else n
        self.mark("import", node.lineno if o is not None else 0, node.end_lineno if o is not None else 0, ctx.scope,
                  op=op, removed=removed, added=added, rebound=rebound, old=old, new=new)

    # ----------------------------------------------------- removed / added
    def deleted(self, o, ctx: _Ctx):
        first, last = first_line(o), o.end_lineno
        if ctx.in_func:
            self.mark("call", first, last, ctx.func, why="deleted")
            return
        if isinstance(o, _FUNC):
            self.mark("defdel", first, last, ctx.scope, name=o.name, body=(o.body[0].lineno, o.end_lineno))
        elif isinstance(o, ast.ClassDef):
            self.mark("classdel", first, last, ctx.scope, name=o.name, range=(first, last),
                      qual=(ctx.scope + "." if ctx.scope else "") + o.name)
        elif isinstance(o, (ast.Import, ast.ImportFrom)):
            self._import(o, None, ctx, "delete")
        elif isinstance(o, _COMPOUND) or (hasattr(ast, "Match") and isinstance(o, ast.Match)):
            self.mark("block", first, last, ctx.scope, op="delete", names=sorted(set(bound_names([o]))))
        else:
            self.mark("value", first, last, ctx.scope, op="delete", targets=sorted(set(bound_names([o]))),
                      keys=None, const_only=False, stmt=(first, last), node=o, new_node=None)

    def inserted(self, n, anchors, ctx: _Ctx, replaced=False):
        if ctx.in_func:
            if not replaced:
                for lo, hi in anchors:
                    self.mark("call", lo, hi, ctx.func, why="neighbour")
            return
        self.to_compile.append(n)
        if isinstance(n, _FUNC):
            self.mark("defins", 0, 0, ctx.scope, name=n.name, anchors=anchors, node=n)
        elif isinstance(n, ast.ClassDef):
            self.mark("classins", 0, 0, ctx.scope, name=n.name, anchors=anchors, node=n,
                      qual=(ctx.scope + "." if ctx.scope else "") + n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            self._import(None, n, ctx, "insert")
            self.res.marks[-1].info["anchors"] = anchors
        elif isinstance(n, _COMPOUND) or (hasattr(ast, "Match") and isinstance(n, ast.Match)):
            self.mark("block", 0, 0, ctx.scope, op="insert", names=sorted(set(bound_names([n]))), anchors=anchors)
        else:
            trivial = isinstance(n, ast.Pass) or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            self.mark("value", 0, 0, ctx.scope, op="insert", targets=sorted(set(bound_names([n]))), keys=None,
                      const_only=trivial, stmt=None, node=None, new_node=n, anchors=anchors, trivial=trivial)


def diff_python(old_text: str, applied: Applied, cache: dict | None = None, key=None, filename: str = "<patched>") -> PyDiff:
    res = PyDiff()
    new_text = applied.new_text()
    try:
        new_tree = ast.parse(new_text.encode("utf-8", "surrogateescape"), filename)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        res.broken = f"{type(exc).__name__}: {exc}"
        return res
    try:
        old_tree = parse_source(old_text, cache, key)
    except (SyntaxError, ValueError, RecursionError) as exc:  # the pinned file itself: not expected
        res.marks.append(Mark("unknown", 1, len(applied.old_lines), "", why=f"pinned file does not parse: {exc}"))
        return res
    res.old_tree, res.new_tree = old_tree, new_tree
    for s in old_tree.body:
        if isinstance(s, ast.ImportFrom) and s.module == "__future__" and any(a.name == "annotations" for a in s.names):
            res.future_annotations = True
    walker = _Walker(applied, res)
    walker.body(old_tree.body, new_tree.body, _Ctx("", False, ""))
    if not res.marks and not walker.to_compile:
        res.same_ast = True
        return res
    # errors the parser accepts and the compiler refuses (`return` at class level,
    # `break` outside a loop, duplicate argument names ...): compile what changed
    for node in walker.to_compile:
        try:
            compile(ast.Module(body=[node], type_ignores=[]), filename, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, RecursionError) as exc:
            res.broken = f"{type(exc).__name__}: {exc}"
            return res
    return res

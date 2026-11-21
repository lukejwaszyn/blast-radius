#!/usr/bin/env python3
"""Make the instrumented copy of sqlglot used by the "reads" pass.

    python tools/instrument.py --src <sqlglot checkout> --static <dir with static.json> --out <dir>

Copies the checkout to --out and rewrites every file under sqlglot/ (and the
expressions of every file under tests/, which read sqlglot's tables too) so
that each load of a tracked name is reported to tools/tracer/blast_reads.py
(see that file for the forms).  Tracked names are

* names bound by an assignment in a class body anywhere in sqlglot
  (FUNCTIONS, TRANSFORMS, arg_types, WEEK_OFFSET, enum members ...), plus names
  a metaclass / __init_subclass__ sets on a class (``klass.NAME = ...``);
* names bound by an assignment at module level (constants, tables), also when
  another module gets them with ``from x import NAME``.

Names bound by ``def`` are not tracked: a function's own lines are seen by the
line tracer.  The rewrite never changes what an expression evaluates to.

Every definition-time statement additionally announces itself
(``_bt.at(<file id> << 16 | <statement index in static.json>)``) so reads made
while importing can be charged to the statement that made them.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil

RT = "_bt"


def collect_names(static: dict):
    class_names: set[str] = set()
    module_data: dict[str, set[str]] = {}  # module -> names bound by assignment at module level
    for rel, info in static["files"].items():
        if not rel.startswith("sqlglot/"):
            continue
        for scope, binds in info["binds"].items():
            for name, kind in binds.items():
                if kind != "data" or (name.startswith("__") and name.endswith("__")):
                    continue
                if scope == "":
                    module_data.setdefault(info["module"], set()).add(name)
                else:
                    class_names.add(name)
    return class_names, module_data


def dynamic_class_attrs(root: str, files) -> set[str]:
    """``klass.NAME = ...`` / ``cls.NAME = ...`` inside functions: set by metaclasses etc."""
    out = set()
    for rel in files:
        tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for tgt in targets:
                if (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                        and tgt.value.id in ("klass", "cls")):
                    out.add(tgt.attr)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr"
                    and len(node.args) == 3 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                out.add(node.args[1].value)
    return out


def local_names(func) -> set[str]:
    out = set()
    declared_global = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not func:
            out.add(node.name)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Global):
            declared_global.update(node.names)
    return out - declared_global


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel, fid, info, attr_names, globals_here, ids, id_index):
        self.rel = rel
        self.fid = fid
        self.info = info
        self.attr_names = attr_names
        self.globals_here = globals_here  # local name -> (origin module, name)
        self.ids = ids
        self.id_index = id_index
        self.locals_stack: list[set[str]] = []
        self.class_stack: list[tuple[str, set[str]]] = []  # only while directly in a class body
        self.stmt_index = {}
        for i, st in enumerate(info["stmts"]):
            self.stmt_index.setdefault((st[0], st[2]), i)
        self.scope = ""

    # ---- helpers ----
    def ident(self, definer: str, name: str) -> int:
        key = (definer, name)
        i = self.id_index.get(key)
        if i is None:
            i = self.id_index[key] = len(self.ids)
            self.ids.append([definer, name])
        return i

    def call(self, fn, *args):
        return ast.Call(func=ast.Attribute(value=ast.Name(id=RT, ctx=ast.Load()), attr=fn, ctx=ast.Load()),
                        args=list(args), keywords=[])

    def ref(self, node):
        """('a', obj, name) / ('g', node, id) for a tracked load, else None."""
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if node.attr in self.attr_names and not (node.attr.startswith("__") and not node.attr.endswith("__")):
                return ("a", node.value, node.attr)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            if self.locals_stack:
                if name in self.locals_stack[-1]:
                    return None
            elif self.class_stack and name in self.class_stack[-1][1]:
                module = self.info["module"]
                return ("g", node, self.ident(f"{module}:{self.class_stack[-1][0]}", name))
            origin = self.globals_here.get(name)
            if origin:
                return ("g", node, self.ident(origin[0] + ":", origin[1]))
        return None

    # ---- expressions ----
    def visit_Attribute(self, node):
        r = self.ref(node)
        if r:
            return ast.copy_location(self.call("attr", self.visit(r[1]), ast.Constant(r[2])), node)
        return self.generic_visit(node)

    def visit_Name(self, node):
        r = self.ref(node)
        if r:
            return ast.copy_location(self.call("g", node, ast.Constant(r[2])), node)
        return node

    def visit_Subscript(self, node):
        if isinstance(node.ctx, ast.Load) and not isinstance(node.slice, ast.Slice):
            r = self.ref(node.value)
            if r and r[0] == "a":
                new = self.call("sub", self.visit(r[1]), ast.Constant(r[2]), self.visit(node.slice))
                return ast.copy_location(new, node)
            if r:
                new = self.call("gsub", r[1], ast.Constant(r[2]), self.visit(node.slice))
                return ast.copy_location(new, node)
        return self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        plain = not node.keywords and not any(isinstance(a, ast.Starred) for a in node.args)
        if plain and isinstance(f, ast.Attribute) and f.attr == "get" and 1 <= len(node.args) <= 2:
            r = self.ref(f.value)
            if r and r[0] == "a":
                new = self.call("get", self.visit(r[1]), ast.Constant(r[2]), *[self.visit(a) for a in node.args])
                return ast.copy_location(new, node)
            if r:
                new = self.call("gget", r[1], ast.Constant(r[2]), *[self.visit(a) for a in node.args])
                return ast.copy_location(new, node)
        if (plain and isinstance(f, ast.Name) and f.id == "getattr" and 2 <= len(node.args) <= 3
                and not (self.locals_stack and "getattr" in self.locals_stack[-1])):
            new = self.call("ga", *[self.visit(a) for a in node.args])
            return ast.copy_location(new, node)
        return self.generic_visit(node)

    def visit_Compare(self, node):
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            r = self.ref(node.comparators[0])
            new = None
            if r and r[0] == "a":
                new = self.call("isin", self.visit(node.left), self.visit(r[1]), ast.Constant(r[2]))
            elif r:
                new = self.call("gin", self.visit(node.left), r[1], ast.Constant(r[2]))
            if new is not None:
                if isinstance(node.ops[0], ast.NotIn):
                    new = ast.UnaryOp(op=ast.Not(), operand=new)
                return ast.copy_location(new, node)
        return self.generic_visit(node)

    def visit_match_case(self, node):
        # patterns must stay patterns
        if node.guard is not None:
            node.guard = self.visit(node.guard)
        node.body = [self.visit(s) for s in node.body]
        return node

    # ---- scopes ----
    def _function(self, node):
        saved_cls, self.class_stack = self.class_stack, []
        self.locals_stack.append(local_names(node) | (self.locals_stack[-1] if self.locals_stack else set()))
        # defaults and decorators belong to the enclosing scope, but treating them as
        # inside the function only costs precision for names that are also locals.
        node = self.generic_visit(node)
        self.locals_stack.pop()
        self.class_stack = saved_cls
        return node

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function
    visit_Lambda = _function

    def visit_ClassDef(self, node):
        if self.locals_stack or not self.stmt_index:  # a class inside a function: leave its body alone, rewrite expressions
            return self.generic_visit(node)
        qual = f"{self.scope}.{node.name}" if self.scope else node.name
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.bases = [self.visit(b) for b in node.bases]
        node.keywords = [self.visit(k) for k in node.keywords]
        saved_scope, self.scope = self.scope, qual
        binds = {n for n, kind in self.info["binds"].get(qual, {}).items() if kind == "data"}
        self.class_stack.append((qual, binds))
        hdr = self.stmt_index.get((min([node.lineno] + [d.lineno for d in node.decorator_list]), saved_scope))
        node.body = self.def_body(node.body)
        if hdr is not None:
            node.body.append(self.marker(hdr))
        self.class_stack.pop()
        self.scope = saved_scope
        return node

    def marker(self, index):
        return ast.Expr(self.call("at", ast.Constant(self.fid << 16 | index)))

    def def_body(self, body):
        """Rewrite a module / class body, announcing each statement before it runs."""
        out = []
        for i, node in enumerate(body):
            is_doc = (i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                      and isinstance(node.value.value, str))
            is_future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
            first = node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                first = min(first, min(d.lineno for d in node.decorator_list))
            index = self.stmt_index.get((first, self.scope))
            if index is not None and not is_doc and not is_future:
                out.append(self.marker(index))
            if isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                for field in ("test", "iter", "items"):
                    if hasattr(node, field):
                        value = getattr(node, field)
                        setattr(node, field, [self.visit(v) for v in value] if isinstance(value, list) else self.visit(value))
                for field in ("body", "orelse", "finalbody"):
                    if getattr(node, field, None):
                        setattr(node, field, self.def_body(getattr(node, field)))
                for handler in getattr(node, "handlers", []) or []:
                    handler.body = self.def_body(handler.body)
                out.append(node)
            else:
                out.append(self.visit(node))
        return out

    def rewrite_module(self, tree: ast.Module) -> ast.Module:
        body = tree.body
        head = []
        while body and (
            (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) and not head)
            or (isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__")
        ):
            head.append(body.pop(0))
        new_body = self.def_body(body)
        prologue = ast.parse(f"import blast_reads as {RT}\n{RT}.enter(__name__)\n").body
        wrapped = ast.Try(
            body=new_body or [ast.Pass()],
            handlers=[],
            orelse=[],
            finalbody=[ast.Expr(self.call("leave", ast.Name(id="__name__", ctx=ast.Load())))],
        )
        tree.body = head + prologue + [wrapped]
        return ast.fix_missing_locations(tree)

    def rewrite_test_module(self, tree: ast.Module) -> ast.Module:
        """Test files: only the expression rewrites (they read sqlglot's tables too)."""
        body = tree.body
        head = []
        while body and (
            (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) and not head)
            or (isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__")
        ):
            head.append(body.pop(0))
        self.stmt_index = {}
        self.info = dict(self.info, binds={})
        new_body = [self.visit(node) for node in body]
        tree.body = head + ast.parse(f"import blast_reads as {RT}\n").body + new_body
        return ast.fix_missing_locations(tree)


def resolve_global(static_by_module, module_data, module, name, seen=None):
    """Origin (module, name) of a module-level data name, following from-imports."""
    seen = seen or set()
    if (module, name) in seen:
        return None
    seen.add((module, name))
    if name in module_data.get(module, ()):
        return (module, name)
    info = static_by_module.get(module)
    if not info:
        return None
    for _line, base, names, top in info["imports"]:
        if top and name in names and base.split(".")[0] == "sqlglot":
            return resolve_global(static_by_module, module_data, base, name, seen)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--static", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    src = os.path.realpath(args.src)
    out = os.path.abspath(args.out)
    static = json.load(open(os.path.join(args.static, "static.json")))
    if os.path.exists(out):
        shutil.rmtree(out)
    shutil.copytree(src, out, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))

    files = sorted(static["files"])
    sq_files = [f for f in files if f.startswith("sqlglot/")]
    class_names, module_data = collect_names(static)
    class_names |= dynamic_class_attrs(src, sq_files)
    all_module_names = set().union(*module_data.values()) if module_data else set()
    attr_names = class_names | all_module_names
    by_module = {info["module"]: info for info in static["files"].values()}

    ids: list = []
    id_index: dict = {}
    for rel in sq_files:
        info = static["files"][rel]
        module = info["module"]
        text = open(os.path.join(src, rel), encoding="utf-8").read()
        tree = ast.parse(text)
        globals_here = {}
        for name in module_data.get(module, ()):
            globals_here[name] = (module, name)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and node.module.split(".")[0] == "sqlglot":
                for alias in node.names:
                    origin = resolve_global(by_module, module_data, node.module, alias.name)
                    if origin:
                        globals_here[alias.asname or alias.name] = origin
        rw = Rewriter(rel, files.index(rel), info, attr_names, globals_here, ids, id_index)
        new_tree = rw.rewrite_module(tree)
        code = ast.unparse(new_tree)
        compile(code, rel, "exec")
        with open(os.path.join(out, rel), "w", encoding="utf-8") as fh:
            fh.write(code + "\n")
    test_files = [f for f in files if f.startswith("tests/")]
    for rel in test_files:
        info = static["files"][rel]
        tree = ast.parse(open(os.path.join(src, rel), encoding="utf-8").read())
        globals_here = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and node.module.split(".")[0] == "sqlglot":
                for alias in node.names:
                    origin = resolve_global(by_module, module_data, node.module, alias.name)
                    if origin:
                        globals_here[alias.asname or alias.name] = origin
        rw = Rewriter(rel, files.index(rel), info, attr_names, globals_here, ids, id_index)
        code = ast.unparse(rw.rewrite_test_module(tree))
        compile(code, rel, "exec")
        with open(os.path.join(out, rel), "w", encoding="utf-8") as fh:
            fh.write(code + "\n")
    with open(os.path.join(out, "blast_ids.json"), "w") as fh:
        json.dump(ids, fh)
    with open(os.path.join(out, "blast_tracked.json"), "w") as fh:
        json.dump({"class_names": sorted(class_names), "module_data": {m: sorted(v) for m, v in module_data.items()}}, fh)
    print(f"instrumented {len(sq_files)} files into {out}; {len(attr_names)} tracked names, {len(ids)} global/class-body ids")


if __name__ == "__main__":
    main()

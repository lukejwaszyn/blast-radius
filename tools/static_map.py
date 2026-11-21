#!/usr/bin/env python3
"""Static (AST) description of every Python file of the pinned checkout.

    python tools/static_map.py --src <sqlglot checkout> --out <dir>

Writes <dir>/static.json:

{"files": {"sqlglot/parsers/dremio.py": {
     "module": "sqlglot.parsers.dremio",
     "scopes": [[qualname, "class", first_line, last_line, [base sources], [keyword sources]], ...],
     "funcs":  [[qualname, first_line(decorators), def_line, body_first_line, last_line,
                 owner_scope, kind, (lambda only) [body line, col, end line, end col]]],
                                               kind: "def" | "lambda"
     "stmts":  [[first_line, last_line, owner_scope, kind, [target names], entries, [names read]]],
     "imports": [[line, imported module, [names], top_level(0/1)]],
     "binds":  {scope: {name: "data" | "def" | "decorated" | "class" | "import" | "annotation"}},
     "spans":  [[first_line, last_line], ...]   multi-line statements / statement headers
  }}}

"stmts" are the statements that run when the module is imported or a class
body is evaluated ("definition time"): owner_scope is "" for the module or the
qualified name of the class.  A function's header (decorators, signature with
its default values) is a statement of kind "defhdr" of the scope that holds
the def.  For an assignment whose value is a dict / set / list / tuple
display, "entries" lists each element: [first_line, last_line, key source or
"" , what] with what in "kv" (dict key: value), "spread" (**x / *x), "el".

Everything else (lines inside function bodies) runs at call time and is
described by "funcs"; the innermost function whose [body_first_line,
last_line] holds a line owns it.  Lambdas are listed as funcs too, because a
lambda written inside a class-level dict has a call-time body on a
definition-time line.
"""

from __future__ import annotations

import argparse
import ast
import json
import os

CONTAINERS = (ast.Dict, ast.Set, ast.List, ast.Tuple)


def module_name(rel: str) -> str:
    name = rel[:-3].replace("/", ".")
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def src(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def names_read(node) -> list[str]:
    """Identifiers a definition-time statement mentions (names and attribute names)."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
    return sorted(out)


def target_names(target) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for elt in target.elts:
            out.extend(target_names(elt))
        return out
    if isinstance(target, ast.Starred):
        return target_names(target.value)
    if isinstance(target, ast.Attribute):
        return [src(target)]
    if isinstance(target, ast.Subscript):
        return [src(target.value) + "[]"]
    return []


def container_entries(value):
    """Elements of the outermost dict/set/list/tuple display of an assigned value."""
    node = value
    nested = 0
    if not isinstance(node, CONTAINERS):
        found = None
        for sub in ast.walk(value):
            if isinstance(sub, CONTAINERS) and (
                found is None
                or (sub.end_lineno - sub.lineno) > (found.end_lineno - found.lineno)
            ):
                found = sub
        if found is None or found.end_lineno == found.lineno:
            return None, 0
        node, nested = found, 1
    entries = []
    if isinstance(node, ast.Dict):
        for key, val in zip(node.keys, node.values):
            if key is None:
                entries.append([val.lineno, val.end_lineno, src(val), "spread"])
            else:
                entries.append([key.lineno, val.end_lineno, src(key), "kv"])
    else:
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                entries.append([elt.lineno, elt.end_lineno, src(elt.value), "spread"])
            else:
                entries.append([elt.lineno, elt.end_lineno, src(elt), "el"])
    return entries, nested


def lambda_cols(node) -> list[int]:
    """[body line, body column, end line, end column] (0-based columns, in characters
    of the source line as ast reports them: UTF-8 byte offsets) of a lambda's body:
    the part of its line(s) that runs at call time, not at definition time."""
    return [node.body.lineno, node.body.col_offset, node.body.end_lineno, node.body.end_col_offset]


class Walker:
    def __init__(self, rel: str, tree: ast.Module):
        self.rel = rel
        self.scopes = []
        self.funcs = []
        self.stmts = []
        self.imports = []
        self.def_names = {}  # scope -> {name: how it is bound}
        self.def_time(tree.body, "", top=True)

    # ---- definition time ------------------------------------------------
    def def_time(self, body, scope, top, cond=0):
        binds = self.def_names.setdefault(scope, {})
        for i, node in enumerate(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.function(node, scope, in_def_scope=True)
                kind = "decorated" if node.decorator_list else "def"
                binds[node.name] = kind
                continue
            if isinstance(node, ast.ClassDef):
                qual = f"{scope}.{node.name}" if scope else node.name
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                self.scopes.append([qual, "class", first, node.end_lineno, [src(b) for b in node.bases],
                                    [src(k) for k in node.keywords]])
                hdr_end = node.body[0].lineno - 1 if node.body else node.end_lineno
                self.stmts.append([first, max(first, hdr_end), scope, "classhdr", [node.name], None,
                                   sorted({n for b in node.bases + node.decorator_list for n in names_read(b)})])
                binds[node.name] = "class"
                self.def_time(node.body, qual, top=False)
                self.lambdas_in(node.decorator_list + node.bases, scope)
                continue
            if isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                # compound statement at definition time: its header, then its blocks
                blocks = []
                for field in ("body", "orelse", "finalbody"):
                    blocks.append(getattr(node, field, []) or [])
                for handler in getattr(node, "handlers", []) or []:
                    blocks.append(handler.body)
                first_inner = min((b[0].lineno for b in blocks if b), default=node.end_lineno + 1)
                hdr = [node.lineno, max(node.lineno, first_inner - 1), scope, "cond", [], None, []]
                if isinstance(node, (ast.If, ast.While)):
                    hdr[6] = names_read(node.test)
                elif isinstance(node, ast.For):
                    hdr[4] = target_names(node.target)
                    hdr[6] = names_read(node.iter)
                elif isinstance(node, ast.With):
                    hdr[6] = sorted({n for item in node.items for n in names_read(item)})
                self.stmts.append(hdr)
                for block in blocks:
                    self.def_time(block, scope, top, cond=1)
                continue

            kind = type(node).__name__.lower()
            targets, entries, value = [], None, None
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    targets.extend(target_names(tgt))
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = target_names(node.target)
                value = node.value
                if value is None:
                    kind = "annotation"
            elif isinstance(node, ast.AugAssign):
                targets = target_names(node.target)
                value = node.value
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = [(a.asname or a.name.split(".")[0]) for a in node.names]
                self.add_import(node, top=1)
            elif isinstance(node, ast.Expr):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    kind = "doc"
                else:
                    kind = "expr"
            if value is not None:
                entries, nested = container_entries(value)
                if entries is not None and nested:
                    kind += "+nested"
            for name in targets:
                if kind.startswith("import"):
                    binds[name] = "import"
                elif value is not None:
                    binds[name] = "data"
                else:
                    binds.setdefault(name, "annotation")
            reads = [] if kind == "doc" else names_read(node)
            if cond:
                kind += "?"
            self.stmts.append([node.lineno, node.end_lineno, scope, kind, targets, entries, reads])
            self.lambdas_in([node], scope)

    # ---- call time ------------------------------------------------------
    def function(self, node, scope, in_def_scope, parent_func=""):
        qual = f"{parent_func or scope}.{node.name}" if (parent_func or scope) else node.name
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        body_first = node.body[0].lineno
        # a docstring is the first body statement; keep it inside the body span
        self.funcs.append([qual, first, node.lineno, body_first, node.end_lineno, scope, "def"])
        if in_def_scope:
            hdr_nodes = node.decorator_list + [node.args] + ([node.returns] if node.returns else [])
            self.stmts.append([first, max(first, body_first - 1), scope, "defhdr", [node.name], None,
                               sorted({n for h in hdr_nodes for n in names_read(h)})])
            self.lambdas_in(node.decorator_list + [node.args], scope)
        for sub in node.body:
            self._inner_node(sub, scope, qual)

    def _inner_node(self, node, scope, parent_func):
        """Nested defs / lambdas / imports inside a function body."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.function(node, scope, in_def_scope=False, parent_func=parent_func)
            return
        if isinstance(node, ast.Lambda):
            self.funcs.append([f"{parent_func}.<lambda>", node.lineno, node.lineno, node.body.lineno,
                               node.end_lineno, scope, "lambda", lambda_cols(node)])
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.add_import(node, top=0)
        for sub in ast.iter_child_nodes(node):
            self._inner_node(sub, scope, parent_func)

    def lambdas_in(self, nodes, scope):
        """Lambdas (and comprehensions do not count) written inside definition-time code."""
        for node in nodes:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Lambda):
                    self.funcs.append([f"{scope}.<lambda>" if scope else "<lambda>", sub.lineno, sub.lineno,
                                       sub.body.lineno, sub.end_lineno, scope, "lambda", lambda_cols(sub)])

    def add_import(self, node, top):
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.imports.append([node.lineno, alias.name, [], top])
        else:
            base = node.module or ""
            if node.level:
                pkg = module_name(self.rel).split(".")
                if not self.rel.endswith("__init__.py"):
                    pkg = pkg[:-1]
                pkg = pkg[: len(pkg) - (node.level - 1)]
                base = ".".join(pkg + ([base] if base else []))
            self.imports.append([node.lineno, base, [a.name for a in node.names], top])


def statement_spans(tree) -> list[list[int]]:
    """[first, last] of every statement (or compound-statement header) that spans
    more than one line.  The line tracer reports a line only if some bytecode
    starts on it, so an edit in the middle of a multi-line statement has to be
    looked up through the whole statement."""
    spans = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        first = node.lineno
        last = node.end_lineno
        blocks = [getattr(node, f) for f in ("body", "orelse", "finalbody") if isinstance(getattr(node, f, None), list)]
        blocks += [h.body for h in getattr(node, "handlers", []) or []]
        blocks += [c.body for c in getattr(node, "cases", []) or []]
        inner = [b[0] for b in blocks if b and isinstance(b[0], ast.stmt)]
        if inner:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                first = min(first, min(d.lineno for d in node.decorator_list))
            last = min(i.lineno for i in inner) - 1
        if last > first:
            spans.add((first, last))
    return [list(s) for s in sorted(spans)]


def analyse(root: str, rel: str) -> dict:
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        text = fh.read()
    tree = ast.parse(text)
    w = Walker(rel, tree)
    # imports written inside "if TYPE_CHECKING:" never run
    return {
        "module": module_name(rel),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "scopes": w.scopes,
        "funcs": sorted(w.funcs, key=lambda f: (f[1], -f[4])),
        "stmts": sorted(w.stmts, key=lambda s: (s[0], -s[1])),
        "imports": w.imports,
        "binds": w.def_names,
        "spans": statement_spans(tree),
    }


def list_py(root: str) -> list[str]:
    out = []
    for top in ("sqlglot", "tests", "sqlglotc"):
        for base, dirs, files in os.walk(os.path.join(root, top)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(out)


def build(root: str) -> dict:
    return {"files": {rel: analyse(root, rel) for rel in list_py(root)}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data = build(os.path.realpath(args.src))
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "static.json")
    with open(path, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"{path}: {os.path.getsize(path) / 1e6:.2f} MB, {len(data['files'])} files")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Make the instrumented copy of sqlglot used by the "comparisons" pass.

    python tools/instrument_cmp.py --src <sqlglot checkout> --out <dir>

Copies the checkout to --out and rewrites, in every file under sqlglot/, each
two-operand comparison that sits inside a function or lambda body and uses one
of the operators below, so that it also evaluates its *neighbour* operator and
notes whether the two ever gave different results (tools/tracer/blast_cmp.py):

    a is b       neighbour  a == b          a < b     neighbour  a <= b
    a is not b   neighbour  a != b          a <= b    neighbour  a < b
                                            a > b     neighbour  a >= b
                                            a >= b    neighbour  a > b

The rewritten expression returns exactly what the original returns.  Chained
comparisons (`a < b < c`) and comparisons at module or class level are left
alone.  --out/blast_cmp_sites.json lists the sites:
[file, line, column, operator] with the positions of the *original* source.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil

RT = "_bc"
OPS = {ast.Is: "is", ast.IsNot: "isnot", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel: str, sites: list):
        self.rel = rel
        self.sites = sites
        self.depth = 0

    def _function(self, node):
        if isinstance(node, ast.Lambda):
            node.args = self.visit(node.args)      # defaults: evaluated where the lambda is defined
            self.depth += 1
            node.body = self.visit(node.body)
            self.depth -= 1
            return node
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.args = self.visit(node.args)
        if node.returns is not None:
            node.returns = self.visit(node.returns)
        self.depth += 1
        node.body = [self.visit(s) for s in node.body]
        self.depth -= 1
        return node

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function
    visit_Lambda = _function

    def visit_Compare(self, node):
        line, col = node.lineno, node.col_offset
        node = self.generic_visit(node)
        if self.depth and len(node.ops) == 1 and type(node.ops[0]) in OPS:
            name = OPS[type(node.ops[0])]
            sid = len(self.sites)
            self.sites.append([self.rel, line, col, name])
            call = ast.Call(func=ast.Attribute(value=ast.Name(id=RT, ctx=ast.Load()), attr="is_" if name == "is" else name, ctx=ast.Load()),
                            args=[ast.Constant(sid), node.left, node.comparators[0]], keywords=[])
            return ast.copy_location(call, node)
        return node

    def visit_match_case(self, node):
        if node.guard is not None:
            node.guard = self.visit(node.guard)
        node.body = [self.visit(s) for s in node.body]
        return node


def rewrite(text: str, rel: str, sites: list) -> str:
    tree = ast.parse(text)
    before = len(sites)
    tree = Rewriter(rel, sites).visit(tree)
    if len(sites) == before:
        return text
    at = 0
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        at = 1
    while at < len(body) and isinstance(body[at], ast.ImportFrom) and body[at].module == "__future__":
        at += 1
    body.insert(at, ast.Import(names=[ast.alias(name="blast_cmp", asname=RT)]))
    ast.fix_missing_locations(tree)
    code = ast.unparse(tree)
    compile(code, rel, "exec")
    return code + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    src = os.path.realpath(args.src)
    out = os.path.realpath(args.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    shutil.copytree(src, out, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    sites: list = []
    n_files = 0
    for base, dirs, files in os.walk(os.path.join(src, "sqlglot")):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(base, name), src).replace(os.sep, "/")
            with open(os.path.join(src, rel), encoding="utf-8") as fh:
                text = fh.read()
            new = rewrite(text, rel, sites)
            if new != text:
                n_files += 1
                with open(os.path.join(out, rel), "w", encoding="utf-8") as fh:
                    fh.write(new)
    with open(os.path.join(out, "blast_cmp_sites.json"), "w") as fh:
        json.dump(sites, fh)
    print(f"instrumented {len(sites)} comparisons in {n_files} files into {out}")


if __name__ == "__main__":
    main()

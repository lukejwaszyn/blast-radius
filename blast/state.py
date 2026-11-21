"""Statements that write state which outlives the frame they run in.

Line coverage answers "which tests run this statement".  For most statements
that is also "which tests can it affect".  It is not for a statement that
writes to something every test shares: an attribute of an imported module or
class (`sqlglot.pretty = True`, `SnowflakeParser.FUNCTIONS["X"] = ...`), a
module global (`global _schema`), the process environment, a registry that is
cleared or popped from.  Such a write is seen by every test that runs later in
the same process, whether or not it runs the statement itself.

`writers(tree)` lists these statements for a whole file, each with the scope
it sits in.  The decision is syntactic and deliberately narrow:

- an assignment, augmented assignment or `del` whose target is an attribute or
  item of something that is *not* a local of the enclosing function: a name
  the file imports, a builtin, the result of a call such as `globals()`.
  `self.x = ...`, `cls.x = ...` and writes through any other parameter or
  local variable are not counted (instance and call-local state);
- an assignment to a name the function declares `global`;
- a call statement of a mutating method (`pop`, `clear`, `update`, `append`,
  ...) on such a non-local object, `setattr` / `delattr` on one, and the calls
  that switch a patcher or a tracer on and off (`start`, `stop`).
"""

from __future__ import annotations

import ast

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
MUTATING_METHODS = frozenset({
    "pop", "popitem", "clear", "update", "setdefault", "append", "extend", "insert", "remove", "add", "discard",
    "sort", "reverse", "appendleft", "extendleft", "difference_update", "intersection_update", "symmetric_difference_update",
    # switches of process-wide behaviour in the standard library
    "setrecursionlimit", "setLevel", "disable", "basicConfig", "addHandler", "removeHandler", "simplefilter",
    "filterwarnings", "resetwarnings", "seed", "chdir", "putenv", "unsetenv", "setlocale", "reload", "register",
    "unregister", "setprofile", "settrace", "set_start_method", "setswitchinterval", "set_int_max_str_digits",
})
SWITCHES = frozenset({"start", "stop", "stopall"})


def _bound(stmts, out: set[str], declared: set[str]) -> None:
    """Names a scope binds by assignment (not by import), not entering nested scopes."""

    def target(t):
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, ast.Starred):
            target(t.value)

    stack = list(stmts)
    while stack:
        s = stack.pop()
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(s.name)
            continue
        if isinstance(s, ast.Global):
            declared.update(s.names)
            continue
        if isinstance(s, ast.Nonlocal):
            out.update(s.names)  # a local of the enclosing function: gone when that call returns
            continue
        if isinstance(s, ast.Assign):
            for t in s.targets:
                target(t)
        elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
            target(s.target)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            target(s.target)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                if item.optional_vars is not None:
                    target(item.optional_vars)
        elif isinstance(s, ast.ExceptHandler) and s.name:
            out.add(s.name)
        for node in ast.iter_child_nodes(s):
            if isinstance(node, ast.NamedExpr):
                target(node.target)
            if isinstance(node, (ast.stmt, ast.ExceptHandler)) or (hasattr(ast, "match_case") and isinstance(node, ast.match_case)):
                stack.append(node)
            elif isinstance(node, ast.expr):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.NamedExpr):
                        target(sub.target)
                    elif isinstance(sub, ast.comprehension):
                        target(sub.target)


def _root(node):
    """The expression an attribute / item / call chain starts from."""
    while True:
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return node


def _shared(node, local: set[str]) -> bool:
    """Is the object this chain starts from something other than a local of the scope?"""
    root = _root(node)
    if isinstance(root, ast.Name):
        return root.id not in local
    return not isinstance(root, (ast.Constant, ast.List, ast.Dict, ast.Set, ast.Tuple, ast.JoinedStr,
                                 ast.ListComp, ast.DictComp, ast.SetComp))


def _is_writer(s, local: set[str], declared: set[str]) -> bool:
    targets = []
    if isinstance(s, ast.Assign):
        targets = list(s.targets)
    elif isinstance(s, ast.AugAssign) or (isinstance(s, ast.AnnAssign) and s.value is not None):
        targets = [s.target]
    elif isinstance(s, ast.Delete):
        targets = list(s.targets)
    while targets:
        t = targets.pop()
        if isinstance(t, (ast.Tuple, ast.List)):
            targets.extend(t.elts)
        elif isinstance(t, ast.Starred):
            targets.append(t.value)
        elif isinstance(t, ast.Name):
            if t.id in declared:
                return True
        elif isinstance(t, (ast.Attribute, ast.Subscript)) and _shared(t.value, local):
            return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        call = s.value
        func = call.func
        if isinstance(func, ast.Attribute):
            if func.attr in SWITCHES:
                return True
            if func.attr in MUTATING_METHODS and _shared(func.value, local):
                return True
        elif isinstance(func, ast.Name) and func.id in ("setattr", "delattr") and call.args and _shared(call.args[0], local):
            return True
    return False


def writers(tree: ast.AST, functions_only: bool = False) -> list[tuple[str, str, str]]:
    """[(scope, what is written, dump of the statement)] for every state-writing statement.

    functions_only: leave out module-level and class-level statements (they run once, at
    import, and what they leave behind is visible to a comparison of the imported modules)."""
    out: list[tuple[str, str, str]] = []

    def what(s) -> str:
        if isinstance(s, ast.Expr):
            return ast.dump(s.value.func)
        if isinstance(s, ast.Assign):
            return "|".join(ast.dump(t) for t in s.targets)
        if isinstance(s, ast.Delete):
            return "del " + "|".join(ast.dump(t) for t in s.targets)
        return ast.dump(s.target)

    def scope(stmts, qual: str, local: set[str], declared: set[str], in_function: bool):
        stack = list(reversed(stmts))
        while stack:
            s = stack.pop()
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner: set[str] = {a.arg for a in s.args.posonlyargs + s.args.args + s.args.kwonlyargs}
                for extra in (s.args.vararg, s.args.kwarg):
                    if extra is not None:
                        inner.add(extra.arg)
                inner_declared: set[str] = set()
                _bound(s.body, inner, inner_declared)
                inner -= inner_declared
                inner_declared |= declared if in_function else set()
                # a closure's free variables are locals of an enclosing function, not shared state
                scope(s.body, f"{qual}.{s.name}" if qual else s.name, inner | (local if in_function else set()), inner_declared, True)
                continue
            if isinstance(s, ast.ClassDef):
                inner = set()
                inner_declared = set()
                _bound(s.body, inner, inner_declared)
                scope(s.body, f"{qual}.{s.name}" if qual else s.name, inner | (local if in_function else set()), inner_declared, in_function)
                continue
            if (in_function or not functions_only) and _is_writer(s, local, declared):
                out.append((qual, what(s), ast.dump(s)))
            for name in ("body", "orelse", "finalbody"):
                block = getattr(s, name, None)
                if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                    stack.extend(reversed(block))
            for h in getattr(s, "handlers", ()):
                stack.extend(reversed(h.body))
            for c in getattr(s, "cases", ()):
                stack.extend(reversed(c.body))

    body = getattr(tree, "body", [])
    top: set[str] = set()
    top_declared: set[str] = set()
    _bound(body, top, top_declared)
    scope(body, "", top, top_declared, False)
    return out


def changed_writers(old_tree, new_tree) -> list[str]:
    """Scopes with a state-writing statement that the patch added, removed or changed."""
    from collections import Counter

    a = Counter((scope, dump) for scope, _what, dump in writers(old_tree))
    b = Counter((scope, dump) for scope, _what, dump in writers(new_tree))
    return sorted({scope or "<module>" for (scope, _dump), n in (a - b).items()} |
                  {scope or "<module>" for (scope, _dump), n in (b - a).items()})


def new_write_targets(old_tree, new_tree) -> list[str]:
    """Scopes where a function of the new file writes shared state through a target (or a call)
    that the old version of that function did not write."""
    a = {(scope, what) for scope, what, _dump in writers(old_tree, functions_only=True)}
    return sorted({scope for scope, what, _dump in writers(new_tree, functions_only=True) if (scope, what) not in a})

#!/usr/bin/env python3
"""Check the maps against patches with known answers (the kit's sample set).

    python tools/verify_samples.py --maps maps --src <checkout> \
        --commits kit/sample/commits --answers kit/sample/answers.json [--json out.json]

For every patch it derives candidate test sets from the maps only, layer by
layer, and checks that every test that really fails is inside:

  lines    per-test line coverage of the changed lines (cov.json + filecov.json),
           looked up through the whole statement a changed line belongs to
  defs     changed definition-time statements -> tests that read the names they
           bind (reads.json), plus function lines that only run at import ->
           the definition lines they ran for (defdeps.json)
  derived  the same, closed over "this definition read that name while
           importing" (derive.json) and class-creation hooks (classes.json)
  data     changed non-Python files -> tests that opened them (opens.json)
  text     changed Python files that some test reads as text (opens.json,
           py_read_as_data); listed even for comment-only edits
  modules  fallback: every test that loads the changed module at all

With --python it also runs the exact recipe for definition-time edits: import
the patched tree, compare value fingerprints with the pinned tree
(tools/snapshot.py), and take the readers of exactly the values that changed
("exact=" is lines + that + data).

This is a checker of the maps, not the selector: it prints sizes so that a
selector can decide how far down the list it needs to go.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import tokenize

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapfmt import dec  # noqa: E402

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str):
    """-> {path: {"old": set of changed/anchor old lines, "hunks": [...], "new": bool, "deleted": bool}}"""
    out = {}
    cur = None
    old_line = 0
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = None
        elif line.startswith("--- "):
            old_path = line[4:].split("\t")[0]
            new_path = lines[i + 1][4:].split("\t")[0] if i + 1 < len(lines) else ""
            path = new_path[2:] if new_path.startswith("b/") else old_path[2:]
            if new_path == "/dev/null":
                path = old_path[2:]
            cur = out.setdefault(path, {"old": set(), "hunks": [], "new": old_path == "/dev/null",
                                        "deleted": new_path == "/dev/null"})
            i += 1
        elif cur is not None and (m := HUNK.match(line)):
            old_line = int(m.group(1))
            hunk = {"start": old_line, "lines": []}
            cur["hunks"].append(hunk)
            i += 1
            while i < len(lines) and lines[i][:1] in (" ", "+", "-", "\\"):
                tag, body = lines[i][:1], lines[i][1:]
                if tag == " ":
                    old_line += 1
                elif tag == "-":
                    cur["old"].add(old_line)
                    old_line += 1
                elif tag == "+":
                    # an insertion sits between old_line-1 and old_line
                    cur.setdefault("ins", set()).update({max(old_line - 1, 1), old_line})
                if tag != "\\":
                    hunk["lines"].append((tag, body))
                i += 1
            continue
        i += 1
    return out


def apply_hunks(text: str, hunks) -> str:
    src = text.split("\n")
    out = []
    pos = 0
    for hunk in hunks:
        start = hunk["start"] - 1
        out.extend(src[pos:start])
        pos = start
        for tag, body in hunk["lines"]:
            if tag == " ":
                out.append(src[pos])
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(body)
    out.extend(src[pos:])
    return "\n".join(out)


def same_code(a: str, b: str) -> bool:
    """True when two Python sources differ only in comments / blank lines / layout."""
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return False


class Maps:
    def __init__(self, root: str):
        def load(name):
            with open(os.path.join(root, name)) as fh:
                return json.load(fh)

        self.tests = load("tests.json")
        self.cov = load("cov.json")
        self.filecov = load("filecov.json")
        self.static = load("static.json")["files"]
        self.defdeps = load("defdeps.json")
        self.modules = load("modules.json")
        self.opens = load("opens.json")
        self.reads = load("reads.json")
        self.derive = load("derive.json")["reads_at_import"]
        self.classes = load("classes.json")
        self.n = len(self.tests["ids"])
        self.tests_of_file = {}
        for number, f in enumerate(self.tests["file_of"]):
            self.tests_of_file.setdefault(self.tests["files"][f], set()).add(number)
        # who reads what at import: name -> [(file, "first-last")]
        self.import_readers = {}
        for rel, stmts in self.derive.items():
            for span, rows in stmts.items():
                for row in rows:
                    name = row if isinstance(row, str) else row[0]
                    self.import_readers.setdefault(name, []).append((rel, span))
        self.subclasses = {}
        for name, info in self.classes["classes"].items():
            for base in info["mro"]:
                self.subclasses.setdefault(base, set()).add(name)

    # -- lookups ---------------------------------------------------------
    def span_lines(self, rel, line):
        best = (line, line)
        for first, last in self.static.get(rel, {}).get("spans", []):
            if first <= line <= last and (best == (line, line) or last - first < best[1] - best[0]):
                best = (first, last)
        return range(best[0], best[1] + 1)

    def _runs(self, table, rel, wanted):
        out = set()
        for first, last, sid in table["files"].get(rel, []):
            if any(first <= w <= last for w in wanted):
                out.add(sid)
        return out

    def line_tests(self, rel, lines):
        wanted = set()
        for line in lines:
            wanted.update(self.span_lines(rel, line))
        tests = set()
        for sid in self._runs(self.cov, rel, wanted):
            tests.update(dec(self.cov["sets"][sid]))
        for sid in self._runs(self.filecov, rel, wanted):
            for f in dec(self.filecov["sets"][sid]):
                tests.update(self.tests_of_file.get(self.filecov["test_files"][f], ()))
        return tests

    def stmt_at(self, rel, line):
        """Innermost definition-time statement holding the line, unless a function body holds it."""
        info = self.static.get(rel)
        if not info:
            return None
        for func in info["funcs"]:
            first, body_first, last, kind = func[1], func[3], func[4], func[6]
            if kind == "def" and body_first <= line <= last:
                return None
            if kind == "lambda" and first < line <= last:
                return None  # inside a multi-line lambda, past its first line: call time only
        best = None
        for st in info["stmts"]:
            if st[0] <= line <= st[1] and (best is None or st[1] - st[0] <= best[1] - best[0]):
                best = st
        return best

    def readers(self, name):
        entry = self.reads["reads"].get(name)
        return set(dec(self.reads["sets"][entry["any"]])) if entry else set()

    def class_users(self, cls):
        tests = set()
        for c in {cls} | self.subclasses.get(cls, set()):
            sid = self.reads["used_classes"].get(c)
            if sid is not None:
                tests.update(dec(self.reads["sets"][sid]))
        return tests

    def names_of(self, rel, st, derived=False):
        """(names bound by a definition statement, tests reached without a name).

        derived=True: the statement itself did not change, something it read while
        importing did.  For a `class` statement that means the class-creation hooks
        (metaclass, __init_subclass__) saw a changed value: what they put on the
        class may differ, the class as such is not a different one."""
        module = self.static[rel]["module"]
        scope, kind, targets = st[2], st[3], st[4]
        names, extra = set(), set()
        definer = f"{module}:{scope}"
        if kind == "classhdr":
            cls = f"{module}:{scope + '.' if scope else ''}{targets[0]}"
            info = self.classes["classes"].get(cls)
            if info:
                for attr in info["set_at_class_creation"]:
                    names.add(f"{cls}|{attr}")
            if not derived:
                names.add(f"{definer}|{targets[0]}")
                extra |= self.class_users(cls)
        elif kind == "defhdr":
            # the function itself: whoever runs its body
            for func in self.static[rel]["funcs"]:
                q, _first, body_first, last, fscope, fkind = func[0], func[1], func[3], func[4], func[5], func[6]
                if fkind == "def" and fscope == scope and q.split(".")[-1] == targets[0] and _first == st[0]:
                    extra |= self.line_tests(rel, range(body_first, last + 1))
            names.add(f"{definer}|{targets[0]}")
        else:
            for t in targets:
                names.add(f"{definer}|{t}")
            if not targets or kind.startswith(("expr", "augassign", "cond")):
                # a statement that binds nothing acts on what it reads
                # (`KEYWORDS.pop("/*+")` in a class body, a module-level `for`)
                for row in self.derive.get(rel, {}).get(f"{st[0]}-{st[1]}", []):
                    names.add(row if isinstance(row, str) else row[0])
        return names, extra

    def def_tests(self, rel, st, closure: bool):
        names, tests = self.names_of(rel, st)
        seen_stmts = {(rel, f"{st[0]}-{st[1]}")}
        todo = list(names)
        seen_names = set(names)
        while todo:
            name = todo.pop()
            tests |= self.readers(name)
            if not closure:
                continue
            for r_rel, span in self.import_readers.get(name, ()):
                if (r_rel, span) in seen_stmts:
                    continue
                seen_stmts.add((r_rel, span))
                first = int(span.split("-")[0])
                st2 = next((s for s in self.static[r_rel]["stmts"] if s[0] == first and f"{s[0]}-{s[1]}" == span), None)
                if st2 is None:
                    continue
                more, extra = self.names_of(r_rel, st2, derived=True)
                tests |= extra
                for n in more - seen_names:
                    seen_names.add(n)
                    todo.append(n)
        return tests

    def module_tests(self, rel):
        module = self.static.get(rel, {}).get("module")
        return set(dec(self.modules["loaded_by_tests"].get(module, ""))) if module else set()


class Exact:
    """Definition-time changes found by importing the patched tree and comparing
    fingerprints of all class-level / module-level values (tools/snapshot.py)."""

    def __init__(self, maps: Maps, src: str, python: str, work: str):
        self.maps, self.src, self.python, self.work = maps, src, os.path.abspath(python), work
        os.makedirs(work, exist_ok=True)
        self.base = self._take(src, "base")
        self.again = self._take(src, "base_again")

    def _take(self, tree: str, name: str) -> dict:
        import subprocess

        out = os.path.join(self.work, name + ".json")
        tool = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot.py")
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([self.python, tool, "take", "--src", tree, "--out", out], env=env,
                       stderr=subprocess.DEVNULL, check=True)
        with open(out) as fh:
            return json.load(fh)

    def tests(self, patch_text: str):
        import shutil

        import snapshot

        changes = {p: c for p, c in parse_patch(patch_text).items() if p.startswith("sqlglot/") and p.endswith(".py")}
        if not changes:
            return set(), {}
        tree = os.path.join(self.work, "tree")
        shutil.rmtree(tree, ignore_errors=True)
        shutil.copytree(os.path.join(self.src, "sqlglot"), os.path.join(tree, "sqlglot"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        for path, change in changes.items():
            full = os.path.join(tree, path)
            if change["deleted"]:
                os.remove(full)
                continue
            before = open(full, encoding="utf-8").read() if os.path.exists(full) else ""
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(apply_hunks(before, change["hunks"]))
        result = snapshot.diff(self.base, self._take(tree, "patched"), self.again)
        maps = self.maps
        reads, sets = maps.reads["reads"], maps.reads["sets"]
        tests = set()
        for name, keys in result["changed"].items():
            if name.endswith("|<bases>"):
                tests |= maps.class_users(name.split("|")[0])
                continue
            entry = reads.get(name)
            if not entry:
                continue
            if isinstance(keys, list):
                if "whole" in entry:
                    tests |= set(dec(sets[entry["whole"]]))
                for key in keys:
                    if key in entry.get("keys", {}):
                        tests |= set(dec(sets[entry["keys"][key]]))
            else:
                tests |= set(dec(sets[entry["any"]]))
        for module in result["errors"]:  # the module no longer imports
            tests |= set(dec(maps.modules["loaded_by_tests"].get(module, "")))
        return tests, result


def analyse(maps: Maps, src: str, patch_text: str) -> dict:
    layers = {k: set() for k in ("lines", "defs", "derived", "data", "modules", "text")}
    notes = []
    def_time = False  # does the patch touch anything that runs at import?
    for path, change in parse_patch(patch_text).items():
        full = os.path.join(src, path)
        old_lines = set(change["old"])
        if path.endswith(".py"):
            # tests that read this file as text see every edit, comments included
            layers["text"] |= set(dec(maps.opens["py_read_as_data"].get(path, "")))
            if change["new"] or not os.path.exists(full):
                notes.append(f"{path}: new file")
                continue
            with open(full, encoding="utf-8") as fh:
                before = fh.read()
            after = "" if change["deleted"] else apply_hunks(before, change["hunks"])
            if not change["deleted"] and same_code(before, after):
                notes.append(f"{path}: comments/layout only")
                continue
            lines = old_lines or change.get("ins", set())
            layers["lines"] |= maps.line_tests(path, lines)
            layers["modules"] |= maps.module_tests(path)
            for line in sorted(lines):
                st = maps.stmt_at(path, line)
                if st is not None:
                    def_time = True
                    layers["defs"] |= maps.def_tests(path, st, closure=False)
                    layers["derived"] |= maps.def_tests(path, st, closure=True)
                site_set = maps.defdeps["deps"].get(path, {}).get(str(line))
                for site in maps.defdeps["site_sets"][site_set] if site_set is not None else []:
                    if site == "?":
                        continue
                    s_rel, s_line = site.rsplit(":", 1)
                    st = maps.stmt_at(s_rel, int(s_line))
                    if st is not None:
                        def_time = True
                        layers["defs"] |= maps.def_tests(s_rel, st, closure=False)
                        layers["derived"] |= maps.def_tests(s_rel, st, closure=True)
        else:
            narrowed = maps.opens["consumers"].get(path)
            tests = set(dec(narrowed["tests"] if narrowed else maps.opens["by_tests"].get(path, "")))
            for f in dec(maps.opens["by_test_file_imports"].get(path, "")):
                tests |= maps.tests_of_file.get(maps.opens["test_files"][f], set())
            layers["data"] |= tests
            if path not in maps.opens["by_tests"] and path not in maps.opens["by_test_file_imports"]:
                notes.append(f"{path}: no test opens it")
    return {"layers": layers, "notes": notes, "def_time": def_time}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--commits", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--tests", help="the tests.json the answers are numbered in (default: the maps' own)")
    ap.add_argument("--json")
    ap.add_argument("--python", help="python with requirements.lock: also compute the exact definition-time diff")
    ap.add_argument("--work", default="/tmp/blast-verify")
    args = ap.parse_args()
    maps = Maps(args.maps)
    exact = Exact(maps, os.path.realpath(args.src), args.python, args.work) if args.python else None
    answers = json.load(open(args.answers))["commits"]
    number_to_ours = None
    if args.tests:
        index = {tid: i for i, tid in enumerate(maps.tests["ids"])}
        number_to_ours = {t["number"]: index[t["id"]] for t in json.load(open(args.tests))["tests"]}
    order = ["lines", "defs", "derived", "data", "text", "modules"]
    rows = []
    ok = True
    print(f"{'commit':6} {'kind':8} {'fail':>4} | {'lines':>5} {'defs':>5} {'deriv':>5} {'data':>5} {'text':>5} {'mods':>5} | "
          f"{'fine':>5} | caught by / notes")
    for name in sorted(os.listdir(args.commits)):
        if not name.endswith(".patch"):
            continue
        cid = name[:-6]
        res = analyse(maps, args.src, open(os.path.join(args.commits, name)).read())
        failing = set(answers[cid]["failing"])
        if number_to_ours:
            failing = {number_to_ours[n] for n in failing}
        layers = res["layers"]
        fine = layers["lines"] | layers["derived"] | layers["defs"] | layers["data"]
        exact_note = ""
        if exact:
            # Only edits of definition-time code need the fingerprint diff.  An edit inside
            # a function body changes that function's code, which the diff would report for
            # every table holding the function; line coverage already answers it exactly.
            ex_tests, ex_diff = set(), {}
            if res["def_time"]:
                ex_tests, ex_diff = exact.tests(open(os.path.join(args.commits, name)).read())
            tight = layers["lines"] | ex_tests | layers["data"]
            if not failing <= tight:
                ok = False
                exact_note = "  EXACT-MISSED"
            changed = ", ".join(f"{n}{k if isinstance(k, list) else ''}" for n, k in list(ex_diff.get("changed", {}).items())[:3])
            exact_note += f"  exact={len(tight)}" + (f" <{changed}>" if changed else "")
        caught = {}
        for t in sorted(failing):
            where = next((k for k in order if t in layers[k]), "MISSED")
            caught.setdefault(where, []).append(t)
        if "MISSED" in caught or not failing <= fine:
            ok = False
        beyond = {k: v for k, v in caught.items() if k != "lines"}
        text = ", ".join(f"{k}:{len(v)}" for k, v in caught.items())
        if beyond:
            text += "  NOT-BY-LINES " + "; ".join(
                f"{k}: " + ", ".join(maps.tests["ids"][t].split("::")[-1] for t in v) for k, v in beyond.items())
        text += exact_note
        if res["notes"]:
            text += "  [" + "; ".join(res["notes"]) + "]"
        print(f"{cid:6} {answers[cid]['kind']:8} {len(failing):4} | "
              + " ".join(f"{len(layers[k]):5}" for k in order) + f" | {len(fine):5} | {text}")
        rows.append({"commit": cid, "kind": answers[cid]["kind"], "failing": sorted(failing),
                     "sizes": {k: len(layers[k]) for k in order}, "fine": len(fine),
                     "caught": caught, "notes": res["notes"],
                     "fine_seconds": round(sum(maps.tests["seconds"][t] for t in fine), 3)})
    print("all failing tests covered by lines+defs+derived+data:", ok)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Turn the raw tracing output into the committed maps.

    python tools/aggregate.py --tests <tests.json> --static <dir> --lines <rawA> \
        --imports <rawI> --reads <rawB> --src <checkout> --out maps/

Inputs: tools/run_trace.py (lines pass -> rawA, reads pass -> rawB),
tools/trace_imports.py (each + deps -> rawI, reads -> rawB/imports_reads.json),
tools/static_map.py (static.json).  Output files are described in maps/README.md.

Test numbers in every map are positions in maps/tests.json "ids" (the official
numbering of the kit's tests.json at build time); a consumer must translate by
test id, not trust the number.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapfmt import bits, enc  # noqa: E402


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as fh:
        return json.load(fh)


def write(path, data, **kw):
    text = json.dumps(data, separators=(",", ":"), sort_keys=kw.get("sort_keys", False))
    with open(path, "w") as fh:
        fh.write(text)
        fh.write("\n")
    print(f"  {os.path.basename(path)}: {len(text) / 1e6:.2f} MB", file=sys.stderr)


class Sets:
    """Interns test sets (as int bitmasks) -> index into a list of range strings."""

    def __init__(self):
        self.index: dict[int, int] = {}
        self.items: list[str] = []

    def add(self, mask: int) -> int:
        i = self.index.get(mask)
        if i is None:
            i = self.index[mask] = len(self.items)
            self.items.append(enc(bits(mask)))
        return i


def read_run(raw: str, test_index: dict[str, int]):
    """-> files {test file: record}, tests {number: record}, subs {number: [records]}"""
    files, tests, subs, ctx_of = {}, {}, {}, {}
    for path in glob.glob(os.path.join(raw, "file.*.json")):
        rec = load(path)
        for f in rec["files"]:
            files[f] = rec
    pending = []
    for path in glob.glob(os.path.join(raw, "t.*.json")):
        rec = load(path)
        if rec["kind"] == "sub":
            pending.append(rec)
            continue
        number = test_index.get(rec["test"])
        if number is None:
            continue  # a test the official environment skips
        if rec["failed"]:
            why = MEASURES_THE_TRACER.get(rec["test"])
            if why is None or not all(why in f[2] for f in rec["failed"]):
                raise SystemExit(f"{rec['test']} failed while tracing: {rec['failed'][0][2][-300:]}")
            # the test ran to its last statement; only its measurement of the process differs
            print(f"note: {rec['test']} fails under the tracer ({why}); its lines are complete", file=sys.stderr)
        tests[number] = rec
        ctx_of[rec["ctx"]] = number
    for rec in pending:
        number = ctx_of.get(rec["ctx"])
        if number is not None:
            subs.setdefault(number, []).append(rec)
    crashes = glob.glob(os.path.join(raw, "*.crash"))
    if crashes:
        raise SystemExit(f"crashed test processes: {crashes[:5]}")
    return files, tests, subs


# Tests that measure the interpreter itself and so see the tracer: allowed to fail
# while tracing, but only with the assertion named here (anything else still stops
# the build).  test_generator_memory asserts that 1000 generator constructions
# allocate under 1 KiB according to tracemalloc; the tracer's own bookkeeping for
# function-entry events is allocated in the same process and counted with it.
MEASURES_THE_TRACER = {
    "tests/test_generator.py::TestGenerator::test_generator_memory": "self.assertLess(after - before, 1024)",
}


def line_kinds(static_file: dict) -> dict[int, str]:
    """line -> 'b' when the line lies in a function body (call time)."""
    out = {}
    for func in static_file["funcs"]:
        body_first, last = func[3], func[4]
        for line in range(body_first, last + 1):
            out[line] = "b"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True, help="the kit's tests.json")
    ap.add_argument("--static", required=True)
    ap.add_argument("--lines", required=True, nargs="+",
                    help="one or more raw dirs of the lines pass (e.g. different hash seeds); they are united")
    ap.add_argument("--imports", required=True)
    ap.add_argument("--reads", nargs="+", help="one or more raw dirs of the reads pass; the first holds imports_reads.json")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    src = os.path.realpath(args.src)

    official = load(args.tests)
    tests_list = official["tests"]
    ids = [t["id"] for t in tests_list]
    test_index = {t["id"]: t["number"] for t in tests_list}
    assert [t["number"] for t in tests_list] == list(range(len(ids)))
    n_tests = len(ids)
    test_files = sorted({t["file"] for t in tests_list})
    tfile_index = {f: i for i, f in enumerate(test_files)}
    tests_of_file = {}
    for t in tests_list:
        tests_of_file.setdefault(t["file"], []).append(t["number"])

    static = load(os.path.join(args.static, "static.json"))["files"]
    files = load(os.path.join(args.lines[0], "files.json"))
    assert files == sorted(static), "static.json and the trace disagree on the file list"
    module_of = {rel: static[rel]["module"] for rel in files}
    rel_of_module = {m: rel for rel, m in module_of.items()}

    # ------------------------------------------------------------------ (f) tests
    struct = []
    for t in tests_list:
        parts = t["id"].split("::")
        where = None
        info = static.get(t["file"])
        if info and len(parts) == 3:
            for func in info["funcs"]:
                q, first, last, kind = func[0], func[1], func[4], func[6]
                if q == f"{parts[1]}.{parts[2]}" and kind == "def":
                    where = [first, last]
                    break
        struct.append(where)
    # ------------------------------------------------------------------ (a) lines
    print("lines pass", file=sys.stderr)
    frecs, trecs, subs = read_run(args.lines[0], test_index)
    write(os.path.join(args.out, "tests.json"), {
        "ids": ids,
        "seconds": [t["seconds"] for t in tests_list],
        "files": test_files,
        "file_of": [tfile_index[t["file"]] for t in tests_list],
        "lines": struct,
        "skipped": official.get("skipped", []),
        # wall seconds of the test run alone in a fork on the map-building machine,
        # class-level setUp included (not comparable with "seconds"; shows which
        # tests are expensive to run one at a time)
        "alone_seconds": [trecs[i]["wall"] if i in trecs else None for i in range(len(ids))],
    })
    for extra in args.lines[1:]:
        assert load(os.path.join(extra, "files.json")) == files
        f2, t2, s2 = read_run(extra, test_index)
        for number, rec in t2.items():
            base = trecs[number]
            for field in ("body", "imp", "top", "opens_body", "opens_imp", "new_modules"):
                base[field] = sorted(set(base[field]) | set(rec[field]))
            if s2.get(number):
                subs.setdefault(number, []).extend(s2[number])
        for tf, rec in f2.items():
            base = frecs[tf]
            for field in ("body", "imp", "top", "opens_body", "opens_imp", "modules"):
                base[field] = sorted(set(base[field]) | set(rec[field]))
    missing = [ids[i] for i in range(n_tests) if i not in trecs]
    if missing:
        raise SystemExit(f"{len(missing)} tests have no trace, e.g. {missing[:3]}")

    cov: dict[int, int] = {}  # line key -> bitmask of tests (call-time execution)
    sub_tests = []
    for number, rec in trecs.items():
        bit = 1 << number
        # "top" inside a test: a module of tests/ imported lazily by the test itself
        keys = set(rec["body"]) | set(rec["top"])
        for sub in subs.get(number, []):
            keys.update(sub["body"])
            keys.update(sub["top"])
        if subs.get(number):
            sub_tests.append(number)
        for key in keys:
            cov[key] = cov.get(key, 0) | bit

    deps = load(os.path.join(args.imports, "defdeps.json"))
    import_ran = set(deps["ran"])
    kinds = {rel: line_kinds(static[rel]) for rel in files}

    # What importing a test file runs apart from importing sqlglot modules: the test
    # modules' own top-level code (class bodies, helpers, module constants) and the
    # sqlglot functions that code calls.  ("imp" = inside the import of a sqlglot
    # module: definition time, described by static/defdeps/reads instead.)
    filecov: dict[int, int] = {}  # line key -> bitmask of test files
    for tf, rec in frecs.items():
        if tf not in tfile_index:
            continue
        bit = 1 << tfile_index[tf]
        for key in set(rec["top"]) | set(rec["body"]):
            rel = files[key >> 16]
            if rel.startswith("sqlglot/") and kinds[rel].get(key & 0xFFFF) != "b":
                continue  # a definition line of sqlglot reached through a lazy import
            filecov[key] = filecov.get(key, 0) | bit

    sets = Sets()

    def by_file(mapping):
        out = {}
        for key in sorted(mapping):
            out.setdefault(files[key >> 16], []).append([key & 0xFFFF, sets.add(mapping[key])])
        packed = {}
        for rel, rows in out.items():
            # run-length: [first_line, last_line, set] for consecutive lines with the same set
            runs = []
            for line, sid in rows:
                if runs and runs[-1][2] == sid and runs[-1][1] == line - 1:
                    runs[-1][1] = line
                else:
                    runs.append([line, line, sid])
            packed[rel] = runs
        return packed

    cov_files = by_file(cov)
    write(os.path.join(args.out, "cov.json"), {"sets": sets.items, "files": cov_files, "subprocess_tests": enc(sub_tests)})
    fsets = Sets()
    sets = fsets
    write(os.path.join(args.out, "filecov.json"), {"sets": fsets.items, "files": by_file(filecov), "test_files": test_files})

    # ------------------------------------------------------------------ (b) definition time
    dd = {}
    site_sets: list = []
    site_index: dict = {}
    for key, sites in sorted(deps["deps"].items(), key=lambda kv: int(kv[0])):
        key = int(key)
        rel = files[key >> 16]
        sites = tuple(sorted({f"{files[s >> 16]}:{s & 0xFFFF}" if s else "?" for s in sites}))
        i = site_index.get(sites)
        if i is None:
            i = site_index[sites] = len(site_sets)
            site_sets.append(list(sites))
        dd.setdefault(rel, {})[str(key & 0xFFFF)] = i
    ran_by_file = {}
    for key in sorted(import_ran):
        ran_by_file.setdefault(files[key >> 16], []).append(key & 0xFFFF)
    write(os.path.join(args.out, "defdeps.json"), {
        "site_sets": site_sets,
        "deps": dd,
        "ran": {rel: enc(lines) for rel, lines in ran_by_file.items()},
    })

    # ------------------------------------------------------------------ (e) modules
    modules = sorted(m for m in rel_of_module if m.split(".")[0] in ("sqlglot", "tests"))
    mindex = {m: i for i, m in enumerate(modules)}

    def mods(names):
        return enc(mindex[m] for m in names if m in mindex)

    alone = {}
    for path in glob.glob(os.path.join(args.imports, "mod.*.json")):
        rec = load(path)
        if rec.get("error"):
            print(f"  import of {rec['module']} alone failed: {rec['error']}", file=sys.stderr)
        alone[rec["module"]] = mods(rec["modules"])
    lazy = {}
    in_sub = {}
    for number, rec in trecs.items():
        if rec["new_modules"]:
            lazy[str(number)] = mods(rec["new_modules"])
        names = set()
        for sub in subs.get(number, []):
            names.update(sub["modules"])
        if names:
            in_sub[str(number)] = mods(names)
    static_imports = {}
    for rel in files:
        deps_here = set()
        for _line, base, names, _top in static[rel]["imports"]:
            if base in mindex:
                deps_here.add(base)
            for name in names:
                if f"{base}.{name}" in mindex:
                    deps_here.add(f"{base}.{name}")
        if module_of[rel] in mindex:
            static_imports[module_of[rel]] = mods(deps_here)
    # which tests load each module, any way: file import, lazily in the test, in a subprocess
    loaded_by = {m: 0 for m in modules}
    file_modules = {}
    for tf in test_files:
        rec = frecs[tf]
        file_modules[tf] = mods(rec["modules"])
        mask = 0
        for number in tests_of_file[tf]:
            mask |= 1 << number
        for m in rec["modules"]:
            if m in loaded_by:
                loaded_by[m] |= mask
    for number, rec in trecs.items():
        names = set(rec["new_modules"])
        for sub in subs.get(number, []):
            names.update(sub["modules"])
        for m in names:
            if m in loaded_by:
                loaded_by[m] |= 1 << number
    write(os.path.join(args.out, "modules.json"), {
        "modules": modules,
        "path": [rel_of_module[m] for m in modules],
        "imports_static": static_imports,
        "loads_alone": alone,
        "test_file_loads": file_modules,
        "test_lazy_loads": lazy,
        "test_subprocess_loads": in_sub,
        "loaded_by_tests": {m: enc(bits(mask)) for m, mask in loaded_by.items()},
    })

    # ------------------------------------------------------------------ (d) files read
    opened: dict[str, int] = {}  # data file -> tests that opened it (any phase, subprocesses included)
    py_read: dict[str, int] = {}  # .py file read as data (not imported) by a test
    listed: dict[str, int] = {}  # directory listed by a test
    opened_by_file: dict[str, int] = {}

    def keep(entry):
        kind, _, rel = entry.partition(":")
        if kind == "w" or not rel or not os.path.exists(os.path.join(src, rel)):
            return None, None
        if os.path.isdir(os.path.join(src, rel)):
            return "dir", rel.rstrip("/") + "/"
        return ("py" if rel.endswith(".py") else "data"), rel

    for number, rec in trecs.items():
        bit = 1 << number
        body_entries = set(rec["opens_body"])
        imp_entries = set(rec["opens_imp"])
        for sub in subs.get(number, []):
            body_entries.update(sub["opens_body"])
            imp_entries.update(sub["opens_imp"])
        for entry in body_entries | imp_entries:
            what, rel = keep(entry)
            if what == "data":
                opened[rel] = opened.get(rel, 0) | bit
        for entry in body_entries:  # not while importing: the import system reads and lists too
            what, rel = keep(entry)
            if what == "py":
                if rec["failed"] and rel == rec["test"].split("::")[0]:
                    continue  # pytest reading the test file to print the tolerated failure's traceback
                py_read[rel] = py_read.get(rel, 0) | bit
            elif what == "dir":
                listed[rel] = listed.get(rel, 0) | bit
    for tf in test_files:
        rec = frecs[tf]
        for entry in set(rec["opens_body"]) | set(rec["opens_imp"]):
            what, rel = keep(entry)
            if what == "data":
                opened_by_file[rel] = opened_by_file.get(rel, 0) | 1 << tfile_index[tf]

    # A fixture loaded once in setUpClass / setUp into an attribute is opened by every
    # test of the class but consumed only by the tests that touch that attribute.
    # Found statically: `cls.NAME = ...<string constant that ends the file's path>...`
    # inside a set-up function; consumers are the tests whose executed lines of that
    # test file mention `.NAME` (per-test line coverage of the test file).
    import ast as _ast

    consumers = {}
    for tf in test_files:
        tree = _ast.parse(open(os.path.join(src, tf), encoding="utf-8").read())
        attr_lines: dict[str, set[int]] = {}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute):
                attr_lines.setdefault(node.attr, set()).update(range(node.lineno, node.end_lineno + 1))
        tf_id = files.index(tf)
        for func in _ast.walk(tree):
            if not (isinstance(func, _ast.FunctionDef) and func.name in ("setUpClass", "setUp", "setUpModule")):
                continue
            for node in _ast.walk(func):
                if not (isinstance(node, _ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], _ast.Attribute)
                        and isinstance(node.targets[0].value, _ast.Name)
                        and node.targets[0].value.id in ("cls", "self")):
                    continue
                consts = [c.value for c in _ast.walk(node.value)
                          if isinstance(c, _ast.Constant) and isinstance(c.value, str) and "." in c.value]
                name = node.targets[0].attr
                for rel in opened:
                    if not any(rel.endswith("/" + c) or rel == c for c in consts):
                        continue
                    site = set(range(node.lineno, node.end_lineno + 1))
                    mask = 0
                    for line in attr_lines.get(name, set()) - site:
                        mask |= cov.get(tf_id << 16 | line, 0)
                    in_file = 0
                    for number in tests_of_file[tf]:
                        in_file |= 1 << number
                    # tests of this file: only the consumers; tests of other files: as opened
                    refined = (opened[rel] & ~in_file) | (mask & in_file)
                    entry = consumers.setdefault(rel, {"tests": opened[rel], "why": []})
                    entry["tests"] &= refined
                    entry["why"].append(f"{tf}:{node.lineno} {node.targets[0].value.id}.{name}")
    write(os.path.join(args.out, "opens.json"), {
        "by_tests": {rel: enc(bits(mask)) for rel, mask in sorted(opened.items())},
        "consumers": {rel: {"tests": enc(bits(e["tests"])), "why": e["why"]} for rel, e in sorted(consumers.items())},
        "py_read_as_data": {rel: enc(bits(mask)) for rel, mask in sorted(py_read.items())},
        "dirs_listed": {rel: enc(bits(mask)) for rel, mask in sorted(listed.items())},
        "by_test_file_imports": {rel: enc(bits(mask)) for rel, mask in sorted(opened_by_file.items())},
        "test_files": test_files,
    })

    # ------------------------------------------------------------------ static
    slim = {}
    for rel, info in static.items():
        slim[rel] = {k: info[k] for k in ("module", "lines", "scopes", "funcs", "stmts", "spans")}
        slim[rel]["stmts"] = [s[:6] for s in info["stmts"]]  # names read: see derive.json
    write(os.path.join(args.out, "static.json"), {"files": slim})

    # ------------------------------------------------------------------ (c) reads
    if not args.reads:
        return
    print("reads pass", file=sys.stderr)
    runs_b = [read_run(raw, test_index) for raw in args.reads]
    for _f, trecs_b, _s in runs_b:
        missing = [ids[i] for i in range(n_tests) if i not in trecs_b]
        if missing:
            raise SystemExit(f"{len(missing)} tests have no reads trace, e.g. {missing[:3]}")
    whole: dict[str, int] = {}
    anyread: dict[str, int] = {}
    keyed: dict[str, dict[str, int]] = {}
    used: dict[str, int] = {}

    def absorb(payload, mask):
        for name, keys in payload["reads"].items():
            if not name.startswith("sqlglot"):
                continue
            anyread[name] = anyread.get(name, 0) | mask
            for key in keys:
                if key is None:
                    whole[name] = whole.get(name, 0) | mask
                else:
                    slot = keyed.setdefault(name, {})
                    slot[key] = slot.get(key, 0) | mask
        for cls in payload["used"]:
            if cls.startswith("sqlglot"):
                used[cls] = used.get(cls, 0) | mask

    file_level = {}
    for frecs_b, trecs_b, subs_b in runs_b:
        for number, rec in trecs_b.items():
            absorb(rec["reads"], 1 << number)
            for sub in subs_b.get(number, []):
                absorb(sub["reads"], 1 << number)
        for tf in test_files:
            rec = frecs_b[tf]
            mask = 0
            for number in tests_of_file[tf]:
                mask |= 1 << number
            absorb(rec["reads"], mask)  # what importing the test file reads counts for all its tests
            names = sorted(n for n in rec["reads"]["reads"] if n.startswith("sqlglot"))
            if names:
                file_level[tf] = sorted(set(file_level.get(tf, [])) | set(names))

    rsets = Sets()
    reads_out = {}
    for name in sorted(anyread):
        entry = {"any": rsets.add(anyread[name])}
        if name in whole:
            entry["whole"] = rsets.add(whole[name])
        if name in keyed:
            entry["keys"] = {k: rsets.add(m) for k, m in sorted(keyed[name].items())}
        reads_out[name] = entry
    write(os.path.join(args.out, "reads.json"), {
        "sets": rsets.items,
        "reads": reads_out,
        "used_classes": {c: rsets.add(m) for c, m in sorted(used.items())},
        "read_at_test_file_import": file_level,
    })

    imp = load(os.path.join(args.reads[0], "imports_reads.json"))
    derive = {}
    for sid, payload in imp["reads"].items():
        sid = int(sid)
        if sid == 0:
            continue
        rel = files[sid >> 16]
        st = static[rel]["stmts"][sid & 0xFFFF]
        rows = []
        for name, keys in sorted(payload.items()):
            if not name.startswith("sqlglot"):
                continue
            rows.append(name if None in keys else [name] + sorted(keys))
        if rows:
            derive.setdefault(rel, {})[f"{st[0]}-{st[1]}"] = rows
    classes = {}
    for name, info in imp["classes"].items():
        module, _, qual = name.partition(":")
        rel = rel_of_module.get(module)
        bound = static[rel]["binds"].get(qual, {}) if rel else {}
        info = dict(info)
        info["set_at_class_creation"] = sorted(a for a in info["attrs"] if a not in bound)
        classes[name] = info
    write(os.path.join(args.out, "derive.json"), {"reads_at_import": derive})
    write(os.path.join(args.out, "classes.json"), {"classes": classes, "module_data": imp["modules"]})


if __name__ == "__main__":
    main()

"""Runs in a child process: import a sqlglot tree, fingerprint every class-level
and module-level value (tools/snapshot.py) and print how it differs from the
fingerprint of the pinned tree taken when the image was built.

    python -S snapshot_child.py diff <tree root> <base.json>      -> JSON on stdout
    python -S snapshot_child.py base <tree root> <base.json>      (image build)

Both sides must run with the same PYTHONHASHSEED; `base` takes the fingerprint
three times and records the names whose value differs between identical runs
(tuples built by iterating sets of classes follow memory addresses) so that
`diff` ignores their order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))


PERTURBATIONS = (0, 211, 4099, 30011)


def _take(root: str) -> dict:
    # Tuples that sqlglot builds by iterating sets of classes are ordered by memory address.
    # Allocating a different number of objects first moves every later address, so repeated
    # base runs show which values depend on that (see `base` below).
    junk = [type("Junk", (), {}) for _ in range(int(os.environ.get("BLAST_PERTURB", "0")))]
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    import logging

    logging.disable(logging.CRITICAL)
    import snapshot  # tools/snapshot.py

    stdout = sys.stdout
    sys.stdout = sys.stderr  # whatever the imported code prints must not reach our JSON
    try:
        return snapshot.take()
    finally:
        sys.stdout = stdout
        del junk


def compare(base: dict, snap: dict) -> dict:
    """-> {"changed": {name: None | [keys] | "order"}, "errors": {module: message}}.

    "order" means: nothing that can be told apart from run-to-run noise changed (same members
    in another order, or a value that already differs between identical runs of the pinned tree)."""
    import snapshot

    variants = base.get("variants", {})
    changed = {}
    for name, keys in snapshot.diff(base, snap)["changed"].items():
        alts = variants.get(name)
        if alts is None:
            changed[name] = keys
            continue
        y = snap["values"].get(name)
        alts = [base["values"].get(name)] + alts
        if y is None or any(a is None for a in alts):
            changed[name] = keys
            continue
        if any(y == a for a in alts):
            continue
        if "k" in y and all("k" in a for a in alts):
            every = set(y["k"])
            for a in alts:
                every |= set(a["k"])
            differing = sorted(k for k in every if all(a["k"].get(k) != y["k"].get(k) for a in alts))
            if any(k.startswith("?") for k in differing):
                changed[name] = None
            elif differing:
                changed[name] = differing
            else:
                changed[name] = "order"
        else:
            changed[name] = "order"
    errors = {m: e for m, e in snap["errors"].items() if m not in base["errors"]}
    # names that vanished, appeared, or are now bound to another function / class / module
    removed = sorted(n for n in changed if n not in snap["values"])
    added = sorted(n for n in changed if n not in base["values"])
    rebound = sorted(n for n in changed if n in snap["values"] and n in base["values"]
                     and base["values"][n].get("i") != snap["values"][n].get("i"))
    return {"changed": changed, "errors": errors, "removed": removed, "added": added, "rebound": rebound}


def main(argv: list[str]) -> int:
    mode, root, base_path = argv[1], argv[2], argv[3]
    if mode == "take":
        json.dump(_take(root), sys.stdout, separators=(",", ":"), sort_keys=True)
        return 0
    if mode == "base":
        snaps = []
        for junk in PERTURBATIONS:
            env = dict(os.environ, PYTHONHASHSEED="0", BLAST_PERTURB=str(junk))
            out = subprocess.run([sys.executable, "-S", os.path.abspath(__file__), "take", root, "-"],
                                 env=env, stdout=subprocess.PIPE, check=True).stdout
            snaps.append(json.loads(out))
        first = snaps[0]
        variants = {}
        for name, fp in first["values"].items():
            others = []
            for other in snaps[1:]:
                got = other["values"].get(name)
                if got != fp and got not in others:
                    others.append(got)
            if others:
                variants[name] = others
        first["variants"] = variants
        os.makedirs(os.path.dirname(os.path.abspath(base_path)), exist_ok=True)
        with open(base_path, "w") as fh:
            json.dump(first, fh, separators=(",", ":"), sort_keys=True)
        print(f"base fingerprint: {len(first['values'])} values, {len(variants)} that differ between identical runs "
              f"({', '.join(sorted(variants)[:6])}), {len(first['errors'])} modules that do not import", file=sys.stderr)
        return 0
    snap = _take(root)
    if snap.get("fatal"):
        # `import sqlglot` itself failed: no value can be compared, every module is broken
        json.dump({"changed": {}, "errors": snap["errors"], "fatal": True}, sys.stdout, separators=(",", ":"))
        return 0
    with open(base_path, "rb") as fh:
        base = json.load(fh)
    json.dump(compare(base, snap), sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

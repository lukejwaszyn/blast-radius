#!/usr/bin/env python3
"""Image build step: the checkout must be the commit the maps were made from,
and the selector must answer a few made-up patches as user-readable files.

    python check_image.py <checkout> <maps dir> <selector.py>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


def main() -> None:
    checkout, maps_dir, selector = sys.argv[1:4]
    with open(os.path.join(maps_dir, "static.json")) as fh:
        static = json.load(fh)["files"]
    wrong = []
    for rel, info in static.items():
        full = os.path.join(checkout, rel)
        if not os.path.isfile(full):
            wrong.append(f"{rel}: missing")
            continue
        with open(full, "rb") as fh:
            lines = fh.read().count(b"\n")
        if abs(lines - info["lines"]) > 1:
            wrong.append(f"{rel}: {lines} lines, the maps say {info['lines']}")
    if wrong:
        raise SystemExit("checkout does not match the maps:\n  " + "\n  ".join(wrong[:20]))
    with open(os.path.join(maps_dir, "tests.json")) as fh:
        tests = json.load(fh)
    with tempfile.TemporaryDirectory() as data:
        os.mkdir(os.path.join(data, "commits"))
        with open(os.path.join(data, "tests.json"), "w") as fh:
            json.dump({"tests": [{"number": i, "id": tid, "file": tid.split("::")[0], "seconds": s}
                                 for i, (tid, s) in enumerate(zip(tests["ids"], tests["seconds"]))]}, fh)
        patches = {
            "comment": "--- a/sqlglot/trie.py\n+++ b/sqlglot/trie.py\n@@ -1,2 +1,3 @@\n import typing as t\n+# note\n from enum import Enum, auto\n",
            "syntax": "--- a/sqlglot/trie.py\n+++ b/sqlglot/trie.py\n@@ -1,2 +1,3 @@\n import typing as t\n+def (:\n from enum import Enum, auto\n",
            "value": "--- a/sqlglot/dialects/tsql.py\n+++ b/sqlglot/dialects/tsql.py\n@@ -17 +17 @@\n-    WEEK_OFFSET = -1\n+    WEEK_OFFSET = -3\n",
        }
        for name, text in patches.items():
            with open(os.path.join(data, "commits", name + ".patch"), "w") as fh:
                fh.write(text)
        env = dict(os.environ, BLAST_DATA=data)
        out = subprocess.run([sys.executable, "-S", selector], env=env, stdout=subprocess.PIPE, check=True).stdout
        got = {}
        for line in out.decode().splitlines():
            row = json.loads(line)
            got[row["commit"]] = sum(len(w) for w in row["workers"])
        if got.get("comment") != 0 or got.get("syntax", 0) < 1000 or not 1 <= got.get("value", 0) <= 50:
            raise SystemExit(f"selector self-check failed: {got}")
    print(f"image check passed: {len(static)} files match the maps; self-check {got}")


if __name__ == "__main__":
    main()

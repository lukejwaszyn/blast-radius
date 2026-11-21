#!/usr/bin/env python3
"""Image build step: download sqlglot at the pinned commit from GitHub and unpack it.

    python fetch_sqlglot.py <target dir>

The only thing the build downloads besides the base image.  The archive URL
names the full commit hash, so the content cannot change after the deadline.
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import time
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URLS = (
    f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz",
    f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}",
)


def download() -> bytes:
    last = None
    for attempt in range(4):
        for url in URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "python-urllib"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except Exception as exc:  # network hiccup: try the other address, then again
                last = exc
        time.sleep(2 + 3 * attempt)
    raise SystemExit(f"could not download sqlglot at {COMMIT}: {last}")


def main() -> None:
    target = sys.argv[1]
    data = download()
    os.makedirs(target, exist_ok=True)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1] or member.name.startswith("pax_global_header"):
                continue
            member.name = parts[1]
            if member.islnk() or member.issym():
                continue
            tar.extract(member, target, filter="data")
            count += 1
    if not os.path.isfile(os.path.join(target, "sqlglot", "__init__.py")):
        raise SystemExit("the archive does not look like sqlglot")
    print(f"sqlglot {COMMIT}: {count} entries in {target}")


if __name__ == "__main__":
    main()

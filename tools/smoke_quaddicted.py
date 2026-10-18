#!/usr/bin/env python3
"""Live smoke test against the real Quaddicted API — run manually, NOT part of the pytest suite.

Steps (small, conservative, nothing published):
  1. fetch one page of the light catalogue listing (https://www.quaddicted.com/api/v1/?q=*:*&fl=sha256,json_timestamp)
  2. use tools.quaddicted.sync_index to bring the listing into a temporary SQLite index
  3. fetch one known entry's full document and its package (a small ZIP ~1.2 MB)
  4. verify the downloaded bytes hash to the entry's SHA-256 and are a ZIP

Usage:  python tools/smoke_quaddicted.py [--mirror-file <name.zip>]
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools.quaddicted as qd
from tools import netfetch


def main() -> int:
    fetch = qd.fetch_fetcher(netfetch.FetchLimits(max_bytes=qd.DOCS_MAX_BYTES, connect_timeout=15, read_timeout=60, max_duration=600, max_attempts=2))
    with tempfile.TemporaryDirectory(prefix="qd-smoke-") as tmp:
        db_path = Path(tmp) / "index.sqlite3"
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        qd.init_schema(conn)

        print("1. listing (first page)…")
        started = time.time()
        listing = qd.fetch_listing(fetch, lambda stage, done, total: None)
        print(f"   {len(listing)} upstream entries in {time.time() - started:.1f}s")
        stats = qd.sync_index(lambda: conn, fetch, lambda stage, done, total: None, full=True)
        print(f"2. full sync: {qd.count(conn)} entries local, stats={stats}")

        entry = qd.get_entry(conn, "04fb25c74b074552d5b6d9ce077adeed67dc66e0919f11cd61111dcb2943d9b3")  # rte.zip, verified small
        if entry is None:
            print("   the known test entry is gone upstream; pick another sha256 from the listing above (index saved at %s)" % db_path)
            return 1
        print(f"3. entry: {entry.display_title!r} by {', '.join(entry.authors)} ({entry.bytes} bytes, updated {entry.json_timestamp})")
        url = entry.download_url
        print(f"   download: {url}")
        limits = netfetch.FetchLimits(max_bytes=64 * 1024 * 1024, connect_timeout=15, read_timeout=60, max_duration=900)
        res = netfetch.fetch_to_file(url, Path(tmp) / "package.bin", limits)
        sha = res.sha256
        print(f"4. downloaded {res.size} bytes in {res.attempts} attempt(s); sha256={sha[:16]}…")
        if sha != entry.sha256:
            print(f"   MISMATCH: entry says {entry.sha256}")
            return 1
        if res.content_type.split(";")[0].strip() != "application/zip" or not Path(tmp, "package.bin").read_bytes().startswith(b"PK\x03\x04"):
            print(f"   unexpected content type: {res.content_type}")
            return 1
        print("   OK: bytes match the entry's SHA-256 and it is a ZIP archive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

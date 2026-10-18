"""Persistence for batch imports (SQLite). No Flask, no file handling: tables and small, atomic state transitions.

A batch is a list of items; each item moves through states and is the source of truth for what happened to one package. Progress
numbers (stage, bytes, percent) are read from the item's current job in the shared `jobs` table.

States
  queued           waiting for a download/staging worker
  acquiring        downloading or copying into staging
  converting       staged; being inspected, converted, validated and installed (shared build code)
  awaiting_review  needs the preview page (e.g. ambiguous root, too many entries) or a replacement review; staged upload kept
  needs_decision   a conflict that a person has to resolve (add-on ID taken); staged upload kept
  completed        a draft exists (add_pk)
  skipped          nothing was imported on purpose (identical content already imported)
  failed / cancelled / interrupted   terminal; retry is possible from these
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

ACTIVE = ("queued", "acquiring", "converting")
WAITING = ("awaiting_review", "needs_decision")
TERMINAL = ("completed", "skipped", "failed", "cancelled", "interrupted")
RETRYABLE = ("failed", "cancelled", "interrupted")

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT NOT NULL,               -- url | folder
        label TEXT NOT NULL,
        submit_key TEXT UNIQUE,                  -- makes a double submit return the same batch
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        source_type TEXT NOT NULL,
        source_key TEXT NOT NULL,                -- hash of the normalised URL / of root+path
        source_display TEXT NOT NULL,            -- what may be shown and logged (redacted)
        source_url TEXT,                         -- full URL, only while needed for retry / re-import; cleared when the item completes or expires
        folder_root INTEGER,
        folder_rel TEXT,
        src_size INTEGER, src_mtime_ns INTEGER, src_ino INTEGER, src_dev INTEGER,
        state TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        job_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        import_token TEXT,
        addon_pk INTEGER,
        sha256 TEXT,
        final_name TEXT,
        host TEXT,
        staged_bytes INTEGER NOT NULL DEFAULT 0,
        derived_json TEXT,                       -- derived add-on id/title
        source_json TEXT,                        -- quaddicted items: the entry's metadata for draft suggestions and provenance
        action_json TEXT,                        -- what a person is being asked to decide
        force INTEGER NOT NULL DEFAULT 0,        -- import even though identical content exists
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        finished_at REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_items_state ON batch_items(state)",
    "CREATE INDEX IF NOT EXISTS idx_batch_items_source ON batch_items(source_key)",
    """
    CREATE TABLE IF NOT EXISTS batch_scans (
        id TEXT PRIMARY KEY,
        root_index INTEGER NOT NULL,
        subpath TEXT NOT NULL,
        recursive INTEGER NOT NULL,
        kinds TEXT NOT NULL,
        created_at REAL NOT NULL,
        entries_json TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )
    """,
)


def init_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(batch_items)").fetchall()}
    if "source_json" not in existing:                        # non-destructive upgrade of existing installations
        conn.execute("ALTER TABLE batch_items ADD COLUMN source_json TEXT")


# ---- batches ---------------------------------------------------------------------------------------------------------
def create_batch(conn: sqlite3.Connection, source_type: str, label: str, submit_key: str | None) -> int:
    cur = conn.execute("INSERT INTO batches (source_type, label, submit_key, created_at) VALUES (?,?,?,?)", (source_type, label, submit_key, time.time()))
    return int(cur.lastrowid)


def batch_by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE submit_key = ?", (key,)).fetchone()


def get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def recent_batches(conn: sqlite3.Connection, limit: int = 15) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        d = dict(row)
        d["counts"] = counts_by_state(conn, row["id"])
        out.append(d)
    return out


def counts_by_state(conn: sqlite3.Connection, batch_id: int) -> dict[str, int]:
    return {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM batch_items WHERE batch_id = ? GROUP BY state", (batch_id,))}


# ---- items -----------------------------------------------------------------------------------------------------------
def add_item(conn: sqlite3.Connection, batch_id: int, position: int, source_type: str, source_key: str, source_display: str, **fields: Any) -> int:
    now = time.time()
    cols = {"batch_id": batch_id, "position": position, "source_type": source_type, "source_key": source_key, "source_display": source_display,
            "state": "queued", "created_at": now, "updated_at": now, **fields}
    cur = conn.execute(f"INSERT INTO batch_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
    return int(cur.lastrowid)


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batch_items WHERE id = ?", (item_id,)).fetchone()


def items_for_batch(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position", (batch_id,)).fetchall()


def update_item(conn: sqlite3.Connection, item_id: int, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    if fields.get("state") in TERMINAL and "finished_at" not in fields:
        fields["finished_at"] = fields["updated_at"]
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE batch_items SET {cols} WHERE id = ?", (*fields.values(), item_id))


def transition(conn: sqlite3.Connection, item_id: int, from_states: tuple[str, ...], to_state: str, **fields: Any) -> bool:
    """Atomic compare-and-set: only one caller can move an item out of `from_states`. Returns whether this call did."""
    fields["state"] = to_state
    fields["updated_at"] = time.time()
    if to_state in TERMINAL:
        fields.setdefault("finished_at", fields["updated_at"])
    elif "finished_at" not in fields:
        fields["finished_at"] = None
    cols = ", ".join(f"{k} = ?" for k in fields)
    marks = ", ".join("?" for _ in from_states)
    cur = conn.execute(f"UPDATE batch_items SET {cols} WHERE id = ? AND state IN ({marks})", (*fields.values(), item_id, *from_states))
    return cur.rowcount == 1


def pending_total(conn: sqlite3.Connection) -> int:
    marks = ", ".join("?" for _ in ACTIVE)
    return conn.execute(f"SELECT COUNT(*) FROM batch_items WHERE state IN ({marks})", ACTIVE).fetchone()[0]


def staged_total(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(staged_bytes), 0) FROM batch_items").fetchone()[0])


def last_completed_for_source(conn: sqlite3.Connection, source_key: str, exclude_item: int = 0) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batch_items WHERE source_key = ? AND state = 'completed' AND addon_pk IS NOT NULL AND id != ? ORDER BY id DESC LIMIT 1",
                        (source_key, exclude_item)).fetchone()


def active_for_source(conn: sqlite3.Connection, source_key: str) -> sqlite3.Row | None:
    marks = ", ".join("?" for _ in ACTIVE + WAITING)
    return conn.execute(f"SELECT * FROM batch_items WHERE source_key = ? AND state IN ({marks}) ORDER BY id DESC LIMIT 1", (source_key, *ACTIVE, *WAITING)).fetchone()


# ---- folder scans ----------------------------------------------------------------------------------------------------
def save_scan(conn: sqlite3.Connection, scan_id: str, root_index: int, subpath: str, recursive: bool, kinds: tuple[str, ...],
              entries: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    conn.execute("INSERT INTO batch_scans (id, root_index, subpath, recursive, kinds, created_at, entries_json, summary_json) VALUES (?,?,?,?,?,?,?,?)",
                 (scan_id, root_index, subpath, int(recursive), ",".join(kinds), time.time(), json.dumps(entries), json.dumps(summary)))


def get_scan(conn: sqlite3.Connection, scan_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM batch_scans WHERE id = ?", (scan_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["entries"] = json.loads(d.pop("entries_json"))
    d["summary"] = json.loads(d.pop("summary_json"))
    d["kinds"] = tuple(k for k in d["kinds"].split(",") if k)
    return d


def expire(conn: sqlite3.Connection, scan_max_age: float, batch_max_age: float) -> dict[str, int]:
    """Housekeeping: old scans are dropped; finished batches older than the retention period are deleted with their items (and any stored URLs)."""
    now = time.time()
    scans = conn.execute("DELETE FROM batch_scans WHERE created_at < ?", (now - scan_max_age,)).rowcount
    marks = ", ".join("?" for _ in ACTIVE + WAITING)
    old = [r["id"] for r in conn.execute(
        f"SELECT b.id FROM batches b WHERE b.created_at < ? AND NOT EXISTS (SELECT 1 FROM batch_items i WHERE i.batch_id = b.id AND i.state IN ({marks}))",
        (now - batch_max_age, *ACTIVE, *WAITING)).fetchall()]
    for bid in old:
        conn.execute("DELETE FROM batch_items WHERE batch_id = ?", (bid,))
        conn.execute("DELETE FROM batches WHERE id = ?", (bid,))
    # a finished item no longer needs its full URL (it may carry a signed token)
    conn.execute("UPDATE batch_items SET source_url = NULL WHERE source_url IS NOT NULL AND state = 'completed'")
    return {"scans": scans, "batches": len(old)}

"""Quaddicted integration: browse Quaddicted's database locally and import packages through the shared batch pipeline.

Verified upstream interface (checked live 2026-10-17; see docs/quaddicted.md):
* Official read-only JSON API ``https://www.quaddicted.com/api/v1/?q=<Solr query>``. Supports ``q``
  (``sha256:<hex>``, ``tags:"key=value"``, ``*:*``, OR groups), ``rows``, ``start`` and ``fl``. A query returns a
  JSON *array* of document objects (not a Solr envelope).
* The same documents are published in the repository github.com/Quaddicted/quaddicted-data
  (json/by-sha256/<2hex>/<sha256>.json). The GitHub repository is a fallback documentation anchor, not used at runtime.
* A stable entry identity is the package content hash (its SHA-256). The canonical entry page for an entry is
  https://www.quaddicted.com/db/canonical/<sha256>.
* ``urls`` lists direct download links. ``https://www.quaddicted.com/files/by-sha256/<2hex>/<sha256>/<name>``
  is content-addressed (verified: HTTP 200, application/zip, honest Content-Length); ``/filebase/<name>`` redirects.
* Raw screenshots follow the convention /files/quaddicted-images/by-sha256/<2hex>/<sha256>/<zip-stem>.jpg
  (confirmed against the quaddicted-images repository for sampled entries). A missing image is fine: the browser
  fetches it directly and shows the alt text; this server never proxies images.
* No ratings in the API, so none are shown. No documented rate limits, so access stays conservative: synchronisation
  is manual (a button), bounded (paged, size- and count-capped), and nothing upstream is requested per page view.

This module holds everything that does not need Flask: document parsing (defensive against schema changes), the local
SQLite index, the sync algorithm and the URL/metadata mapping. Routes live in quaddicted.QuaddictedService.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote

from flask import flash, redirect, render_template, request, url_for

from tools import netfetch
from tools.jobs import JobOutcome

API_BASE = "https://www.quaddicted.com/api/v1/"
SITE_BASE = "https://www.quaddicted.com"

SYNC_ROWS = 500                                   # documents per full-sync page (observed ~4.7 MB per page)
SYNC_MAX_PAGES = 14                               # hard cap: 14 * 500 = 7000 documents
CHUNK_SIZE = 40                                   # sha256 values per delta query
CHUNK_MAX_QUERIES = 25                            # 25 * 40 = 1000 changed entries per delta sync, else full refetch
DESCRIPTION_MAX = 4000
URLS_MAX = 10
LISTING_MAX_BYTES = 8 * 1024 * 1024
DOCS_MAX_BYTES = 32 * 1024 * 1024
SYNC_MIN_SECONDS = 300.0                          # at most one synchronisation per 5 minutes (upstream stays conservative)

_SHA = re.compile(r"^[0-9a-f]{64}$")
_STEM_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class UpstreamProblem(Exception):
    """The upstream answer could not be used (schema change, outage, rate limit). Never carries URLs or query text."""


def listing_url(start: int) -> str:
    return f"{API_BASE}?q=*:*&fl=sha256,json_timestamp&start={int(start)}&rows={SYNC_ROWS}"


def full_page_url(start: int) -> str:
    """A page of full documents (no `fl` restriction)."""
    return f"{API_BASE}?q=*:*&start={int(start)}&rows={SYNC_ROWS}"


def docs_url(shas: list[str], start: int = 0) -> str:
    query = "sha256:(" + "%20OR%20".join(quote(s) for s in shas) + ")"
    return f"{API_BASE}?q={query}&start={int(start)}&rows={len(shas)}"


def page_url(sha256: str) -> str:
    return f"{SITE_BASE}/db/canonical/{sha256}"


def human_age(synced_at: float | None, now: float | None = None) -> str:
    """A short display of how old the cached copy is (shown so a stale index is never mistaken for current)."""
    if not synced_at:
        return "never"
    seconds = (now if now is not None else time.time()) - synced_at
    if seconds < 90:
        return "less than a minute ago"
    for size, unit in ((86400 * 60, "month"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def parse_page(value: str | None) -> int:
    try:
        return int(value or "")
    except (TypeError, ValueError):
        return 1


def screenshot_url(sha256: str, filename: str | None) -> str | None:
    """Derived from the verified naming convention; returns None when the filename does not fit it. The URL points at
    quaddicted.com, is https and contains no query, and is checked against a narrow pattern before it is shown."""
    if not filename or not filename.lower().endswith(".zip"):
        return None
    stem = filename[:-4]
    if not _STEM_SAFE.match(stem):
        return None
    if not _SHA.match(sha256):
        return None
    return f"{SITE_BASE}/files/quaddicted-images/by-sha256/{sha256[:2]}/{sha256}/{stem}.jpg"


def pick_download_url(urls: list[str]) -> str | None:
    """Prefer the content-addressed canonical link on www.quaddicted.com, then any quaddicted.com link, then the first."""
    https_urls = [u for u in urls if isinstance(u, str) and u.startswith(("https://", "http://"))]
    by_sha = [u for u in https_urls if u.startswith(f"{SITE_BASE}/files/by-sha256/")]
    if by_sha:
        return by_sha[0]
    own = [u for u in https_urls if u.startswith(f"{SITE_BASE}/")]
    return own[0] if own else (https_urls[0] if https_urls else None)


# ---- document parsing (defensive: upstream schema changes must not crash the app) --------------------------------------

class _TextOnly(HTMLParser):
    """Collect only character data: upstream notes may contain HTML, which is never rendered or trusted."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)


def strip_html(text: str, limit: int = DESCRIPTION_MAX) -> str:
    """Upstream `notes` sometimes contains HTML. Convert to plain text (tags dropped, entities decoded); nothing upstream
    is ever rendered as markup."""
    if not text:
        return ""
    extractor = _TextOnly()
    try:
        extractor.feed(text)
        extractor.close()
    except Exception:                                   # a malformed fragment must not break the sync
        return ""
    joined = re.sub(r"\s+", " ", " ".join(extractor.parts)).strip()
    return joined[:limit] if len(joined) > limit else joined


def parse_tags(tags: Any) -> dict[str, list[str]]:
    """`tags` is a list of "key=value" strings (verified). Malformed members are skipped, not fatal."""
    out: dict[str, list[str]] = {}
    if not isinstance(tags, list):
        return out
    for tag in tags:
        if isinstance(tag, str) and "=" in tag:
            key, value = tag.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and value:
                out.setdefault(key, []).append(value)
    return out


def parse_doc(raw: Any) -> "Entry | None":
    """One API document -> Entry, or None if it is not a usable entry (bad/missing sha256). Unknown fields are ignored;
    odd field types are coerced or dropped; no exception can escape a malformed document."""
    if not isinstance(raw, dict):
        return None
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _SHA.match(sha):
        return None
    tags = parse_tags(raw.get("tags"))
    urls = [u for u in raw.get("urls") or [] if isinstance(u, str) and u.startswith("http")][:URLS_MAX]
    bytes_value = raw.get("bytes")
    if isinstance(bytes_value, bool) or not isinstance(bytes_value, int):
        try:
            bytes_value = int(str(bytes_value))
        except (ValueError, TypeError):
            bytes_value = None
    description = raw.get("description")
    # Upstream text fields are reduced to plain text as early as possible: whatever markup they contain is gone before any page renders.
    description = strip_html(description, DESCRIPTION_MAX) if isinstance(description, str) else None
    notes = strip_html(raw.get("notes") if isinstance(raw.get("notes"), str) else "")
    install = raw.get("install")
    install_extract = _text(install.get("extract"), 200) if isinstance(install, dict) else None
    if not install_extract:
        install_extract = _first_keyish(tags, ("zipbasedir", "install"))
    return Entry(
        sha256=sha,
        title=_plain(_first(tags, "title")),
        authors=[_plain(a) for a in tags.get("author", [])[:20]],
        filename=_first(tags, "filename"),
        types=tags.get("type", [])[:10],
        game_modes=tags.get("game_mode", [])[:10],
        release_date=_first(tags, "release_date"),
        description=description,
        notes=notes,
        bytes=bytes_value,
        urls=urls,
        install_extract=install_extract,
        json_timestamp=_text(raw.get("json_timestamp"), 40),
        json_version=_text(raw.get("json_version"), 40),
        depends=tags.get("depends", [])[:20],
        provides=tags.get("provides", [])[:20],
        mods=tags.get("mod", [])[:20],
        dependency_files=tags.get("dependency", [])[:20],
        exceeds_limits=_first(tags, "exceeds_quake_limits") is not None or _first(tags, "exceedslimits") is not None,
        nuked=_first(tags, "nuked") is not None,
        startmaps=tags.get("startmap", [])[:20],
        themes=tags.get("theme", [])[:40],
        links=tags.get("link", [])[:10],
        homepages=[u for u in tags.get("link:homepage", [])[:5] if u.startswith(("http://", "https://"))],
    )


def _first_keyish(tags: dict[str, list[str]], keys: tuple[str, ...]) -> str | None:
    """The extraction hint: prefer `zipbasedir` (e.g. id1/); `install` arrives as its own document field in some exports."""
    for key in keys:
        value = _first(tags, key)
        if value:
            return value
    return None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _plain(value: str | None, limit: int = 200) -> str | None:
    """Reduce an upstream text field to plain text (tags dropped, entities decoded, whitespace collapsed)."""
    if not value:
        return value
    stripped = strip_html(value, limit)
    return stripped or value.strip()


def _first(tags: dict[str, list[str]], key: str) -> str | None:
    values = tags.get(key)
    return values[0] if values else None


@dataclass(frozen=True)
class Entry:
    sha256: str
    title: str | None
    authors: list[str]
    filename: str | None
    types: list[str]
    game_modes: list[str]
    release_date: str | None
    description: str | None
    notes: str
    bytes: int | None
    urls: list[str]
    install_extract: str | None
    json_timestamp: str | None
    json_version: str | None
    depends: list[str]
    provides: list[str]
    mods: list[str]
    dependency_files: list[str]
    exceeds_limits: bool
    nuked: bool
    startmaps: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    homepages: list[str] = field(default_factory=list)

    @classmethod
    def from_doc(cls, raw: Any) -> "Entry | None":
        return parse_doc(raw)

    @property
    def page(self) -> str:
        return page_url(self.sha256)

    @property
    def screenshot(self) -> str | None:
        return screenshot_url(self.sha256, self.filename)

    @property
    def download_url(self) -> str | None:
        return pick_download_url(self.urls)

    @property
    def safe_links(self) -> list[str]:
        """Upstream link tags, restricted to absolute http(s) URLs (never javascript:, data:, or relative values)."""
        out = []
        for candidate in [*self.homepages, *self.links]:
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")) and "<" not in candidate and '"' not in candidate:
                out.append(candidate)
        return out[:10]

    @property
    def display_title(self) -> str:
        return self.title or self.filename or self.sha256[:12]

    def view(self) -> dict[str, Any]:
        """Everything the templates may show (all plain text; Jinja escapes it)."""
        return {
            "sha256": self.sha256, "sha256_short": self.sha256[:12], "title": self.display_title, "authors": self.authors,
            "filename": self.filename, "types": self.types, "game_modes": self.game_modes, "release_date": self.release_date,
            "description": self.description, "notes": self.notes, "bytes": self.bytes, "download_url": self.download_url,
            "install_extract": self.install_extract, "json_timestamp": self.json_timestamp, "json_version": self.json_version,
            "depends": self.depends, "provides": self.provides, "mods": self.mods, "dependency_files": self.dependency_files,
            "exceeds_limits": self.exceeds_limits, "nuked": self.nuked, "startmaps": self.startmaps, "themes": self.themes,
            "links": self.links, "homepages": self.homepages, "safe_links": self.safe_links, "page": self.page, "screenshot": self.screenshot,
        }

    def source_json(self) -> str:
        """The compact per-item metadata carried through the batch pipeline (for draft suggestions and provenance).
        Deliberately excludes `files` and the full tag list: it is stored per batch item and must stay small."""
        return json.dumps({
            "sha256": self.sha256, "title": self.title, "author": "; ".join(self.authors), "filename": self.filename,
            "date": self.release_date, "description": self.description, "types": self.types,
            "page_url": self.page, "download_url": self.download_url, "upstream_updated": self.json_timestamp,
            "depends": self.depends, "provides": self.provides, "mods": self.mods, "exceeds_limits": self.exceeds_limits,
        }, sort_keys=True)


# ---- the local index (SQLite) ------------------------------------------------------------------------------------------
# All search, filter, sorting and paging happens here, never upstream. Refreshed only when an administrator asks; the
# previous copy stays browsable while a refresh runs or upstream is down (the UI shows its age).

_COLUMNS = (
    "sha256 TEXT PRIMARY KEY",
    "title TEXT", "filename TEXT", "authors_json TEXT NOT NULL DEFAULT '[]'", "types_json TEXT NOT NULL DEFAULT '[]'",
    "game_modes_json TEXT NOT NULL DEFAULT '[]'", "release_date TEXT", "description TEXT", "notes TEXT",
    "bytes INTEGER", "urls_json TEXT NOT NULL DEFAULT '[]'", "install_extract TEXT", "json_timestamp TEXT",
    "json_version TEXT", "depends_json TEXT NOT NULL DEFAULT '[]'", "provides_json TEXT NOT NULL DEFAULT '[]'",
    "mods_json TEXT NOT NULL DEFAULT '[]'", "dependency_files_json TEXT NOT NULL DEFAULT '[]'",
    "exceeds_limits INTEGER NOT NULL DEFAULT 0", "nuked INTEGER NOT NULL DEFAULT 0", "tags_json TEXT NOT NULL DEFAULT '[]'",
    "synced_at REAL NOT NULL",
)

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS quaddicted_entries (" + ", ".join(_COLUMNS) + ")",
    "CREATE TABLE IF NOT EXISTS quaddicted_state (key TEXT PRIMARY KEY, value TEXT)",
)
_ENTRY_ROW_COLUMNS = [c.split()[0] for c in _COLUMNS]
_TAG_FIELD_MAP = {"authors_json": "authors", "types_json": "types", "game_modes_json": "game_modes", "urls_json": "urls",
                  "depends_json": "depends", "provides_json": "provides", "mods_json": "mods", "dependency_files_json": "dependency_files"}


def init_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)


def _state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM quaddicted_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO quaddicted_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def upsert_entry(conn: sqlite3.Connection, entry: Entry, now: float) -> None:
    tags: dict[str, list[str]] = {"title": [entry.title] if entry.title else [], "filename": [entry.filename] if entry.filename else []}
    row = {
        "sha256": entry.sha256, "title": entry.title, "filename": entry.filename,
        "authors_json": json.dumps(entry.authors), "types_json": json.dumps(entry.types),
        "game_modes_json": json.dumps(entry.game_modes), "release_date": entry.release_date,
        "description": entry.description, "notes": entry.notes, "bytes": entry.bytes,
        "urls_json": json.dumps(entry.urls), "install_extract": entry.install_extract,
        "json_timestamp": entry.json_timestamp, "json_version": entry.json_version,
        "depends_json": json.dumps(entry.depends), "provides_json": json.dumps(entry.provides),
        "mods_json": json.dumps(entry.mods), "dependency_files_json": json.dumps(entry.dependency_files),
        "exceeds_limits": int(entry.exceeds_limits), "nuked": int(entry.nuked), "tags_json": json.dumps(tags),
        "synced_at": now,
    }
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "sha256")
    conn.execute(f"INSERT INTO quaddicted_entries ({columns}) VALUES ({marks}) ON CONFLICT(sha256) DO UPDATE SET {updates}", tuple(row.values()))


def prune(conn: sqlite3.Connection, keep_shas: set[str], now: float) -> int:
    """Remove rows that no longer exist upstream (called for full syncs and delta syncs). Bounded by the table size."""
    rows = [r[0] for r in conn.execute("SELECT sha256 FROM quaddicted_entries").fetchall()]
    gone = [s for s in rows if s not in keep_shas]
    for s in gone:
        conn.execute("DELETE FROM quaddicted_entries WHERE sha256 = ?", (s,))
    return len(gone)


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM quaddicted_entries").fetchone()[0])


def last_synced_at(conn: sqlite3.Connection) -> float | None:
    value = _state_get(conn, "synced_at")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def get_entry(conn: sqlite3.Connection, sha256: str) -> Entry | None:
    if not _SHA.match(sha256 or ""):
        return None
    row = conn.execute(f"SELECT {', '.join(_ENTRY_ROW_COLUMNS)} FROM quaddicted_entries WHERE sha256 = ?", (sha256,)).fetchone()
    return _row_to_entry(row) if row else None


def entries_by_sha(conn: sqlite3.Connection, shas: list[str]) -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    clean = [s for s in shas if _SHA.match(s or "")]
    for i in range(0, len(clean), 400):
        chunk = clean[i:i + 400]
        marks = ", ".join("?" * len(chunk))
        for row in conn.execute(f"SELECT {', '.join(_ENTRY_ROW_COLUMNS)} FROM quaddicted_entries WHERE sha256 IN ({marks})", chunk).fetchall():
            entry = _row_to_entry(row)
            if entry:
                out[entry.sha256] = entry
    return out


def _row_to_entry(row: sqlite3.Row) -> Entry | None:
    values = {k: row[k] for k in _ENTRY_ROW_COLUMNS}
    def lst(key: str) -> list[str]:
        try:
            out = json.loads(values.get(key) or "[]")
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return Entry(
        sha256=values["sha256"], title=values.get("title"), filename=values.get("filename"), authors=lst("authors_json"),
        types=lst("types_json"), game_modes=lst("game_modes_json"), release_date=values.get("release_date"),
        description=values.get("description"), notes=values.get("notes") or "", bytes=values.get("bytes"),
        urls=lst("urls_json"), install_extract=values.get("install_extract"), json_timestamp=values.get("json_timestamp"),
        json_version=values.get("json_version"), depends=lst("depends_json"), provides=lst("provides_json"),
        mods=lst("mods_json"), dependency_files=lst("dependency_files_json"), exceeds_limits=bool(values.get("exceeds_limits")),
        nuked=bool(values.get("nuked")),
    )


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


SORTS = ("title", "release", "size")
SORT_SQL = {"title": "LOWER(COALESCE(title, filename, sha256)) COLLATE NOCASE ASC",
            "release": "release_date IS NULL ASC, release_date DESC, " + "LOWER(COALESCE(title, filename, sha256)) COLLATE NOCASE ASC",
            "size": "bytes IS NULL ASC, bytes DESC, LOWER(COALESCE(title, filename, sha256)) COLLATE NOCASE ASC"}
PAGE_SIZE = 25
MAX_PAGE = 400


def search(conn: sqlite3.Connection, query: str, kind: str | None, mode: str | None, sort: str, page: int,
           page_size: int = PAGE_SIZE) -> tuple[list[Entry], int]:
    """Bounded local search (LIKE on the cached copy). Returns (entries, total matching)."""
    where, params = [], []
    text = (query or "").strip()
    if text:
        # Search the display fields; each term must appear somewhere (AND).
        for term in text.split()[:8]:
            needle = f"%{_like_escape(term.lower())}%"
            where.append("(LOWER(COALESCE(title, '')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(filename, '')) LIKE ? ESCAPE '\\' "
                         "OR LOWER(authors_json) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(description, '')) LIKE ? ESCAPE '\\')")
            params += [needle, needle, needle, needle]
    if kind:
        where.append("types_json LIKE ? ESCAPE '\\'")
        params.append(f"%\"{_like_escape(kind.lower())}\"%")
    if mode:
        where.append("game_modes_json LIKE ? ESCAPE '\\'")
        params.append(f"%\"{_like_escape(mode.lower())}\"%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = SORT_SQL.get(sort, SORT_SQL["title"])
    total = int(conn.execute(f"SELECT COUNT(*) FROM quaddicted_entries {clause}", params).fetchone()[0])
    pages = max(1, -(-total // page_size))
    page = min(max(int(page), 1), min(pages, MAX_PAGE))
    rows = conn.execute(
        f"SELECT {', '.join(_ENTRY_ROW_COLUMNS)} FROM quaddicted_entries {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size)).fetchall()
    return [e for e in (_row_to_entry(r) for r in rows) if e], total


def distinct_values(conn: sqlite3.Connection, column: str) -> list[str]:
    """Values of a JSON list column (types / game_modes), for filter dropdowns."""
    counters: collections.Counter[str] = collections.Counter()
    allowed = {"types_json", "game_modes_json"}
    if column not in allowed:
        return []
    for (blob,) in conn.execute(f"SELECT {column} FROM quaddicted_entries"):
        try:
            values = json.loads(blob or "[]")
        except ValueError:
            values = []
        counters.update(v.lower() for v in values if isinstance(v, str))
    return sorted(counters)


def imported_status(conn: sqlite3.Connection, shas: list[str]) -> dict[str, dict[str, Any]]:
    """Which of these entries already exist locally: a completed draft (by source hash) or an active batch item."""
    from tools import batchstore as store  # noqa: PLC0415  (import inside to keep this module import-light)
    out: dict[str, dict[str, Any]] = {}
    clean = [s for s in shas if _SHA.match(s or "")]
    for i in range(0, len(clean), 400):
        chunk = clean[i:i + 400]
        marks = ", ".join("?" * len(chunk))
        for row in conn.execute(f"SELECT source_sha256, id, addon_id, published FROM addons WHERE source_sha256 IN ({marks})", chunk).fetchall():
            out[row["source_sha256"]] = {"state": "published" if row["published"] else "draft", "addon_id": row["addon_id"], "addon_pk": row["id"]}
        keys = [hashlib.sha256(f"quaddicted\0{s}".encode()).hexdigest() for s in chunk]
        kmarks = ", ".join("?" * len(keys))
        for row in conn.execute(f"SELECT source_key, state FROM batch_items WHERE source_key IN ({kmarks}) AND state NOT IN ('failed','cancelled','interrupted')", keys).fetchall():
            sha = _sha_from_key(row["source_key"])
            if sha and sha not in out:
                out[sha] = {"state": "queued" if row["state"] != "skipped" else "skipped", "addon_id": None, "addon_pk": None}
    return out


def _sha_from_key(source_key: str) -> str | None:
    prefix = "quaddicted\0"
    return source_key.split("\0", 1)[1] if source_key.startswith(prefix) else None


def source_key(sha256: str) -> str:
    return hashlib.sha256(f"quaddicted\0{sha256}".encode()).hexdigest()


# ---- synchronisation ---------------------------------------------------------------------------------------------------

def fetch_fetcher(limits: netfetch.FetchLimits) -> Callable[[str, int], bytes]:
    """The real network boundary used by sync jobs (overridable in tests): a bounded JSON GET."""
    def fetch(url: str, max_bytes: int) -> bytes:
        result = netfetch.fetch_to_memory(url, limits, accepted_types=("application/json",), max_bytes=max_bytes)
        return result.body
    return fetch


def fetch_listing(fetch: Callable[[str, int], bytes], progress) -> dict[str, str]:
    """Page through the light listing (sha256 + json_timestamp only) to learn the upstream catalogue without full documents."""
    known: dict[str, str] = {}
    for page in range(SYNC_MAX_PAGES):
        progress("Listing upstream entries", page, SYNC_MAX_PAGES)
        try:
            data = json.loads(fetch(listing_url(page * SYNC_ROWS), LISTING_MAX_BYTES).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpstreamProblem(f"The upstream answer is not valid JSON ({exc.__class__.__name__}).") from None
        if not isinstance(data, list):
            raise UpstreamProblem("The upstream listing has an unexpected shape (not a list). The API may have changed; integration paused until it is re-checked.")
        for doc in data:
            sha = doc.get("sha256") if isinstance(doc, dict) else None
            stamp = doc.get("json_timestamp") if isinstance(doc, dict) else None
            if isinstance(sha, str) and _SHA.match(sha) and isinstance(stamp, str):
                if sha in known:
                    raise UpstreamProblem("The upstream listing returned an entry twice; refusing to build an index from it.")
                known[sha] = stamp
        if len(data) < SYNC_ROWS:
            break
    else:
        raise UpstreamProblem(f"The upstream catalogue is larger than the supported {SYNC_MAX_PAGES * SYNC_ROWS} entries.")
    return known


def sync_index(txn: Callable[[str, int], Any], fetch: Callable[[str, int], bytes], progress, *, full: bool = False) -> dict[str, int]:
    """Bring the local index in line with upstream. Bounded: a small listing first; full documents only for entries that
    changed (chunked OR queries), or a bounded paged refetch when more changed than the chunk budget covers.
    `txn` is a factory yielding a connection inside a transaction, used only for short local passes; the network calls run
    without holding the write lock so progress reporting and other jobs never block on a half-done sync."""
    now = time.time()
    listing = fetch_listing(fetch, progress)
    with txn() as conn:
        local = {row[0]: row[1] for row in conn.execute("SELECT sha256, json_timestamp FROM quaddicted_entries").fetchall()}
    changed = [sha for sha, stamp in listing.items() if full or local.get(sha) != stamp]
    stats = {"known": len(listing), "changed": len(changed), "removed": 0, "mode": "delta"}
    if changed and len(changed) <= CHUNK_MAX_QUERIES * CHUNK_SIZE:
        fetched = 0
        collected: list[Entry] = []
        for i in range(0, len(changed), CHUNK_SIZE):
            chunk = changed[i:i + CHUNK_SIZE]
            progress("Fetching changed entries", i // CHUNK_SIZE, -(-len(changed) // CHUNK_SIZE))
            page_entries, _served = docs_from_json(fetch(docs_url(chunk, 0), DOCS_MAX_BYTES))
            collected.extend(page_entries)
        fetched = len(collected)
        stats["changed_fetched"] = fetched
        fetched_shas = {e.sha256 for e in collected}
        missing = [sha for sha in changed if sha not in fetched_shas]
        if missing:
            # Upstream answered the listing but not the documents; refuse to prune what we could not refresh.
            stats["missing"] = len(missing)
            raise UpstreamProblem(f"{len(missing)} changed entries could not be fetched from upstream; the local index was left as it is.")
        with txn() as conn:
            for entry in collected:
                upsert_entry(conn, entry, now)
            stats["removed"] = prune(conn, set(listing), now)
    elif changed:
        stats["mode"] = "full"
        seen: set[str] = set()
        collected: list[Entry] = []
        for page in range(SYNC_MAX_PAGES):
            progress("Fetching upstream catalogue", page, SYNC_MAX_PAGES)
            page_entries, served = docs_from_json(fetch(full_page_url(page * SYNC_ROWS), DOCS_MAX_BYTES))
            collected.extend(page_entries)
            seen.update(e.sha256 for e in page_entries)
            if served < SYNC_ROWS:
                break
        with txn() as conn:
            for entry in collected:
                upsert_entry(conn, entry, now)
            stats["removed"] = prune(conn, seen, now)
    else:
        with txn() as conn:
            stats["removed"] = prune(conn, set(listing), now)
    with txn() as conn:
        _state_set(conn, "synced_at", repr(now))
    return stats


def docs_from_json(payload: bytes) -> tuple[list[Entry], int]:
    """One paged full-doc response -> (usable entries, documents served). A wrong shape stops the sync (the index stays untouched)."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpstreamProblem(f"The upstream answer is not valid JSON ({exc.__class__.__name__}).") from None
    if not isinstance(data, list):
        raise UpstreamProblem("The upstream catalogue has an unexpected shape (not a list). The local index was left as it is.")
    return [e for e in (parse_doc(raw) for raw in data) if e], len(data)


# ---- the web service ----------------------------------------------------------------------------------------------------

class QuaddictedService:
    """Admin-only browsing/search/import over the locally cached Quaddicted index. Importing hands resolved package URLs to
    the existing BatchService, which runs the same acquisition, dedupe, conversion and draft creation as every other source."""

    def __init__(self, app, core, batch):
        self.app = app
        self.core = core                     # get_db / require_auth / jobs / load_import / db_txn
        self.batch = batch
        self.sync_fetch: Callable[[str, int], bytes] | None = None   # test hook; production always uses fetch_fetcher
        self.batch = batch

    def cfg(self, name: str, default: Any) -> Any:
        value = self.app.config.get(name)
        return default if value in (None, "") else value

    def limits(self) -> netfetch.FetchLimits:
        """Conservative metadata-fetch limits: small time and size budget, at most two attempts."""
        return netfetch.FetchLimits(max_bytes=DOCS_MAX_BYTES, connect_timeout=float(self.cfg("QUADDICTED_CONNECT_TIMEOUT", 10)),
                                    read_timeout=float(self.cfg("QUADDICTED_READ_TIMEOUT", 60)), max_duration=float(self.cfg("QUADDICTED_MAX_DURATION", 600)),
                                    max_redirects=5, max_attempts=2, backoff_base=2.0, retry_after_max=120.0)

    def _entries(self) -> list[Entry]:
        with self.db() as c:
            return entries_by_sha(c, self.request_shas())

    def request_shas(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in request.form.getlist("sha"):
            sha = str(raw).strip().lower()
            if _SHA.match(sha) and sha not in seen:
                seen.add(sha)
                out.append(sha)
        return out

    def sync_job(self, progress) -> JobOutcome:
        fetch = self.sync_fetch or fetch_fetcher(self.limits())
        stats = sync_index(self.core.db_txn, fetch, progress, full=False)
        with self.db() as c:
            total = count(c)
        if stats.get("missing"):
            return JobOutcome(f"Refresh incomplete: {stats['missing']} changed entr{'y' if stats['missing'] == 1 else 'ies'} could not be fetched. "
                              f"The cached index ({total} entries) was left as it was.", "warning")
        changed = stats.get("changed", 0)
        return JobOutcome(f"Quaddicted index refreshed: {stats['known']} entries upstream, {changed} changed"
                          f" ({stats['mode']} fetch), {total} in the local index.", "success" if changed else "info")

    @contextmanager
    def db(self):
        conn = self.core.get_db()
        try:
            yield conn
        finally:
            conn.close()

    def log(self, message: str) -> None:
        print(f"[quaddicted] {message}", flush=True)

    def context(self, **extra: Any) -> dict[str, Any]:
        with self.db() as c:
            total = count(c)
            synced_at = last_synced_at(c)
            types = distinct_values(c, "types_json")
            modes = distinct_values(c, "game_modes_json")
        return {"total": total, "synced_at": synced_at, "types": types, "modes": modes, "page_size": PAGE_SIZE,
                "min_sync_seconds": int(SYNC_MIN_SECONDS), "api_reference": API_BASE, **extra}

    def sync_started_recently(self, conn) -> float:
        """Seconds since the sync state marker was written (used to enforce the conservative pace)."""
        value = _state_get(conn, "sync_started_at")
        try:
            return time.time() - float(value) if value else 1e12
        except ValueError:
            return 1e12

    def import_prepared(self, shas: list[str]) -> dict[str, Any]:
        """Validate a selection against the local index and prepare batch items (URL resolution, per-item metadata).
        Returns {'prepared': [...], 'problems': [(sha, reason)]}."""
        prepared, problems = [], []
        with self.db() as c:
            known = entries_by_sha(c, shas)
        for sha in shas:
            entry = known.get(sha)
            if entry is None:
                problems.append((sha[:12], "not in the local index; refresh it if this entry is new"))
                continue
            url = pick_download_url(entry.urls)
            if url is None:
                problems.append((sha[:12], "the upstream entry lists no usable direct download link"))
                continue
            prepared.append({"sha256": sha, "url": url, "display": entry.filename or sha[:12], "source_json": entry.source_json()})
        return {"prepared": prepared, "problems": problems}

    def register_routes(self) -> None:
        app, core, batch = self.app, self.core, self.batch

        @app.route("/quaddicted")
        def quaddicted_home() -> Any:
            core.require_auth()
            query = request.args.get("q", "")[:200]
            kind = request.args.get("type", "")[:40]
            mode = request.args.get("mode", "")[:40]
            sort = request.args.get("sort", "title")[:20]
            page = parse_page(request.args.get("page"))
            with self.db() as c:
                found, total = search(c, query, kind or None, mode or None, sort, page)
                known = imported_status(c, [e.sha256 for e in found])
            pages = max(1, -(-total // PAGE_SIZE))
            with self.db() as c:
                synced_at = last_synced_at(c)
            age = human_age(synced_at) if synced_at else None
            ctx = self.context(entries=[e.view() for e in found], known=known, matching=total,
                               q=query, kind=kind, mode=mode, sort=sort if sort in SORTS else "title",
                               page=page, pages=pages, age=age, submit_key=uuid.uuid4().hex,
                               max_items=int(self.cfg("BATCH_MAX_ITEMS", 50)))
            return render_template("quaddicted_home.html", **ctx)

        @app.route("/quaddicted/entry/<sha>")
        def quaddicted_entry(sha: str) -> Any:
            core.require_auth()
            with self.db() as c:
                entry = get_entry(c, sha)
                known = imported_status(c, [sha]) if entry else {}
            if entry is None:
                return render_template("quaddicted_entry.html", entry=None, known={}), 404
            view = entry.view()
            view["status"] = known.get(sha)
            return render_template("quaddicted_entry.html", entry=view, submit_key=uuid.uuid4().hex,
                                   max_items=int(self.cfg("BATCH_MAX_ITEMS", 50)))

        @app.route("/quaddicted/import", methods=["POST"])
        def quaddicted_import() -> Any:
            core.require_auth()
            shas = self.request_shas()
            if not shas:
                flash("Select at least one Quaddicted entry to import.", "error")
                return redirect(url_for("quaddicted_home"))
            result = self.import_prepared(shas)
            if not result["prepared"]:
                flash("Nothing could be imported: " + "; ".join(f"{s}: {reason}" for s, reason in result["problems"]), "error")
                return redirect(url_for("quaddicted_home"))
            created = batch.create_quaddicted_batch(result["prepared"], request.form.get("submit_key") or None)
            if "batch_id" not in created:
                flash(created["error"], "error")
                return redirect(url_for("quaddicted_home"))
            if result["problems"]:
                flash("Some entries could not be imported: "
                      + "; ".join(f"{s}: {reason}" for s, reason in result["problems"]), "warning")
            if created.get("already_queued"):
                flash(f"{created['already_queued']} selected entr{'y was' if created['already_queued'] == 1 else 'ies were'} already queued in an earlier import and were not added again.", "success")
            return redirect(url_for("batch_page", batch_id=created["batch_id"]))

        @app.route("/quaddicted/sync", methods=["POST"])
        def quaddicted_sync() -> Any:
            core.require_auth()
            force = bool(request.form.get("force"))
            with self.db() as c:
                waited = self.sync_started_recently(c)
                if waited < SYNC_MIN_SECONDS and not force:
                    flash(f"Only a little while has passed since the last refresh; upstream access stays conservative "
                          f"(at most one refresh every {int(SYNC_MIN_SECONDS // 60)} minutes).", "info")
                    return redirect(url_for("quaddicted_home"))
                running = c.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'quaddicted-sync' AND state IN ('queued','running')").fetchone()[0]
            if running:
                flash("A refresh is already running.", "info")
                return redirect(url_for("quaddicted_home"))
            self.log("synchronisation requested")
            job_id = core.jobs.submit("quaddicted-sync", "Quaddicted index refresh", lambda p: self.sync_job(p), lane="net")
            with self.db() as c:
                _state_set(c, "sync_started_at", repr(time.time()))
            return redirect(url_for("job_page", job_id=job_id))


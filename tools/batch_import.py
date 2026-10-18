"""Batch import: many packages from pasted URLs or from a server folder, through the one shared conversion pipeline.

Item life cycle (see tools/batchstore.py for the states):

    submit -> [net lane] acquire: download (SSRF-safe) or copy from the import folder into staging, hash, check duplicates
           -> [work lane] the shared build code (app._build_import): inspect, convert, validate, install as a DRAFT
           -> completed | skipped | awaiting_review | needs_decision | failed

Nothing here converts, writes PAKs or publishes: it only acquires a package into the same staged-import layout the upload form uses,
then calls the shared build service. Publishing stays an explicit, separate action on each draft.

Retention of source URLs: the full URL is kept in `batch_items.source_url` (SQLite, under /appdata, never served, never logged) only
so an item can be retried or re-imported. It is cleared as soon as the item completes (a draft exists), and the whole item, URL included,
is deleted after BATCH_RETENTION_DAYS. Logs, pages and error messages only ever show the redacted form (host + file name).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from tools import batchstore as store
from tools import localscan, netfetch
from tools.jobs import JobCancelled, JobFailed, JobOutcome
from tools.quaddicted import source_key as source_key_of

STATE_LABELS = {
    "queued": "Queued", "acquiring": "Fetching", "converting": "Converting", "awaiting_review": "Needs review", "needs_decision": "Needs a decision",
    "completed": "Draft created", "skipped": "Skipped", "failed": "Failed", "cancelled": "Cancelled", "interrupted": "Interrupted",
}
SCAN_MAX_AGE = 3600.0
PAGE_SIZE_DEFAULT = 100


def slugify(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_").lower()[:40].strip("-_")
    if not slug or not slug[0].isalnum():
        slug = "addon" + (f"-{slug}" if slug else "")
    return slug


def title_from(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return re.sub(r"\s+", " ", re.sub(r"[_]+", " ", stem)).strip()[:80] or "Imported add-on"


def quaddicted_source_meta(item) -> dict[str, Any]:
    """Metadata carried for Quaddicted items (entry identity, suggestions, provenance). Never trusted: templates escape it."""
    try:
        meta = json.loads(item["source_json"] or "{}")
        return meta if isinstance(meta, dict) else {}
    except ValueError:
        return {}


def form_from_item(item, final_name: str, addon_id: str) -> dict[str, str]:
    """The draft's starting metadata. Upstream (Quaddicted) metadata is only a suggestion: it lands in editable form fields
    and never overwrites a record the administrator has already edited (replacement flows reuse the existing values)."""
    meta = quaddicted_source_meta(item)
    base = {"title": title_from(final_name), "author": "Unknown", "date": "", "description": ""}
    if meta:
        if meta.get("title"):
            base["title"] = str(meta["title"])[:200]
        if meta.get("author"):
            base["author"] = str(meta["author"])[:200]
        if meta.get("date"):
            base["date"] = str(meta["date"])[:40]
        if meta.get("description"):
            base["description"] = str(meta["description"])[:4000]
    return {"title": base["title"], "addon_id": addon_id, "gamedir": addon_id, "author": base["author"], "date": base["date"],
            "description": base["description"]}


class BatchService:
    def __init__(self, app, core):
        self.app = app
        self.core = core
        self.transport: dict[str, Any] = {}      # tests only (resolver / connector / ssl_context / sleep); nothing sets this in production

    # ---- configuration -----------------------------------------------------------------------------------------------
    def cfg(self, name: str, default: Any) -> Any:
        value = self.app.config.get(name)
        return default if value in (None, "") else value

    def roots(self) -> list[localscan.ImportRoot]:
        return localscan.parse_roots(self.cfg("IMPORT_ROOTS", ""))

    def limits(self, max_bytes: int) -> netfetch.FetchLimits:
        c = self.cfg
        return netfetch.FetchLimits(max_bytes=max_bytes, connect_timeout=float(c("BATCH_CONNECT_TIMEOUT", 10)), read_timeout=float(c("BATCH_READ_TIMEOUT", 30)),
                                    max_duration=float(c("BATCH_MAX_DURATION", 3600)), max_redirects=int(c("BATCH_MAX_REDIRECTS", 5)),
                                    max_attempts=int(c("BATCH_MAX_ATTEMPTS", 3)), backoff_base=float(c("BATCH_RETRY_BASE", 2)),
                                    retry_after_max=float(c("BATCH_RETRY_AFTER_MAX", 120)), max_url_length=int(c("BATCH_MAX_URL_LENGTH", 2048)))

    @contextmanager
    def db(self) -> Iterator[Any]:
        conn = self.core.get_db()
        try:
            yield conn
        finally:
            conn.close()

    def log(self, message: str) -> None:
        print(f"[batch] {message}", flush=True)

    # ---- creating batches --------------------------------------------------------------------------------------------
    def _room_for(self, count: int) -> str | None:
        with self.db() as c:
            pending = store.pending_total(c)
        limit = int(self.cfg("BATCH_MAX_PENDING", 200))
        if pending + count > limit:
            return f"The import queue is full ({pending} waiting, limit {limit}). Wait for some to finish or cancel some."
        return None

    def create_quaddicted_batch(self, prepared: list[dict[str, str]], submit_key: str | None) -> dict[str, Any]:
        """prepared items come from the Quaddicted service: one selected entry each, with the resolved canonical download URL.
        Duplicate detection (same entry already queued, same bytes already imported) works through source_key and
        source_sha256 like every other source."""
        if not prepared:
            return {"error": "Nothing to import."}
        max_items = int(self.cfg("BATCH_MAX_ITEMS", 50))
        if len(prepared) > max_items:
            return {"error": f"{len(prepared)} entries selected; at most {max_items} can be imported at once."}
        full = self._room_for(len(prepared))
        if full:
            return {"error": full}
        with self.db() as c:
            if submit_key and (existing := store.batch_by_key(c, submit_key)):
                return {"batch_id": existing["id"], "repeat": True}
            c.execute("BEGIN IMMEDIATE")
            try:
                batch_id = store.create_batch(c, "quaddicted", f"{len(prepared)} Quaddicted package(s)", submit_key)
                item_ids, already = [], 0
                for pos, p in enumerate(prepared):
                    key = source_key_of(p["sha256"])
                    if store.active_for_source(c, key):
                        already += 1
                        continue
                    item_ids.append(store.add_item(c, batch_id, pos, "quaddicted", key, p["display"], source_url=p["url"],
                                                   source_json=p["source_json"]))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        for item_id in item_ids:
            self._submit_acquire(item_id)
        return {"batch_id": batch_id, "already_queued": already}

    def create_url_batch(self, text: str, submit_key: str | None) -> dict[str, Any]:
        """Returns {'batch_id'} on success or {'error', 'problems', 'duplicates'}."""
        max_items = int(self.cfg("BATCH_MAX_ITEMS", 50))
        accepted, problems, dups = netfetch.parse_url_list(text, max_items, int(self.cfg("BATCH_MAX_URL_LENGTH", 2048)))
        if problems:
            return {"error": "Some lines are not usable links; nothing was queued.", "problems": problems, "duplicates": dups}
        if not accepted:
            return {"error": "Paste at least one link (one per line).", "problems": [], "duplicates": []}
        full = self._room_for(len(accepted))
        if full:
            return {"error": full, "problems": [], "duplicates": []}
        with self.db() as c:
            if submit_key and (existing := store.batch_by_key(c, submit_key)):
                return {"batch_id": existing["id"], "duplicates": dups, "repeat": True}
            c.execute("BEGIN IMMEDIATE")
            try:
                batch_id = store.create_batch(c, "url", f"{len(accepted)} remote link(s)", submit_key)
                item_ids = []
                for pos, (_, parsed) in enumerate(accepted):
                    item_ids.append(store.add_item(c, batch_id, pos, "url", netfetch.url_key(parsed.url), netfetch.redact_url(parsed.url),
                                                   source_url=parsed.url, host=parsed.host))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        for item_id in item_ids:
            self._submit_acquire(item_id)
        return {"batch_id": batch_id, "duplicates": dups}

    def create_folder_batch(self, scan_id: str, selected: list[str], select_all: bool, submit_key: str | None) -> dict[str, Any]:
        with self.db() as c:
            scan = store.get_scan(c, scan_id)
        if not scan:
            return {"error": "That folder scan has expired. Scan the folder again."}
        roots = self.roots()
        if not (0 <= scan["root_index"] < len(roots)):
            return {"error": "That import folder is no longer configured."}
        root = roots[scan["root_index"]]
        by_rel = {e["rel"]: e for e in scan["entries"]}
        if select_all:
            chosen = [e for e in scan["entries"] if e["eligible"]]
        else:
            chosen, seen = [], set()
            for rel in selected:
                entry = by_rel.get(rel)
                if entry is None:
                    return {"error": "A selected file was not part of the scan. Scan the folder again."}
                if not entry["eligible"]:
                    return {"error": f"{rel} cannot be imported: {entry['reason']}."}
                if rel not in seen:
                    seen.add(rel)
                    chosen.append(entry)
        if not chosen:
            return {"error": "Select at least one file."}
        max_items = int(self.cfg("BATCH_MAX_ITEMS", 50))
        if len(chosen) > max_items:
            return {"error": f"{len(chosen)} files selected; at most {max_items} can be imported at once. Select fewer, or raise BATCH_MAX_ITEMS."}
        full = self._room_for(len(chosen))
        if full:
            return {"error": full}
        with self.db() as c:
            if submit_key and (existing := store.batch_by_key(c, submit_key)):
                return {"batch_id": existing["id"], "repeat": True}
            c.execute("BEGIN IMMEDIATE")
            try:
                batch_id = store.create_batch(c, "folder", f"{len(chosen)} file(s) from {root.label}", submit_key)
                item_ids, already = [], 0
                for pos, e in enumerate(chosen):
                    key = hashlib.sha256(f"folder\0{root.path}\0{e['rel']}".encode()).hexdigest()
                    if store.active_for_source(c, key):
                        already += 1
                        continue
                    item_ids.append(store.add_item(c, batch_id, pos, "folder", key, f"{root.label}/{e['rel']}", folder_root=root.index, folder_rel=e["rel"],
                                                   src_size=e["size"], src_mtime_ns=e["mtime_ns"], src_ino=e["ino"], src_dev=e["dev"]))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        for item_id in item_ids:
            self._submit_acquire(item_id)
        return {"batch_id": batch_id, "already_queued": already}

    # ---- job plumbing ------------------------------------------------------------------------------------------------
    def _submit_acquire(self, item_id: int) -> None:
        job_id = self.core.jobs.submit("batch-fetch", f"Batch item {item_id}", lambda p: self._acquire(item_id, p), lane="net",
                                       finalizer=lambda state, message, level: self._on_final(item_id, state, message))
        with self.db() as c:
            c.execute("UPDATE batch_items SET job_id = ? WHERE id = ? AND job_id IS NULL", (job_id, item_id))

    def _submit_build(self, item_id: int) -> None:
        job_id = self.core.jobs.submit("batch-build", f"Batch item {item_id}", lambda p: self._build(item_id, p), lane="work",
                                       finalizer=lambda state, message, level: self._on_final(item_id, state, message))
        with self.db() as c:
            c.execute("UPDATE batch_items SET job_id = ? WHERE id = ? AND state = 'converting'", (job_id, item_id))

    def _scrub(self, item: Any, text: str) -> str:
        return netfetch.redact_text(text, item["source_url"]) if item["source_url"] else text

    def _on_final(self, item_id: int, state: str, message: str) -> None:
        """Called when a job ends. Only items still in an active state are touched; a state the job itself set (completed, skipped,
        awaiting review, needs a decision) is never overwritten."""
        with self.db() as c:
            item = store.get_item(c, item_id)
            if item is None or item["state"] not in store.ACTIVE:
                return
            if state == "failed":
                store.transition(c, item_id, store.ACTIVE, "failed", message=self._scrub(item, message)[:2000], staged_bytes=0)
            elif state == "cancelled":
                store.transition(c, item_id, store.ACTIVE, "cancelled", message="Cancelled.", staged_bytes=0)
            else:
                return
        token = item["import_token"]
        if token:
            self.core.delete_import(token)

    # ---- acquisition ---------------------------------------------------------------------------------------------------
    def _acquire(self, item_id: int, progress) -> JobOutcome:
        with self.db() as c:
            started = store.transition(c, item_id, ("queued",), "acquiring", job_id=progress.job_id, message="")
            if started:
                c.execute("UPDATE batch_items SET attempts = attempts + 1 WHERE id = ?", (item_id,))
            item = store.get_item(c, item_id)
        if not started:
            return JobOutcome("Not started: the item was cancelled or is already being handled.", "info")
        token = uuid.uuid4().hex
        staging = Path(self.app.config["UPLOAD_FOLDER"]) / token
        handed_off = False
        try:
            staging.mkdir(parents=True, exist_ok=True)
            max_bytes, space_bound = self._download_budget(staging)
            dest = staging / "source.download"
            if item["source_type"] == "url":
                limits = self.limits(max_bytes)
                try:
                    res = netfetch.fetch_to_file(item["source_url"], dest, limits, progress, **self.transport)
                except netfetch.FetchError as exc:
                    if exc.kind in ("too-large", "disk") and space_bound:
                        raise JobFailed(self.STAGING_FULL) from None
                    suffix = f" (after {exc.attempts} attempts)" if exc.attempts > 1 else ""
                    raise JobFailed(self._scrub(item, f"{exc}{suffix}")) from None
                sha, final_name, host, size = res.sha256, res.filename, res.host, res.size
                self.log(f"item {item_id}: downloaded {size} bytes from {item['source_display']} in {res.attempts} attempt(s)")
            elif item["source_type"] == "quaddicted":
                limits = self.limits(max_bytes)
                try:
                    res = netfetch.fetch_to_file(item["source_url"], dest, limits, progress, **self.transport)
                except netfetch.FetchError as exc:
                    if exc.kind in ("too-large", "disk") and space_bound:
                        raise JobFailed(self.STAGING_FULL) from None
                    suffix = f" (after {exc.attempts} attempts)" if exc.attempts > 1 else ""
                    raise JobFailed(self._scrub(item, f"{exc}{suffix}")) from None
                sha, final_name, host, size = res.sha256, res.filename, res.host, res.size
                expected = quaddicted_source_meta(item).get("sha256", "").lower()
                if expected and sha != expected:
                    # The entry identity IS the content hash, so a mismatch means the link did not serve the package.
                    raise JobFailed(f"The download does not match the Quaddicted entry (its content hash is {sha[:12]}…, the entry says {expected[:12]}…). "
                                    "Refresh the index and try again; if it persists, the upstream entry may be broken.") from None
                self.log(f"item {item_id}: downloaded {size} bytes for Quaddicted entry {expected[:12]}… in {res.attempts} attempt(s)")
            else:
                roots = self.roots()
                if item["folder_root"] is None or not (0 <= item["folder_root"] < len(roots)):
                    raise JobFailed("That import folder is no longer configured.")
                expected = (item["src_size"], item["src_mtime_ns"], item["src_ino"], item["src_dev"])
                try:
                    sha, size = localscan.copy_candidate(roots[item["folder_root"]], item["folder_rel"], dest, expected, max_bytes, progress)
                except localscan.LocalImportError as exc:
                    if exc.kind in ("too-large", "disk") and space_bound:
                        raise JobFailed(self.STAGING_FULL) from None
                    raise JobFailed(str(exc)) from None
                final_name, host = netfetch.safe_display_filename(item["folder_rel"].rsplit("/", 1)[-1]), None
                self.log(f"item {item_id}: copied {size} bytes from {item['source_display']}")
            progress("Checking the package", 0, 0)
            fmt = self.core.detect_package_format(dest)
            if fmt not in ("zip", "pak"):
                raise JobFailed("The file is not a ZIP archive or a Quake PAK file.")
            source = staging / f"source.{fmt}"
            os.replace(dest, source)
            with self.db() as c:
                store.update_item(c, item_id, sha256=sha, final_name=final_name, host=host, staged_bytes=size, import_token=token)
                item = store.get_item(c, item_id)
            outcome = self._after_acquire(item, token, source, fmt, final_name)
            handed_off = outcome is not None
            return outcome or JobOutcome("Skipped.", "info")
        except (JobCancelled, JobFailed):
            raise
        except Exception as exc:
            # Never let an unexpected exception (whose text may embed the link) reach the generic handler, which logs the traceback.
            self.log(f"item {item_id}: unexpected {exc.__class__.__name__}: {self._scrub(item, str(exc))[:300]}")
            raise JobFailed(f"Unexpected error while fetching ({exc.__class__.__name__}). See the server log.") from None
        finally:
            if not handed_off:
                shutil.rmtree(staging, ignore_errors=True)

    STAGING_FULL = ("The temporary staging area is full or too small for this file (limit BATCH_STAGING_LIMIT_GIB, or the disk behind /uploads is "
                    "nearly full). Wait for other imports to finish or free space, then retry this one.")

    def _download_budget(self, staging: Path) -> tuple[int, bool]:
        """(bytes this item may use, True if the staging area or the disk, rather than the per-item limit, is what bounds it)."""
        limit = int(self.cfg("BATCH_STAGING_LIMIT_BYTES", 20 * 1024 ** 3))
        with self.db() as c:
            used = store.staged_total(c)
        free = shutil.disk_usage(staging).free - 64 * 1024 * 1024
        per_item = int(self.cfg("BATCH_MAX_DOWNLOAD_BYTES", self.app.config.get("MAX_CONTENT_LENGTH") or 2 * 1024 ** 3))
        room = min(per_item, limit - used, free)
        if room <= 0:
            raise JobFailed(self.STAGING_FULL)
        return int(room), room < per_item

    def _after_acquire(self, item, token: str, source: Path, fmt: str, final_name: str) -> JobOutcome | None:
        """Duplicate / conflict decisions, then hand over to the shared build code. Returns None only if the item was skipped (staging removed)."""
        item_id = item["id"]
        with self.db() as c:
            twin = self._addon_with_source_hash(c, item["sha256"])
            previous = store.last_completed_for_source(c, item["source_key"], item_id)
        if twin and not item["force"]:
            self._finish_skipped(item_id, token, f"Skipped: identical content was already imported as add-on '{twin['addon_id']}'.",
                                 {"kind": "identical-source", "addon_pk": twin["id"]})
            return None
        base = slugify(final_name)
        reason = None
        with self.db() as c:
            taken = c.execute("SELECT id, addon_id, gamedir FROM addons WHERE addon_id = ? OR gamedir = ? LIMIT 1", (base, base)).fetchone()
            prev_addon = c.execute("SELECT id, addon_id FROM addons WHERE id = ?", (previous["addon_pk"],)).fetchone() if previous else None
            if prev_addon and previous["sha256"] != item["sha256"]:
                reason, existing = "changed", prev_addon
            elif taken:
                reason, existing = "id-taken", taken
            new_id = self._free_id(c, base)
        form = form_from_item(item, final_name, base if not reason else new_id)
        if item["force"] and taken:
            form["addon_id"] = form["gamedir"] = new_id                                   # explicit "import anyway": use a free ID
            reason = None
        if reason and not item["force"]:
            self.core.save_import(token, "new", None, form, source, final_name, fmt, owns_source=True, keep_source=False, review=True, batch_item_id=item_id)
            action = {"kind": reason, "existing_addon_pk": existing["id"], "existing_id": existing["addon_id"], "suggested_id": new_id}
            with self.db() as c:
                store.transition(c, item_id, ("acquiring",), "needs_decision", action_json=json.dumps(action), derived_json=json.dumps(form),
                                 message=("The content at this source changed since it was imported as add-on '%s'." % existing["addon_id"]) if reason == "changed"
                                 else f"An add-on with the ID or directory '{base}' already exists.")
            return JobOutcome("Waiting for a decision.", "info")
        self.core.save_import(token, "new", None, form, source, final_name, fmt, owns_source=True, keep_source=False, review=False, batch_item_id=item_id)
        with self.db() as c:
            store.transition(c, item_id, ("acquiring",), "converting", derived_json=json.dumps(form))
        self._submit_build(item_id)
        return JobOutcome("Staged; conversion queued.", "info")

    def _addon_with_source_hash(self, conn, sha: str | None):
        if not sha:
            return None
        row = conn.execute("SELECT id, addon_id FROM addons WHERE source_sha256 = ? LIMIT 1", (sha,)).fetchone()
        if row:
            return row
        for r in conn.execute("SELECT id, addon_id, conversion_report FROM addons WHERE source_sha256 IS NULL AND conversion_report IS NOT NULL").fetchall():
            try:
                if json.loads(r["conversion_report"]).get("source_sha256") == sha:
                    return r
            except (ValueError, AttributeError):
                continue
        return None

    def _free_id(self, conn, base: str) -> str:
        for n in range(1, 1000):
            cand = base if n == 1 else f"{base}-{n}"
            if not conn.execute("SELECT 1 FROM addons WHERE addon_id = ? OR gamedir = ?", (cand, cand)).fetchone():
                return cand
        return f"{base}-{uuid.uuid4().hex[:6]}"

    def _finish_skipped(self, item_id: int, token: str | None, message: str, action: dict[str, Any]) -> None:
        if token:
            self.core.delete_import(token)
        with self.db() as c:
            store.transition(c, item_id, store.ACTIVE + store.WAITING, "skipped", message=message, action_json=json.dumps(action), staged_bytes=0,
                             import_token=None, addon_pk=action.get("addon_pk"))

    # ---- conversion (shared build code) --------------------------------------------------------------------------------
    def _build(self, item_id: int, progress) -> JobOutcome:
        with self.db() as c:
            item = store.get_item(c, item_id)
            if item is None or item["state"] != "converting" or not item["import_token"]:
                return JobOutcome("Not run: the item was cancelled or changed.", "info")
            c.execute("UPDATE batch_items SET job_id = ? WHERE id = ?", (progress.job_id, item_id))
        token = item["import_token"]
        provenance = {"type": item["source_type"], "file": item["final_name"], "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "batch_item_id": item_id}
        if item["source_type"] == "url":
            provenance["host"] = item["host"]
        elif item["source_type"] == "quaddicted":
            meta = quaddicted_source_meta(item)
            provenance.update({"source": "quaddicted", "quaddicted_sha256": item["sha256"], "source_page": meta.get("page_url") or "",
                               "upstream_updated": meta.get("upstream_updated") or ""})
        else:
            roots = self.roots()
            provenance["folder"] = roots[item["folder_root"]].label if item["folder_root"] is not None and item["folder_root"] < len(roots) else "?"
        policy = {"keep_on_review": True, "skip_identical_output": not item["force"], "provenance": provenance}
        try:
            result = self.core.build_import(token, progress, policy)
        except self.core.ReviewRequired as exc:
            try:
                self.core.plan_import(token)
            except Exception as plan_exc:                                           # the plan is a convenience; the review page can recompute it
                self.log(f"item {item_id}: plan not precomputed ({plan_exc.__class__.__name__})")
            with self.db() as c:
                store.transition(c, item_id, ("converting",), "awaiting_review", message=str(exc)[:1000],
                                 action_json=json.dumps({"kind": "layout", "import_token": token}))
            return JobOutcome("Needs review.", "info")
        except self.core.ImportConflict:
            with self.db() as c:
                base = slugify(item["final_name"] or "addon")
                existing = c.execute("SELECT id, addon_id FROM addons WHERE addon_id = ? OR gamedir = ? LIMIT 1", (base, base)).fetchone()
                new_id = self._free_id(c, base)
                action = {"kind": "id-taken", "existing_addon_pk": existing["id"] if existing else None,
                          "existing_id": existing["addon_id"] if existing else base, "suggested_id": new_id}
                store.transition(c, item_id, ("converting",), "needs_decision", action_json=json.dumps(action),
                                 message="An add-on with this ID or directory appeared while the package was being converted.")
            return JobOutcome("Waiting for a decision.", "info")
        with self.db() as c:
            if result.skipped:
                store.transition(c, item_id, ("converting",), "skipped", message=result.text, addon_pk=result.skipped_addon_pk, staged_bytes=0, import_token=None,
                                 action_json=json.dumps({"kind": "identical-output", "addon_pk": result.skipped_addon_pk}))
            else:
                store.transition(c, item_id, ("converting",), "completed", message=result.text, addon_pk=result.addon_pk, staged_bytes=0, import_token=None, source_url=None)
        return JobOutcome(result.text, result.level)

    # ---- actions on items ------------------------------------------------------------------------------------------------
    def reconcile(self, batch_id: int | None = None) -> None:
        """Bring item rows in line with what really exists: a draft linked to the item means it completed (even if the process died
        right after installing); a discarded review means cancelled. This is what makes retries safe from duplicate drafts."""
        with self.db() as c:
            q = "SELECT * FROM batch_items WHERE state IN ('queued','acquiring','converting','awaiting_review','needs_decision','failed','interrupted','cancelled')"
            rows = c.execute(q + (" AND batch_id = ?" if batch_id else ""), (batch_id,) if batch_id else ()).fetchall()
            for item in rows:
                addon = c.execute("SELECT id FROM addons WHERE batch_item_id = ?", (item["id"],)).fetchone()
                if addon:
                    store.transition(c, item["id"], tuple(r for r in store.ACTIVE + store.WAITING + store.RETRYABLE), "completed", addon_pk=addon["id"],
                                     message="Draft created.", staged_bytes=0, import_token=None, source_url=None)
                    continue
                if item["state"] in store.WAITING and (not item["import_token"] or self.core.load_import(item["import_token"]) is None):
                    store.transition(c, item["id"], store.WAITING, "cancelled", message="The review was discarded.", staged_bytes=0, import_token=None)

    def recover(self) -> int:
        """At startup nothing is running: items that were in flight need an administrator's Retry (nothing is retried automatically)."""
        count = 0
        with self.db() as c:
            for item in c.execute("SELECT * FROM batch_items WHERE state IN ('queued','acquiring','converting')").fetchall():
                if c.execute("SELECT 1 FROM addons WHERE batch_item_id = ?", (item["id"],)).fetchone():
                    continue
                if store.transition(c, item["id"], store.ACTIVE, "interrupted", message="Interrupted by a restart of the application. Retry to run it again.",
                                    staged_bytes=0, import_token=None):
                    count += 1
            store.expire(c, SCAN_MAX_AGE, float(self.cfg("BATCH_RETENTION_DAYS", 14)) * 86400)
        self.reconcile()
        return count

    def retry(self, item_id: int) -> tuple[bool, str]:
        self.reconcile()
        with self.db() as c:
            item = store.get_item(c, item_id)
            if item is None:
                return False, "Unknown item."
            if item["state"] not in store.RETRYABLE:
                return False, f"This item is {STATE_LABELS.get(item['state'], item['state']).lower()}; only failed, cancelled or interrupted items can be retried."
            if item["source_type"] in ("url", "quaddicted") and not item["source_url"]:
                return False, "The link was not kept. Submit it again."
            if not store.transition(c, item_id, store.RETRYABLE, "queued", message="", job_id=None, import_token=None, action_json=None):
                return False, "Another request already retried this item."
        self._submit_acquire(item_id)
        return True, "Retry queued."

    def cancel(self, item_id: int) -> tuple[bool, str]:
        with self.db() as c:
            item = store.get_item(c, item_id)
            if item is None:
                return False, "Unknown item."
            state = item["state"]
        if state == "queued":
            with self.db() as c:
                ok = store.transition(c, item_id, ("queued",), "cancelled", message="Cancelled before it started.")
            return ok, "Cancelled." if ok else "It had already started."
        if state in ("acquiring", "converting"):
            if item["job_id"] and self.core.jobs.request_cancel(item["job_id"]):
                return True, "Cancelling…"
            with self.db() as c:
                ok = store.transition(c, item_id, ("acquiring", "converting"), "cancelled", message="Cancelled.", staged_bytes=0)
            if ok and item["import_token"]:
                self.core.delete_import(item["import_token"])
            return ok, "Cancelled."
        if state in store.WAITING:
            if item["import_token"]:
                self.core.delete_import(item["import_token"])
            with self.db() as c:
                store.transition(c, item_id, store.WAITING, "cancelled", message="Discarded by the administrator.", staged_bytes=0, import_token=None)
            return True, "Discarded."
        return False, "Nothing to cancel."

    def resolve(self, item_id: int, choice: str) -> tuple[bool, str]:
        with self.db() as c:
            item = store.get_item(c, item_id)
        if item is None:
            return False, "Unknown item."
        action = json.loads(item["action_json"] or "{}")
        state, token = item["state"], item["import_token"]
        if state == "needs_decision":
            if not token or self.core.load_import(token) is None:
                return False, "The staged upload is gone; retry the item."
            if choice == "skip":
                self.core.delete_import(token)
                with self.db() as c:
                    store.transition(c, item_id, ("needs_decision",), "skipped", message="Skipped by the administrator.", staged_bytes=0, import_token=None)
                return True, "Skipped."
            if choice == "new_id":
                with self.db() as c:
                    new_id = self._free_id(c, action.get("suggested_id") or slugify(item["final_name"] or "addon"))
                imp = self.core.load_import(token)
                form = json.loads(imp["form_json"])
                form["addon_id"] = form["gamedir"] = new_id
                self.core.update_import(token, form_json=json.dumps(form), review=0)
                with self.db() as c:
                    ok = store.transition(c, item_id, ("needs_decision",), "converting", message=f"Creating a separate draft with the ID '{new_id}'.")
                if ok:
                    self._submit_build(item_id)
                return ok, f"Creating a separate draft as '{new_id}'."
            if choice == "replace" and action.get("existing_addon_pk"):
                with self.db() as c:
                    addon = c.execute("SELECT * FROM addons WHERE id = ?", (action["existing_addon_pk"],)).fetchone()
                if not addon:
                    return False, "The add-on to replace no longer exists."
                form = {k: (addon[k] or "") for k in ("title", "addon_id", "gamedir", "author", "date", "description")}
                self.core.update_import(token, kind="replace", addon_pk=addon["id"], form_json=json.dumps(form), review=1)
                try:
                    self.core.plan_import(token)
                except Exception:
                    pass
                with self.db() as c:
                    store.transition(c, item_id, ("needs_decision",), "awaiting_review", message=f"Review the replacement of add-on '{addon['addon_id']}'. The published copy keeps being served until you publish again.",
                                     action_json=json.dumps({"kind": "replacement", "import_token": token}))
                return True, "Open the review page to replace the existing add-on."
            return False, "That choice is not available for this item."
        if state == "skipped" and choice == "import_anyway" and action.get("kind") in ("identical-source", "identical-output"):
            with self.db() as c:
                ok = store.transition(c, item_id, ("skipped",), "queued", force=1, message="", job_id=None, action_json=None, attempts=item["attempts"])
            if ok:
                if item["source_type"] == "url" and not item["source_url"]:
                    with self.db() as c:
                        store.transition(c, item_id, ("queued",), "failed", message="The link was not kept. Submit it again.")
                    return False, "The link was not kept. Submit it again."
                self._submit_acquire(item_id)
            return ok, "Importing again as a separate draft."
        return False, "That choice is not available for this item."

    def retry_all(self, batch_id: int) -> int:
        with self.db() as c:
            ids = [r["id"] for r in store.items_for_batch(c, batch_id) if r["state"] in store.RETRYABLE]
        return sum(1 for i in ids if self.retry(i)[0])

    def cancel_all(self, batch_id: int) -> int:
        with self.db() as c:
            ids = [r["id"] for r in store.items_for_batch(c, batch_id) if r["state"] in store.ACTIVE + store.WAITING]
        return sum(1 for i in ids if self.cancel(i)[0])

    # ---- presentation ----------------------------------------------------------------------------------------------------
    def item_view(self, item, jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        job = jobs.get(item["job_id"] or "")
        state = item["state"]
        view: dict[str, Any] = {
            "id": item["id"], "position": item["position"] + 1, "source_type": item["source_type"], "display": item["source_display"], "state": state,
            "label": STATE_LABELS.get(state, state), "message": item["message"], "attempts": item["attempts"], "final_name": item["final_name"],
            "sha256": (item["sha256"] or "")[:12], "addon_url": url_for("addon_detail", addon_id=item["addon_pk"]) if item["addon_pk"] else None,
            "review_url": url_for("import_plan", token=item["import_token"]) if state in ("awaiting_review",) and item["import_token"] else None,
            "stage": "", "done": 0, "total": 0, "percent": None, "choices": [], "can_retry": state in store.RETRYABLE,
            "can_cancel": state in store.ACTIVE + store.WAITING,
        }
        if job and state in store.ACTIVE:
            view["stage"] = job.get("stage") or ("Waiting to start…" if job["state"] == "queued" else "")
            view["done"], view["total"] = job.get("done") or 0, job.get("total") or 0
            if view["total"] > 0:                                               # a percentage only where there is a meaningful total
                view["percent"] = round(100 * min(1, view["done"] / view["total"]))
        elif state == "queued":
            view["stage"] = "Waiting to start…"
        action = json.loads(item["action_json"] or "{}")
        if state == "needs_decision":
            view["choices"] = [("new_id", f"Create a separate draft as '{action.get('suggested_id', '…')}'")]
            if action.get("existing_addon_pk"):
                view["choices"].append(("replace", f"Review as a replacement for '{action.get('existing_id')}'"))
            view["choices"].append(("skip", "Skip this one"))
        elif state == "skipped" and action.get("kind") in ("identical-source", "identical-output"):
            view["choices"] = [("import_anyway", "Import anyway as a separate draft")]
        return view

    def batch_view(self, batch_id: int) -> dict[str, Any] | None:
        self.reconcile(batch_id)
        with self.db() as c:
            batch = store.get_batch(c, batch_id)
            if batch is None:
                return None
            items = store.items_for_batch(c, batch_id)
            counts = store.counts_by_state(c, batch_id)
            job_ids = [i["job_id"] for i in items if i["job_id"]]
            jobs: dict[str, dict[str, Any]] = {}
            for i in range(0, len(job_ids), 500):
                chunk = job_ids[i:i + 500]
                for r in c.execute(f"SELECT id, state, stage, done, total FROM jobs WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
                    jobs[r["id"]] = dict(r)
        views = [self.item_view(i, jobs) for i in items]
        active = sum(counts.get(s, 0) for s in store.ACTIVE)
        total = len(items)
        return {"batch": dict(batch), "items": views, "counts": counts, "total": total, "active": active,
                "waiting": sum(counts.get(s, 0) for s in store.WAITING), "finished": total - active - sum(counts.get(s, 0) for s in store.WAITING),
                "labels": STATE_LABELS}

    def scan_known_status(self, root: localscan.ImportRoot, entries: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        keys = {hashlib.sha256(f"folder\0{root.path}\0{e['rel']}".encode()).hexdigest(): e for e in entries}
        out: dict[str, dict[str, str]] = {}
        with self.db() as c:
            klist = list(keys)
            for i in range(0, len(klist), 400):
                chunk = klist[i:i + 400]
                for r in c.execute(f"SELECT * FROM batch_items WHERE source_key IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk).fetchall():
                    e = keys[r["source_key"]]
                    same = (r["src_size"], r["src_mtime_ns"]) == (e["size"], e["mtime_ns"])
                    if r["state"] in store.ACTIVE + store.WAITING:
                        out[e["rel"]] = {"kind": "active", "text": "Already queued or waiting for review"}
                    elif r["state"] == "completed":
                        out[e["rel"]] = {"kind": "done" if same else "changed", "text": "Imported before" if same else "Imported before; the file has changed since"}
                    elif r["state"] == "skipped":
                        out[e["rel"]] = {"kind": "done", "text": "Skipped before (identical content already imported)"}
                    elif r["state"] in store.RETRYABLE:
                        out[e["rel"]] = {"kind": "failed", "text": "A previous import attempt did not finish"}
        return out

    # ---- routes ---------------------------------------------------------------------------------------------------------------
    def register_routes(self) -> None:
        app, core = self.app, self.core

        def wants_json() -> bool:
            return request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def home_context(**extra: Any) -> dict[str, Any]:
            roots = self.roots()
            with self.db() as c:
                recent = store.recent_batches(c)
            return {"roots": [{"index": r.index, "label": r.label, "path": r.path, "problem": localscan.root_problem(r)} for r in roots],
                    "recent": recent, "max_items": int(self.cfg("BATCH_MAX_ITEMS", 50)), "max_url_length": int(self.cfg("BATCH_MAX_URL_LENGTH", 2048)),
                    "submit_key": uuid.uuid4().hex, "labels": STATE_LABELS, **extra}

        @app.route("/batches")
        def batch_home() -> Any:
            core.require_auth()
            return render_template("batch_home.html", **home_context())

        @app.route("/batches/urls", methods=["POST"])
        def batch_submit_urls() -> Any:
            core.require_auth()
            text = request.form.get("urls", "")
            if len(text) > 400_000:
                flash("That is too much text for one batch.", "error")
                return render_template("batch_home.html", **home_context(urls=text[:2000]))
            result = self.create_url_batch(text, request.form.get("submit_key") or None)
            if "batch_id" not in result:
                return render_template("batch_home.html", **home_context(urls=netfetch.mask_credentials(text), problems=result["problems"],
                                                                         duplicates=result.get("duplicates", []), error=result["error"])), 400
            if result.get("duplicates"):
                flash(f"{len(result['duplicates'])} repeated link(s) in the list were imported only once.", "success")
            return redirect(url_for("batch_page", batch_id=result["batch_id"]))

        @app.route("/batches/scan", methods=["POST"])
        def batch_scan() -> Any:
            core.require_auth()
            roots = self.roots()
            try:
                index = int(request.form.get("root", "-1"))
                root = roots[index]
            except (ValueError, IndexError):
                flash("Choose one of the configured import folders.", "error")
                return redirect(url_for("batch_home"))
            kinds = ("zip", "pak") if request.form.get("include_pak") else ("zip",)
            recursive = bool(request.form.get("recursive"))
            sub = request.form.get("subpath", "").strip().strip("/")
            problem = localscan.root_problem(root)
            if problem:
                flash(f"{root.label}: {problem}", "error")
                return redirect(url_for("batch_home"))
            try:
                found = localscan.scan_root(root, sub, recursive, kinds, max_depth=int(self.cfg("IMPORT_SCAN_MAX_DEPTH", 8)),
                                            max_entries=int(self.cfg("IMPORT_SCAN_MAX_ENTRIES", 20000)), max_results=int(self.cfg("IMPORT_SCAN_MAX_RESULTS", 2000)),
                                            max_file_bytes=int(self.cfg("BATCH_MAX_DOWNLOAD_BYTES", app.config.get("MAX_CONTENT_LENGTH") or 2 * 1024 ** 3)))
            except localscan.LocalImportError as exc:
                flash(f"Cannot scan that folder: {exc}", "error")
                return redirect(url_for("batch_home"))
            entries = [{"rel": c.rel, "size": c.size, "mtime_ns": c.mtime_ns, "ino": c.ino, "dev": c.dev, "kind": c.kind, "eligible": c.eligible, "reason": c.reason}
                       for c in found.candidates]
            summary = {"visited": found.visited, "truncated": found.truncated, "other_files": found.other_files, "skipped_links": found.skipped_links, "errors": found.errors[:20]}
            scan_id = uuid.uuid4().hex
            with self.db() as c:
                store.save_scan(c, scan_id, root.index, sub, recursive, kinds, entries, summary)
            return redirect(url_for("batch_scan_results", scan_id=scan_id))

        @app.route("/batches/scan/<scan_id>")
        def batch_scan_results(scan_id: str) -> Any:
            core.require_auth()
            with self.db() as c:
                scan = store.get_scan(c, scan_id)
            roots = self.roots()
            if not scan or not (0 <= scan["root_index"] < len(roots)):
                flash("That scan has expired. Scan the folder again.", "error")
                return redirect(url_for("batch_home"))
            root = roots[scan["root_index"]]
            page_size = int(self.cfg("IMPORT_PAGE_SIZE", PAGE_SIZE_DEFAULT))
            entries = scan["entries"]
            pages = max(1, -(-len(entries) // page_size))
            try:
                page = min(max(int(request.args.get("page", "1")), 1), pages)
            except ValueError:
                page = 1
            shown = entries[(page - 1) * page_size: page * page_size]
            known = self.scan_known_status(root, shown)
            eligible = [e for e in entries if e["eligible"]]
            return render_template("batch_scan.html", scan_id=scan_id, scan=scan, root=root, shown=shown, known=known, page=page, pages=pages,
                                   eligible_count=len(eligible), eligible_bytes=sum(e["size"] for e in eligible), total=len(entries), submit_key=uuid.uuid4().hex,
                                   max_items=int(self.cfg("BATCH_MAX_ITEMS", 50)))

        @app.route("/batches/folder", methods=["POST"])
        def batch_submit_folder() -> Any:
            core.require_auth()
            scan_id = request.form.get("scan_id", "")
            result = self.create_folder_batch(scan_id, request.form.getlist("selected"), request.form.get("mode") == "all", request.form.get("submit_key") or None)
            if "batch_id" not in result:
                flash(result["error"], "error")
                return redirect(url_for("batch_scan_results", scan_id=scan_id) if scan_id else url_for("batch_home"))
            if result.get("already_queued"):
                flash(f"{result['already_queued']} file(s) were already queued and were not added again.", "success")
            return redirect(url_for("batch_page", batch_id=result["batch_id"]))

        @app.route("/batches/<int:batch_id>")
        def batch_page(batch_id: int) -> Any:
            core.require_auth()
            view = self.batch_view(batch_id)
            if view is None:
                abort(404)
            if request.args.get("fragment"):
                return render_template("_batch_items.html", **view)
            return render_template("batch_view.html", **view)

        @app.route("/batches/<int:batch_id>/status")
        def batch_status(batch_id: int) -> Any:
            core.require_auth()
            view = self.batch_view(batch_id)
            if view is None:
                abort(404)
            response = jsonify({"counts": view["counts"], "active": view["active"], "waiting": view["waiting"], "total": view["total"], "items": view["items"]})
            response.headers["Cache-Control"] = "no-store"
            return response

        def after_action(ok: bool, message: str, batch_id: int | None = None) -> Any:
            if wants_json():
                return jsonify({"ok": ok, "message": message}), (200 if ok else 409)
            flash(message, "success" if ok else "error")
            return redirect(url_for("batch_page", batch_id=batch_id) if batch_id else url_for("batch_home"))

        def item_or_404(item_id: int):
            with self.db() as c:
                item = store.get_item(c, item_id)
            if item is None:
                abort(404)
            return item

        @app.route("/batches/items/<int:item_id>/retry", methods=["POST"])
        def batch_item_retry(item_id: int) -> Any:
            core.require_auth()
            item = item_or_404(item_id)
            ok, message = self.retry(item_id)
            return after_action(ok, message, item["batch_id"])

        @app.route("/batches/items/<int:item_id>/cancel", methods=["POST"])
        def batch_item_cancel(item_id: int) -> Any:
            core.require_auth()
            item = item_or_404(item_id)
            ok, message = self.cancel(item_id)
            return after_action(ok, message, item["batch_id"])

        @app.route("/batches/items/<int:item_id>/resolve", methods=["POST"])
        def batch_item_resolve(item_id: int) -> Any:
            core.require_auth()
            item = item_or_404(item_id)
            ok, message = self.resolve(item_id, request.form.get("choice", ""))
            return after_action(ok, message, item["batch_id"])

        @app.route("/batches/<int:batch_id>/retry", methods=["POST"])
        def batch_retry_all(batch_id: int) -> Any:
            core.require_auth()
            if self.batch_view(batch_id) is None:
                abort(404)
            n = self.retry_all(batch_id)
            return after_action(n > 0, f"{n} item(s) queued for retry." if n else "Nothing to retry.", batch_id)

        @app.route("/batches/<int:batch_id>/cancel", methods=["POST"])
        def batch_cancel_all(batch_id: int) -> Any:
            core.require_auth()
            if self.batch_view(batch_id) is None:
                abort(404)
            n = self.cancel_all(batch_id)
            return after_action(n > 0, f"{n} item(s) cancelled." if n else "Nothing to cancel.", batch_id)

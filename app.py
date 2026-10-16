from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import sqlite3
import threading
import time
import uuid
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.exceptions import BadRequest, Forbidden, NotFound, RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from tools.catalog import (ReleaseNeedsReview, _compute_sha256 as _sha256_file, _validate_date, build_single_pak_from_zip, detect_package_format,
                           plan_zip_release, validate_package, validate_pak_paths)
from tools.convert_options import PRESETS, ConvertOptions, options_from_form
from tools import batchstore
from tools.batch_import import BatchService
from tools.jobs import JobCancelled, JobFailed, JobManager, JobOutcome, Progress
from tools.pak_report import analyze_pak, summarize_conversion

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("APP_SECRET_KEY", "dev-secret-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", str(2 * 1024 * 1024 * 1024))),
    UPLOAD_FOLDER=os.environ.get("UPLOAD_FOLDER", "/tmp/quake-addons-upload"),
    PERSIST_ROOT=os.environ.get("PERSIST_ROOT", "/appdata"),
    DB_PATH=os.environ.get("DB_PATH", ""),
    PUBLIC_ROOT=os.environ.get("PUBLIC_ROOT", "/tmp/quake-addons-public"),
    BASE_URL=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080"),
    ADMIN_PORT=os.environ.get("PORT", "5000"),
    JOB_MODE=os.environ.get("JOB_MODE", ""),            # "thread" (default) runs conversions in the background; "inline" runs them inside the request
)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Batch import (remote URLs / server folder). Every limit is bounded and overridable; see README "Batch import".
app.config.update(
    # absolute container paths, ':' separated. Unset: /imports is used automatically, but only if something is mounted there,
    # so a remote-only deployment has no import folder and nothing to configure.
    IMPORT_ROOTS=os.environ.get("IMPORT_ROOTS") or ("/imports" if os.path.isdir("/imports") else ""),
    IMPORT_SCAN_MAX_DEPTH=_env_int("IMPORT_SCAN_MAX_DEPTH", 8, 0),
    IMPORT_SCAN_MAX_ENTRIES=_env_int("IMPORT_SCAN_MAX_ENTRIES", 20000, 100),
    IMPORT_SCAN_MAX_RESULTS=_env_int("IMPORT_SCAN_MAX_RESULTS", 2000, 10),
    IMPORT_PAGE_SIZE=_env_int("IMPORT_PAGE_SIZE", 100, 10),
    BATCH_MAX_ITEMS=_env_int("BATCH_MAX_ITEMS", 50, 1),
    BATCH_MAX_PENDING=_env_int("BATCH_MAX_PENDING", 200, 1),
    BATCH_MAX_URL_LENGTH=_env_int("BATCH_MAX_URL_LENGTH", 2048, 64),
    BATCH_MAX_DOWNLOAD_BYTES=_env_int("BATCH_MAX_DOWNLOAD_BYTES", int(os.environ.get("MAX_CONTENT_LENGTH", 2 * 1024 ** 3)), 1024),
    BATCH_STAGING_LIMIT_BYTES=_env_int("BATCH_STAGING_LIMIT_GIB", 20, 1) * 1024 ** 3,
    BATCH_CONNECT_TIMEOUT=_env_float("BATCH_CONNECT_TIMEOUT", 10),
    BATCH_READ_TIMEOUT=_env_float("BATCH_READ_TIMEOUT", 30),
    BATCH_MAX_DURATION=_env_float("BATCH_MAX_DURATION", 3600),
    BATCH_MAX_REDIRECTS=_env_int("BATCH_MAX_REDIRECTS", 5, 0),
    BATCH_MAX_ATTEMPTS=_env_int("BATCH_MAX_ATTEMPTS", 3, 1),
    BATCH_RETRY_BASE=_env_float("BATCH_RETRY_BASE", 2),
    BATCH_RETRY_AFTER_MAX=_env_float("BATCH_RETRY_AFTER_MAX", 120),
    BATCH_RETENTION_DAYS=_env_int("BATCH_RETENTION_DAYS", 14, 1),
)

LOCK = threading.RLock()
APP_VERSION = os.environ.get("APP_VERSION", "dev")


def _db_path() -> Path:
    configured = str(app.config.get("DB_PATH") or "").strip()
    return Path(configured) if configured else Path(app.config["PERSIST_ROOT"]) / "addons.sqlite3"


def _resolve_db_path(ensure_parent: bool = True) -> Path:
    """The one configured database path. There is deliberately no fallback location."""
    path = _db_path()
    if ensure_parent:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PermissionError(f"Cannot create database directory {path.parent}: {exc}") from exc
    return path


def _diagnose_path(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": str(path), "exists": path.exists(), "is_dir": path.is_dir(), "is_file": path.is_file()}
    if path.exists():
        st = path.stat()
        info.update({"mode": oct(st.st_mode & 0o777), "uid": st.st_uid, "gid": st.st_gid})
    return info


def _effective_config() -> Dict[str, str]:
    return {
        "PERSIST_ROOT": str(app.config["PERSIST_ROOT"]),
        "DB_PATH": str(app.config.get("DB_PATH") or ""),
        "PUBLIC_ROOT": str(app.config["PUBLIC_ROOT"]),
        "UPLOAD_FOLDER": str(app.config["UPLOAD_FOLDER"]),
    }


def _inspection_warnings(path: Path, gamedir: str) -> int:
    try:
        return analyze_pak(path, gamedir)["warning_count"]
    except Exception as exc:  # inspection is advisory and must never block an upload
        print(f"[inspect] could not inspect {path.name}: {exc}", flush=True)
        return 0


# ---- staged imports and background jobs ------------------------------------------------------------------------------
# A POST only stages the upload and records a job; the heavy work runs in the job worker (tools/jobs.py), so no HTTP request is
# held open while a large package is converted or published and a reverse proxy has nothing to time out.

IMPORT_MAX_AGE = 24 * 3600
FORM_FIELDS = ("title", "addon_id", "gamedir", "author", "date", "description")


def _workdir() -> Path:
    path = Path(app.config["UPLOAD_FOLDER"]) / ".work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_import(token: str) -> sqlite3.Row | None:
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM imports WHERE token = ?", (token,)).fetchone()
    finally:
        conn.close()


def _delete_import(token: str) -> None:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM imports WHERE token = ?", (token,)).fetchone()
        conn.execute("DELETE FROM imports WHERE token = ?", (token,))
    finally:
        conn.close()
    if row and row["owns_source"]:
        staging = Path(row["source_path"]).parent
        if staging.name == token and staging.parent == Path(app.config["UPLOAD_FOLDER"]):
            shutil.rmtree(staging, ignore_errors=True)


def _save_import(token: str, kind: str, addon_pk: int | None, form: dict[str, str], source: Path, name: str, fmt: str,
                 owns_source: bool, keep_source: bool, review: bool, options: ConvertOptions | None = None, batch_item_id: int | None = None) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO imports (token, kind, addon_pk, form_json, source_path, source_name, source_format, owns_source, keep_source, review, options_json, created_at, batch_item_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, kind, addon_pk, json.dumps(form), str(source), name, fmt, int(owns_source), int(keep_source), int(review),
             json.dumps((options or ConvertOptions()).to_dict()), time.time(), batch_item_id),
        )
    finally:
        conn.close()


def expire_stale_imports(max_age: float = IMPORT_MAX_AGE) -> int:
    """Abandoned uploads (nobody pressed Build) are removed after a day; a job running that long would have been interrupted anyway."""
    conn = get_db()
    try:
        tokens = [row["token"] for row in conn.execute("SELECT token FROM imports WHERE created_at < ?", (time.time() - max_age,)).fetchall()]
    finally:
        conn.close()
    for token in tokens:
        _delete_import(token)
    return len(tokens)


def startup_cleanup() -> None:
    """Run once at startup, when no job can be running: drop what a killed process left behind."""
    interrupted = jobs.recover_interrupted()
    if interrupted:
        print(f"[startup] marked {interrupted} interrupted job(s) as failed", flush=True)
    shutil.rmtree(Path(app.config["UPLOAD_FOLDER"]) / ".work", ignore_errors=True)
    batch_service.recover()          # in-flight batch items become "interrupted" (retry is explicit); old scans/batches are expired
    # A direct (no-preview) import exists only to be built at once; if the process died, nothing can resume it, so its staged
    # upload is discarded. Previewed imports stay available from the dashboard.
    conn = get_db()
    try:
        orphans = [r["token"] for r in conn.execute("SELECT token FROM imports WHERE review = 0").fetchall()]
    finally:
        conn.close()
    for token in orphans:
        _delete_import(token)
    batch_service.reconcile()
    conn = get_db()
    try:
        referenced = {r["token"] for r in conn.execute("SELECT token FROM imports").fetchall()}
    finally:
        conn.close()
    for child in Path(app.config["UPLOAD_FOLDER"]).glob("*"):         # staging directories nothing refers to (a download/copy died half-way)
        if child.is_dir() and re.fullmatch(r"[0-9a-f]{32}", child.name) and child.name not in referenced:
            shutil.rmtree(child, ignore_errors=True)
    for folder, pattern in ((_packages_dir(), ".staging-*.pak"), (_public_addons_dir(), ".publish-*.tmp"), (_public_addons_dir(), "*.pak.tmp")):
        for leftover in folder.glob(pattern):
            leftover.unlink(missing_ok=True)
    expire_stale_imports()


def _copy_hashing(src: Path, dst: Path, progress: Progress, stage: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = src.stat().st_size
    done = 0
    progress(stage, 0, total)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(1024 * 1024), b""):
            digest.update(chunk)
            fout.write(chunk)
            done += len(chunk)
            progress(stage, done, total)
    return digest.hexdigest(), done


def _build_stages(fmt: str, options: ConvertOptions, keep_source: bool) -> list[tuple[str, float]]:
    if fmt == "zip":
        stages = [("Fingerprinting source", 4), ("Reading archive", 3), ("Reading embedded PAKs", 6), ("Extracting files", 30)]
        if options.reencode_textures or options.reencode_skyboxes:
            stages.append(("Re-encoding images", 40))
        stages += [("Writing package", 12), ("Checking package", 8)]
    else:
        stages = [("Copying package", 70), ("Checking package", 25)]
    if keep_source:
        stages.append(("Keeping source copy", 8))
    return stages + [("Installing", 2)]


class ImportConflict(JobFailed):
    """The add-on ID or install directory is already taken (a person has to decide: new ID, replace, or skip)."""


class ReviewRequired(JobFailed):
    """The package cannot be built as it is, but the preview options (root folder, leaving files out) could fix it."""


@dataclass
class BuildResult:
    text: str
    level: str
    addon_pk: int | None = None
    warning_count: int = 0
    replaced: bool = False
    published: bool = False
    skipped: str | None = None            # set when nothing was installed because identical content already exists
    skipped_addon_pk: int | None = None


def _build_import(token: str, progress: Progress, policy: dict[str, Any] | None = None) -> BuildResult:
    """Convert/validate a staged import and install it as a draft (or replace an add-on's package). The only conversion path:
    browser uploads, local imports and batch items all end up here.

    policy (all optional): skip_identical_output - install nothing if another add-on already has exactly this converted package;
    keep_on_review - keep the staged import (instead of deleting it) when the release needs a decision or a preview;
    provenance - a dict recorded in the conversion record (source type, host, file name, import time)."""
    policy = policy or {}
    imp = _load_import(token)
    if imp is None:
        raise JobFailed("This import was cancelled or has expired. Start again from the add-on form.", "add_addon")
    batch_item_id = imp["batch_item_id"]
    form = json.loads(imp["form_json"])
    replace = imp["kind"] == "replace"
    fmt = imp["source_format"]
    source = Path(imp["source_path"])
    options = ConvertOptions.from_dict(json.loads(imp["options_json"] or "{}"))
    keep_source = bool(imp["keep_source"]) and fmt == "zip"
    keep_import_on_failure = bool(imp["review"]) and fmt == "zip"
    if keep_import_on_failure:
        retry = ("import_plan", {"token": token})
    elif replace:
        retry = ("edit_addon", {"addon_id": imp["addon_pk"]})
    else:
        retry = ("add_addon", {})
    staged = _packages_dir() / f".staging-{uuid.uuid4().hex}.pak"
    kept: Path | None = None
    installed_target: Path | None = None
    try:
        progress.plan(_build_stages(fmt, options, keep_source))
        if fmt == "zip":
            source_sha = _sha256_file(source, progress, "Fingerprinting source")
            info = build_single_pak_from_zip(source, staged, options=options, progress=progress, workdir=_workdir())
            print(
                f"[import] ZIP release ({info['root_mapping']}{' ' + repr(info['wrapper']) if info['wrapper'] else ''}) consolidated into single PAK with "
                f"{info['entries']} entries from {len(info['embedded_paks'])} embedded PAK(s) and {info['loose_files']} loose file(s); "
                f"{len(info['overrides'])} path(s) overridden by engine precedence"
                + (f"; {info['excluded']['total']} excluded by option" if info.get("excluded") else "")
                + (f"; {info['reencoded']['converted']} image(s) re-encoded" if info.get("reencoded") else ""),
                flush=True,
            )
            package = validate_package(staged, sha256=info["sha256"])
            record = summarize_conversion(info, imp["source_name"], source_sha)
            source_digest = source_sha
            message = "Uploaded ZIP release was inspected and packaged from its embedded PAK."
        else:
            sha, _ = _copy_hashing(source, staged, progress, "Copying package")
            package = validate_package(staged, sha256=sha)
            record = {"report_version": 1, "kind": "pak-upload", "source_name": imp["source_name"],
                      "output_sha256": package["sha256"], "output_size": package["size"]}
            source_digest = package["sha256"]
            message = "Uploaded PAK was stored unchanged."
        if policy.get("provenance"):
            record["provenance"] = policy["provenance"]
        print(f"[import] validated package for {form['addon_id']}: size={package['size']} sha256={package['sha256']}", flush=True)
        if policy.get("skip_identical_output"):
            conn = get_db()
            try:
                twin = conn.execute("SELECT id, addon_id FROM addons WHERE package_sha256 = ? AND id != ? LIMIT 1", (package["sha256"], imp["addon_pk"] or -1)).fetchone()
            finally:
                conn.close()
            if twin:
                _delete_import(token)
                return BuildResult(f"Skipped: the converted package is identical to add-on '{twin['addon_id']}'.", "info", skipped="identical-output", skipped_addon_pk=twin["id"])
        warning_count = _inspection_warnings(staged, form["gamedir"])

        source_path = source_name = None
        if keep_source:
            kept_dir = Path(app.config["PERSIST_ROOT"]) / "sources" / token
            kept_dir.mkdir(parents=True, exist_ok=True)
            kept = kept_dir / (secure_filename(imp["source_name"]) or "source.zip")
            _copy_hashing(source, kept, progress, "Keeping source copy")
            source_path, source_name = str(kept), imp["source_name"]
        progress("Installing", 0, 1)

        old_source: str | None = None
        with db_txn() as conn:
            if replace:
                addon = conn.execute("SELECT * FROM addons WHERE id = ?", (imp["addon_pk"],)).fetchone()
                if not addon:
                    raise ValueError("The add-on was deleted while its package was being built")
                if conn.execute("SELECT 1 FROM addons WHERE addon_id = ? AND id != ?", (form["addon_id"], addon["id"])).fetchone():
                    raise sqlite3.IntegrityError("duplicate addon id")
                if conn.execute("SELECT 1 FROM addons WHERE gamedir = ? AND id != ?", (form["gamedir"], addon["id"])).fetchone():
                    raise sqlite3.IntegrityError("duplicate gamedir")
                target = Path(addon["package_path"])
                old_source = addon["source_path"] if source_path else None
                conn.execute(
                    "UPDATE addons SET title=?, addon_id=?, gamedir=?, author=?, date=?, description=?, package_size=?, package_sha256=?, "
                    "package_mtime=?, conversion_report=?, source_path=COALESCE(?, source_path), source_name=COALESCE(?, source_name), "
                    "source_sha256=?, batch_item_id=COALESCE(?, batch_item_id), updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (form["title"], form["addon_id"], form["gamedir"], form["author"], form["date"], form["description"], package["size"],
                     package["sha256"], int(staged.stat().st_mtime), json.dumps(record, sort_keys=True), source_path, source_name, source_digest, batch_item_id, addon["id"]),
                )
                addon_pk, published = addon["id"], bool(addon["published"])
            else:
                if conn.execute("SELECT 1 FROM addons WHERE addon_id = ?", (form["addon_id"],)).fetchone():
                    raise sqlite3.IntegrityError("duplicate addon id")
                if conn.execute("SELECT 1 FROM addons WHERE gamedir = ?", (form["gamedir"],)).fetchone():
                    raise sqlite3.IntegrityError("duplicate gamedir")
                target = _packages_dir() / f"{form['addon_id']}.pak"
                cur = conn.execute(
                    """
                    INSERT INTO addons (
                        addon_id, title, gamedir, author, date, description,
                        package_name, package_path, package_size, package_sha256, package_mtime,
                        published, draft, manifest_path, conversion_report, source_path, source_name, source_sha256, batch_item_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, NULL, ?, ?, ?, ?, ?)
                    """,
                    (form["addon_id"], form["title"], form["gamedir"], form["author"], form["date"], form["description"], target.name, str(target),
                     package["size"], package["sha256"], int(staged.stat().st_mtime), json.dumps(record, sort_keys=True), source_path, source_name, source_digest, batch_item_id),
                )
                addon_pk, published = cur.lastrowid, False
            os.replace(staged, target)                       # last step: only after every database statement succeeded
            installed_target = target
        installed_target = None
        kept = None
        if old_source:
            shutil.rmtree(Path(old_source).parent, ignore_errors=True)
        _delete_import(token)
        if published:
            generate_catalog()
    except JobCancelled:
        if not keep_import_on_failure:
            _delete_import(token)
        raise
    except (sqlite3.IntegrityError, OSError, ValueError) as exc:
        if installed_target is not None and not replace:
            installed_target.unlink(missing_ok=True)
        conflict = isinstance(exc, sqlite3.IntegrityError) and "duplicate" in str(exc)
        needs_review = isinstance(exc, ReleaseNeedsReview)
        keep = keep_import_on_failure or (bool(policy.get("keep_on_review")) and (conflict or needs_review))
        if not keep:
            _delete_import(token)
        elif not keep_import_on_failure:
            conn = get_db()
            try:
                conn.execute("UPDATE imports SET review = 1 WHERE token = ?", (token,))     # survives a restart and stays on the dashboard
            finally:
                conn.close()
        if policy.get("keep_on_review") and conflict:
            raise ImportConflict(f"Package rejected: {exc}", *retry) from exc
        if policy.get("keep_on_review") and needs_review:
            raise ReviewRequired(f"Package rejected: {exc}", "import_plan", {"token": token}) from exc
        raise JobFailed(f"Package rejected: {exc}", *retry) from exc
    finally:
        staged.unlink(missing_ok=True)
        if kept is not None:
            shutil.rmtree(kept.parent, ignore_errors=True)

    note = f" Inspection found {warning_count} warning(s); see the add-on page." if warning_count else ""
    if replace:
        tail = " The public download still serves the previous package until you click Publish again." if published else ""
        text = f"{message} Add-on updated.{note}{tail}"
    else:
        text = f"{message} Add-on saved as a draft.{note} Review and publish when ready."
    return BuildResult(text, "warning" if warning_count else "success", addon_pk, warning_count, replace, published)


def _run_build(token: str, progress: Progress) -> JobOutcome:
    result = _build_import(token, progress)
    return JobOutcome(result.text, result.level, "addon_detail", {"addon_id": result.addon_pk})


def _run_plan(token: str, progress: Progress) -> JobOutcome:
    imp = _load_import(token)
    if imp is None:
        raise JobFailed("This import was cancelled or has expired. Start again from the add-on form.", "add_addon")
    options = ConvertOptions.from_dict(json.loads(imp["options_json"] or "{}"))
    progress.plan([("Reading archive", 4), ("Reading embedded PAKs", 96)])
    try:
        plan = plan_zip_release(Path(imp["source_path"]), options, progress=progress, workdir=_workdir())
    except (OSError, ValueError) as exc:
        _delete_import(token)
        raise JobFailed(f"Package rejected: {exc}", "add_addon") from exc
    except JobCancelled:
        _delete_import(token)
        raise
    conn = get_db()
    try:
        conn.execute("UPDATE imports SET plan_json = ? WHERE token = ?", (json.dumps(plan), token))
    finally:
        conn.close()
    return JobOutcome("The conversion preview is ready.", "success", "import_plan", {"token": token}, auto_redirect=True)


def _run_publish(addon_pk: int, progress: Progress) -> JobOutcome:
    conn = get_db()
    try:
        addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_pk,)).fetchone()
    finally:
        conn.close()
    if not addon:
        raise JobFailed("The add-on no longer exists.", "dashboard")
    package_path = Path(addon["package_path"])
    if not package_path.exists():
        raise JobFailed("Publish failed: the package file is missing.", "addon_detail", {"addon_id": addon_pk})
    progress.plan([("Checking package", 3), ("Copying to the public folder", 97)])
    progress("Checking package", 0, 1)
    validate_pak_paths(package_path)
    progress("Checking package", 1, 1)
    public_dir = _public_addons_dir()
    public_file = public_dir / f"{addon['addon_id']}.pak"
    tmp_file = public_dir / f".publish-{uuid.uuid4().hex}.tmp"
    try:
        sha, size = _copy_hashing(package_path, tmp_file, progress, "Copying to the public folder")
        if addon["package_sha256"] and addon["package_sha256"] != sha:
            raise ValueError("Stored package hash does not match the file on disk")
        os.replace(tmp_file, public_file)
    except (OSError, ValueError) as exc:
        raise JobFailed(f"Publish failed: {exc}", "addon_detail", {"addon_id": addon_pk}) from exc
    finally:
        tmp_file.unlink(missing_ok=True)
    with db_txn() as conn:
        conn.execute(
            "UPDATE addons SET published = 1, draft = 0, package_size = ?, package_sha256 = ?, package_mtime = ?, manifest_path = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (size, sha, int(package_path.stat().st_mtime), str(public_file), addon_pk),
        )
    generate_catalog()
    return JobOutcome("Add-on published and catalog regenerated.", "success", "dashboard")


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _job_response(job_id: str) -> Any:
    target = url_for("job_page", job_id=job_id)
    return jsonify({"redirect": target}) if _wants_json() else redirect(target)


def _form_failure(message: str, template: str = "addon_form.html", **ctx: Any) -> Any:
    if _wants_json():
        return jsonify({"error": message}), 400
    flash(message, "error")
    return render_template(template, **ctx)


@app.template_filter("filesize")
def filesize_filter(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "?"
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


def ensure_dirs() -> None:
    for p in [
        app.config["UPLOAD_FOLDER"],
        app.config["PUBLIC_ROOT"],
    ]:
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PermissionError(f"Cannot create required directory {p}: {exc}") from exc


def get_db() -> sqlite3.Connection:
    ensure_dirs()
    db_path = _resolve_db_path()
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_txn() -> Iterator[sqlite3.Connection]:
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _job_mode_inline() -> bool:
    mode = app.config.get("JOB_MODE") or ("inline" if app.config.get("TESTING") else "thread")
    return mode == "inline"


jobs = JobManager(lambda: get_db(), _job_mode_inline, log=lambda m: print(m, flush=True),
                  lanes={"work": 1, "net": _env_int("BATCH_DOWNLOAD_WORKERS", 2, 1)})     # fixed-size pools shared by every import source


def init_db() -> None:
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS addons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            addon_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            gamedir TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT 'Unknown',
            date TEXT,
            description TEXT,
            package_name TEXT NOT NULL,
            package_path TEXT NOT NULL,
            package_size INTEGER NOT NULL,
            package_sha256 TEXT,
            package_mtime INTEGER,
            manifest_path TEXT,
            published INTEGER NOT NULL DEFAULT 0,
            draft INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            view_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(addons)").fetchall()}
    if "package_sha256" not in existing:
        conn.execute("ALTER TABLE addons ADD COLUMN package_sha256 TEXT")
    if "package_mtime" not in existing:
        conn.execute("ALTER TABLE addons ADD COLUMN package_mtime INTEGER")
    if "conversion_report" not in existing:
        conn.execute("ALTER TABLE addons ADD COLUMN conversion_report TEXT")
    if "source_path" not in existing:
        conn.execute("ALTER TABLE addons ADD COLUMN source_path TEXT")
    if "source_name" not in existing:
        conn.execute("ALTER TABLE addons ADD COLUMN source_name TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imports (
            token TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            addon_pk INTEGER,
            form_json TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_format TEXT NOT NULL,
            owns_source INTEGER NOT NULL DEFAULT 1,
            keep_source INTEGER NOT NULL DEFAULT 0,
            review INTEGER NOT NULL DEFAULT 0,
            options_json TEXT,
            plan_json TEXT,
            created_at REAL NOT NULL,
            batch_item_id INTEGER
        )
        """
    )
    if "batch_item_id" not in {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}:
        conn.execute("ALTER TABLE imports ADD COLUMN batch_item_id INTEGER")
    if "source_sha256" not in {row[1] for row in conn.execute("PRAGMA table_info(addons)").fetchall()}:
        conn.execute("ALTER TABLE addons ADD COLUMN source_sha256 TEXT")
    if "batch_item_id" not in {row[1] for row in conn.execute("PRAGMA table_info(addons)").fetchall()}:
        conn.execute("ALTER TABLE addons ADD COLUMN batch_item_id INTEGER")
    # one draft per batch item, enforced by the database, so a retry or a crash cannot create a second one
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_addons_batch_item ON addons(batch_item_id) WHERE batch_item_id IS NOT NULL")
    batchstore.init_schema(conn)
    jobs.init_schema(conn)
    conn.commit()
    conn.close()


def _owner_hint(path: Path) -> str:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        st = probe.stat()
        owner = f"{probe} is owned by uid:gid {st.st_uid}:{st.st_gid} mode {oct(st.st_mode & 0o777)}"
    except OSError:
        owner = f"{probe} could not be inspected"
    return (f"{owner}; the container runs as uid:gid {os.getuid() if hasattr(os, 'getuid') else '?'}:{os.getgid() if hasattr(os, 'getgid') else '?'}. "
            "Fix the host directory ownership/mode (or run the container with --user matching the owner). Existing data was not modified.")


def _check_dir_writable(path: Path, label: str) -> str | None:
    """Prove a directory is usable by creating and removing a real file. Never changes ownership or modes."""
    try:
        if path.exists() and not path.is_dir():
            return f"{label}: {path} exists but is not a directory"
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{uuid.uuid4().hex}"
        with open(probe, "wb") as fh:
            fh.write(b"ok")
        probe.unlink()
    except OSError as exc:
        import errno
        if exc.errno == errno.EROFS:
            return f"{label}: {path} is on a read-only filesystem/mount (mount it read-write)"
        if exc.errno in (errno.EACCES, errno.EPERM):
            return f"{label}: permission denied writing to {path}. {_owner_hint(path)}"
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return f"{label}: no space or quota left for {path} ({exc})"
        if exc.errno == errno.ENOENT:
            return f"{label}: {path} is missing and could not be created ({exc})"
        return f"{label}: {path} is not usable: {exc}"
    return None


def _check_database(db_path: Path, lock_timeout: float = 5.0) -> str | None:
    if db_path.exists():
        if not db_path.is_file():
            return f"database: {db_path} exists but is not a regular file"
        if not os.access(db_path, os.R_OK | os.W_OK):
            return f"database: permission denied on existing file {db_path}. {_owner_hint(db_path)}"
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=lock_timeout, isolation_level=None)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return f"database: {db_path} is locked by another process ({exc}); stop the other instance or fix the network/FUSE share locking"
        if "readonly" in msg or "read-only" in msg:
            return f"database: {db_path} is read-only ({exc}). {_owner_hint(db_path)}"
        if "not a database" in msg or "malformed" in msg:
            return f"database: {db_path} is not a valid SQLite database ({exc}); it was left untouched, restore it from backup"
        if "unable to open" in msg:
            return f"database: cannot open {db_path} ({exc}). {_owner_hint(db_path)}"
        return f"database: {db_path}: {exc}"
    finally:
        if conn is not None:
            conn.close()
    return None


def _mountinfo_points(text: str) -> set[str]:
    points = set()
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(fields[4].replace("\\040", " "))
    return points


def persistence_warnings(mountinfo: str | None = None) -> list[str]:
    """Non-fatal: warn when PERSIST_ROOT/PUBLIC_ROOT are not mounts, i.e. data would live in the container layer."""
    try:
        text = mountinfo if mountinfo is not None else Path("/proc/self/mountinfo").read_text()
    except OSError:
        return []
    points = _mountinfo_points(text)
    warnings = []
    for key in ("PERSIST_ROOT", "PUBLIC_ROOT"):
        root = os.path.realpath(str(app.config[key]))
        if root not in points:
            warnings.append(f"{key} {root} is not a mount point; if this is a container, its data lives in the container layer and will be LOST when it is recreated. Map a host directory to {root}.")
    return warnings


def startup_preflight(lock_timeout: float = 5.0) -> Tuple[bool, list[str]]:
    """Diagnose the configured locations only. Never relocates data, never changes config, never chmods."""
    issues: list[str] = []
    db_path = _db_path()
    checked: set[str] = set()
    for label, path in (
        ("persist root", Path(app.config["PERSIST_ROOT"])),
        ("database directory", db_path.parent),
        ("public root", Path(app.config["PUBLIC_ROOT"])),
        ("upload folder", Path(app.config["UPLOAD_FOLDER"])),
    ):
        if str(path) in checked:
            continue
        checked.add(str(path))
        problem = _check_dir_writable(path, label)
        if problem:
            issues.append(problem)
    if not issues:
        for sub in ("packages", "metadata"):
            problem = _check_dir_writable(Path(app.config["PERSIST_ROOT"]) / sub, f"persist root/{sub}")
            if problem:
                issues.append(problem)
        public_addons = _check_dir_writable(Path(app.config["PUBLIC_ROOT"]) / "addons", "public root/addons")
        if public_addons:
            issues.append(public_addons)
    if not issues:
        problem = _check_database(db_path, lock_timeout)
        if problem:
            issues.append(problem)
    return (not issues), issues


def log_startup_banner() -> None:
    db_path = _db_path()
    print(
        json.dumps(
            {
                "event": "startup",
                "version": APP_VERSION,
                "uid": os.getuid() if hasattr(os, "getuid") else None,
                "gid": os.getgid() if hasattr(os, "getgid") else None,
                "config": _effective_config(),
                "db_path": str(db_path),
                "warnings": persistence_warnings(),
                "note": "If startup preflight fails with permissions, the host mount ownership/mode must be fixed; the app will not chmod or chown existing data.",
            },
            sort_keys=True,
        ),
        flush=True,
    )


@app.route("/doctor")
def doctor() -> Any:
    require_auth()
    db_path = _db_path()
    payload = {
        "version": APP_VERSION,
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "gid": os.getgid() if hasattr(os, "getgid") else None,
        "paths": {
            "persist_root": _diagnose_path(Path(app.config["PERSIST_ROOT"])),
            "public_root": _diagnose_path(Path(app.config["PUBLIC_ROOT"])),
            "upload_root": _diagnose_path(Path(app.config["UPLOAD_FOLDER"])),
            "db_path": _diagnose_path(db_path),
        },
    }
    ok, issues = startup_preflight()
    payload["ok"] = ok
    payload["issues"] = issues
    payload["warnings"] = persistence_warnings()
    return jsonify(payload), (200 if ok else 503)


def _packages_dir() -> Path:
    path = Path(app.config["PERSIST_ROOT"]) / "packages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _metadata_dir() -> Path:
    path = Path(app.config["PERSIST_ROOT"]) / "metadata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _staging_dir() -> Path:
    path = Path(app.config["PERSIST_ROOT"]) / "staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _public_addons_dir() -> Path:
    path = Path(app.config["PUBLIC_ROOT"]) / "addons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = uuid.uuid4().hex
        session["csrf_token"] = token
    return token


def check_csrf() -> None:
    if request.method == "POST":
        form_token = request.form.get("csrf_token")
        session_token = session.get("csrf_token")
        if not session_token or not form_token or form_token != session_token:
            raise Forbidden("Invalid CSRF token")


@app.before_request
def ensure_csrf() -> None:
    """Every state-changing request needs the session's CSRF token. (This used to cover only /admin, /dashboard and /addons/, which left
    /imports, /jobs and any later route open to cross-site POSTs from a logged-in administrator.)"""
    if request.method == "POST":
        check_csrf()
    make_csrf_token()


@app.context_processor
def inject_globals() -> Dict[str, Any]:
    return {
        "public_base_url": app.config["BASE_URL"],
        "current_user": session.get("username"),
        "csrf_token": make_csrf_token(),
        "presets": PRESETS,
        "default_options": ConvertOptions(),
    }


@app.after_request
def security_headers(response):
    response.headers.setdefault("Referrer-Policy", "no-referrer")          # a pasted link or a file name must never leak through a Referer header
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(exc: RequestEntityTooLarge):
    message = "Upload too large. Increase MAX_CONTENT_LENGTH if you need to support bigger ZIP/PAK uploads."
    if request.path.startswith("/addons/"):
        flash(message, "error")
        return render_template("addon_form.html", mode="new"), 413
    return jsonify({"ok": False, "error": message}), 413


@app.route("/health")
def health() -> Any:
    ok, issues = startup_preflight()
    status = 200 if ok else 503
    return jsonify({"ok": ok, "status": "healthy" if ok else "degraded", "issues": issues}), status


@app.route("/")
def index() -> Any:
    if not session.get("username"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session["username"] = username
            flash("Logged in.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout() -> Any:
    session.pop("username", None)
    flash("Logged out.", "success")
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup() -> Any:
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
    if row["count"] > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("setup.html")
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
        conn.commit()
        conn.close()
        flash("Administrator account created.", "success")
        return redirect(url_for("login"))
    return render_template("setup.html")


def require_auth() -> None:
    if not session.get("username"):
        raise Forbidden()


@app.route("/dashboard")
def dashboard() -> Any:
    require_auth()
    conn = get_db()
    addons = conn.execute("SELECT * FROM addons ORDER BY created_at DESC").fetchall()
    pending = conn.execute("SELECT token, kind, source_name, form_json, created_at, plan_json IS NOT NULL AS has_plan FROM imports WHERE review = 1 ORDER BY created_at DESC").fetchall()
    conn.close()
    pending_imports = [{"token": r["token"], "name": r["source_name"], "kind": r["kind"], "title": json.loads(r["form_json"]).get("title", ""),
                        "age_minutes": int((time.time() - r["created_at"]) / 60)} for r in pending]
    return render_template("dashboard.html", addons=addons, active_jobs=jobs.active(), pending_imports=pending_imports)


def _read_addon_form() -> dict[str, str]:
    return {
        "title": request.form.get("title", "").strip(),
        "addon_id": request.form.get("addon_id", "").strip(),
        "gamedir": request.form.get("gamedir", "").strip(),
        "author": request.form.get("author", "Unknown").strip(),
        "date": request.form.get("date", "").strip(),
        "description": request.form.get("description", "").strip(),
    }


def _stage_source(token: str) -> tuple[Path, str, bool]:
    """Resolve the package the administrator supplied: a browser upload (saved under its own token directory) or a server path
    (used in place, read-only). Returns (path, display name, owned_by_us). Raises ValueError with a user-facing message."""
    if request.form.get("import_mode", "upload") == "local":
        local_source = request.form.get("local_source", "").strip()
        if not local_source:
            raise ValueError("Local source path is required for local import.")
        source_path = Path(local_source).expanduser()
        if not source_path.is_absolute():
            raise ValueError("Local source path must be absolute.")
        if not source_path.exists():
            raise ValueError("Local source file does not exist.")
        if source_path.suffix.lower() not in (".zip", ".pak"):
            raise ValueError("Local source must be a .zip or .pak file.")
        print(f"[import] local source selected: {source_path}", flush=True)
        return source_path, source_path.name, False
    package_file = request.files.get("package")
    if not package_file or not package_file.filename:
        raise ValueError("Package file is required for upload mode.")
    safe_name = secure_filename(package_file.filename)
    if not safe_name:
        raise ValueError("Invalid package filename.")
    staging = Path(app.config["UPLOAD_FOLDER"]) / token
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / safe_name
    print(f"[import] browser upload selected: {safe_name}", flush=True)
    package_file.save(dest)
    return dest, safe_name, True


def _start_import(kind: str, addon_pk: int | None, form: dict[str, str], ctx: dict[str, Any]) -> Any:
    """Shared by 'new' and 'replace': stage the file, record the import, start the plan or build job."""
    token = uuid.uuid4().hex
    template = "addon_form.html"
    owned = False
    try:
        options = options_from_form(request.form, defaults=form)          # validate before anything is staged
    except ValueError as exc:
        return _form_failure(f"Options rejected: {exc}", template, **ctx)
    try:
        try:
            source, name, owned = _stage_source(token)
        except OSError as exc:
            raise ValueError(str(exc)) from exc
        fmt = detect_package_format(source)
        print(f"[import] detected {fmt} for {name}; staged at {source}", flush=True)
        if fmt not in ("zip", "pak"):
            raise ValueError(f"Unsupported package format for {name!r}; expected a ZIP or Quake PAK")
        conn = get_db()
        try:
            exclude = addon_pk if addon_pk is not None else -1
            if conn.execute("SELECT 1 FROM addons WHERE addon_id = ? AND id != ?", (form["addon_id"], exclude)).fetchone():
                raise ValueError("duplicate addon id")
            if conn.execute("SELECT 1 FROM addons WHERE gamedir = ? AND id != ?", (form["gamedir"], exclude)).fetchone():
                raise ValueError("duplicate gamedir")
        finally:
            conn.close()
    except ValueError as exc:
        shutil.rmtree(Path(app.config["UPLOAD_FOLDER"]) / token, ignore_errors=True)    # only ever exists if this request created it
        message = str(exc)
        if not message.startswith(("Local source", "Package file", "Invalid package")):
            message = f"Package rejected: {message}"
        return _form_failure(message, template, **ctx)
    expire_stale_imports()
    review = bool(request.form.get("review")) and fmt == "zip"
    _save_import(token, kind, addon_pk, form, source, name, fmt, owned, bool(request.form.get("keep_source")) and fmt == "zip", review,
                 options if fmt == "zip" else ConvertOptions())
    if review:
        job_id = jobs.submit("plan", f"Preview of {name}", lambda progress: _run_plan(token, progress))
    else:
        job_id = jobs.submit("build", f"Convert {name}", lambda progress: _run_build(token, progress))
    return _job_response(job_id)


@app.route("/addons/new", methods=["GET", "POST"])
def add_addon() -> Any:
    require_auth()
    if request.method == "POST":
        form = _read_addon_form()
        if not form["title"] or not form["addon_id"] or not form["gamedir"]:
            return _form_failure("Title, ID, and game dir are required.", mode="new")
        return _start_import("new", None, form, {"mode": "new"})
    return render_template("addon_form.html", mode="new")


def _addon_inspection(addon: sqlite3.Row) -> Dict[str, Any]:
    """Advisory, read-only view of the stored package. Never raises into the page."""
    current = {k: addon[k] or "" for k in ("title", "author", "date", "description")}
    result: Dict[str, Any] = {"report": None, "error": None, "conversion": None}
    try:
        result["conversion"] = json.loads(addon["conversion_report"]) if addon["conversion_report"] else None
    except ValueError:
        result["conversion"] = None
    try:
        result["report"] = analyze_pak(Path(addon["package_path"]), addon["gamedir"], current)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    return result


@app.route("/addons/<int:addon_id>")
def addon_detail(addon_id: int) -> Any:
    require_auth()
    conn = get_db()
    addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
    conn.close()
    if not addon:
        raise NotFound()
    return render_template("addon_detail.html", addon=addon, inspection=_addon_inspection(addon))


SUGGESTION_FIELDS = ("title", "author", "date", "description")


@app.route("/addons/<int:addon_id>/suggestion/<field>", methods=["POST"])
def apply_suggestion(addon_id: int, field: str) -> Any:
    """Apply one metadata suggestion. The value is recomputed from the stored package, never taken from the request."""
    require_auth()
    if field not in SUGGESTION_FIELDS:
        raise NotFound()
    with db_txn() as conn:
        addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
        if not addon:
            raise NotFound()
        inspection = _addon_inspection(addon)
        suggestion = (inspection["report"] or {}).get("suggestions", {}).get(field)
        if not suggestion:
            flash(f"No {field} suggestion is available for this add-on.", "error")
            return redirect(url_for("addon_detail", addon_id=addon_id))
        value = suggestion["value"]
        try:
            if field == "date":
                _validate_date(value)
        except ValueError as exc:
            flash(f"Suggested date rejected: {exc}", "error")
            return redirect(url_for("addon_detail", addon_id=addon_id))
        conn.execute(f"UPDATE addons SET {field} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (value, addon_id))
        published = bool(addon["published"])
    if published:
        generate_catalog()
    flash(f"Set {field} from {suggestion['source']}.", "success")
    return redirect(url_for("addon_detail", addon_id=addon_id))


@app.route("/addons/<int:addon_id>/edit", methods=["GET", "POST"])
def edit_addon(addon_id: int) -> Any:
    require_auth()
    conn = get_db()
    try:
        addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
    finally:
        conn.close()
    if not addon:
        raise NotFound()
    ctx = {"mode": "edit", "addon": dict(addon)}
    if request.method == "POST":
        form = _read_addon_form()
        if not form["title"] or not form["addon_id"] or not form["gamedir"]:
            return _form_failure("Title, ID, and game dir are required.", **ctx)
        if form["addon_id"] != addon["addon_id"] and addon["published"]:
            return _form_failure("Unpublish this add-on before changing its ID (the public download path is derived from it).", **ctx)
        package_file = request.files.get("package")
        if (package_file and package_file.filename) or request.form.get("import_mode") == "local":
            return _start_import("replace", addon_id, form, ctx)
        try:
            with db_txn() as conn:
                if conn.execute("SELECT 1 FROM addons WHERE addon_id = ? AND id != ?", (form["addon_id"], addon_id)).fetchone():
                    raise sqlite3.IntegrityError("duplicate addon id")
                if conn.execute("SELECT 1 FROM addons WHERE gamedir = ? AND id != ?", (form["gamedir"], addon_id)).fetchone():
                    raise sqlite3.IntegrityError("duplicate gamedir")
                conn.execute(
                    "UPDATE addons SET title = ?, addon_id = ?, gamedir = ?, author = ?, date = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (form["title"], form["addon_id"], form["gamedir"], form["author"], form["date"], form["description"], addon_id),
                )
        except sqlite3.IntegrityError as exc:
            return _form_failure(f"Update rejected: {exc}", **ctx)
        if addon["published"]:
            generate_catalog()
        flash("Add-on updated.", "success")
        return redirect(url_for("addon_detail", addon_id=addon_id))
    return render_template("addon_form.html", **ctx)


@app.route("/addons/<int:addon_id>/rebuild", methods=["POST"])
def rebuild_addon(addon_id: int) -> Any:
    """Re-run the conversion from the source ZIP that was kept at upload time, with options chosen on the preview page."""
    require_auth()
    conn = get_db()
    try:
        addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
    finally:
        conn.close()
    if not addon:
        raise NotFound()
    source = Path(addon["source_path"]) if addon["source_path"] else None
    if source is None or not source.is_file():
        flash("No source ZIP was kept for this add-on, so it cannot be rebuilt. Upload the ZIP again from Edit.", "error")
        return redirect(url_for("addon_detail", addon_id=addon_id))
    fmt = detect_package_format(source)
    if fmt != "zip":
        flash("The kept source is not a ZIP file.", "error")
        return redirect(url_for("addon_detail", addon_id=addon_id))
    token = uuid.uuid4().hex
    form = {k: (addon[k] or "") for k in FORM_FIELDS}
    _save_import(token, "replace", addon_id, form, source, addon["source_name"] or source.name, fmt, owns_source=False, keep_source=False, review=True)
    job_id = jobs.submit("plan", f"Preview of {source.name}", lambda progress: _run_plan(token, progress))
    return _job_response(job_id)


@app.route("/imports/<token>")
def import_plan(token: str) -> Any:
    require_auth()
    imp = _load_import(token)
    if imp is None:
        flash("This import was cancelled or has expired.", "error")
        return redirect(url_for("add_addon"))
    plan = json.loads(imp["plan_json"]) if imp["plan_json"] else None
    options = ConvertOptions.from_dict(json.loads(imp["options_json"] or "{}"))
    return render_template("import_plan.html", imp=imp, plan=plan, options=options, form=json.loads(imp["form_json"]), presets=PRESETS,
                           space=_space_estimate(plan, imp))


def _space_estimate(plan: dict[str, Any] | None, imp: sqlite3.Row) -> dict[str, Any] | None:
    """Rough free-space check for the conversion: extracted files + the new package in the work area, plus a kept copy of the source."""
    if not plan:
        return None
    need = 2 * plan["payload_bytes"] + sum(p["bytes"] for p in plan["embedded_paks"])
    if imp["keep_source"]:
        try:
            need += Path(imp["source_path"]).stat().st_size
        except OSError:
            pass
    try:
        free_work = shutil.disk_usage(_workdir()).free
        free_data = shutil.disk_usage(_packages_dir()).free
    except OSError:
        return None
    return {"need": need, "free_work": free_work, "free_data": free_data,
            "low": free_work < need or free_data < plan["payload_bytes"] + 64 * plan["entries"]}


def _import_or_404(token: str) -> sqlite3.Row:
    imp = _load_import(token)
    if imp is None:
        raise NotFound()
    return imp


def _store_options(token: str, imp: sqlite3.Row) -> ConvertOptions | None:
    try:
        options = options_from_form(request.form, defaults=json.loads(imp["form_json"]))
    except ValueError as exc:
        flash(f"Options rejected: {exc}", "error")
        return None
    conn = get_db()
    try:
        keep = 1 if request.form.get("keep_source") else 0
        conn.execute("UPDATE imports SET options_json = ?, keep_source = ? WHERE token = ?", (json.dumps(options.to_dict()), keep, token))
    finally:
        conn.close()
    return options


@app.route("/imports/<token>/plan", methods=["POST"])
def import_replan(token: str) -> Any:
    require_auth()
    imp = _import_or_404(token)
    if _store_options(token, imp) is None:
        return redirect(url_for("import_plan", token=token))
    return _job_response(jobs.submit("plan", f"Preview of {imp['source_name']}", lambda progress: _run_plan(token, progress)))


@app.route("/imports/<token>/build", methods=["POST"])
def import_build(token: str) -> Any:
    require_auth()
    imp = _import_or_404(token)
    if _store_options(token, imp) is None:
        return redirect(url_for("import_plan", token=token))
    return _job_response(jobs.submit("build", f"Convert {imp['source_name']}", lambda progress: _run_build(token, progress)))


@app.route("/imports/<token>/cancel", methods=["POST"])
def import_cancel(token: str) -> Any:
    require_auth()
    imp = _import_or_404(token)
    _delete_import(token)
    flash("Import cancelled; the uploaded file was removed.", "success")
    return redirect(url_for("edit_addon", addon_id=imp["addon_pk"]) if imp["addon_pk"] else url_for("dashboard"))


@app.route("/jobs/<job_id>")
def job_page(job_id: str) -> Any:
    require_auth()
    job = jobs.get(job_id)
    if job is None:
        raise NotFound()
    return render_template("job.html", job=job, next_url=_job_next_url(job))


def _job_next_url(job: dict[str, Any]) -> str | None:
    if not job.get("next_endpoint"):
        return None
    try:
        return url_for(job["next_endpoint"], **job["next_values"])
    except Exception:
        return None


@app.route("/jobs/<job_id>/status")
def job_status(job_id: str) -> Any:
    require_auth()
    job = jobs.get(job_id)
    if job is None:
        raise NotFound()
    now = time.time()
    finished = job["state"] in ("done", "failed", "cancelled")
    response = jsonify({
        "id": job["id"], "kind": job["kind"], "title": job["title"], "state": job["state"], "stage": job["stage"],
        "done": job["done"], "total": job["total"], "percent": job["percent"], "message": job["message"], "level": job["level"],
        "finished": finished, "next_url": _job_next_url(job) if finished else None, "auto_redirect": bool(job["next_auto"]) and job["state"] == "done",
        "elapsed": round((job["finished_at"] or now) - (job["started_at"] or job["created_at"]), 1),
        "seconds_since_update": round(now - job["updated_at"], 1),
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id: str) -> Any:
    require_auth()
    if jobs.get(job_id) is None:
        raise NotFound()
    jobs.request_cancel(job_id)
    if _wants_json():
        return jsonify({"ok": True})
    return redirect(url_for("job_page", job_id=job_id))


@app.route("/addons/<int:addon_id>/publish", methods=["POST"])
def publish_addon(addon_id: int) -> Any:
    require_auth()
    conn = get_db()
    try:
        addon = conn.execute("SELECT title FROM addons WHERE id = ?", (addon_id,)).fetchone()
    finally:
        conn.close()
    if not addon:
        raise NotFound()
    return _job_response(jobs.submit("publish", f"Publish {addon['title']}", lambda progress: _run_publish(addon_id, progress)))


@app.route("/addons/<int:addon_id>/unpublish", methods=["POST"])
def unpublish_addon(addon_id: int) -> Any:
    require_auth()
    with db_txn() as conn:
        conn.execute("UPDATE addons SET published = 0, draft = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (addon_id,))
    generate_catalog()
    flash("Add-on unpublished; it is no longer in the public catalog.", "success")
    return redirect(url_for("dashboard"))


@app.route("/addons/<int:addon_id>/delete", methods=["POST"])
def delete_addon(addon_id: int) -> Any:
    require_auth()
    with db_txn() as conn:
        addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
        if addon:
            public_file = Path(app.config["PUBLIC_ROOT"]) / "addons" / f"{addon['addon_id']}.pak"
            if public_file.exists():
                public_file.unlink(missing_ok=True)
            package_file = Path(addon["package_path"])
            if package_file.exists():
                package_file.unlink(missing_ok=True)
            if addon["source_path"]:
                kept_dir = Path(addon["source_path"]).parent
                if kept_dir.parent == Path(app.config["PERSIST_ROOT"]) / "sources":
                    shutil.rmtree(kept_dir, ignore_errors=True)
            conn.execute("DELETE FROM addons WHERE id = ?", (addon_id,))
    generate_catalog()
    flash("Add-on deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/content.json")
def public_catalog() -> Any:
    root = Path(app.config["PUBLIC_ROOT"])
    if not (root / "content.json").exists():
        generate_catalog()
    return send_from_directory(str(root), "content.json", mimetype="application/json")


@app.route("/addons/<path:filename>")
def public_addon(filename: str) -> Any:
    safe_root = _public_addons_dir()
    return send_from_directory(str(safe_root), filename)


def generate_catalog() -> None:
    conn = get_db()
    rows = conn.execute("SELECT * FROM addons WHERE published = 1 ORDER BY title ASC").fetchall()
    conn.close()
    catalog = {"addons": []}
    for row in rows:
        served = Path(app.config["PUBLIC_ROOT"]) / "addons" / f"{row['addon_id']}.pak"
        if not served.is_file():
            print(f"[catalog] skipping {row['addon_id']}: published file missing at {served}", flush=True)
            continue
        entry = {
            "download": f"addons/{row['addon_id']}.pak",
            "gamedir": row["gamedir"],
            "name": row["title"],
            "author": row["author"],
            "date": row["date"],
            "size": served.stat().st_size,
        }
        if row["description"]:
            entry["description"] = {"en": row["description"]}
        catalog["addons"].append(entry)

    public_root = Path(app.config["PUBLIC_ROOT"])
    public_root.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=str(public_root), delete=False, encoding="utf-8") as fh:
            tmp = fh.name
            fh.write(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, public_root / "content.json")
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ironwail add-on manager")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "doctor"], help="Command to run")
    args = parser.parse_args()
    if args.command == "doctor":
        ok, issues = startup_preflight()
        report = {
            "version": APP_VERSION,
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "gid": os.getgid() if hasattr(os, "getgid") else None,
            "resolved_db_path": str(_db_path()),
            "warnings": persistence_warnings(),
            "ok": ok,
            "issues": issues,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ok else 1
    ok, issues = startup_preflight()
    log_startup_banner()
    if not ok:
        print("Startup preflight failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    try:
        init_db()
    except Exception as exc:
        print(f"Database initialization failed: {exc}", file=sys.stderr)
        return 1
    startup_cleanup()
    generate_catalog()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True)
    return 0


def _update_import(token: str, **fields: Any) -> None:
    allowed = {"kind", "addon_pk", "form_json", "review", "options_json"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    conn = get_db()
    try:
        conn.execute(f"UPDATE imports SET {', '.join(k + ' = ?' for k in fields)} WHERE token = ?", (*fields.values(), token))
    finally:
        conn.close()


def _plan_import(token: str) -> None:
    """Compute and store the conversion preview for a staged import (used when a batch item needs a person to look at it)."""
    imp = _load_import(token)
    if imp is None or imp["source_format"] != "zip":
        return
    options = ConvertOptions.from_dict(json.loads(imp["options_json"] or "{}"))
    plan = plan_zip_release(Path(imp["source_path"]), options, workdir=_workdir())
    conn = get_db()
    try:
        conn.execute("UPDATE imports SET plan_json = ? WHERE token = ?", (json.dumps(plan), token))
    finally:
        conn.close()


batch_service = BatchService(app, SimpleNamespace(
    get_db=get_db, jobs=jobs, require_auth=require_auth, detect_package_format=detect_package_format,
    save_import=_save_import, load_import=_load_import, delete_import=_delete_import, update_import=_update_import,
    build_import=_build_import, plan_import=_plan_import, ReviewRequired=ReviewRequired, ImportConflict=ImportConflict,
))
batch_service.register_routes()


def create_app() -> Flask:
    return app


if __name__ == "__main__":
    raise SystemExit(main())

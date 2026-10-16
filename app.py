from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import sqlite3
import threading
import uuid
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.exceptions import BadRequest, Forbidden, NotFound, RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from tools.catalog import _compute_sha256 as _sha256_file, build_single_pak_from_zip, detect_package_format, inspect_zip_release, validate_package, validate_pak_paths

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


def _build_release_package_from_zip(source_path: Path, target: Path, staging_dir: Path) -> dict[str, Any]:
    info = build_single_pak_from_zip(source_path, target)
    print(
        f"[import] ZIP release ({info['root_mapping']}{' ' + repr(info['wrapper']) if info['wrapper'] else ''}) consolidated into single PAK with "
        f"{info['entries']} entries from {len(info['embedded_paks'])} embedded PAK(s) and {info['loose_files']} loose file(s); "
        f"{len(info['overrides'])} path(s) overridden by engine precedence",
        flush=True,
    )
    return validate_package(target)


def _stage_package(upload_source: Path, detected: str, safe_name: str) -> Tuple[Path, dict[str, Any], str]:
    """Produce a validated package in a staging file next to the final location (same filesystem).

    Nothing at the final package path is touched here; the caller swaps it in with os.replace.
    """
    staged = _packages_dir() / f".staging-{uuid.uuid4().hex}.pak"
    try:
        if detected == "zip":
            info = _build_release_package_from_zip(upload_source, staged, upload_source.parent)
            msg = "Uploaded ZIP release was inspected and packaged from its embedded PAK."
        elif detected == "pak":
            shutil.copyfile(upload_source, staged)
            print(f"[import] staged PAK unchanged at {staged}", flush=True)
            info = validate_package(staged)
            msg = "Uploaded PAK was stored unchanged."
        else:
            raise ValueError(f"Unsupported package format for {safe_name!r}; expected a ZIP or Quake PAK")
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged, info, msg


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
    if request.path.startswith("/admin") or request.path.startswith("/dashboard") or request.path.startswith("/addons/"):
        if request.method == "POST" and request.path != "/content.json":
            check_csrf()
        make_csrf_token()


@app.context_processor
def inject_globals() -> Dict[str, Any]:
    return {
        "public_base_url": app.config["BASE_URL"],
        "current_user": session.get("username"),
        "csrf_token": make_csrf_token(),
    }


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
    conn.close()
    return render_template("dashboard.html", addons=addons)


@app.route("/addons/new", methods=["GET", "POST"])
def add_addon() -> Any:
    require_auth()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        addon_id = request.form.get("addon_id", "").strip()
        gamedir = request.form.get("gamedir", "").strip()
        author = request.form.get("author", "Unknown").strip()
        date = request.form.get("date", "").strip()
        description = request.form.get("description", "").strip()
        package_file = request.files.get("package")
        local_source = request.form.get("local_source", "").strip()
        import_mode = request.form.get("import_mode", "upload")
        if not title or not addon_id or not gamedir:
            flash("Title, ID, and game dir are required.", "error")
            return render_template("addon_form.html", mode="new")

        if import_mode == "local":
            if not local_source:
                flash("Local source path is required for local import.", "error")
                return render_template("addon_form.html", mode="new")
            source_path = Path(local_source).expanduser()
            if not source_path.is_absolute():
                flash("Local source path must be absolute.", "error")
                return render_template("addon_form.html", mode="new")
            if not source_path.exists():
                flash("Local source file does not exist.", "error")
                return render_template("addon_form.html", mode="new")
            if source_path.suffix.lower() not in (".zip", ".pak"):
                flash("Local source must be a .zip or .pak file.", "error")
                return render_template("addon_form.html", mode="new")
            safe_name = source_path.name
        else:
            if not package_file or not package_file.filename:
                flash("Package file is required for upload mode.", "error")
                return render_template("addon_form.html", mode="new")
            safe_name = secure_filename(package_file.filename)
            if not safe_name:
                flash("Invalid package filename.", "error")
                return render_template("addon_form.html", mode="new")

        target = _packages_dir() / f"{addon_id}.pak"
        upload_dir = Path(app.config["UPLOAD_FOLDER"]) / uuid.uuid4().hex
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_source = upload_dir / safe_name
        staged: Path | None = None
        installed = False

        try:
            if import_mode == "local":
                print(f"[import] local source selected: {source_path}", flush=True)
                shutil.copy2(source_path, upload_source)
            else:
                print(f"[import] browser upload selected: {safe_name}", flush=True)
                package_file.save(upload_source)
            detected = detect_package_format(upload_source)
            print(f"[import] detected {detected} for {safe_name}; staging at {upload_source}", flush=True)
            conn = get_db()
            try:
                if conn.execute("SELECT 1 FROM addons WHERE addon_id = ?", (addon_id,)).fetchone():
                    raise sqlite3.IntegrityError("duplicate addon id")
                if conn.execute("SELECT 1 FROM addons WHERE gamedir = ?", (gamedir,)).fetchone():
                    raise sqlite3.IntegrityError("duplicate gamedir")
            finally:
                conn.close()
            staged, package_info, conversion_msg = _stage_package(upload_source, detected, safe_name)
            print(f"[import] validated package for {addon_id}: size={package_info['size']} sha256={package_info['sha256']}", flush=True)
            with db_txn() as conn:
                if conn.execute("SELECT 1 FROM addons WHERE addon_id = ?", (addon_id,)).fetchone():
                    raise sqlite3.IntegrityError("duplicate addon id")
                if conn.execute("SELECT 1 FROM addons WHERE gamedir = ?", (gamedir,)).fetchone():
                    raise sqlite3.IntegrityError("duplicate gamedir")
                os.replace(staged, target)
                installed = True
                conn.execute(
                    """
                    INSERT INTO addons (
                        addon_id, title, gamedir, author, date, description,
                        package_name, package_path, package_size, package_sha256, package_mtime,
                        published, draft, manifest_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, NULL)
                    """,
                    (addon_id, title, gamedir, author, date, description, target.name, str(target), package_info["size"], package_info["sha256"], int(target.stat().st_mtime)),
                )
        except (sqlite3.IntegrityError, OSError, ValueError) as exc:
            if installed:
                target.unlink(missing_ok=True)
            flash(f"Package rejected: {exc}", "error")
            return render_template("addon_form.html", mode="new")
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
            shutil.rmtree(upload_dir, ignore_errors=True)

        if import_mode == "local":
            flash(f"Imported local file and {conversion_msg.lower()} Add-on saved as a draft. Review and publish when ready.", "success")
        else:
            flash(f"{conversion_msg} Add-on saved as a draft. Review and publish when ready.", "success")
        return redirect(url_for("dashboard"))
    return render_template("addon_form.html", mode="new")


@app.route("/addons/<int:addon_id>")
def addon_detail(addon_id: int) -> Any:
    require_auth()
    conn = get_db()
    addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
    conn.close()
    if not addon:
        raise NotFound()
    return render_template("addon_detail.html", addon=addon)


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
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        addon_id_value = request.form.get("addon_id", "").strip()
        gamedir = request.form.get("gamedir", "").strip()
        author = request.form.get("author", "Unknown").strip()
        date = request.form.get("date", "").strip()
        description = request.form.get("description", "").strip()
        package_file = request.files.get("package")
        if not title or not addon_id_value or not gamedir:
            flash("Title, ID, and game dir are required.", "error")
            return render_template("addon_form.html", mode="edit", addon=dict(addon))
        if addon_id_value != addon["addon_id"] and addon["published"]:
            flash("Unpublish this add-on before changing its ID (the public download path is derived from it).", "error")
            return render_template("addon_form.html", mode="edit", addon=dict(addon))

        staged: Path | None = None
        upload_dir: Path | None = None
        package_info: dict[str, Any] | None = None
        replaced_package = False
        try:
            if package_file and package_file.filename:
                safe_name = secure_filename(package_file.filename)
                if not safe_name:
                    raise ValueError("Invalid package filename.")
                upload_dir = Path(app.config["UPLOAD_FOLDER"]) / uuid.uuid4().hex
                upload_dir.mkdir(parents=True, exist_ok=True)
                upload_source = upload_dir / safe_name
                package_file.save(upload_source)
                detected = detect_package_format(upload_source)
                staged, package_info, _msg = _stage_package(upload_source, detected, safe_name)
            current_path = Path(addon["package_path"])
            with db_txn() as conn:
                if conn.execute("SELECT 1 FROM addons WHERE addon_id = ? AND id != ?", (addon_id_value, addon_id)).fetchone():
                    raise sqlite3.IntegrityError("duplicate addon id")
                if conn.execute("SELECT 1 FROM addons WHERE gamedir = ? AND id != ?", (gamedir, addon_id)).fetchone():
                    raise sqlite3.IntegrityError("duplicate gamedir")
                if package_info is not None:
                    conn.execute(
                        "UPDATE addons SET package_size = ?, package_sha256 = ?, package_mtime = ? WHERE id = ?",
                        (package_info["size"], package_info["sha256"], package_info["mtime"], addon_id),
                    )
                conn.execute(
                    "UPDATE addons SET title = ?, addon_id = ?, gamedir = ?, author = ?, date = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (title, addon_id_value, gamedir, author, date, description, addon_id),
                )
                if staged is not None:
                    os.replace(staged, current_path)
                    replaced_package = True
        except (sqlite3.IntegrityError, OSError, ValueError) as exc:
            flash(f"Package rejected: {exc}" if package_info is not None or upload_dir is not None else f"Update rejected: {exc}", "error")
            return render_template("addon_form.html", mode="edit", addon=dict(addon))
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
            if upload_dir is not None:
                shutil.rmtree(upload_dir, ignore_errors=True)
        if addon["published"]:
            generate_catalog()
            if replaced_package:
                flash("Add-on updated. The public download still serves the previous package until you click Publish again.", "success")
            else:
                flash("Add-on updated.", "success")
        else:
            flash("Add-on updated.", "success")
        return redirect(url_for("addon_detail", addon_id=addon_id))
    return render_template("addon_form.html", mode="edit", addon=dict(addon))


@app.route("/addons/<int:addon_id>/publish", methods=["POST"])
def publish_addon(addon_id: int) -> Any:
    require_auth()
    with db_txn() as conn:
        addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
        if not addon:
            raise NotFound()
        package_path = Path(addon["package_path"])
        if not package_path.exists():
            raise FileNotFoundError("Package file is missing")
        package_info = validate_package(package_path)
        if addon["package_sha256"] and addon["package_sha256"] != package_info["sha256"]:
            raise ValueError("Stored package hash does not match the file on disk")

        public_dir = _public_addons_dir()
        public_file = public_dir / f"{addon['addon_id']}.pak"
        tmp_file = public_dir / f".publish-{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(package_path, tmp_file)
            if _sha256_file(tmp_file) != package_info["sha256"]:
                raise ValueError("Published copy does not match the validated package; publication aborted")
            os.replace(tmp_file, public_file)
        finally:
            tmp_file.unlink(missing_ok=True)

        conn.execute(
            "UPDATE addons SET published = 1, draft = 0, package_size = ?, package_sha256 = ?, package_mtime = ?, manifest_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (package_info["size"], package_info["sha256"], package_info["mtime"], str(public_file), addon_id),
        )
    generate_catalog()
    flash("Add-on published and catalog regenerated.", "success")
    return redirect(url_for("dashboard"))


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
    generate_catalog()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
    return 0


def create_app() -> Flask:
    return app


if __name__ == "__main__":
    raise SystemExit(main())

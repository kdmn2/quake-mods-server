from __future__ import annotations

import json
import os
import shutil
import tempfile
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.exceptions import BadRequest, Forbidden, NotFound
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from tools.catalog import detect_package_format, repack_zip_to_pak, validate_package, validate_pak_paths

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("APP_SECRET_KEY", "dev-secret-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", str(128 * 1024 * 1024))),
    UPLOAD_FOLDER=os.environ.get("UPLOAD_FOLDER", "/tmp/quake-addons-upload"),
    PERSIST_ROOT=os.environ.get("PERSIST_ROOT", "/tmp/quake-addons-data"),
    DB_PATH=os.environ.get("DB_PATH", ""),
    PUBLIC_ROOT=os.environ.get("PUBLIC_ROOT", "/tmp/quake-addons-public"),
    BASE_URL=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080"),
    ADMIN_PORT=os.environ.get("PORT", "5000"),
)


LOCK = threading.RLock()


def ensure_dirs() -> None:
    persist_root = Path(app.config["PERSIST_ROOT"])
    db_path = Path(app.config["DB_PATH"]) if app.config.get("DB_PATH") else persist_root / "addons.sqlite3"
    for p in [
        app.config["UPLOAD_FOLDER"],
        persist_root,
        app.config["PUBLIC_ROOT"],
        db_path.parent,
        persist_root / "packages",
        persist_root / "metadata",
        persist_root / "staging",
        Path(app.config["PUBLIC_ROOT"]) / "addons",
    ]:
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PermissionError(f"Cannot create required directory {p}: {exc}") from exc


ensure_dirs()


def get_db() -> sqlite3.Connection:
    ensure_dirs()
    db_path = Path(app.config["DB_PATH"]) if app.config.get("DB_PATH") else Path(app.config["PERSIST_ROOT"]) / "addons.sqlite3"
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
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


init_db()


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


@app.route("/health")
def health() -> Any:
    return jsonify({"ok": True, "status": "healthy"}), 200


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
        if not title or not addon_id or not gamedir or not package_file or not package_file.filename:
            flash("Title, ID, game dir, and a package upload are required.", "error")
            return render_template("addon_form.html", mode="new")

        safe_name = secure_filename(package_file.filename)
        if not safe_name:
            flash("Invalid package filename.", "error")
            return render_template("addon_form.html", mode="new")

        storage_dir = Path(app.config["PERSIST_ROOT"]) / "packages"
        storage_dir.mkdir(parents=True, exist_ok=True)
        target = storage_dir / f"{addon_id}.pak"
        upload_dir = Path(app.config["UPLOAD_FOLDER"]) / uuid.uuid4().hex
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_source = upload_dir / safe_name

        try:
            package_file.save(upload_source)
            detected = detect_package_format(upload_source)
            if detected == "zip":
                repack_zip_to_pak(upload_source, target)
                conversion_msg = "Uploaded ZIP was repacked into a Quake PAK."
            elif detected == "pak":
                shutil.copy2(upload_source, target)
                conversion_msg = "Uploaded PAK was stored unchanged."
            else:
                raise ValueError(f"Unsupported package format for {safe_name!r}; expected a ZIP or Quake PAK")

            package_info = validate_package(target)
            with db_txn() as conn:
                if conn.execute("SELECT 1 FROM addons WHERE addon_id = ?", (addon_id,)).fetchone():
                    raise sqlite3.IntegrityError("duplicate addon id")
                if conn.execute("SELECT 1 FROM addons WHERE gamedir = ?", (gamedir,)).fetchone():
                    raise sqlite3.IntegrityError("duplicate gamedir")
                conn.execute(
                    """
                    INSERT INTO addons (
                        addon_id, title, gamedir, author, date, description,
                        package_name, package_path, package_size, package_sha256, package_mtime,
                        published, draft, manifest_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, NULL)
                    """,
                    (addon_id, title, gamedir, author, date, description, package_info["filename"], str(target), package_info["size"], package_info["sha256"], package_info["mtime"]),
                )
        except (sqlite3.IntegrityError, OSError, ValueError) as exc:
            target.unlink(missing_ok=True)
            flash(f"Package rejected: {exc}", "error")
            return render_template("addon_form.html", mode="new")
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)

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
    addon = conn.execute("SELECT * FROM addons WHERE id = ?", (addon_id,)).fetchone()
    if not addon:
        conn.close()
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

        if package_file and package_file.filename:
            safe_name = secure_filename(package_file.filename)
            target = Path(app.config["PERSIST_ROOT"]) / "packages" / f"{addon_id_value}.pak"
            upload_dir = Path(app.config["UPLOAD_FOLDER"]) / uuid.uuid4().hex
            upload_dir.mkdir(parents=True, exist_ok=True)
            upload_source = upload_dir / safe_name
            try:
                package_file.save(upload_source)
                detected = detect_package_format(upload_source)
                if detected == "zip":
                    repack_zip_to_pak(upload_source, target)
                elif detected == "pak":
                    shutil.copy2(upload_source, target)
                else:
                    raise ValueError(f"Unsupported package format for {safe_name!r}; expected a ZIP or Quake PAK")
                package_info = validate_package(target)
            except (OSError, ValueError) as exc:
                target.unlink(missing_ok=True)
                flash(f"Package rejected: {exc}", "error")
                return render_template("addon_form.html", mode="edit", addon=dict(addon))
            finally:
                shutil.rmtree(upload_dir, ignore_errors=True)
            conn.execute(
                "UPDATE addons SET package_name = ?, package_path = ?, package_size = ?, package_sha256 = ?, package_mtime = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (f"{addon_id_value}.pak", str(target), package_info["size"], package_info["sha256"], package_info["mtime"], addon_id),
            )

        conn.execute(
            "UPDATE addons SET title = ?, addon_id = ?, gamedir = ?, author = ?, date = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (title, addon_id_value, gamedir, author, date, description, addon_id),
        )
        flash("Add-on updated.", "success")
        return redirect(url_for("addon_detail", addon_id=addon_id))
    conn.close()
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

        public_dir = Path(app.config["PUBLIC_ROOT"]) / "addons"
        public_dir.mkdir(parents=True, exist_ok=True)
        public_file = public_dir / f"{addon['addon_id']}.pak"
        tmp_file = public_file.with_name(public_file.name + ".tmp")
        shutil.copyfile(package_path, tmp_file)
        os.replace(tmp_file, public_file)

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
    safe_root = Path(app.config["PUBLIC_ROOT"]) / "addons"
    return send_from_directory(str(safe_root), filename)


def generate_catalog() -> None:
    conn = get_db()
    rows = conn.execute("SELECT * FROM addons WHERE published = 1 ORDER BY title ASC").fetchall()
    conn.close()
    catalog = {"addons": []}
    for row in rows:
        entry = {
            "download": f"addons/{row['addon_id']}.pak",
            "gamedir": row["gamedir"],
            "name": row["title"],
            "author": row["author"],
            "date": row["date"],
            "size": row["package_size"],
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


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PERSIST_ROOT"], exist_ok=True)
    os.makedirs(app.config["PUBLIC_ROOT"], exist_ok=True)
    generate_catalog()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))

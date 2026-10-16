import errno
import hashlib
import os
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from app import _db_path, app, generate_catalog, get_db, init_db, persistence_warnings, startup_preflight
from tests.test_publication import PAK_A, PublicationBase

ROOT = Path(__file__).resolve().parent.parent
NOT_ROOT = hasattr(os, "geteuid") and os.geteuid() != 0


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PreflightTests(PublicationBase):
    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.name.endswith((".sqlite3", ".sqlite3-journal")))

    def test_fresh_install_creates_directories_and_database_only_at_configured_path(self):
        base = self.tmp / "fresh"
        app.config.update(PERSIST_ROOT=str(base / "appdata"), PUBLIC_ROOT=str(base / "public"), UPLOAD_FOLDER=str(base / "uploads"), DB_PATH="")
        ok, issues = startup_preflight()
        self.assertEqual((ok, issues), (True, []))
        init_db()
        self.assertEqual(_db_path(), base / "appdata" / "addons.sqlite3")
        self.assertEqual([p for p in base.rglob("*.sqlite3")], [base / "appdata" / "addons.sqlite3"])

    @unittest.skipUnless(NOT_ROOT, "permission checks are meaningless as root")
    def test_unwritable_mount_is_reported_not_worked_around(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        os.chmod(locked, 0o555)
        self.addCleanup(os.chmod, locked, 0o755)
        before = dict(app.config)
        tree_before = self.tree()
        app.config.update(PERSIST_ROOT=str(locked / "appdata"), DB_PATH=str(locked / "appdata" / "addons.sqlite3"))
        ok, issues = startup_preflight()
        self.assertFalse(ok)
        text = " ".join(issues)
        self.assertIn("permission denied", text)
        self.assertIn(f"{os.getuid()}:{os.getgid()}", text)
        self.assertIn("was not modified", text)
        self.assertEqual(app.config["PERSIST_ROOT"], str(locked / "appdata"), "preflight must not relocate data")
        self.assertEqual(app.config["UPLOAD_FOLDER"], before["UPLOAD_FOLDER"])
        self.assertEqual(self.tree(), tree_before, "no database may be created in a fallback location")

    @unittest.skipUnless(NOT_ROOT, "permission checks are meaningless as root")
    def test_existing_database_without_write_permission_is_left_untouched(self):
        init_db()
        db = _db_path()
        before = sha(db)
        os.chmod(db, 0o444)
        self.addCleanup(os.chmod, db, 0o644)
        ok, issues = startup_preflight()
        self.assertFalse(ok)
        self.assertIn("permission denied on existing file", " ".join(issues))
        self.assertEqual(sha(db), before)

    def test_corrupt_database_is_reported_and_preserved(self):
        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"this is definitely not sqlite" * 50)
        before = sha(db)
        ok, issues = startup_preflight()
        self.assertFalse(ok)
        self.assertIn("not a valid SQLite database", " ".join(issues))
        self.assertEqual(sha(db), before)

    def test_locked_database_is_diagnosed_separately(self):
        init_db()
        holder = sqlite3.connect(str(_db_path()), isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        ok, issues = startup_preflight(lock_timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("locked", " ".join(issues))
        self.assertNotIn("permission", " ".join(issues))

    def test_database_directory_that_is_a_file_is_diagnosed(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("x")
        app.config.update(DB_PATH=str(blocker / "addons.sqlite3"))
        ok, issues = startup_preflight()
        self.assertFalse(ok)
        self.assertTrue(any("not a directory" in i or "not usable" in i or "missing" in i for i in issues), issues)

    def test_read_only_filesystem_is_diagnosed_separately(self):
        real_open = open

        def fake_open(file, mode="r", *a, **kw):
            if "w" in mode and Path(str(file)).name.startswith(".probe-"):
                raise OSError(errno.EROFS, "Read-only file system")
            return real_open(file, mode, *a, **kw)

        with mock.patch("builtins.open", fake_open):
            ok, issues = startup_preflight()
        self.assertFalse(ok)
        self.assertIn("read-only filesystem", " ".join(issues))

    def test_disk_full_is_diagnosed_separately(self):
        real_open = open

        def fake_open(file, mode="r", *a, **kw):
            if "w" in mode and Path(str(file)).name.startswith(".probe-"):
                raise OSError(errno.EDQUOT, "Disk quota exceeded")
            return real_open(file, mode, *a, **kw)

        with mock.patch("builtins.open", fake_open):
            ok, issues = startup_preflight()
        self.assertFalse(ok)
        self.assertIn("no space or quota", " ".join(issues))

    def test_public_and_upload_directories_are_probed_for_writability(self):
        real_open = open

        def fake_open(file, mode="r", *a, **kw):
            if "w" in mode and Path(str(file)).name.startswith(".probe-") and str(app.config["PUBLIC_ROOT"]) in str(file):
                raise PermissionError(errno.EACCES, "denied")
            return real_open(file, mode, *a, **kw)

        with mock.patch("builtins.open", fake_open):
            ok, issues = startup_preflight()
        self.assertFalse(ok)
        self.assertTrue(any(i.startswith("public root") for i in issues), issues)

    def test_unmounted_persist_root_produces_a_loss_warning(self):
        persist = os.path.realpath(app.config["PERSIST_ROOT"])
        public = os.path.realpath(app.config["PUBLIC_ROOT"])
        line = "36 25 0:30 / {} rw,relatime - ext4 /dev/sda1 rw\n"
        self.assertEqual(persistence_warnings(line.format(persist) + line.format(public)), [])
        warn = persistence_warnings(line.format("/somewhere/else"))
        self.assertEqual(len(warn), 2)
        self.assertTrue(all("LOST" in w for w in warn))
        only_public = persistence_warnings(line.format(public))
        self.assertEqual(len(only_public), 1)
        self.assertTrue(only_public[0].startswith("PERSIST_ROOT"))

    def test_unwritable_locations_are_each_reported_once(self):
        real_open = open

        def fake_open(file, mode="r", *a, **kw):
            if "w" in mode and Path(str(file)).name.startswith(".probe-"):
                raise PermissionError(errno.EACCES, "denied")
            return real_open(file, mode, *a, **kw)

        app.config.update(DB_PATH="")  # database lives directly in the persist root
        with mock.patch("builtins.open", fake_open):
            ok, issues = startup_preflight()
        self.assertFalse(ok)
        self.assertEqual([i.split(":")[0] for i in issues], ["persist root", "public root", "upload folder"])

    def test_state_survives_restart(self):
        self.upload("keep", PAK_A)
        self.publish("keep")
        # "restart": nothing in memory is reused; schema init and catalog regeneration run on the same mounts
        init_db()
        generate_catalog()
        self.assertEqual(self.row("keep")["package_sha256"], hashlib.sha256(PAK_A).hexdigest())
        self.assertEqual(self.c.get("/addons/keep.pak").data, PAK_A)
        self.assertEqual([a["download"] for a in self.catalog()], ["addons/keep.pak"])
        ok, issues = startup_preflight()
        self.assertEqual((ok, issues), (True, []))


class DeploymentFileConsistencyTests(unittest.TestCase):
    def template(self, path):
        cfg = {}
        for c in ElementTree.parse(ROOT / path).getroot().iter("Config"):
            cfg[c.get("Target")] = (c.get("Default"), (c.text or "").strip())
        return cfg

    def repository(self, path):
        return ElementTree.parse(ROOT / path).getroot().findtext("Repository")

    def test_both_unraid_templates_are_identical(self):
        self.assertEqual((ROOT / "unraid-template.xml").read_text(), (ROOT / "unraid/templates/ironwail-addons.xml").read_text())

    def test_templates_compose_and_image_agree_on_container_paths(self):
        for path in ("unraid-template.xml", "unraid/templates/ironwail-addons.xml"):
            cfg = self.template(path)
            self.assertEqual({t for t in cfg if t.startswith("/")}, {"/appdata", "/public", "/uploads", "/imports"}, path)
            self.assertEqual(cfg["PERSIST_ROOT"][1], "/appdata")
            self.assertEqual(cfg["DB_PATH"][1], "/appdata/addons.sqlite3")
            self.assertEqual(cfg["PUBLIC_ROOT"][1], "/public")
            self.assertEqual(cfg["UPLOAD_FOLDER"][1], "/uploads")
            self.assertIn("<Privileged>false</Privileged>", (ROOT / path).read_text())
        compose = (ROOT / "docker-compose.portainer.yml").read_text()
        for target in ("/appdata", "/public", "/uploads"):
            self.assertRegex(compose, rf"(?m):{target}\s*$", target)
        self.assertNotRegex(compose, r":/data\b")
        self.assertIn("image: ghcr.io/kdmn2/quake-mods-server:latest", compose)
        self.assertEqual(self.repository("unraid-template.xml"), "ghcr.io/kdmn2/quake-mods-server:latest")
        self.assertNotIn("privileged: true", compose)
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("/appdata /public /uploads", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER appuser")
        self.assertIn("unzip", dockerfile)
        for env in ("PERSIST_ROOT=/appdata", "DB_PATH=/appdata/addons.sqlite3", "PUBLIC_ROOT=/public", "UPLOAD_FOLDER=/uploads"):
            self.assertIn(env, dockerfile, "image must default to its own mount points, never /tmp")
        self.assertNotRegex(dockerfile + compose, r"chmod\s+-?R?\s*777")

    def test_the_import_mount_is_optional_read_only_and_never_mandatory(self):
        for path in ("unraid-template.xml", "unraid/templates/ironwail-addons.xml"):
            root = ElementTree.parse(ROOT / path).getroot()
            imports = [c for c in root.iter("Config") if c.get("Target") == "/imports"]
            self.assertEqual(len(imports), 1, path)
            c = imports[0]
            self.assertEqual((c.get("Type"), c.get("Mode"), c.get("Required"), c.get("Default"), (c.text or "").strip()), ("Path", "ro", "false", "", ""), path)
            self.assertEqual(root.findtext("Privileged"), "false")
        compose = (ROOT / "docker-compose.portainer.yml").read_text()
        active = [l for l in compose.splitlines() if not l.strip().startswith("#")]
        self.assertFalse(any("/imports" in l for l in active), "the import mount must be opt-in (commented out) in the compose example")
        self.assertIn(":/imports:ro", compose)
        everything = compose + (ROOT / "Dockerfile").read_text() + (ROOT / "unraid-template.xml").read_text()
        for forbidden in ("docker.sock", "privileged: true", "network_mode: host", "cap_add"):
            self.assertNotIn(forbidden, everything)
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("/imports", dockerfile)                                   # nothing mandatory: a remote-only deployment has no such folder
        self.assertNotIn("IMPORT_ROOTS", dockerfile)

    def test_every_variable_in_the_template_is_one_the_application_reads(self):
        source = (ROOT / "app.py").read_text()
        read = set(re.findall(r'(?:os\.environ\.get|_env_int|_env_float)\(\s*"([A-Z_]+)"', source))
        read |= {"PERSIST_ROOT", "DB_PATH", "PUBLIC_ROOT", "UPLOAD_FOLDER", "PORT", "APP_SECRET_KEY", "PUBLIC_BASE_URL"}
        read |= {"BATCH_DOWNLOAD_WORKERS", "BATCH_STAGING_LIMIT_GIB"}
        for path in ("unraid-template.xml", "unraid/templates/ironwail-addons.xml"):
            for c in ElementTree.parse(ROOT / path).getroot().iter("Config"):
                if c.get("Type") == "Variable":
                    self.assertIn(c.get("Target"), read, f"{path}: {c.get('Target')} is not read by the application")

    def test_documented_batch_variables_exist_in_the_code(self):
        source = (ROOT / "app.py").read_text()
        readme = (ROOT / "README.md").read_text()
        for name in ("IMPORT_ROOTS", "IMPORT_SCAN_MAX_DEPTH", "IMPORT_SCAN_MAX_ENTRIES", "IMPORT_SCAN_MAX_RESULTS", "IMPORT_PAGE_SIZE", "BATCH_MAX_ITEMS", "BATCH_MAX_PENDING",
                     "BATCH_MAX_URL_LENGTH", "BATCH_MAX_DOWNLOAD_BYTES", "BATCH_STAGING_LIMIT_GIB", "BATCH_CONNECT_TIMEOUT", "BATCH_READ_TIMEOUT", "BATCH_MAX_DURATION",
                     "BATCH_MAX_REDIRECTS", "BATCH_MAX_ATTEMPTS", "BATCH_RETRY_BASE", "BATCH_RETRY_AFTER_MAX", "BATCH_RETENTION_DAYS", "BATCH_DOWNLOAD_WORKERS"):
            self.assertIn(f'"{name}"', source, name)
            self.assertIn(name, readme, f"{name} is not documented")


if __name__ == "__main__":
    unittest.main()

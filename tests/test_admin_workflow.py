import json
import os
import tempfile
import unittest
from pathlib import Path

from app import app, get_db, generate_password_hash, init_db


class AdminWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-admin-tests-"))
        app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            PERSIST_ROOT=str(self.tmp / "data"),
            PUBLIC_ROOT=str(self.tmp / "public"),
            UPLOAD_FOLDER=str(self.tmp / "upload"),
            BASE_URL="http://localhost:8080",
        )
        self.app = app.test_client()
        os.makedirs(app.config["PERSIST_ROOT"], exist_ok=True)
        os.makedirs(app.config["PUBLIC_ROOT"], exist_ok=True)
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        self._create_user()

    def _create_user(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM addons")
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("admin", generate_password_hash("secret")))
        conn.commit()
        conn.close()

    def _csrf(self):
        self.app.get("/login")
        with self.app.session_transaction() as sess:
            return sess.get("csrf_token")

    def _login(self):
        return self.app.post("/login", data={"username": "admin", "password": "secret", "csrf_token": self._csrf()}, follow_redirects=True)

    def _make_pak(self, path: Path, names=None) -> None:
        import struct
        names = names or ["maps/e1m1.bsp"]
        records = bytearray()
        for name in names:
            encoded = name.encode("utf-8")
            record = encoded.ljust(56, b"\0") + struct.pack("<II", 0, 0) + struct.pack("<I", 0)
            records.extend(record)
        payload = b"PACK" + struct.pack("<II", 12, len(records)) + bytes(records)
        path.write_bytes(payload)

    def _make_zip(self, path: Path, files: dict[str, bytes]) -> None:
        import zipfile
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)

    def test_admin_login_and_publish(self):
        self._login()
        self.assertEqual(self.app.get("/dashboard").status_code, 200)

        pkg = self.tmp / "sample.pak"
        self._make_pak(pkg)
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Sample Mod",
                "addon_id": "samplemod",
                "gamedir": "samplemod",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Example",
                "package": (pkg.open("rb"), "samplemod.pak"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Add-on saved as a draft", res.data)

        conn = get_db()
        row = conn.execute("SELECT * FROM addons WHERE addon_id = ?", ("samplemod",)).fetchone()
        self.assertIsNotNone(row)
        conn.close()

        res = self.app.post(f"/addons/{row['id']}/publish", data={"csrf_token": self._csrf()}, follow_redirects=True)
        self.assertIn(b"published", res.data.lower())

        catalog = self.app.get("/content.json")
        self.assertEqual(catalog.status_code, 200)
        data = json.loads(catalog.data.decode("utf-8"))
        self.assertEqual(data["addons"][0]["gamedir"], "samplemod")

        download = self.app.get("/addons/samplemod.pak")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, pkg.read_bytes())

    def test_unpublished_draft_not_listed(self):
        self._login()
        pkg = self.tmp / "draft.pak"
        self._make_pak(pkg)
        self.app.post(
            "/addons/new",
            data={
                "title": "Draft Mod",
                "addon_id": "draftmod",
                "gamedir": "draftmod",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Draft only",
                "package": (pkg.open("rb"), "draftmod.pak"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        catalog = self.app.get("/content.json")
        data = json.loads(catalog.data.decode("utf-8"))
        self.assertEqual(data["addons"], [])

    def test_invalid_archive_rejected(self):
        self._login()
        bad = self.tmp / "bad.pak"
        bad.write_bytes(b"not a pak")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Bad Mod",
                "addon_id": "badmod",
                "gamedir": "badmod",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Bad",
                "package": (bad.open("rb"), "bad.pak"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Package rejected", res.data)
        self.assertEqual(json.loads(self.app.get("/content.json").data.decode("utf-8"))["addons"], [])

    def test_zip_upload_is_repacked_to_pak(self):
        self._login()
        zip_path = self.tmp / "sample.zip"
        self._make_zip(zip_path, {"maps/e1m1.bsp": b"abc", "progs.dat": b"def"})
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Zip Mod",
                "addon_id": "zipmod",
                "gamedir": "zipmod",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "ZIP upload",
                "package": (zip_path.open("rb"), "source.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"repacked into a Quake PAK", res.data)
        conn = get_db()
        row = conn.execute("SELECT * FROM addons WHERE addon_id = ?", ("zipmod",)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertTrue(Path(row["package_path"]).exists())
        self.assertEqual(Path(row["package_path"]).read_bytes()[:4], b"PACK")

    def test_content_based_detection_not_extension_based(self):
        self._login()
        masquerade = self.tmp / "fake.pak"
        self._make_zip(masquerade, {"maps/e1m1.bsp": b"abc"})
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Masquerade",
                "addon_id": "fakezip",
                "gamedir": "fakezip",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Looks like pak but is zip",
                "package": (masquerade.open("rb"), "fake.pak"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"repacked into a Quake PAK", res.data)

    def test_unauthorized_admin_actions_rejected(self):
        self.assertEqual(self.app.get("/dashboard").status_code, 403)
        self.assertEqual(self.app.post("/addons/new").status_code, 403)

    def test_duplicate_ids_rejected(self):
        self._login()
        pkg = self.tmp / "dup.pak"
        self._make_pak(pkg)
        payload = {
            "title": "Duplicate",
            "addon_id": "dupmod",
            "gamedir": "dupmod",
            "author": "Tester",
            "date": "2026/10/15",
            "description": "Dup",
            "package": (pkg.open("rb"), "dup.pak"),
            "csrf_token": self._csrf(),
        }
        self.app.post("/addons/new", data=payload, content_type="multipart/form-data", follow_redirects=True)
        pkg2 = self.tmp / "dup2.pak"
        self._make_pak(pkg2)
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Duplicate2",
                "addon_id": "dupmod",
                "gamedir": "dupmod2",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Dup2",
                "package": (pkg2.open("rb"), "dup2.pak"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Package rejected", res.data)


if __name__ == "__main__":
    unittest.main()

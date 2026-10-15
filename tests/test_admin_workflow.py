import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from app import app


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
        from app import get_db, generate_password_hash, init_db
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM users")
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("admin", generate_password_hash("secret")))
        conn.commit()
        conn.close()

    def _login(self):
        return self.app.post("/login", data={"username": "admin", "password": "secret"}, follow_redirects=True)

    def _make_pak(self, path: Path) -> None:
        import struct
        record = b"maps/e1m1.bsp".ljust(56, b"\0") + struct.pack("<II", 0, 0) + struct.pack("<I", 0)
        payload = b"PACK" + struct.pack("<II", 12, len(record)) + record
        path.write_bytes(payload)

    def test_admin_login_and_publish(self):
        self._login()
        res = self.app.get("/dashboard")
        self.assertEqual(res.status_code, 200)

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
                "csrf_token": "test-token",
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Add-on saved as a draft", res.data)

        # Add package as a file.
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Sample Mod 2",
                "addon_id": "samplemod2",
                "gamedir": "samplemod2",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Example",
                "package": (pkg.open("rb"), "samplemod2.pak"),
                "csrf_token": "test-token",
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)

        conn = __import__("app").get_db()
        row = conn.execute("SELECT * FROM addons WHERE addon_id = ?", ("samplemod2",)).fetchone()
        self.assertIsNotNone(row)
        conn.close()

        res = self.app.post(f"/addons/{row['id']}/publish", follow_redirects=True)
        self.assertIn(b"published", res.data.lower())

        catalog = self.app.get("/content.json")
        self.assertEqual(catalog.status_code, 200)
        data = json.loads(catalog.data.decode("utf-8"))
        self.assertEqual(data["addons"][0]["gamedir"], "samplemod2")

    def test_unpublished_draft_not_listed(self):
        pkg = self.tmp / "draft.pak"
        self._make_pak(pkg)
        self._login()
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
                "csrf_token": "test-token",
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        catalog = self.app.get("/content.json")
        data = json.loads(catalog.data.decode("utf-8"))
        self.assertEqual(data["addons"], [])


if __name__ == "__main__":
    unittest.main()

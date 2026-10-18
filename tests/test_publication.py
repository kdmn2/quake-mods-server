import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app import app, generate_catalog, get_db, generate_password_hash, init_db
from tests.test_zip_conversion import make_pak_bytes, parse_pak, write_zip


class PublicationBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._config_at_start = dict(app.config)

    @classmethod
    def tearDownClass(cls):
        # Regression: a test once left MAX_CONTENT_LENGTH=1 behind and broke later tests.
        for key in ("MAX_CONTENT_LENGTH", "PERSIST_ROOT", "PUBLIC_ROOT", "UPLOAD_FOLDER", "BASE_URL", "SECRET_KEY"):
            assert app.config[key] == cls._config_at_start[key], f"test leaked app.config[{key!r}]"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-pub-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        saved = dict(app.config)
        self.addCleanup(lambda: (app.config.clear(), app.config.update(saved)))
        app.config.update(
            TESTING=True, SECRET_KEY="test-secret", PERSIST_ROOT=str(self.tmp / "data"),
            PUBLIC_ROOT=str(self.tmp / "public"), UPLOAD_FOLDER=str(self.tmp / "upload"), BASE_URL="http://localhost:8080",
        )
        for key in ("PERSIST_ROOT", "PUBLIC_ROOT", "UPLOAD_FOLDER"):
            os.makedirs(app.config[key], exist_ok=True)
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM addons")
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("admin", generate_password_hash("secret")))
        conn.commit()
        conn.close()
        self.c = app.test_client()
        self.c.post("/login", data={"username": "admin", "password": "secret", "csrf_token": self.csrf()})

    def csrf(self):
        self.c.get("/login")
        with self.c.session_transaction() as sess:
            return sess.get("csrf_token")

    def upload(self, addon_id, data, filename="p.pak", gamedir=None, path="/addons/new", **extra):
        f = self.tmp / filename
        f.write_bytes(data)
        form = {"title": addon_id, "addon_id": addon_id, "gamedir": gamedir or addon_id, "author": "T", "date": "2026/10/16",
                "description": "d", "package": (f.open("rb"), filename), "csrf_token": self.csrf()}
        form.update(extra)
        return self.c.post(path, data=form, content_type="multipart/form-data", follow_redirects=True)

    def row(self, addon_id):
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM addons WHERE addon_id = ?", (addon_id,)).fetchone()
        finally:
            conn.close()

    def publish(self, addon_id):
        return self.c.post(f"/addons/{self.row(addon_id)['id']}/publish", data={"csrf_token": self.csrf()}, follow_redirects=True)

    def catalog(self):
        return json.loads(self.c.get("/content.json").data)["addons"]

    def leftovers(self):
        found = []
        for root in (Path(app.config["PERSIST_ROOT"]) / "packages", Path(app.config["PUBLIC_ROOT"]) / "addons"):
            if root.is_dir():
                found += [p.name for p in root.iterdir() if p.name.startswith(".") or p.name.endswith(".tmp")]
        return found


PAK_A = make_pak_bytes([("maps/a.bsp", b"AAAA")])
PAK_B = make_pak_bytes([("maps/b.bsp", b"BBBBBBBB")])


class PublicationTests(PublicationBase):
    def test_duplicate_id_upload_never_touches_existing_package(self):
        self.upload("dup", PAK_A)
        self.publish("dup")
        row = self.row("dup")
        before_pkg = Path(row["package_path"]).read_bytes()
        before_pub = self.c.get("/addons/dup.pak").data
        res = self.upload("dup", PAK_B, gamedir="other")
        self.assertIn(b"Package rejected", res.data)
        self.assertEqual(Path(row["package_path"]).read_bytes(), before_pkg)
        self.assertEqual(self.c.get("/addons/dup.pak").data, before_pub)
        self.assertEqual(self.row("dup")["package_sha256"], hashlib.sha256(PAK_A).hexdigest())
        self.assertEqual(self.leftovers(), [])

    def test_duplicate_gamedir_upload_rejected_without_side_effects(self):
        self.upload("one", PAK_A, gamedir="shared")
        res = self.upload("two", PAK_B, gamedir="shared")
        self.assertIn(b"Package rejected", res.data)
        self.assertIsNone(self.row("two"))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse((Path(app.config["PERSIST_ROOT"]) / "packages" / "two.pak").exists())

    def test_failed_replacement_keeps_unpublished_package(self):
        self.upload("draft", PAK_A)
        row = self.row("draft")
        res = self.upload("draft", b"PACK-but-not-really", filename="bad.pak", path=f"/addons/{row['id']}/edit")
        self.assertIn(b"Package rejected", res.data)
        self.assertEqual(Path(row["package_path"]).read_bytes(), PAK_A)
        self.assertEqual(self.row("draft")["package_sha256"], hashlib.sha256(PAK_A).hexdigest())
        self.assertEqual(self.leftovers(), [])

    def test_failed_zip_replacement_keeps_published_download_and_catalog(self):
        self.upload("pub", PAK_A)
        self.publish("pub")
        row = self.row("pub")
        bad = self.tmp / "bad.zip"
        write_zip(bad, [("moda/maps/a", b"a"), ("modb/maps/b", b"b")])
        res = self.upload("pub", bad.read_bytes(), filename="bad.zip", path=f"/addons/{row['id']}/edit")
        self.assertIn(b"ambiguous layout", res.data)
        self.assertEqual(self.c.get("/addons/pub.pak").data, PAK_A)
        self.assertEqual(Path(row["package_path"]).read_bytes(), PAK_A)
        self.assertEqual(self.catalog()[0]["size"], len(PAK_A))
        self.assertEqual(self.leftovers(), [])

    def test_valid_replacement_goes_live_only_after_republish(self):
        self.upload("live", PAK_A)
        self.publish("live")
        row = self.row("live")
        res = self.upload("live", PAK_B, path=f"/addons/{row['id']}/edit")
        self.assertIn(b"until you click Publish again", res.data)
        self.assertEqual(Path(row["package_path"]).read_bytes(), PAK_B)
        self.assertEqual(self.c.get("/addons/live.pak").data, PAK_A)
        self.assertEqual(self.catalog()[0]["size"], len(PAK_A), "catalog must describe the bytes actually served")
        self.publish("live")
        self.assertEqual(self.c.get("/addons/live.pak").data, PAK_B)
        self.assertEqual(self.catalog()[0]["size"], len(PAK_B))
        self.assertEqual(self.leftovers(), [])

    def test_catalog_size_comes_from_served_file_not_database(self):
        self.upload("sz", PAK_A)
        self.publish("sz")
        conn = get_db()
        conn.execute("UPDATE addons SET package_size = 1 WHERE addon_id = 'sz'")
        conn.commit()
        conn.close()
        generate_catalog()
        entry = self.catalog()[0]
        self.assertEqual(entry["size"], len(self.c.get("/addons/sz.pak").data))

    def test_catalog_omits_entries_whose_public_file_is_missing(self):
        self.upload("gone", PAK_A)
        self.publish("gone")
        (Path(app.config["PUBLIC_ROOT"]) / "addons" / "gone.pak").unlink()
        generate_catalog()
        self.assertEqual(self.catalog(), [])

    def test_editing_to_another_addons_id_is_rejected_cleanly(self):
        self.upload("first", PAK_A)
        self.upload("second", PAK_B)
        second = self.row("second")
        res = self.upload("first", PAK_A, path=f"/addons/{second['id']}/edit", gamedir="second")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"duplicate addon id", res.data)
        self.assertEqual(Path(self.row("first")["package_path"]).read_bytes(), PAK_A)
        self.assertEqual(Path(second["package_path"]).read_bytes(), PAK_B)

    def test_id_of_published_addon_cannot_change(self):
        self.upload("fixed", PAK_A)
        self.publish("fixed")
        row = self.row("fixed")
        res = self.c.post(f"/addons/{row['id']}/edit", data={"title": "x", "addon_id": "renamed", "gamedir": "fixed", "csrf_token": self.csrf()}, follow_redirects=True)
        self.assertIn(b"Unpublish this add-on before changing its ID", res.data)
        self.assertEqual(self.row("fixed")["addon_id"], "fixed")
        self.assertEqual(self.catalog()[0]["download"], "addons/fixed.pak")

    def test_release_zip_over_http_matches_engine_precedence(self):
        pak0 = make_pak_bytes([("a", b"p0-a"), ("b", b"p0-b"), ("c", b"p0-c")])
        pak1 = make_pak_bytes([("b", b"p1-b")])
        z = self.tmp / "rel.zip"
        write_zip(z, [("c", b"loose-c"), ("pak1.pak", pak1), ("e", b"loose-e"), ("pak0.pak", pak0), ("pun.txt", b"doc")])
        res = self.upload("rel", z.read_bytes(), filename="rel.zip")
        self.assertIn(b"packaged from", res.data)
        self.publish("rel")
        entry = self.catalog()[0]
        served = self.c.get("/" + entry["download"]).data
        self.assertEqual(entry["size"], len(served))
        self.assertEqual(self.row("rel")["package_sha256"], hashlib.sha256(served).hexdigest())
        out = self.tmp / "served.pak"
        out.write_bytes(served)
        self.assertEqual(parse_pak(out), {"a": b"p0-a", "b": b"p1-b", "c": b"p0-c", "e": b"loose-e", "pun.txt": b"doc"})

    def test_standalone_pak_over_engine_limits_is_rejected(self):
        res = self.upload("big", make_pak_bytes([(f"f{i}", b"") for i in range(2049)]))
        self.assertIn(b"Package rejected", res.data)
        self.assertIn(b"2048", res.data)
        self.assertIsNone(self.row("big"))


if __name__ == "__main__":
    unittest.main()

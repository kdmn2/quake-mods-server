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


# ---- integration: ZIP upload -> draft -> reprocess -> publish -> download (Stage 3) -------------

class ReprocessThenPublishIntegration(PublicationBase):
    """Full lifecycle on isolated storage:

      1. Upload a ZIP with a mix of keepable content and authoring/junk/docs entries.
      2. The package lands as an unpublished draft.
      3. Reprocess the stored PAK in place with cleanup=authoring AND reencode_textures=1.
      4. Verify the staged output (independently inspected) shows the expected removals and the
         expected bytes of the kept content.
      5. Publish the result.
      6. Download the public artifact and verify final bytes for the kept content + the PNG conversion.
      7. Confirm that a metadata-only edit between steps did NOT trigger another conversion.
    """

    def _zip_with_mixed_content(self):
        """A ZIP that maps cleanly to a PAK with maps/, gfx/, and authoring/junk entries."""
        from io import BytesIO
        import zipfile
        # TGA: 8x8 24bpp top-left so PNG conversion produces a smaller file
        tga = bytes([0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 8, 0, 24, 32]) + (b"\x00\x00\xFF" * 8) * 8
        zbuf = BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            zf.writestr("maps/start.bsp", b"BSP-DATA-START")
            zf.writestr("maps/end.bsp", b"BSP-DATA-END")
            zf.writestr("gfx/wall.tga", tga)
            zf.writestr("mapsrc/working.map", b"working.map content")
            zf.writestr("src/progs.qc", b"// progs.qc")
            zf.writestr("Thumbs.db", b"thumbs")
        return zbuf.getvalue()

    def test_full_lifecycle_reprocess_then_publish(self):
        zip_bytes = self._zip_with_mixed_content()
        res = self.upload("lifecycle", zip_bytes, filename="lifecycle.zip", path="/addons/new")
        self.assertEqual(res.status_code, 200)
        row = self.row("lifecycle")
        self.assertIsNotNone(row)
        self.assertEqual(row["published"], 0)
        self.assertEqual(row["draft"], 1)
        original_sha = row["package_sha256"]

        # The as-uploaded PAK keeps every loose file (no cleanup is applied by default for ZIPs)
        from tools.catalog import read_pak_directory
        entries_before = sorted(e["path"] for e in read_pak_directory(Path(row["package_path"])))
        self.assertIn("maps/start.bsp", entries_before)
        self.assertIn("mapsrc/working.map", entries_before)                        # authoring junk still present
        self.assertIn("src/progs.qc", entries_before)
        self.assertIn("Thumbs.db", entries_before)
        self.assertIn("gfx/wall.tga", entries_before)                                # TGA still a TGA

        # Reprocess in place: apply 'authoring' cleanup AND reencode_textures
        res = self.c.post(f"/addons/{row['id']}/reprocess",
                          data={"csrf_token": self.csrf(), "exclude_presets_submitted": "1",
                                "exclude_presets": ["authoring"], "reencode_textures": "1",
                                "reencode_skyboxes": "0"})
        self.assertEqual(res.status_code, 302)
        row_after_reproc = self.row("lifecycle")
        self.assertNotEqual(row_after_reproc["package_sha256"], original_sha)

        # Independently inspect: required entries kept with correct bytes; authoring entries removed; TGA→PNG
        entries_after = sorted(e["path"] for e in read_pak_directory(Path(row_after_reproc["package_path"])))
        self.assertIn("maps/start.bsp", entries_after)
        self.assertIn("maps/end.bsp", entries_after)
        self.assertNotIn("mapsrc/working.map", entries_after)                     # authoring preset
        self.assertNotIn("src/progs.qc", entries_after)                            # authoring preset (src/)
        self.assertIn("Thumbs.db", entries_after)                                  # 'junk' preset NOT selected — kept
        self.assertNotIn("gfx/wall.tga", entries_after)                            # replaced by PNG
        self.assertIn("gfx/wall.png", entries_after)
        with open(row_after_reproc["package_path"], "rb") as f:
            for name, expected in [("maps/start.bsp", b"BSP-DATA-START"), ("maps/end.bsp", b"BSP-DATA-END")]:
                entry = next(e for e in read_pak_directory(Path(row_after_reproc["package_path"])) if e["path"] == name)
                f.seek(entry["offset"])
                self.assertEqual(f.read(entry["size"]), expected, f"bytes of {name} changed!")

        # Metadata-only edit does not re-run conversion (sanity check before publish)
        pre_pub_sha = row_after_reproc["package_sha256"]
        res = self.c.post(f"/addons/{row['id']}/edit",
                          data={"title": "Lifecycle renamed", "addon_id": "lifecycle", "gamedir": "lifecycle",
                                "author": "T", "date": "", "description": "", "csrf_token": self.csrf()})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.row("lifecycle")["package_sha256"], pre_pub_sha)    # no re-conversion
        self.assertEqual(self.row("lifecycle")["title"], "Lifecycle renamed")       # metadata updated

        # Publish
        pub_res = self.c.post(f"/addons/{row['id']}/publish", data={"csrf_token": self.csrf()}, follow_redirects=True)
        self.assertEqual(pub_res.status_code, 200)
        self.assertEqual(self.row("lifecycle")["published"], 1)
        self.assertEqual(self.row("lifecycle")["draft"], 0)

        # Download the public artifact and verify the FINAL bytes match expectations
        cat = self.catalog()
        entry = next(e for e in cat if e["gamedir"] == "lifecycle")
        download = self.c.get("/" + entry["download"]).data
        self.assertEqual(len(download), entry["size"])                              # catalog size matches download
        # parse_pak reads from a path; dump the download to a sibling tempfile for inspection
        download_path = self.tmp / "lifecycle-download.pak"
        download_path.write_bytes(download)
        downloaded_entries = parse_pak(download_path)
        self.assertIn("maps/start.bsp", downloaded_entries)
        self.assertIn("maps/end.bsp", downloaded_entries)
        self.assertEqual(downloaded_entries["maps/start.bsp"], b"BSP-DATA-START")  # bytes preserved end-to-end
        self.assertEqual(downloaded_entries["maps/end.bsp"], b"BSP-DATA-END")
        self.assertIn("gfx/wall.png", downloaded_entries)
        self.assertNotIn("gfx/wall.tga", downloaded_entries)                       # TGA replaced by PNG in the served file
        self.assertNotIn("mapsrc/working.map", downloaded_entries)                  # authoring removed in served file
        self.assertNotIn("src/progs.qc", downloaded_entries)                        # authoring removed in served file

        # Verify the conversion report records the actual operation
        report = json.loads(self.row("lifecycle")["conversion_report"])
        self.assertEqual(report["kind"], "pak-reprocess")
        reenc = report.get("reencoded") or {}
        self.assertGreaterEqual(reenc.get("converted", 0), 1)
        excl = report.get("excluded") or {}
        self.assertGreaterEqual(excl.get("total", 0), 2)                            # mapsrc + src at minimum

    def test_reprocess_failure_keeps_old_artifact_and_publication(self):
        """§D failure semantics: a structural failure mid-reprocess leaves the artifact, hash,
        publication state, and metadata untouched."""
        pak = make_pak_bytes([("maps/a.bsp", b"BSPDATA")])
        res = self.upload("keep", pak)
        self.assertEqual(res.status_code, 200)
        row = self.row("keep")
        before_size = Path(row["package_path"]).stat().st_size
        before_bytes = Path(row["package_path"]).read_bytes()
        before_sha = row["package_sha256"]
        before_title = row["title"]
        # Use the helper directly with an options object that excludes everything
        from app import _reprocess_existing_pak
        from tools.convert_options import ConvertOptions
        with self.assertRaises(ValueError):
            _reprocess_existing_pak(row["id"], ConvertOptions(exclude_patterns=("*.bsp",)))
        # Everything is unchanged
        self.assertEqual(Path(row["package_path"]).stat().st_size, before_size)
        self.assertEqual(Path(row["package_path"]).read_bytes(), before_bytes)
        after = self.row("keep")
        self.assertEqual(after["package_sha256"], before_sha)
        self.assertEqual(after["title"], before_title)
        self.assertEqual(after["published"], 0)
        self.assertEqual(after["draft"], 1)


# ---- integration: ZIP with overlong entry -> default-skip -> publish -> download (overlong policy)

class OverlongNameZipIntegration(PublicationBase):
    """Full lifecycle on isolated storage with the user's example overlong path:

      1. Upload a ZIP with valid assets AND a 63-byte AlbumArt_…/UUID…/Large.jpg entry.
      2. The package lands as a draft (default skip-on, so the overlong is recorded as
         an exclusion rather than blocking the upload).
      3. The stored PAK contains the keepable entries with the right bytes; the overlong
         path is absent; the conversion report names it.
      4. Publish and download the public artifact; the served PAK matches.
    """

    LONG_NAME = "music/AlbumArt_{B5020207-474E-4720-D903-8139E4180600}_Large.jpg"
    KEEPABLES = [("maps/start.bsp", b"BSP-DATA-START"),
                 ("maps/end.bsp", b"BSP-DATA-END"),
                 ("sound/keep.wav", b"RIFF-WAVE-DATA")]

    def _zip_with_mixed_content(self):
        from io import BytesIO
        import zipfile
        zbuf = BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            for name, data in self.KEEPABLES:
                zf.writestr(name, data)
            zf.writestr(self.LONG_NAME, b"ALBUM-ART-BYTES")
        return zbuf.getvalue()

    def test_full_lifecycle_skip_then_publish(self):
        zip_bytes = self._zip_with_mixed_content()
        res = self.upload("overlong", zip_bytes, filename="overlong.zip", path="/addons/new")
        self.assertEqual(res.status_code, 200)
        row = self.row("overlong")
        self.assertIsNotNone(row)
        self.assertEqual(row["published"], 0)
        self.assertEqual(row["draft"], 1)
        # The PAK was built successfully (the overlong was skipped, not blocking).
        from tools.catalog import read_pak_directory
        kept = sorted(e["path"] for e in read_pak_directory(Path(row["package_path"])))
        self.assertEqual(kept, sorted(name for name, _ in self.KEEPABLES))
        # Each kept entry has the right bytes.
        pak_path = Path(row["package_path"])
        entries = {e["path"]: e for e in read_pak_directory(pak_path)}
        for name, expected in self.KEEPABLES:
            with open(pak_path, "rb") as f:
                f.seek(entries[name]["offset"])
                self.assertEqual(f.read(entries[name]["size"]), expected,
                                 f"bytes of {name} changed")
        # The overlong name is in the conversion report with the documented wording.
        report = json.loads(row["conversion_report"])
        self.assertIsNotNone(report["excluded"])
        self.assertEqual(report["excluded"]["total"], 1)
        # The exclusion rule text names the entry's byte length and the limit.
        rule_text = next(iter(report["excluded"]["by_rule"]))
        self.assertIn(self.LONG_NAME, report["excluded"]["sample"][0]["path"])
        self.assertIn(str(len(self.LONG_NAME.encode("utf-8"))), rule_text)
        self.assertIn("55", rule_text)
        # The on-disk package size and hash match the actual bytes.
        self.assertEqual(row["package_size"], pak_path.stat().st_size)
        # Publish and download.
        pub_res = self.c.post(f"/addons/{row['id']}/publish",
                              data={"csrf_token": self.csrf()}, follow_redirects=True)
        self.assertEqual(pub_res.status_code, 200)
        self.assertEqual(self.row("overlong")["published"], 1)
        # The catalog entry has the same size and download URL.
        cat = self.catalog()
        entry = next(e for e in cat if e["gamedir"] == "overlong")
        download = self.c.get("/" + entry["download"]).data
        self.assertEqual(len(download), entry["size"])
        # Independent parse: keepable entries present with right bytes, overlong absent.
        download_path = self.tmp / "overlong-download.pak"
        download_path.write_bytes(download)
        downloaded = parse_pak(download_path)
        self.assertEqual(set(downloaded.keys()), set(name for name, _ in self.KEEPABLES))
        for name, expected in self.KEEPABLES:
            self.assertEqual(downloaded[name], expected)
        # The overlong AlbumArt entry is not in the served file.
        self.assertNotIn(self.LONG_NAME, downloaded)

    def test_strict_mode_rejects_the_same_zip_with_actionable_message(self):
        """The same ZIP under explicit strict mode (skip_overlong_names=False) is rejected
        outright with a clear message that names the offending path."""
        from io import BytesIO
        import zipfile
        zbuf = BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            for name, data in self.KEEPABLES:
                zf.writestr(name, data)
            zf.writestr(self.LONG_NAME, b"ALBUM-ART-BYTES")
        zip_bytes = zbuf.getvalue()
        res = self.upload("strict_zip", zip_bytes, filename="strict.zip", path="/addons/new",
                          skip_overlong_names="0")
        self.assertEqual(res.status_code, 200)
        body = res.data if isinstance(res.data, bytes) else res.data.encode("utf-8", "replace")
        # The failure message names the entry and the byte count vs limit.
        self.assertIn(b"Entry name too long for PAK", body)
        self.assertIn(b"63", body)                                   # the actual byte length
        self.assertIn(b"55", body)                                   # the limit
        # Nothing was installed.
        self.assertIsNone(self.row("strict_zip"))

    def test_only_overlong_entries_in_zip_refuses_to_write_an_empty_package(self):
        """A ZIP whose only entry is overlong is rejected with a clear empty-package message."""
        from io import BytesIO
        import zipfile
        zbuf = BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            zf.writestr(self.LONG_NAME, b"X")
        zip_bytes = zbuf.getvalue()
        res = self.upload("empty_after_skip", zip_bytes, filename="empty.zip", path="/addons/new")
        self.assertEqual(res.status_code, 200)
        body = res.data if isinstance(res.data, bytes) else res.data.encode("utf-8", "replace")
        self.assertIn(b"Refusing to write an empty package", body)
        self.assertIn(b"too long", body)
        self.assertIsNone(self.row("empty_after_skip"))


if __name__ == "__main__":
    unittest.main()

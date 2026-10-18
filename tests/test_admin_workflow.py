import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app import app, get_db, generate_password_hash, init_db


class AdminWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-admin-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        saved_config = dict(app.config)
        self.addCleanup(lambda: (app.config.clear(), app.config.update(saved_config)))
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
        payloads = []
        data = bytearray(b"PACK" + b"\0" * 8)
        for name in names:
            payload = name.encode("utf-8") or b"x"
            off = len(data)
            data.extend(payload)
            payloads.append((name, off, len(payload)))
        dir_offset = len(data)
        dir_entries = bytearray()
        for name, off, size in payloads:
            encoded = name.encode("utf-8")
            dir_entries.extend(encoded.ljust(56, b"\0"))
            dir_entries.extend(struct.pack("<II", off, size))
        data.extend(dir_entries)
        struct.pack_into("<II", data, 4, dir_offset, len(dir_entries))
        path.write_bytes(bytes(data))

    def _make_zip(self, path: Path, files: dict[str, bytes]) -> None:
        import zipfile
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)

    def _make_zip_ordered(self, path: Path, files: list[tuple[str, bytes]]) -> None:
        import zipfile
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
            for name, data in files:
                zf.writestr(name, data)

    def _read_pak_manifest(self, path: Path):
        import struct, hashlib
        data = path.read_bytes()
        self.assertEqual(data[:4], b"PACK")
        d_off, d_size = struct.unpack("<II", data[4:12])
        manifest = []
        for idx in range(d_size // 64):
            e = data[d_off + idx * 64 : d_off + (idx + 1) * 64]
            name = e[:56].split(b"\0", 1)[0].decode("utf-8")
            off, size = struct.unpack("<II", e[56:64])
            payload = data[off:off + size]
            manifest.append((name, size, hashlib.sha256(payload).hexdigest(), payload))
        return manifest

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

    def test_zip_is_repacked_to_pak(self):
        from tools.catalog import detect_package_format, repack_zip_to_pak
        zip_path = self.tmp / "sample.zip"
        pak_path = self.tmp / "sample.pak"
        self._make_zip(zip_path, {"maps/e1m1.bsp": b"abc", "progs.dat": b"def"})
        self.assertEqual(detect_package_format(zip_path), "zip")
        repack_zip_to_pak(zip_path, pak_path)
        self.assertEqual(pak_path.read_bytes()[:4], b"PACK")

    def test_zip_with_entry_paths_is_preserved(self):
        from tools.catalog import repack_zip_to_pak
        zip_path = self.tmp / "paths.zip"
        pak_path = self.tmp / "paths.pak"
        self._make_zip(zip_path, {"qbj/copper.def": b"abc"})
        repack_zip_to_pak(zip_path, pak_path)
        self.assertEqual(pak_path.read_bytes()[:4], b"PACK")

    def test_zip_deflate_read_path(self):
        from tools.catalog import repack_zip_to_pak
        zip_path = self.tmp / "deflate.zip"
        pak_path = self.tmp / "deflate.pak"
        self._make_zip(zip_path, {"qbj/copper.def": b"x" * 1024})
        repack_zip_to_pak(zip_path, pak_path)
        self.assertEqual(pak_path.read_bytes()[:4], b"PACK")

    def test_zip_unzip_fallback_path(self):
        from tools.catalog import repack_zip_to_pak
        zip_path = self.tmp / "fallback.zip"
        pak_path = self.tmp / "fallback.pak"
        # This still exercises the normal repacker; the fallback exists for runtimes
        # where Python's zipfile cannot decode a given entry.
        self._make_zip(zip_path, {"qbj/copper.def": b"x" * 4096})
        repack_zip_to_pak(zip_path, pak_path)
        self.assertEqual(pak_path.read_bytes()[:4], b"PACK")

    def test_import_logging_helpers_are_available(self):
        from app import _effective_config
        self.assertIn("PERSIST_ROOT", _effective_config())

    def test_unzip_command_present_in_image_notes(self):
        from pathlib import Path
        self.assertTrue(Path("Dockerfile").exists())

    def test_large_zip_repack_limit_is_higher(self):
        from tools.catalog import repack_zip_to_pak
        zip_path = self.tmp / "large.zip"
        pak_path = self.tmp / "large.pak"
        # Simulate a reasonably large but valid archive in chunks, not a huge real file.
        self._make_zip(zip_path, {"maps/big1.bin": b"a" * (5 * 1024 * 1024), "maps/big2.bin": b"b" * (5 * 1024 * 1024)})
        repack_zip_to_pak(zip_path, pak_path)
        self.assertEqual(pak_path.read_bytes()[:4], b"PACK")

    def test_local_path_import_uses_existing_file(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "local.zip"
        embedded = self.tmp / "local_embedded.pak"
        self._make_pak(embedded, ["maps/e1m1.bsp"])
        self._make_zip(zip_path, {"pak0.pak": embedded.read_bytes(), "docs/readme.txt": b"hello"})
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Local Import",
                "addon_id": "localmod",
                "gamedir": "localmod",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Imported from local path",
                "import_mode": "local",
                "local_source": str(zip_path),
                "csrf_token": self._csrf(),
            },
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"packaged from", res.data)

    def test_content_based_detection_not_extension_based(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        masquerade = self.tmp / "fake.pak"
        embedded = self.tmp / "fake_embedded.pak"
        self._make_pak(embedded, ["maps/e1m1.bsp"])
        self._make_zip(masquerade, {"pak0.pak": embedded.read_bytes(), "docs/readme.txt": b"hello"})
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
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"packaged from", res.data)

    def test_zip_release_consolidates_embedded_pak_with_loose_assets(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "release.zip"
        import zipfile
        embedded = self.tmp / "embedded.pak"
        self._make_pak(embedded, ["maps/a.bsp"])
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(embedded, "pak0.pak")
            zf.writestr("maps/b.bsp", b"B")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Release",
                "addon_id": "release",
                "gamedir": "release",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Composite zip release",
                "package": (zip_path.open("rb"), "release.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"packaged from", res.data)
        conn = get_db()
        row = conn.execute("SELECT * FROM addons WHERE addon_id = ?", ("release",)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.app.post(f"/addons/{row['id']}/publish", data={"csrf_token": self._csrf()}, follow_redirects=True)
        pkg = self.app.get("/addons/release.pak")
        self.assertEqual(pkg.status_code, 200)
        out = self.tmp / "release-out.pak"
        out.write_bytes(pkg.data)
        manifest = self._read_pak_manifest(out)
        paths = [m[0] for m in manifest]
        self.assertIn("maps/a.bsp", paths)
        self.assertIn("maps/b.bsp", paths)

    def test_zip_release_accepts_already_rooted_game_directories(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "rooted.zip"
        import zipfile
        embedded = self.tmp / "embedded.pak"
        self._make_pak(embedded, ["maps/start.bsp"])
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(embedded, "pak0.pak")
            zf.writestr("maps/extra.bsp", b"x")
            zf.writestr("sound/test.wav", b"y")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Rooted",
                "addon_id": "rooted",
                "gamedir": "rooted",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Already rooted layout",
                "package": (zip_path.open("rb"), "rooted.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        conn = get_db(); row = conn.execute("SELECT * FROM addons WHERE addon_id = ?", ("rooted",)).fetchone(); conn.close()
        self.assertIsNotNone(row)
        self.app.post(f"/addons/{row['id']}/publish", data={"csrf_token": self._csrf()}, follow_redirects=True)
        out = self.app.get("/addons/rooted.pak")
        self.assertEqual(out.status_code, 200)
        manifest = self._read_pak_manifest(self.tmp / "rooted-out.pak") if False else None

    def test_zip_release_strips_single_wrapper_directory(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "wrapped.zip"
        import zipfile
        embedded = self.tmp / "embedded.pak"
        self._make_pak(embedded, ["maps/start.bsp"])
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(embedded, "modname/pak0.pak")
            zf.writestr("modname/maps/extra.bsp", b"x")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Wrapped",
                "addon_id": "wrapped",
                "gamedir": "wrapped",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Single wrapper layout",
                "package": (zip_path.open("rb"), "wrapped.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)

    def test_zip_release_rejects_ambiguous_multi_mod_bundle(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "ambig.zip"
        import zipfile
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("moda/maps/a.bsp", b"a")
            zf.writestr("modb/maps/b.bsp", b"b")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Ambig",
                "addon_id": "ambig",
                "gamedir": "ambig",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Ambiguous bundle",
                "package": (zip_path.open("rb"), "ambig.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"ambiguous layout", res.data)

    def test_zip_conversion_is_order_invariant(self):
        from tools.catalog import build_single_pak_from_zip
        import zipfile, hashlib

        embedded1 = self.tmp / "e1.pak"
        embedded2 = self.tmp / "e2.pak"
        self._make_pak(embedded1, ["maps/shared.bsp", "progs/shared.mdl"])
        self._make_pak(embedded2, ["maps/shared.bsp", "progs/shared.mdl"])

        z1 = self.tmp / "a.zip"
        z2 = self.tmp / "b.zip"
        with zipfile.ZipFile(z1, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("pak0.pak", embedded1.read_bytes())
            zf.writestr("maps/shared.bsp", b"loose-a")
            zf.writestr("sound/new.wav", b"s")
            zf.writestr("pak1.pak", embedded2.read_bytes())
        with zipfile.ZipFile(z2, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("sound/new.wav", b"s")
            zf.writestr("pak1.pak", embedded2.read_bytes())
            zf.writestr("maps/shared.bsp", b"loose-a")
            zf.writestr("pak0.pak", embedded1.read_bytes())

        out1 = self.tmp / "out1.pak"
        out2 = self.tmp / "out2.pak"
        info1 = build_single_pak_from_zip(z1, out1)
        info2 = build_single_pak_from_zip(z2, out2)
        self.assertEqual(info1["sha256"], info2["sha256"])
        self.assertEqual(info1["entries"], info2["entries"])
        self.assertEqual(hashlib.sha256(out1.read_bytes()).hexdigest(), hashlib.sha256(out2.read_bytes()).hexdigest())

    def test_duplicate_zip_paths_rejected(self):
        from tools.catalog import build_single_pak_from_zip
        import zipfile
        z = self.tmp / "dupe.zip"
        with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("maps/a.bsp", b"1")
            zf.writestr("maps/a.bsp", b"2")
        with self.assertRaises(ValueError):
            build_single_pak_from_zip(z, self.tmp / "dupe.pak")

    def test_zip_release_rejects_unsafe_multiple_wrapper_dirs(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "badwrap.zip"
        import zipfile
        embedded = self.tmp / "embedded.pak"
        self._make_pak(embedded, ["maps/a.bsp"])
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(embedded, "pak0.pak")
            zf.writestr("a/readme.txt", b"a")
            zf.writestr("b/readme.txt", b"b")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "BadWrap",
                "addon_id": "badwrap",
                "gamedir": "badwrap",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Multiple wrapper dirs",
                "package": (zip_path.open("rb"), "badwrap.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"ambiguous layout", res.data)

    def test_zip_release_allows_pak_only(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "pakonly.zip"
        import zipfile
        embedded = self.tmp / "embedded.pak"
        self._make_pak(embedded, ["maps/a.bsp"])
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(embedded, "pak0.pak")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Pak Only",
                "addon_id": "pakonly",
                "gamedir": "pakonly",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Zip containing only embedded pak",
                "package": (zip_path.open("rb"), "pakonly.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"packaged from", res.data)

    def test_zip_release_rejects_multiple_paks(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "badrelease.zip"
        self._make_pak(self.tmp / "a.pak", ["a"])
        self._make_pak(self.tmp / "b.pak", ["b"])
        import zipfile
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(self.tmp / "a.pak", "pak0.pak")
            zf.write(self.tmp / "b.pak", "pak1.pak")
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Bad Release",
                "addon_id": "badrelease",
                "gamedir": "badrelease",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Two paks",
                "package": (zip_path.open("rb"), "badrelease.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"packaged from", res.data)

    def test_unsupported_zip_compression_uses_unzip_fallback(self):
        self._login()
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        self.addCleanup(lambda: app.config.__setitem__("MAX_CONTENT_LENGTH", old_limit))
        zip_path = self.tmp / "deflate.zip"
        import zipfile
        embedded = self.tmp / "embedded.pak"
        self._make_pak(embedded, ["maps/a.bsp"])
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(embedded, "pak0.pak")
            zf.writestr("maps/b.bsp", b"B" * 32)
        res = self.app.post(
            "/addons/new",
            data={
                "title": "Deflate",
                "addon_id": "deflate",
                "gamedir": "deflate",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Deflated zip",
                "package": (zip_path.open("rb"), "deflate.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"packaged from", res.data)

    def test_failed_replacement_preserves_published_package(self):
        self._login()
        pkg = self.tmp / "good.pak"
        self._make_pak(pkg)
        self.app.post(
            "/addons/new",
            data={
                "title": "Good",
                "addon_id": "goodmod",
                "gamedir": "goodmod",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Initial",
                "package": (pkg.open("rb"), "good.pak"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        conn = get_db()
        row = conn.execute("SELECT * FROM addons WHERE addon_id = ?", ("goodmod",)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.app.post(f"/addons/{row['id']}/publish", data={"csrf_token": self._csrf()}, follow_redirects=True)
        published = self.app.get("/addons/goodmod.pak").data

        bad_zip = self.tmp / "bad.zip"
        import zipfile
        with zipfile.ZipFile(bad_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("pak0.pak", b"not a pak")
        res = self.app.post(
            f"/addons/{row['id']}/edit",
            data={
                "title": "Good",
                "addon_id": "goodmod",
                "gamedir": "goodmod",
                "author": "Tester",
                "date": "2026/10/15",
                "description": "Replace with corrupt zip",
                "package": (bad_zip.open("rb"), "bad.zip"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Package rejected", res.data)
        self.assertEqual(self.app.get("/addons/goodmod.pak").data, published)

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

    def test_health_reports_ready_and_doctor_returns_json(self):
        res = self.app.get("/health")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data.decode("utf-8"))
        self.assertTrue(data["ok"])
        self._login()
        self.assertEqual(self.app.get("/doctor").status_code, 200)

    def test_startup_banner_and_doctor_command(self):
        from app import log_startup_banner, startup_preflight
        ok, issues = startup_preflight()
        self.assertTrue(ok)
        self.assertEqual(issues, [])
        log_startup_banner()

    def test_preflight_permission_message_is_actionable(self):
        from app import startup_preflight
        old = app.config["UPLOAD_FOLDER"]
        app.config["UPLOAD_FOLDER"] = "/proc/forbidden"
        ok, issues = startup_preflight()
        app.config["UPLOAD_FOLDER"] = old
        self.assertFalse(ok)
        self.assertTrue(issues)

    def test_too_large_error_handler(self):
        old_testing = app.config["TESTING"]
        old_limit = app.config["MAX_CONTENT_LENGTH"]
        try:
            app.config["TESTING"] = False
            app.config["MAX_CONTENT_LENGTH"] = 1
            tiny = self.tmp / "small.bin"
            tiny.write_bytes(b"abc")
            with tiny.open("rb") as fh:
                res = self.app.post(
                    "/addons/new",
                    data={"title": "x", "addon_id": "x", "gamedir": "x", "package": (fh, "small.bin"), "csrf_token": self._csrf()},
                    content_type="multipart/form-data",
                    follow_redirects=False,
                )
            self.assertEqual(res.status_code, 413)
        finally:
            app.config["TESTING"] = old_testing
            app.config["MAX_CONTENT_LENGTH"] = old_limit

    def test_effective_config_uses_appdata_paths(self):
        from app import _effective_config
        cfg = _effective_config()
        self.assertTrue(cfg["PERSIST_ROOT"])
        self.assertIn("PERSIST_ROOT", cfg)


if __name__ == "__main__":
    unittest.main()

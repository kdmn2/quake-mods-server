import json
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.catalog import build_catalog, collect_metadata, validate_catalog


def make_pak(path: Path, entries: list[str]) -> None:
    """Write a structurally valid PAK: 12-byte header, file data, then 64-byte directory records."""
    body = bytearray()
    records = bytearray()
    for name in entries:
        payload = name.encode("utf-8")
        records.extend(payload.ljust(56, b"\0") + struct.pack("<II", 12 + len(body), len(payload)))
        body.extend(payload)
    path.write_bytes(b"PACK" + struct.pack("<II", 12 + len(body), len(records)) + bytes(body) + bytes(records))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-addon-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store"
        self.public = self.tmp / "public"
        self.store.mkdir()
        (self.store / "packages").mkdir()
        (self.store / "metadata").mkdir()

    def _write_pkg(self, name: str, id_: str = None):
        pkg_path = self.store / "packages" / f"{id_ or name}.pak"
        make_pak(pkg_path, ["maps/e1m1.bsp", "progs.dat"])
        meta = {
            "id": id_ or name,
            "gamedir": id_ or name,
            "title": name,
            "author": "Tester",
            "date": "2026/10/15",
            "description": "Sample descriptor",
            "package": f"{id_ or name}.pak",
        }
        (self.store / "metadata" / f"{id_ or name}.json").write_text(json.dumps(meta), encoding="utf-8")
        return pkg_path

    def test_catalog_generation_and_validation(self):
        self._write_pkg("examplemod")
        manifest = build_catalog(self.store / "metadata", self.public, "http://localhost:8080")
        self.assertEqual(len(manifest["addons"]), 1)
        self.assertTrue((self.public / "content.json").exists())
        self.assertTrue((self.public / "addons" / "examplemod.pak").exists())
        data = validate_catalog(self.public / "content.json")
        self.assertEqual(data["addons"][0]["gamedir"], "examplemod")

    def test_missing_package_file(self):
        self._write_pkg("missingpkg")
        build_catalog(self.store / "metadata", self.public, "http://localhost:8080")
        package = self.public / "addons" / "missingpkg.pak"
        package.unlink()
        with self.assertRaises(FileNotFoundError):
            validate_catalog(self.public / "content.json")

    def test_duplicate_ids_and_conflicting_dirs(self):
        self._write_pkg("dup", "dup")
        with open(self.store / "metadata" / "dup2.json", "w", encoding="utf-8") as fh:
            json.dump({"id": "dup", "gamedir": "otherdir", "title": "Second", "author": "Tester", "date": "2026/10/15", "description": "x", "package": "dup2.pak"}, fh)
        self.assertRaises(ValueError, collect_metadata, self.store / "metadata")

        for path in list((self.store / "metadata").glob("*.json")):
            path.unlink()
        for path in list((self.store / "packages").glob("*.pak")):
            path.unlink()
        self._write_pkg("one", "one")
        with open(self.store / "metadata" / "two.json", "w", encoding="utf-8") as fh:
            json.dump({"id": "two", "gamedir": "one", "title": "Two", "author": "Tester", "date": "2026/10/15", "description": "x", "package": "two.pak"}, fh)
        self.assertRaises(ValueError, collect_metadata, self.store / "metadata")

    def test_unsafe_archive_paths_rejected(self):
        unsafe = self.tmp / "unsafe.pak"
        make_pak(unsafe, ["../../etc/passwd", "safe/map.bsp"])
        with self.assertRaises(ValueError):
            from tools.catalog import validate_pak_paths
            validate_pak_paths(unsafe)

    def test_http_delivery(self):
        self._write_pkg("httpmod")
        build_catalog(self.store / "metadata", self.public, "http://localhost:8080")
        public_dir = str(self.public)

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=public_dir, **kwargs)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            import urllib.request
            data = urllib.request.urlopen(f"http://127.0.0.1:{port}/content.json", timeout=5).read()
            self.assertIn(b'"addons"', data)
            self.assertTrue(urllib.request.urlopen(f"http://127.0.0.1:{port}/addons/httpmod.pak", timeout=5).status == 200)
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import re
import sqlite3
import unittest
from pathlib import Path

from app import app, generate_catalog, get_db, init_db
from tests.test_pak_report import PUN_TXT, lit_for, make_bsp, wav
from tests.test_publication import PublicationBase
from tests.test_zip_conversion import make_pak_bytes, parse_pak, write_zip
from tools.catalog import build_single_pak_from_zip

BSP_A = make_bsp(worldspawn={"message": "From The PAK"}, lighting=100)
BSP_B = make_bsp(worldspawn={"message": "Loose Copy"}, lighting=100)


def release_members(extra=()):
    pak0 = make_pak_bytes([("maps/a.bsp", BSP_A), ("maps/a.lit", lit_for(100)), ("sound/x.wav", wav())])
    return [("pak0.pak", pak0), ("maps/a.bsp", BSP_B), ("pun.txt", PUN_TXT), *extra]


class InspectionUiTests(PublicationBase):
    def upload_release(self, addon_id="rel", members=None, **kw):
        z = self.tmp / "rel.zip"
        write_zip(z, members or release_members())
        return z, self.upload(addon_id, z.read_bytes(), filename="rel.zip", **kw)

    def detail(self, addon_id):
        res = self.c.get(f"/addons/{self.row(addon_id)['id']}")
        self.assertEqual(res.status_code, 200)
        return res.data.decode()

    # ---- conversion record -----------------------------------------------------------------------
    def test_conversion_record_is_stored_from_independent_facts(self):
        z, res = self.upload_release()
        self.assertIn(b"packaged from", res.data)
        rec = json.loads(self.row("rel")["conversion_report"])
        self.assertEqual(rec["kind"], "zip-release")
        self.assertEqual(rec["source_sha256"], hashlib.sha256(z.read_bytes()).hexdigest())
        self.assertEqual((rec["source_name"], rec["root_mapping"], rec["wrapper"]), ("rel.zip", "rooted", None))
        self.assertEqual((rec["embedded_paks"], rec["loose_files"]), (["pak0.pak"], 2))
        self.assertEqual(rec["output_sha256"], self.row("rel")["package_sha256"])
        # maps/a.bsp exists loose and in pak0.pak: the PAK copy must be the one kept (engine search order)
        self.assertEqual(rec["overrides"], [{"path": "maps/a.bsp", "overridden": "loose:maps/a.bsp", "by": "pak0.pak"}])
        self.assertEqual(rec["overrides_total"], 1)
        stored = Path(self.row("rel")["package_path"])
        self.assertEqual(parse_pak(stored)["maps/a.bsp"], BSP_A)

    def test_pak_upload_records_that_it_was_stored_unchanged(self):
        pak = make_pak_bytes([("maps/a.bsp", BSP_A)])
        self.upload("plain", pak)
        rec = json.loads(self.row("plain")["conversion_report"])
        self.assertEqual((rec["kind"], rec["source_name"], rec["output_sha256"]), ("pak-upload", "p.pak", hashlib.sha256(pak).hexdigest()))
        self.assertIn("stored unchanged", self.detail("plain"))

    def test_record_is_replaced_when_package_is_replaced_and_kept_when_not(self):
        self.upload("swap", make_pak_bytes([("maps/a.bsp", BSP_A)]))
        before = self.row("swap")["conversion_report"]
        row = self.row("swap")
        self.c.post(f"/addons/{row['id']}/edit", data={"title": "t2", "addon_id": "swap", "gamedir": "swap", "csrf_token": self.csrf()}, follow_redirects=True)
        self.assertEqual(self.row("swap")["conversion_report"], before)
        z = self.tmp / "r.zip"
        write_zip(z, release_members())
        self.upload("swap", z.read_bytes(), filename="r.zip", path=f"/addons/{row['id']}/edit")
        self.assertEqual(json.loads(self.row("swap")["conversion_report"])["kind"], "zip-release")

    def test_failed_replacement_keeps_the_old_record(self):
        self.upload("keep", make_pak_bytes([("maps/a.bsp", BSP_A)]))
        before = self.row("keep")["conversion_report"]
        bad = self.tmp / "bad.zip"
        write_zip(bad, [("moda/maps/a", b"a"), ("modb/maps/b", b"b")])
        self.upload("keep", bad.read_bytes(), filename="bad.zip", path=f"/addons/{self.row('keep')['id']}/edit")
        self.assertEqual(self.row("keep")["conversion_report"], before)

    # ---- bytes are unaffected --------------------------------------------------------------------
    def test_inspection_does_not_change_generated_bytes(self):
        z, _ = self.upload_release()
        direct = self.tmp / "direct.pak"
        build_single_pak_from_zip(z, direct)
        stored = Path(self.row("rel")["package_path"])
        self.assertEqual(stored.read_bytes(), direct.read_bytes())
        self.detail("rel")
        self.detail("rel")
        self.assertEqual(stored.read_bytes(), direct.read_bytes())
        self.publish("rel")
        self.assertEqual(self.c.get("/addons/rel.pak").data, direct.read_bytes())

    # ---- detail page --------------------------------------------------------------------------------
    def test_detail_page_shows_inspection_conversion_and_suggestions(self):
        z, _ = self.upload_release(members=release_members([("descript.ion", b"x")]))
        html = self.detail("rel")
        self.assertIn("Package inspection", html)
        self.assertIn("descript.ion.inert", html)
        self.assertIn("1 warning(s)", html)
        self.assertIn("From The PAK", html)                                  # map title read from the kept copy
        self.assertIn(hashlib.sha256(z.read_bytes()).hexdigest(), html)
        self.assertIn("maps/a.bsp", html)
        for field in ("title", "author", "date", "description"):
            self.assertIn(f"/addons/{self.row('rel')['id']}/suggestion/{field}", html)
        self.assertIn("The Punishment Due", html)

    def test_warning_count_is_part_of_the_upload_message(self):
        _, res = self.upload_release(members=release_members([("descript.ion", b"x")]))
        self.assertIn(b"Inspection found 1 warning(s)", res.data)
        _, res = self.upload_release("clean", members=release_members(), gamedir="clean")
        self.assertNotIn(b"Inspection found", res.data)

    def test_package_url_uses_the_real_public_route(self):
        self.upload("urlcheck", make_pak_bytes([("maps/a.bsp", BSP_A)]))
        html = self.detail("urlcheck")
        self.assertIn("/addons/urlcheck.pak", html)
        self.assertNotIn("/public/addons/", html)
        self.assertEqual(self.c.get("/addons/urlcheck.pak").status_code, 404)    # unpublished
        self.publish("urlcheck")
        self.assertEqual(self.c.get("/addons/urlcheck.pak").status_code, 200)

    def test_untrusted_text_is_escaped(self):
        evil = b"Title: <script>alert('x')</script>\nAuthor: <img src=x onerror=alert(1)>\n"
        z = self.tmp / "e.zip"
        write_zip(z, [("pak0.pak", make_pak_bytes([("maps/a.bsp", make_bsp(worldspawn={"message": "<b onmouseover=1>m</b>", "sky": "<i>"}))])),
                      ("evil.txt", evil), ("mapdb.json", json.dumps({"episodes": [{"name": "<svg onload=1>", "dir": "evil"}]}).encode())])
        self.upload("evil", z.read_bytes(), filename="e.zip")
        html = self.detail("evil")
        for raw in ("<script>alert", "<img src=x", "<b onmouseover", "<svg onload", "<i>"):
            self.assertNotIn(raw, html, raw)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)            # author suggestion, escaped
        self.assertIn("&lt;svg onload=1&gt;", html)

    def test_legacy_row_without_record_still_renders(self):
        self.upload("old", make_pak_bytes([("maps/a.bsp", BSP_A)]))
        conn = get_db()
        conn.execute("UPDATE addons SET conversion_report = NULL WHERE addon_id = 'old'")
        conn.commit()
        conn.close()
        self.assertIn("No conversion record", self.detail("old"))
        conn = get_db()
        conn.execute("UPDATE addons SET conversion_report = '{not json' WHERE addon_id = 'old'")
        conn.commit()
        conn.close()
        self.assertIn("No conversion record", self.detail("old"))

    def test_missing_or_corrupt_package_does_not_break_the_page(self):
        self.upload("gone", make_pak_bytes([("maps/a.bsp", BSP_A)]))
        path = Path(self.row("gone")["package_path"])
        path.write_bytes(b"garbage")
        self.assertIn("could not be inspected", self.detail("gone"))
        path.unlink()
        self.assertIn("could not be inspected", self.detail("gone"))

    # ---- applying suggestions -----------------------------------------------------------------------
    def apply(self, addon_id, field, **extra):
        return self.c.post(f"/addons/{self.row(addon_id)['id']}/suggestion/{field}", data={"csrf_token": self.csrf(), **extra}, follow_redirects=True)

    def test_apply_sets_exactly_that_field_from_the_package(self):
        self.upload_release()
        before = dict(self.row("rel"))
        res = self.apply("rel", "title")
        self.assertIn(b"Set title from pun.txt (title)", res.data)
        after = dict(self.row("rel"))
        self.assertEqual(after["title"], "The Punishment Due")
        for key in ("author", "date", "description", "addon_id", "gamedir", "package_sha256", "published"):
            self.assertEqual(after[key], before[key], key)
        self.apply("rel", "date")
        self.apply("rel", "author")
        self.apply("rel", "description")
        row = self.row("rel")
        self.assertEqual((row["date"], row["author"]), ("2021/03/15", "Julian 'Juz' Priestley"))
        self.assertEqual(row["description"], "A 5 map episode for Quake.")
        self.assertNotIn("/suggestion/", self.detail("rel"))                  # nothing left to suggest

    def test_request_cannot_inject_a_value(self):
        self.upload_release()
        self.apply("rel", "title", value="HACKED", title="HACKED", suggestion="HACKED")
        self.assertEqual(self.row("rel")["title"], "The Punishment Due")

    def test_apply_updates_published_catalog(self):
        self.upload_release()
        self.publish("rel")
        self.assertEqual(self.catalog()[0]["name"], "rel")
        self.apply("rel", "title")
        self.apply("rel", "description")
        entry = self.catalog()[0]
        self.assertEqual((entry["name"], entry["description"]["en"]), ("The Punishment Due", "A 5 map episode for Quake."))
        self.assertEqual(entry["download"], "addons/rel.pak")
        self.assertEqual(self.c.get("/addons/rel.pak").data, Path(self.row("rel")["package_path"]).read_bytes())

    def test_unavailable_field_and_unknown_field(self):
        self.upload("nosuggest", make_pak_bytes([("maps/a.bsp", BSP_A)]))
        res = self.apply("nosuggest", "title")
        self.assertIn(b"No title suggestion is available", res.data)
        self.assertEqual(self.row("nosuggest")["title"], "nosuggest")
        self.assertEqual(self.c.post(f"/addons/{self.row('nosuggest')['id']}/suggestion/gamedir", data={"csrf_token": self.csrf()}).status_code, 404)
        self.assertEqual(self.c.post(f"/addons/{self.row('nosuggest')['id']}/suggestion/title;DROP", data={"csrf_token": self.csrf()}).status_code, 404)
        self.assertEqual(self.c.post("/addons/9999/suggestion/title", data={"csrf_token": self.csrf()}).status_code, 404)

    def test_csrf_and_auth_are_enforced(self):
        self.upload_release()
        pk = self.row("rel")["id"]
        self.assertEqual(self.c.post(f"/addons/{pk}/suggestion/title", data={}).status_code, 403)
        self.assertEqual(self.c.post(f"/addons/{pk}/suggestion/title", data={"csrf_token": "wrong"}).status_code, 403)
        self.assertEqual(self.row("rel")["title"], "rel")
        anon = app.test_client()
        anon.get("/login")
        with anon.session_transaction() as s:
            token = s["csrf_token"]
        self.assertEqual(anon.post(f"/addons/{pk}/suggestion/title", data={"csrf_token": token}).status_code, 403)
        self.assertEqual(anon.get(f"/addons/{pk}").status_code, 403)
        self.assertEqual(self.row("rel")["title"], "rel")

    def test_copper_style_mapdb_does_not_produce_a_wrong_title(self):
        z = self.tmp / "c.zip"
        write_zip(z, [("qbj/pak0.pak", make_pak_bytes([("maps/start.bsp", BSP_A)])), ("qbj/mapdb.json", json.dumps({"episodes": [{"name": "Underdark Overbright", "dir": "copper"}]}).encode()),
                      ("qbj/copper_readme.txt", b"Author: Lunaran\nDate: 2019-01-01\n")])
        self.upload("qbj", z.read_bytes(), filename="c.zip")
        html = self.detail("qbj")
        self.assertNotIn("Underdark Overbright", html.split("mapdb.no_name")[0].split("Suggested metadata")[-1] if "Suggested metadata" in html else "")
        self.assertNotIn("/suggestion/", html)
        self.assertIn("mapdb.no_name", html)

    # ---- schema migration ---------------------------------------------------------------------------
    def test_migration_adds_column_and_keeps_rows(self):
        import os
        db = Path(app.config["PERSIST_ROOT"]) / "legacy.sqlite3"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE addons (id INTEGER PRIMARY KEY AUTOINCREMENT, addon_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL, gamedir TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT 'Unknown', date TEXT, description TEXT, package_name TEXT NOT NULL, package_path TEXT NOT NULL, package_size INTEGER NOT NULL,
            package_sha256 TEXT, package_mtime INTEGER, manifest_path TEXT, published INTEGER NOT NULL DEFAULT 0, draft INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, view_count INTEGER NOT NULL DEFAULT 0)""")
        con.execute("INSERT INTO addons (addon_id,title,gamedir,package_name,package_path,package_size) VALUES ('old','Old','old','old.pak','/x/old.pak',5)")
        con.commit()
        con.close()
        app.config["DB_PATH"] = str(db)
        init_db()
        init_db()                                                           # idempotent
        con = sqlite3.connect(db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(addons)")]
        row = con.execute("SELECT addon_id, title, conversion_report FROM addons").fetchone()
        con.close()
        self.assertIn("conversion_report", cols)
        self.assertEqual(row, ("old", "Old", None))


PUN_ZIP = Path(__file__).resolve().parent.parent / "pun.zip"


@unittest.skipUnless(PUN_ZIP.exists(), "pun.zip reference file is not present (never committed)")
class PunExactInputTests(PublicationBase):
    EXPECTED_SHA = "bd31ff4a0f6ff589f04d762aac34b1b5a14750bf53c91c8f5ae68c9267d4e6d7"

    def test_pun_zip_through_the_application(self):
        res = self.upload("pun", PUN_ZIP.read_bytes(), filename="pun.zip")
        self.assertIn(b"packaged from", res.data)
        self.assertNotIn(b"Inspection found", res.data)                      # no warnings on the known-good release
        row = self.row("pun")
        self.assertEqual(row["package_sha256"], self.EXPECTED_SHA)          # report-only changes must not alter the output
        html = self.c.get(f"/addons/{row['id']}").get_data(as_text=True)
        self.assertIn("0 warnings", html.replace("No warnings.", "0 warnings"))
        self.assertIn("The Punishment Due", html)
        for title in ("Azure Annoyance", "Let Them Eat Quake", "Tour De Vores", "With Fiends Like These", "Ogre And Out", "Start for Start&#39;s Sake"):
            self.assertIn(title, html)
        self.assertEqual(len(re.findall(r'<td class="mono">(?:start|pun\d)</td>', html)), 6)
        self.assertEqual(json.loads(row["conversion_report"])["overrides_total"], 0)


if __name__ == "__main__":
    unittest.main()

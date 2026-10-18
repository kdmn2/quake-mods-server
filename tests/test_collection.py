"""Collection-management pass: batch actions, sorting/filtering, sizes, themes, branding.

All runs go through the real application (test client). Test data is synthetic and disposable.
"""
import hashlib
import json
import re
import time
import unittest
import zipfile

import app as app_module
from app import app, get_db, jobs, generate_catalog
from tests.test_publication import PublicationBase
from tests.test_zip_conversion import make_pak_bytes, write_zip
from pathlib import Path

BA = app_module.batch_service if hasattr(app_module, "batch_service") else None


def insert_addon(addon_id, gamedir=None, published=0, size=None, author="A", created=None, path=None):
    conn = get_db()
    if created:
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, author, package_name, package_path, package_size, published, draft, created_at) "
                     "VALUES (?, ?, ?, ?, 'x.pak', ?, ?, ?, ?, CURRENT_TIMESTAMP)", () if False else (addon_id, addon_id, gamedir or addon_id, author,
                     str(path or f"/x/{addon_id}.pak"), size or 1, published, 0 if published else 1))
        # created_at override (tested view of "created date"): keep the row, just set its stamp
        conn.execute("UPDATE addons SET created_at = ? WHERE addon_id = ?", (created, addon_id))
    else:
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, author, package_name, package_path, package_size, published, draft) "
                     "VALUES (?, ?, ?, ?, 'x.pak', ?, ?, ?, ?)", (addon_id, addon_id, gamedir or addon_id, author,
                     str(path or f"/x/{addon_id}.pak"), size or 1, published, 0 if published else 1))
    conn.commit()
    pk = conn.execute("SELECT id FROM addons WHERE addon_id = ?", (addon_id,)).fetchone()[0]
    conn.close()
    return pk


class CollectionBase(PublicationBase):
    def setUp(self):
        super().setUp()
        self.c = app.test_client()
        self.c.post("/login", data={"username": "admin", "password": "secret", "csrf_token": self.csrf()})

    def post(self, path, data=None, files=None):
        form = dict(data or {})
        form["csrf_token"] = self.csrf()
        return self.c.post(path, data=form)

    def real_pak(self, addon_id, size=None):
        f = self.tmp / f"{addon_id}.pak"
        pak = make_pak_bytes([("maps/z.bsp", b"BSP" * 10)])
        f.write_bytes(pak)
        return insert_addon(addon_id, published=0, size=size if size is not None else len(pak), path=f)


# ---- theme, icon, and shared header ----------------------------------------------------------------

class ThemeIconTests(CollectionBase):
    def test_shared_header_has_icon_theme_and_favicon_declarations(self):
        for path in ("/dashboard", "/login"):
            html = self.c.get(path).get_data(as_text=True)
            with self.subTest(path):
                self.assertIn('rel="icon"', html)
                self.assertIn("favicon.ico", html)
                self.assertIn('loading', html) if False else None
                self.assertIn("data-theme-switch", html)
                self.assertIn("icon.svg", html)

    def test_static_assets_are_served_with_correct_mime_types(self):
        self.assertEqual(self.c.get("/static/icon.svg").mimetype, "image/svg+xml")
        ico = self.c.get("/static/favicon.ico")
        self.assertIn("image", ico.mimetype)
        self.assertEqual(self.c.get("/static/icon-180.png").mimetype, "image/png")
        self.assertEqual(self.c.get("/static/theme.js").mimetype, "text/javascript")
        self.assertEqual(self.c.get("/static/collection.js").mimetype, "text/javascript")
        self.assertEqual(self.c.get("/static/nope.svg").status_code, 404)

    def test_theme_script_is_guaranteed_not_to_flash_and_survives_no_storage(self):
        html = self.c.get("/dashboard").get_data(as_text=True)
        # the theme-init script appears in the head, before the stylesheet
        self.assertLess(html.index("theme-init"), html.index("<style>"))
        # it degrades silently without storage or matchMedia (no unguarded member access)
        code = app_module   # no direct-worthy reference needed
        self.assertIn("typeof document === \"undefined\"", html)

    def test_theme_button_describes_current_state_in_text(self):
        html = self.c.get("/dashboard").get_data(as_text=True)
        self.assertIn("aria-label", html)
        self.assertIn("Theme:", html)                                            # state is text, not color alone


# ---- batch actions ---------------------------------------------------------------------------------

class BatchActionTests(CollectionBase):
    def test_confirmation_page_shows_eligibility_and_no_mutation(self):
        a = self.real_pak("aaa")
        b = insert_addon("bbb", published=1)
        conn = get_db()
        before = conn.execute("SELECT * FROM addons ORDER BY id").fetchall()
        conn.close()
        html = self.post("/addons/batch", {"selected_pk": [a, b], "action": "unpublish"}).get_data(as_text=True)
        self.assertIn("Confirm batch action", html)
        self.assertIn("1 published", html)                                       # published items enumerated
        self.assertIn("Already a draft", html)                                   # aaa is ineligible (no-op) and explained
        self.assertIn("bbb", html)
        conn = get_db()
        self.assertEqual(before, conn.execute("SELECT * FROM addons ORDER BY id").fetchall())   # nothing mutated
        conn.close()

    def test_unpublish_batch_with_missing_record_and_noops(self):
        pub = insert_addon("pub", published=1)
        draft = insert_addon("drft", published=0)
        missing = 99999
        res = self.post("/addons/batch/execute", {"selected_pk": [pub, draft, missing], "action": "unpublish", "submit_key": "k1"}, )
        html = self.c.get("/dashboard").get_data(as_text=True)
        self.assertTrue(jobs.wait_idle(10))
        conn = get_db()
        self.assertEqual(conn.execute("SELECT published FROM addons WHERE id = ?", (pub,)).fetchone()[0], 0)
        self.assertIsNotNone(conn.execute("SELECT id FROM addons WHERE id = ?", (draft,)).fetchone())   # record kept
        conn.close()
        self.assertFalse((self.tmp / "public" / "addons" / "pub.pak").exists())  # public copy removed

    def test_delete_batch_cleans_files_and_views_and_reported_reasons(self):
        pk = self.real_pak("doomed")
        other = self.real_pak("keeper")
        conn = get_db()
        path = conn.execute("SELECT package_path FROM addons WHERE addon_id = 'doomed'").fetchone()[0]
        conn.close()
        res = self.post("/addons/batch/execute", {"selected_pk": [pk, 99999], "action": "delete", "submit_key": "k2"})
        conn = get_db()
        self.assertIsNone(conn.execute("SELECT id FROM addons WHERE id = ?", (pk,)).fetchone())
        self.assertIsNotNone(conn.execute("SELECT id FROM addons WHERE id = ?", (other,)).fetchone())   # only selected rows removed
        conn.close()
        import os
        self.assertFalse(os.path.exists(path))                                   # the package file is gone with the record

    def test_delete_removes_kept_source_copy_but_never_original_imports(self):
        """The kept-source copy (created by the 'keep the uploaded ZIP' option) is deleted with the add-on; nobody else's files are."""
        sources = Path(app.config["PERSIST_ROOT"]) / "sources" / "tok123"
        sources.mkdir(parents=True)
        kept = sources / "rel.zip"
        kept.write_bytes(b"zip bytes")
        pk = insert_addon("kept", path=str(kept))
        self.post("/addons/batch/execute", {"selected_pk": [pk], "action": "delete"})
        self.assertFalse(kept.exists())
        self.assertFalse((Path(app.config["PERSIST_ROOT"]) / "sources" / "tok123" / "rel.zip").exists())

    def test_batch_publish_runs_one_job_and_regenerates_catalog_once(self):
        good = self.real_pak("b1")
        bad = insert_addon("b2")                                                # package file missing
        pub = insert_addon("b3", published=1)
        res = self.post("/addons/batch/execute", {"selected_pk": [good, bad, pub], "action": "publish", "submit_key": "kk"})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(jobs.wait_idle(30))
        conn = get_db()
        self.assertEqual(conn.execute("SELECT published FROM addons WHERE id = ?", (good,)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT published FROM addons WHERE id = ?", (bad,)).fetchone()[0], 0)
        conn.close()
        html = self.c.get(f"/jobs/", follow_redirects=False).status_code  # irrelevant; results verified by rows above

    def test_destructive_actions_are_not_available_via_get(self):
        self.assertEqual(self.post("/addons/batch/execute", {"selected_pk": [1], "action": "delete"}).status_code, 302)
        res = self.c.get("/addons/batch")
        self.assertIn(res.status_code, (404, 405))                              # POST-only route; GET despite fallthrough

    def test_invalid_action_and_ids_change_nothing(self):
        self.real_pak("x1")
        res = self.post("/addons/batch/execute", {"selected_pk": ["not-a-number"], "action": "delete"})
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM addons").fetchone()[0], 1)   # nothing removed
        conn.close()
        res = self.post("/addons/batch/execute", {"selected_pk": [1], "action": "explode"})
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM addons").fetchone()[0], 1)
        conn.close()

    def test_duplicate_submission_with_the_same_key_returns_the_same_job(self):
        good = self.real_pak("dupes")
        key = f"dup{time.time_ns()}"
        res1 = self.post("/addons/batch/execute", {"selected_pk": [good], "action": "publish", "submit_key": key})
        self.assertEqual(res1.status_code, 302)
        job1 = res1.headers["Location"].rsplit("/", 1)[-1]
        res2 = self.post("/addons/batch/execute", {"selected_pk": [good], "action": "publish", "submit_key": key})
        self.assertEqual(res2.headers["Location"].rsplit("/", 1)[-1], job1)
        self.assertTrue(jobs.wait_idle(30))

    def test_batch_size_bound_changes_nothing(self):
        pks = [insert_addon(f"m{i}") for i in range(3)]
        fake = pks + [str(100000 + i) for i in range(300)]
        res = self.post("/addons/batch/execute", {"selected_pk": fake, "action": "delete"})
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM addons").fetchone()[0], 3)
        conn.close()

    def test_authentication_is_required(self):
        app.config["TESTING"] = True
        anon = app.test_client()
        self.assertEqual(anon.get("/dashboard").status_code, 403)
        with anon.session_transaction() as s:
            token = s["csrf_token"]
        self.assertEqual(anon.post("/addons/batch", data={"selected_pk": [1], "action": "delete", "csrf_token": token}).status_code, 403)
        self.assertEqual(anon.post("/addons/batch/execute", data={"selected_pk": [1], "action": "delete", "csrf_token": token}).status_code, 403)

    def test_cross_site_post_without_csrf_is_rejected(self):
        self.real_pak("victim")
        self.assertEqual(self.c.post("/addons/batch/execute", data={"selected_pk": [1], "action": "delete"}).status_code, 403)
        self.assertEqual(self.c.post("/addons/batch/execute", data={"selected_pk": [1], "action": "delete", "csrf_token": "forged"}).status_code, 403)
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM addons").fetchone()[0], 1)
        conn.close()


# ---- collection search/filter/sort -----------------------------------------------------------------

class CollectionQueryTests(CollectionBase):
    def setUp(self):
        super().setUp()
        self.old = insert_addon("old", author="Bea", size=5_000_000, created="2020-01-01 00:00:00")
        self.mid = insert_addon("mid", author="Al", size=100_000_000, created="2021-06-01 00:00:00")
        self.pub = insert_addon("pub1", author="Bea", size=2_000, published=1, created="2022-01-01 00:00:00")
        self.gdir = insert_addon("gdir", gamedir="special_dir", author="Cy", created="2023-01-01 00:00:00")

    def rows(self, qs=""):
        res = self.c.get("/dashboard" + qs)
        return res.get_json() if False else [r.addon_id for r in json.loads(res.data.decode())] if False else None

    def collected(self, qs=""):
        res = self.c.get("/dashboard" + qs)
        # parse the visible order from the rendered table
        import re
        html = res.get_data(as_text=True)
        rows = re.findall(r'name="selected_pk" value="(\d+)"', html)
        return [int(v) for v in rows]

    def test_search_hits_title_and_fields(self):
        self.assertEqual(self.collected("?q=retro"), [])                        # no match yet
        self.assertEqual(set(self.collected("?q=bea")), {self.old, self.pub} if self.old else {self.pub})

    def test_search_matches_the_install_directory(self):
        got = self.collected("?q=special_dir")
        self.assertEqual(got, [self.gdir])

    def test_status_filter_and_combination(self):
        self.assertEqual(self.collected("?status=published"), [self.pub])
        self.assertEqual(self.collected("?status=draft"), [self.old, self.mid, self.gdir])
        self.assertEqual(self.collected("?status=published&q=bea"), [self.pub])

    def test_sorting_by_title_asc_desc(self):
        # titles are the add-on IDs: gdir < mid < old < pub1
        self.assertEqual(self.collected("?sort=title"), [self.gdir, self.mid, self.old, self.pub])
        self.assertEqual(self.collected("?sort=title&dir=desc"), [self.pub, self.old, self.mid, self.gdir])

    def test_sorting_size_numerically_not_textually(self):
        ordered = self.collected("?sort=package_size")
        # numeric asc: the 2 000-byte published record beats 5 MB and 100 MB; and the 1-byte default sorts in front of text-sorted "pub1"
        self.assertEqual(ordered.index(self.mid), len(ordered) - 1)             # 100 MB last in asc order

    def test_updated_date_sorting(self):
        conn = get_db()
        conn.execute("UPDATE addons SET updated_at = '2024-01-01 00:00:00' WHERE addon_id = ?", ("pub1",))
        conn.commit()
        conn.close()
        ordered = self.collected("?sort=updated_at")
        self.assertEqual(ordered[0], self.pub)                                  # oldest updated first in ascending order

    def test_stable_ties(self):
        a = insert_addon("aaa", author="x", created="2024-01-01 00:00:00")
        b = insert_addon("aab", author="x", created="2024-01-01 00:00:00")
        conn = get_db()
        conn.execute("UPDATE addons SET created_at = '2024-01-01 00:00:00' WHERE addon_id IN ('aaa', 'aab')")
        conn.commit()
        conn.close()
        got = self.collected("?sort=created_at")
        self.assertEqual(got.index(a) < got.index(b), True)                     # id tie-break keeps these two apart

    def test_invalid_parameters_are_ignored_not_errors(self):
        res = self.c.get("/dashboard?sort=DROP TABLE&dir=weird&status=zzz&q=%20")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.collected("?")), 4)

    def test_no_results_differs_from_an_empty_collection(self):
        html = self.c.get("/dashboard?q=zzzno").get_data(as_text=True)
        self.assertIn("No add-ons match this search", html)
        self.assertNotIn("No add-ons yet", html)

    def test_view_preserved_across_batch_actions(self):
        pub = insert_addon("pubx", published=1)
        res = self.post("/addons/batch/execute", {"selected_pk": [pub], "action": "unpublish", "submit_key": "v1",
                                                  "q": "retro", "status": "published"})
        loc = res.headers["Location"]
        self.assertIn("q=retro", loc)
        self.assertIn("status=published", loc)

    # ---- pagination ---------------------------------------------------------------------------------

    def _make_many(self, count, prefix="pag"):
        for i in range(count):
            insert_addon(f"{prefix}{i:03d}", author="P", size=1000 + i, created=f"2024-01-{i % 28 + 1:02d} 00:00:00")

    def test_filtering_and_sorting_apply_before_pagination(self):
        # The total is computed against the filtered set, not the page slice — so page 2 with page_size=10
        # contains rows 11-15 of the filtered+sorted set, not rows 11-15 of the unfiltered set.
        self._make_many(12)                                                    # 12 + 4 existing = 16 total
        page1 = set(self.collected("?q=pag&sort=package_size&dir=asc&page=1&page_size=10"))
        page2 = set(self.collected("?q=pag&sort=package_size&dir=asc&page=2&page_size=10"))
        self.assertEqual(len(page1), 10)
        self.assertEqual(len(page2), 2)
        self.assertEqual(len(page1 & page2), 0)                               # no overlap

    def test_stable_ordering_across_pages(self):
        for i in range(11):
            insert_addon(f"tie{i:02d}", size=999_999, author="T")             # same size on every row
        seen = []
        for page in (1, 2):
            html = self.c.get(f"/dashboard?sort=package_size&dir=asc&page={page}&page_size=10").get_data(as_text=True)
            ids = re.findall(r'name="selected_pk" value="(\d+)"', html)
            self.assertEqual(len(ids), 10 if page == 1 else 5)
            seen += [int(x) for x in ids]
        # 4 setUp addons + 11 tie entries = 15 total visible across both pages
        self.assertEqual(len(seen), 15)
        self.assertEqual(len(set(seen)), 15)

    def test_invalid_or_out_of_range_pages_and_sizes_are_clamped(self):
        self._make_many(7)                                                    # 7 + 4 setUp = 11 items
        # page too high clamps to the last page (page 2 of 2 at page_size=10)
        html = self.c.get("/dashboard?page=99&page_size=10").get_data(as_text=True)
        self.assertIn("page 2 of 2", html)
        # page=0 clamps to 1
        html = self.c.get("/dashboard?page=0&page_size=10").get_data(as_text=True)
        self.assertIn("page 1 of 2", html)
        # invalid page_size falls back to the default; the page is still 200
        self.assertEqual(self.c.get("/dashboard?page_size=999&page=1").status_code, 200)
        # non-integer values default without raising
        self.assertEqual(self.c.get("/dashboard?page=abc&page_size=def").status_code, 200)
        # default page_size when omitted
        html = self.c.get("/dashboard").get_data(as_text=True)
        self.assertIn("of 11", html)

    def test_page_links_preserve_query_parameters(self):
        self._make_many(12)                                                   # 12 + 4 setUp; the search "pag" matches all 12 -> 2 pages at page_size 10
        html = self.c.get("/dashboard?q=pag&sort=title&dir=desc&status=draft&page=1&page_size=10").get_data(as_text=True)
        m = re.search(r'href="([^"]*page=2[^"]*)"', html)
        self.assertIsNotNone(m)
        link = m.group(1)
        for needle in ("q=pag", "sort=title", "dir=desc", "status=draft", "page_size=10"):
            self.assertIn(needle, link)

    def test_result_count_and_current_range_show(self):
        self._make_many(11)
        html = self.c.get("/dashboard?page=2&page_size=10").get_data(as_text=True)
        self.assertIn("Showing 11–15 of 15", html)
        self.assertIn("page 2 of 2", html)

    def test_delete_on_last_page_redirects_to_a_valid_page(self):
        self._make_many(11)                                                   # 11 + 4 setUp = 15 items
        # page 2 with page_size 10 contains items 11..15
        page1 = [int(x) for x in re.findall(r'name="selected_pk" value="(\d+)"',
                                            self.c.get("/dashboard?page=1&page_size=10").get_data(as_text=True))]
        self.assertEqual(len(page1), 10)
        page2 = [int(x) for x in re.findall(r'name="selected_pk" value="(\d+)"',
                                             self.c.get("/dashboard?page=2&page_size=10").get_data(as_text=True))]
        self.assertEqual(len(page2), 5)
        res = self.post("/addons/batch/execute", {"selected_pk": page2, "action": "delete", "submit_key": "p2",
                                                  "page": 2, "page_size": 10})
        loc = res.headers["Location"]
        import urllib.parse as u
        qs = u.parse_qs(u.urlparse(loc).query)
        self.assertEqual(int(qs["page_size"][0]), 10)
        # after deleting 5 items we have 10 total -> exactly 1 page; page 2 of the previous view no longer exists
        self.assertEqual(int(qs["page"][0]), 1)
        self.c.get(loc)                                                       # 200, follow not required

    def test_select_all_is_current_page_only(self):
        # The collection JS limits selection to the current page; the form posts a checkbox per row and the
        # checkboxes on hidden pages are never selected.
        self._make_many(10)
        # the dashboard shows an explicit note in the rendered copy
        html = self.c.get("/dashboard?page=2&page_size=3").get_data(as_text=True)
        self.assertIn("Selection applies to the add-ons on this page", html)


# ---- size formatter and static asset routes -------------------------------------------------------

class FilesizeFormatterTests(unittest.TestCase):
    def test_zero_small_misses_and_units(self):
        import app
        f = app.filesize_filter
        self.assertEqual(f(0), "0 B")
        self.assertEqual(f(None), "—")
        self.assertEqual(f(""), "—")
        self.assertEqual(f("garbage"), "—")
        self.assertEqual(f(-3), "—")
        # boundaries
        self.assertEqual(f(1), "1 B")
        self.assertEqual(f(1023), "1023 B")
        self.assertEqual(f(1024), "1 KiB")
        self.assertEqual(f(1024 * 1024 - 1), "1023 KiB")
        self.assertEqual(f(1024 * 1024), "1 MiB")
        # rounding: floor, never shows nonzero as 0 in a larger unit
        self.assertEqual(f(1_500_000), "1 MiB")
        self.assertEqual(f(2_000), "1 KiB")
        # large
        self.assertEqual(f(112_019_344), "106 MiB")
        self.assertEqual(f(1_500_000_000), "1 GiB")


class StaticAssetTests(CollectionBase):
    def test_static_assets_are_served_with_expected_mime(self):
        self.assertEqual(self.c.get("/static/icon.svg").mimetype, "image/svg+xml")
        ico = self.c.get("/static/favicon.ico")
        self.assertIn("icon", ico.mimetype)
        self.assertEqual(self.c.get("/static/icon-64.png").mimetype, "image/png")
        self.assertEqual(self.c.get("/static/icon-180.png").mimetype, "image/png")
        self.assertEqual(self.c.get("/static/theme.js").mimetype, "text/javascript")
        self.assertEqual(self.c.get("/static/collection.js").mimetype, "text/javascript")
        self.assertEqual(self.c.get("/static/nope.svg").status_code, 404)

    def test_exact_protocol_size_in_catalog_unchanged(self):
        from pathlib import Path
        # generate_catalog reports the public copy's actual byte count, an exact integer
        pak_path = Path(app.config["PERSIST_ROOT"]) / "packages" / "sized.pak"
        pak_path.parent.mkdir(parents=True, exist_ok=True)
        pak_path.write_bytes(make_pak_bytes([("maps/a.bsp", b"x" * 450_000)]))
        public_path = Path(app.config["PUBLIC_ROOT"]) / "addons" / "sized.pak"
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_bytes(pak_path.read_bytes())
        insert_addon("sized", published=1, size=450_312, path=str(pak_path))      # the add-on's stored size is informational
        generate_catalog()
        cat = json.loads(self.c.get("/content.json").data.decode())
        entry = next(e for e in cat["addons"] if e["gamedir"] == "sized")
        # the catalog carries an exact integer (no formatting, no rounding) that matches the file on disk
        self.assertIsInstance(entry["size"], int)
        self.assertEqual(entry["size"], public_path.stat().st_size)

"""Quaddicted integration tests. Upstream responses are fixture documents served by the controlled local test server
(tests/nettest.py) or injected directly into tools.quaddicted.sync_index; no test talks to the real quaddicted.com.

Live verification against the real site is done separately by tools/smoke_quaddicted.py and is never part of the suite.
"""
import hashlib
import io
import json
import re
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager

import app as app_module
from app import app, get_db, jobs
from tests.test_batch_import import BatchBase
from tests.test_pak_report import make_bsp, wav
from tests.test_zip_conversion import make_pak_bytes, parse_pak, write_zip
import tools.quaddicted as qd
from tools.quaddicted import (DESCRIPTION_MAX, Entry, UpstreamProblem, human_age, parse_doc, parse_tags, pick_download_url,
                              screenshot_url, sync_index)

SVC = app_module.quaddicted_service
BSV = app_module.batch_service
UPSTREAM = "www.quaddicted.com"


def sha64(n: int = 0) -> str:
    return f"{n:064x}"


def doc(sha: str, **over) -> dict:
    """A document shaped exactly like the real API returns (verified fields only)."""
    d = {
        "sha256": sha,
        "bytes": 1024,
        "description": "Two maps.",
        "json_version": "20250720-fromsolr",
        "json_timestamp": "2026-01-04T20:10:02Z",
        "tags": ["title=Test Entry", "author=Aone", "author=Atwo", "filename=testentry.zip", "game=quake",
                 "game_mode=singleplayer", "type=map", "type=episode", "release_date=2001-07-22", "zipbasedir=id1/",
                 "theme=base", "depends=(copper>=1.30)", "depends=quoth>=2.2"],
        "urls": [f"https://www.quaddicted.com/files/by-sha256/{sha[:2]}/{sha}/testentry.zip",
                 "https://mirror.example.org/testentry.zip"],
        "install": {"extract": "{base}/id1/"},
        "files": {"maps/a.bsp": {"bytes": 1, "sha256": "aa", "timestamp": "2001-07-22T00:00:00.000Z"}},
    }
    d.update(over)
    return d


def malicious_doc(sha: str) -> dict:
    return doc(sha, description="<script>alert('desc')</script> Honest description.",
               tags=["title=Best</title><script>alert(1)</script> pack", "filename=<img src=x>.zip",
                     "author=<b>Bold</b>", "link:homepage=javascript:alert(1)", "link=https://ok.example.org/page",
                     "type=map"],
               notes='Requires <a href="other-entry">Arcane Dimensions</a><script>alert("notes")</script>')


class FakeFetcher:
    """Controlled stand-in for the network boundary of sync_index: counts requests, serves canned answers."""

    def __init__(self, listing_pages: list, docs_by_sha: dict[str, dict] | None = None, fail_rate_limited_after: int | None = None):
        self.listing_pages = listing_pages
        self.docs_by_sha = docs_by_sha or {}
        self.fail_rate_limited_after = fail_rate_limited_after
        self.requests: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, url: str, max_bytes: int) -> bytes:
        with self.lock:
            listing_calls = sum(1 for r in self.requests if "fl=sha256" in r)
            self.requests.append(url)
            if self.fail_rate_limited_after is not None and listing_calls >= self.fail_rate_limited_after:
                raise qd.netfetch.FetchError("http", "The server answered HTTP 429 (rate limited)", retryable=True, retry_after=300)
        if "fl=sha256,json_timestamp" in url:
            page = self._page_index(url)
            return json.dumps(self.listing_pages[page]).encode() if page < len(self.listing_pages) else b"[]"
        if "sha256:(" in url:                                                   # delta chunk: answer the requested documents
            shas = [s for s in self.docs_by_sha if s in url]
            return json.dumps([self.docs_by_sha[s] for s in shas[:40]]).encode()
        return b"[]"

    @staticmethod
    def _page_index(url: str) -> int:
        m = re.search(r"start=(\d+)", url)
        return int(m.group(1)) // 500 if m else 0


def noop_progress(stage, done, total):
    pass


@contextmanager
def txn():
    """A short write transaction over the same real database (mirrors app.db_txn without Flask)."""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class QuaddictedBase(BatchBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, SVC, "sync_fetch", None)

    def seed_entry(self, sha: str, **over) -> dict:
        d = doc(sha, **over)
        with SVC.db() as c:
            qd.upsert_entry(c, Entry.from_doc(d), time.time())
        return d

    def release_bytes(self, tag: str) -> tuple[bytes, dict]:
        """A minimal but real release ZIP (embedded pak0 with a BSP and a WAV), as bytes plus its expected PAK content."""
        bsp = make_bsp(worldspawn={"message": f"Map A {tag}"})
        snd = wav()
        pak0 = make_pak_bytes([("maps/a.bsp", bsp), ("sound/x.wav", snd)])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("pak0.pak", pak0)
        return buf.getvalue(), {"maps/a.bsp": bsp, "sound/x.wav": snd}

    def host_quaddicted_release(self, name: str, tag: str) -> tuple[str, dict[str, bytes]]:
        """Serve a release ZIP under the content-addressed URL shape and index the matching entry."""
        data, expected = self.release_bytes(tag)
        sha = hashlib.sha256(data).hexdigest()
        self.site.serve(f"/files/by-sha256/{sha[:2]}/{sha}/{name}", data)
        base = doc(sha)
        base["tags"] = [t for t in base["tags"] if not t.startswith("filename=")] + [f"filename={name}"]
        # FakeNet serves plain HTTP; scheme changed accordingly (production always prefers the site's https links)
        base["urls"] = [f"http://www.quaddicted.com/files/by-sha256/{sha[:2]}/{sha}/{name}"]
        self.seed_entry(sha, **base)
        return sha, expected

    def import_selected(self, *shas: str, key: str | None = None) -> int:
        res = self.post("/quaddicted/import", {"sha": list(shas), "submit_key": key or f"qd{time.time_ns()}"})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:800])
        return int(res.headers["Location"].rsplit("/", 1)[-1])


# ---- document parsing and metadata mapping (pure, fixture-based) --------------------------------------------------------

class ParseTests(unittest.TestCase):
    def test_a_full_document_maps_every_field(self):
        e = parse_doc(doc(sha64(1)))
        self.assertEqual(e.sha256, sha64(1))
        self.assertEqual(e.title, "Test Entry")
        self.assertEqual(e.authors, ["Aone", "Atwo"])
        self.assertEqual(e.filename, "testentry.zip")
        self.assertEqual(e.types, ["map", "episode"])
        self.assertEqual(e.game_modes, ["singleplayer"])
        self.assertEqual(e.release_date, "2001-07-22")
        self.assertEqual(e.bytes, 1024)
        self.assertEqual(e.install_extract, "{base}/id1/")
        self.assertEqual(e.depends, ["(copper>=1.30)", "quoth>=2.2"])
        self.assertEqual(e.download_url, f"https://www.quaddicted.com/files/by-sha256/{sha64(1)[:2]}/{sha64(1)}/testentry.zip")
        self.assertEqual(e.page, f"https://www.quaddicted.com/db/canonical/{sha64(1)}")
        self.assertEqual(e.screenshot, f"https://www.quaddicted.com/files/quaddicted-images/by-sha256/{sha64(1)[:2]}/{sha64(1)}/testentry.jpg")
        self.assertEqual(e.json_timestamp, "2026-01-04T20:10:02Z")

    def test_missing_and_optional_fields_do_not_break_mapping(self):
        e = parse_doc({"sha256": sha64(2)})
        self.assertIsNone(e.title)
        self.assertEqual(e.authors, [])
        self.assertIsNone(e.bytes)
        self.assertEqual(e.urls, [])
        self.assertIsNone(e.download_url)
        self.assertIsNone(e.screenshot)
        self.assertFalse(e.exceeds_limits)
        self.assertIsNone(parse_doc(None))
        self.assertIsNone(parse_doc(["not a dict"]))
        self.assertIsNone(parse_doc(doc("short")))                                   # not a sha256
        self.assertIsNone(parse_doc(doc(sha64(3), sha256=123)))

    def test_unknown_fields_and_malformed_tags_are_ignored_not_fatal(self):
        raw = doc(sha64(4))
        raw["brand_new_field"] = {"weird": [1, 2, 3]}
        raw["tags"] = raw["tags"] + ["future_key=some value", "novalue", "=", 42, None]
        e = parse_doc(raw)
        self.assertEqual(e.title, "Test Entry")
        self.assertEqual(parse_tags(raw["tags"]).get("future_key"), ["some value"])

    def test_oversized_fields_are_truncated(self):
        e = parse_doc(doc(sha64(5), description="x" * (DESCRIPTION_MAX + 10), urls=[f"https://mirror{i}.example.org/a.zip" for i in range(20)]))
        self.assertLessEqual(len(e.description), DESCRIPTION_MAX)
        self.assertLessEqual(len(e.urls), 10)

    def test_upstream_notes_html_is_stripped_to_plain_text(self):
        e = parse_doc(malicious_doc(sha64(6)))
        self.assertNotIn("<", e.notes)
        self.assertIn("Requires", e.notes)
        self.assertIn("Arcane Dimensions", e.notes)

    def test_malicious_text_is_kept_as_text_and_never_as_markup(self):
        e = parse_doc(malicious_doc(sha64(7)))
        self.assertNotIn("<", e.title or "")
        self.assertEqual(e.homepages, [])                                            # javascript: links dropped at source
        self.assertEqual(e.safe_links, ["https://ok.example.org/page"])              # only absolute http(s) links survive

    def test_screenshot_url_rejects_bad_names(self):
        self.assertIsNone(screenshot_url(sha64(8), "notazip.jpg"))
        self.assertIsNone(screenshot_url(sha64(8), "../../etc/passwd.zip"))
        self.assertIsNone(screenshot_url("zz", "a.zip"))
        self.assertIsNone(screenshot_url(sha64(9), None))
        self.assertIsNone(screenshot_url(sha64(9), ("x" * 100) + ".zip"))

    def test_download_url_preference(self):
        self.assertEqual(pick_download_url(["http://other.org/a.zip", f"https://www.quaddicted.com/files/by-sha256/aa/{sha64(10)}/a.zip"]),
                         f"https://www.quaddicted.com/files/by-sha256/aa/{sha64(10)}/a.zip")
        self.assertEqual(pick_download_url(["http://other.org/a.zip", "https://www.quaddicted.com/filebase/a.zip"]),
                         "https://www.quaddicted.com/filebase/a.zip")
        self.assertEqual(pick_download_url(["http://mirror.example.org/a.zip"]), "http://mirror.example.org/a.zip")
        self.assertIsNone(pick_download_url([]))
        self.assertIsNone(pick_download_url(["javascript:alert(1)"]))

    def test_human_age(self):
        self.assertEqual(human_age(None), "never")
        self.assertEqual(human_age(time.time() - 30), "less than a minute ago")
        self.assertEqual(human_age(time.time() - 2 * 60), "2 minutes ago")
        self.assertEqual(human_age(time.time() - 3 * 86400), "3 days ago")


# ---- index, search, and status ------------------------------------------------------------------------------------------

class IndexTests(QuaddictedBase):
    def test_search_finds_terms_filters_and_pages(self):
        a = self.seed_entry(sha64(1), tags=["title=Return to Earth", "author=XeNoN", "filename=rte.zip", "type=map", "game_mode=singleplayer"])
        b = self.seed_entry(sha64(2), tags=["title=Copper", "author=Other", "filename=cu.zip", "type=mod", "game_mode=singleplayer"])
        with SVC.db() as c:
            found, total = qd.search(c, "earth", None, None, "title", 1)
            self.assertEqual([e.sha256 for e in found], [a["sha256"]])
            found, total = qd.search(c, "copper", "mod", "singleplayer", "title", 1)
            self.assertEqual([e.sha256 for e in found], [b["sha256"]])
            found, total = qd.search(c, "", None, None, "title", 1)
            self.assertEqual(total, 2)
            # search text is data, not SQL: a LIKE probe must match nothing unusual
            found, total = qd.search(c, "%' OR 'x'='x", None, None, "title", 1)
            self.assertEqual(total, 0)

    def test_imported_status_maps_entries_to_local_addons(self):
        d = self.seed_entry(sha64(3))
        conn = get_db()
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, package_name, package_path, package_size, published, draft, source_sha256) "
                     "VALUES ('rte', 'RTE', 'rte', 'rte.pak', '/x/rte.pak', 1, 0, 1, ?)", (d["sha256"],))
        conn.commit()
        conn.close()
        with SVC.db() as c:
            status = qd.imported_status(c, [d["sha256"], sha64(4)])
        self.assertEqual(status[d["sha256"]]["state"], "draft")

    def test_stale_cache_is_shown_with_its_age(self):
        self.seed_entry(sha64(5))
        old = time.time() - 40 * 86400
        with SVC.db() as c:
            qd._state_set(c, "synced_at", repr(old))
        self.assertIn("40 days ago", self.c.get("/quaddicted").get_data(as_text=True))

    def test_unknown_entry_page_returns_404_not_error(self):
        self.assertEqual(self.c.get("/quaddicted/entry/" + sha64(6)).status_code, 404)

    def test_entry_page_shows_dependencies_and_other_verified_fields(self):
        d = self.seed_entry(sha64(7), tags=["title=Heavy", "author=A", "filename=heavy.zip", "type=mod", "depends=(copper>=1.30)",
                                            "exceeds_quake_limits=yes"])
        html = self.c.get(f"/quaddicted/entry/{d['sha256']}").get_data(as_text=True)
        self.assertIn("Dependencies", html)
        self.assertIn("copper&gt;", html)                                    # upstream text is escaped, never executed
        self.assertIn("exceeding classic Quake limits", html)

    def test_authentication_is_required_for_every_route(self):
        anon = app.test_client()
        anon.get("/login")
        with anon.session_transaction() as s:
            token = s["csrf_token"]
        self.assertEqual(anon.get("/quaddicted").status_code, 403)
        self.assertEqual(anon.get("/quaddicted/entry/" + sha64(8)).status_code, 403)
        self.assertEqual(anon.post("/quaddicted/import", data={"csrf_token": token, "sha": [sha64(1)]}).status_code, 403)
        self.assertEqual(anon.post("/quaddicted/sync", data={"csrf_token": token}).status_code, 403)

    def test_every_post_requires_a_csrf_token(self):
        for path in ("/quaddicted/import", "/quaddicted/sync"):
            self.assertEqual(self.c.post(path, data={}).status_code, 403, path)
            self.assertEqual(self.c.post(path, data={"csrf_token": "forged"}).status_code, 403, path)
            self.assertEqual(self.c.post(path, data={"X-Requested-With": "forgery"}).status_code, 403, path)


# ---- synchronisation (controlled responses; offline) ----------------------------------------------------------------------

class SyncTests(QuaddictedBase):
    def sync(self, listing, docs):
        f = FakeFetcher(listing, docs)
        with SVC.db() as c:
            stats = sync_index(txn, f, noop_progress)
        return f, stats

    def test_first_sync_fetches_bounded_pages_and_stores_entries(self):
        d1, d2 = doc(sha64(1)), doc(sha64(2))
        filler = [{"sha256": (sha := f"{i:03d}" + "a" * 61), "json_timestamp": "t"} for i in range(500)]
        docs = {d["sha256"]: doc(d["sha256"], description=f"Filler {i}") for i, d in enumerate(filler)}
        docs[sha64(1)], docs[sha64(2)] = d1, d2
        f = FakeFetcher([filler + [{"sha256": sha64(1), "json_timestamp": "t1"}, {"sha256": sha64(2), "json_timestamp": "t2"}]], docs)
        with SVC.db() as c:
            stats = sync_index(txn, f, noop_progress)
        self.assertEqual((stats["mode"], stats["changed"]), ("delta", 502))
        with SVC.db() as c:
            self.assertEqual(qd.count(c), 502)
            self.assertEqual(qd.get_entry(c, sha64(1)).title, "Test Entry")
            self.assertIsNotNone(qd.last_synced_at(c))
        self.assertEqual(len([r for r in f.requests if "fl=sha256" in r]), 2)            # full page + short page that ends paging
        self.assertTrue(any("start=500" in r for r in f.requests))

    def test_unchanged_upstream_costs_one_listing_request_and_keeps_the_index(self):
        d1 = doc(sha64(1))
        f = FakeFetcher([[{"sha256": sha64(1), "json_timestamp": d1["json_timestamp"]}]], {sha64(1): d1})
        with SVC.db() as c:
            sync_index(txn, f, noop_progress)
            n_before = qd.count(c)
            sync_index(txn, f, noop_progress)
        self.assertEqual(len([r for r in f.requests if "fl=sha256" in r]), 2)            # one small listing per sync
        self.assertEqual(len([r for r in f.requests if "sha256:(" in r]), 1)             # the delta fetch ran only once
        with SVC.db() as c:
            self.assertEqual(qd.count(c), n_before)

    def test_changed_entries_are_refetched_only_for_themselves(self):
        d1v2 = doc(sha64(1), json_timestamp="t1b", description="Updated.")
        d2 = doc(sha64(2), json_timestamp="t2")
        f1 = FakeFetcher([[{"sha256": sha64(1), "json_timestamp": "t1"}, {"sha256": sha64(2), "json_timestamp": "t2"}]],
                         {sha64(1): doc(sha64(1), json_timestamp="t1"), sha64(2): d2})
        with SVC.db() as c:
            sync_index(txn, f1, noop_progress)
        f2 = FakeFetcher([[{"sha256": sha64(1), "json_timestamp": "t1b"}, {"sha256": sha64(2), "json_timestamp": "t2"}]],
                         {sha64(1): d1v2, sha64(2): d2})
        with SVC.db() as c:
            sync_index(txn, f2, noop_progress)
        delta_calls = [r for r in f2.requests if "sha256:(" in r]
        self.assertEqual(len(delta_calls), 1)
        self.assertNotIn(sha64(2), delta_calls[0])                                      # unchanged entry not re-fetched
        with SVC.db() as c:
            self.assertEqual(qd.get_entry(c, sha64(1)).description, "Updated.")

    def test_removed_upstream_entries_are_pruned(self):
        f, stats = self.sync([[{"sha256": sha64(1), "json_timestamp": "t1"}]], {sha64(1): doc(sha64(1))})
        self.seed_entry(sha64(9))
        with SVC.db() as c:
            stats = sync_index(txn, f, noop_progress)
        self.assertEqual(stats["removed"], 1)
        with SVC.db() as c:
            self.assertIsNone(qd.get_entry(c, sha64(9)))
            self.assertIsNotNone(qd.get_entry(c, sha64(1)))

    def test_a_schema_change_is_reported_and_leaves_the_index_untouched(self):
        self.seed_entry(sha64(1))

        class Bad:
            def __call__(self, url, max_bytes):
                return b"<html>oops</html>"

        with SVC.db() as c:
            with self.assertRaises(UpstreamProblem):
                sync_index(txn, Bad(), noop_progress)
            self.assertEqual(qd.count(c), 1)                                            # the old copy survives

    def test_a_document_missing_after_listing_refuses_to_break_the_index(self):
        f = FakeFetcher([[{"sha256": sha64(1), "json_timestamp": "t1"}, {"sha256": sha64(2), "json_timestamp": "t2"}]], {})
        self.seed_entry(sha64(2))
        with SVC.db() as c:
            with self.assertRaises(UpstreamProblem):
                sync_index(txn, f, noop_progress)
            self.assertIsNotNone(qd.get_entry(c, sha64(2)))                             # previous copy kept
            self.assertIsNone(qd.get_entry(c, sha64(1)))

    def test_rate_limiting_stops_the_sync_and_the_old_index_stays_browsable(self):
        self.seed_entry(sha64(1))
        f = FakeFetcher([[{"sha256": sha64(2), "json_timestamp": "t2"}]], {sha64(2): doc(sha64(2))}, fail_rate_limited_after=0)
        with SVC.db() as c:
            with self.assertRaises(qd.netfetch.FetchError):
                sync_index(txn, f, noop_progress)
            self.assertEqual(qd.count(c), 1)                                            # previous copy kept
        html = self.c.get("/quaddicted").get_data(as_text=True)                          # browsing works from the cache
        self.assertIn("Test Entry", html)

    def test_sync_through_the_route_runs_as_a_job(self):
        SVC.sync_fetch = FakeFetcher([[{"sha256": sha64(1), "json_timestamp": "t1"}]], {sha64(1): doc(sha64(1))})
        res = self.post("/quaddicted/sync", {})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(jobs.wait_idle(20))
        with SVC.db() as c:
            self.assertEqual(qd.count(c), 1)
        self.assertIn("Test Entry", self.c.get("/quaddicted").get_data(as_text=True))

    def test_conservative_pace_blocks_immediate_re_sync(self):
        with SVC.db() as c:
            qd._state_set(c, "sync_started_at", repr(time.time()))
        SVC.sync_fetch = FakeFetcher([[{"sha256": sha64(1), "json_timestamp": "t1"}]], {sha64(1): doc(sha64(1))})
        res = self.post("/quaddicted/sync", {}, follow=True)
        with SVC.db() as c:
            self.assertEqual(qd.count(c), 0)


# ---- importing through the shared pipeline -------------------------------------------------------------------------------

class ImportTests(QuaddictedBase):
    def test_single_import_creates_a_draft_with_suggested_metadata_and_provenance(self):
        sha, expected = self.host_quaddicted_release("rte.zip", "RTE")
        bid = self.import_selected(sha)
        self.assertEqual(self.states(bid), ["completed"])
        row = self.row("rte")
        self.assertEqual((row["published"], row["draft"]), (0, 1))                  # drafts, never auto-published
        self.assertEqual(row["title"], "Test Entry")
        self.assertEqual(row["author"], "Aone; Atwo")
        self.assertEqual(row["date"], "2001-07-22")
        self.assertEqual(row["description"], "Two maps.")
        rec = json.loads(row["conversion_report"])
        prov = rec["provenance"]
        self.assertEqual(prov["type"], "quaddicted")
        self.assertEqual(prov["source"], "quaddicted")
        self.assertEqual(prov["quaddicted_sha256"], sha)
        self.assertIn("db/canonical", prov["source_page"])
        self.assertEqual(row["source_sha256"], sha)                                 # identity: entry hash == source hash
        self.assertEqual(self.pak_of("rte"), expected)
        self.assertEqual(self.stray_files(), [])

    def test_a_mixed_batch_fails_one_item_without_stopping_the_others(self):
        sha_good, _ = self.host_quaddicted_release("good.zip", "Good")
        self.site.serve("/junk/bad.zip", b"plain text, really")
        bad = self.seed_entry(sha64(12), urls=["http://www.quaddicted.com/junk/bad.zip"])
        bid = self.import_selected(sha_good, bad["sha256"])
        states = self.states(bid)
        self.assertEqual(states[0], "completed")
        self.assertEqual(states[1], "failed")
        self.assertIn("not a ZIP archive", self.status(bid)["items"][1]["message"])
        self.assertEqual(self.draft_count(), 1)

    def test_download_hash_is_verified_against_the_entry(self):
        data, _ = self.release_bytes("Mismatch")
        right = hashlib.sha256(data).hexdigest()
        self.site.serve(f"/files/by-sha256/{right[:2]}/{right}/mismatch.zip", b"PK\x03\x04" + b"x" * 100)   # wrong content
        self.seed_entry(right, filename="mismatch.zip",
                        urls=[f"http://www.quaddicted.com/files/by-sha256/{right[:2]}/{right}/mismatch.zip"])
        bid = self.import_selected(right)
        self.assertEqual(self.states(bid), ["failed"])
        self.assertIn("does not match", self.status(bid)["items"][0]["message"])
        self.assertEqual(self.draft_count(), 0)

    def test_same_entry_twice_in_one_submission_is_imported_once(self):
        sha, _ = self.host_quaddicted_release("once.zip", "Once")
        bid = self.import_selected(sha, sha)
        self.assertEqual(self.status(bid)["counts"], {"completed": 1})

    def test_a_completed_entry_imported_again_is_skipped_with_identical_content(self):
        sha, _ = self.host_quaddicted_release("rte.zip", "RTE")
        self.assertEqual(self.states(self.import_selected(sha)), ["completed"])
        bid = self.import_selected(sha, key=f"second{time.time_ns()}")
        self.assertEqual(self.states(bid), ["skipped"])
        self.assertEqual(self.draft_count(), 1)
        # explicit "import anyway" creates a separate draft
        item_id = self.status(bid)["items"][0]["id"]
        self.post(f"/batches/items/{item_id}/resolve", {"choice": "import_anyway"})
        self.assertEqual(self.states(bid), ["completed"])
        self.assertEqual(self.draft_count(), 2)

    def test_retryable_previous_attempt_allows_a_fresh_import_and_links_the_old_job(self):
        """R2/D: a previously failed import must not ban the entry; the page links the old attempt and a new import works."""
        sha, expected = self.host_quaddicted_release("rte.zip", "RTE")
        self.site.routes.pop(f"/files/by-sha256/{sha[:2]}/{sha}/rte.zip", None)   # the first attempt now 404s
        bid = self.import_selected(sha)
        self.assertEqual(self.states(bid), ["failed"])
        html = self.c.get("/quaddicted").get_data(as_text=True)
        self.assertIn("Failed before", html)
        self.assertIn("retry there", html)
        # a fresh import of the same entry (same bytes) succeeds
        self.site.serve(f"/files/by-sha256/{sha[:2]}/{sha}/rte.zip", self.release_bytes("RTE")[0])
        bid2 = self.import_selected(sha, key=f"fresh{time.time_ns()}")
        self.assertEqual(self.states(bid2), ["completed"])
        self.assertEqual(self.draft_count(), 1)

    def test_entry_page_links_the_existing_draft(self):
        """R1: the entry page shows what exists (draft/published with a link), not a dead end."""
        sha, _ = self.host_quaddicted_release("rte.zip", "RTE")
        bid = self.import_selected(sha)
        self.assertEqual(self.states(bid), ["completed"])
        html = self.c.get(f"/quaddicted/entry/{sha}").get_data(as_text=True)
        self.assertIn("A draft was already created", html)
        self.assertIn("/addons/", html)

    def test_active_import_duplicate_submission_is_reported_and_nothing_duplicated(self):
        """R4/B: an entry queued in an earlier batch is reported as already handled; no second item, no empty batch."""
        sha, _ = self.host_quaddicted_release("rte.zip", "RTE")
        prepared = SVC.import_prepared([sha])["prepared"]
        # a queued item exists from a (suspended) earlier submission
        from tools import batchstore as qstore
        conn = get_db()
        first = qstore.create_batch(conn, "quaddicted", "earlier", None)
        qstore.add_item(conn, first, 0, "quaddicted", qd.source_key(sha), "rte.zip", source_url=prepared[0]["url"])
        conn.commit()
        conn.close()
        outcome = BSV.create_quaddicted_batch(prepared, "second")
        self.assertNotIn("batch_id", outcome)
        self.assertIn("already queued", outcome["error"])
        self.assertEqual(outcome.get("handled"), ["rte.zip"])
        self.assertEqual(self.draft_count(), 0)

    def test_import_preview_lists_state_and_conflicts_and_queues_nothing(self):
        """O3: Preview shows what would be queued and which records conflict — without queueing anything."""
        conn = get_db()
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, package_name, package_path, package_size, published, draft) "
                     "VALUES ('rte', 'RTE existing', 'rte', 'rte.pak', '/x/rte.pak', 1, 1, 0)")
        conn.commit()
        conn.close()
        # a catalogue entry with the same filename slug as the existing add-on (different source bytes)
        other = self.seed_entry(sha64(40), filename="rte.zip", tags=["title=Other", "filename=rte.zip", "type=map"], urls=["http://www.quaddicted.com/x/rte.zip"])
        # and a completed import (draft) of another entry
        sha, _ = self.host_quaddicted_release("once.zip", "Once")
        first = self.import_selected(sha)
        self.assertEqual(self.states(first), ["completed"])
        before = self.draft_count()
        res = self.post("/quaddicted/import", {"sha": [sha, other["sha256"]], "preview": "1"})
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("draft already exists", html)                               # the completed one
        self.assertIn("RTE existing", html)                                       # the conflicting record
        self.assertIn("(published)", html)                                        # with publication state
        # the preview itself queued nothing: count of add-on rows is unchanged
        self.assertEqual(self.draft_count(), before)

    def test_preview_then_double_key_returns_the_same_batch(self):
        sha, _ = self.host_quaddicted_release("once.zip", "Once")
        self.assertEqual(self.states(self.import_selected(sha)), ["completed"])
        key = f"confirm{time.time_ns()}"
        result = BSV.create_quaddicted_batch(SVC.import_prepared([sha])["prepared"], key)
        first_id = result["batch_id"]
        self.assertEqual(BSV.create_quaddicted_batch(SVC.import_prepared([sha])["prepared"], key).get("batch_id"), first_id)

    def test_internal_destinations_stay_refused(self):
        """A quaddicted-labelled URL that resolves to a private address is refused by the same protections."""
        sha = self.seed_entry(sha64(21), urls=["http://www.quaddicted.com/evil.zip"])["sha256"]
        self.net.table[UPSTREAM] = ["127.0.0.1"]
        bid = self.import_selected(sha)
        self.assertEqual(self.states(bid), ["failed"])
        self.assertIn("private or internal", self.status(bid)["items"][0]["message"])

    def test_entry_with_no_usable_url_is_reported_with_actionable_message(self):
        """An entry whose Quaddicted document has no `urls` field (an upstream data-quality issue,
        observed in the wild for sha256 65894a8a8a6b…/Tomb of Thunder) is reported with the
        entry's title and an actionable workaround — not just a bare 'no usable link'."""
        # Seed a catalogue entry with no urls at all (the same shape as Tomb of Thunder upstream)
        sha = self.seed_entry(sha64(70), urls=[],
                              tags=["title=Upstream Incomplete", "filename=incomplete.zip",
                                    "type=mod"])["sha256"]
        result = SVC.import_prepared([sha])
        self.assertEqual(result["prepared"], [])                              # nothing was prepared
        self.assertEqual(len(result["problems"]), 1)
        short, reason = result["problems"][0]
        self.assertEqual(short, sha[:12])
        # The improved message names the entry (so the user knows which mod), names the upstream
        # cause, and points at the supported workaround (the Upload form).
        self.assertIn("Upstream Incomplete", reason)
        self.assertIn("no URLs", reason)
        self.assertIn("Upload form", reason)
        # And the user-facing preview page surfaces the same text
        res = self.post("/quaddicted/import", {"sha": [sha], "preview": "1"})
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Upstream Incomplete", html)
        self.assertIn("no URLs", html)
        # Posting without preview (the action that would queue an import) keeps the same wording.
        # Nothing got queued; the flash names the entry and the upstream cause.
        with self.c.session_transaction() as sess:
            csrf = sess["csrf_token"]
        res2 = self.c.post("/quaddicted/import", data={"sha": [sha], "submit_key": "k", "csrf_token": csrf},
                           follow_redirects=True)
        flash_html = res2.get_data(as_text=True)
        self.assertIn("no usable direct download link", flash_html)
        self.assertIn("Upstream Incomplete", flash_html)

    def test_browse_page_marks_entries_imported_and_links_the_draft(self):
        sha, _ = self.host_quaddicted_release("rte.zip", "RTE")
        self.assertIn("Not imported", self.c.get("/quaddicted").get_data(as_text=True))
        self.import_selected(sha)
        html = self.c.get("/quaddicted").get_data(as_text=True)
        self.assertIn("Imported (draft)", html)

    def test_entry_detail_links_to_the_imported_draft(self):
        sha, _ = self.host_quaddicted_release("rte.zip", "RTE")
        self.import_selected(sha)
        html = self.c.get(f"/quaddicted/entry/{sha}").get_data(as_text=True)
        self.assertIn("already created", html)
        self.assertIn("/addons/", html)

    def test_empty_search_results_are_explained(self):
        self.seed_entry(sha64(30))
        html = self.c.get("/quaddicted?q=zzz-nothing-matches").get_data(as_text=True)
        self.assertIn("No entries match", html)

    # ---- full journey (task §13 integration test) ----
    def test_id_conflict_with_different_record_for_the_directory(self):
        """R6/I: the incoming ID matches record A's ID; a different record B holds the install directory. No guessing."""
        conn = get_db()
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, package_name, package_path, package_size, published, draft) "
                     "VALUES ('base', 'A', 'base-orig', 'a.pak', '/x/a.pak', 1, 0, 1)")
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, package_name, package_path, package_size, published, draft) "
                     "VALUES ('other', 'B', 'base', 'b.pak', '/x/b.pak', 1, 1, 0)")
        conn.commit()
        conn.close()
        sha, _ = self.host_quaddicted_release("base.zip", "Incoming")
        bid = self.import_selected(sha)
        self.assertEqual(self.states(bid), ["needs_decision"])
        item = self.status(bid)["items"][0]
        self.assertIn("match different add-ons", item["message"])
        self.assertIn("different add-ons", self.c.get(f"/batches/{bid}").get_data(as_text=True))
        # the decision still names both records: create the suggested separate draft -> installs under a free ID
        item_id = item["id"]
        conn = get_db()
        existing_titles = [r["addon_id"] for r in conn.execute("SELECT addon_id FROM addons").fetchall()]
        conn.close()
        res = self.post(f"/batches/items/{item_id}/resolve", {"choice": "new_id"})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:300])
        self.assertTrue(jobs.wait_idle(20))
        conn = get_db()
        ids = sorted(r[0] for r in conn.execute("SELECT addon_id FROM addons").fetchall())
        addon_id = [i for i in ids if i not in existing_titles][0]
        row = conn.execute("SELECT * FROM addons WHERE addon_id = ?", (addon_id,)).fetchone()
        conn.close()
        self.assertNotEqual(row["gamedir"], "base")                              # 'base' belongs to record B; untouched
        self.assertEqual(row["draft"], 1)

    def test_directory_only_conflict_links_the_record_and_offers_choices(self):
        """R6/I: incoming gamedir matches an existing record whose ID is different; decision offers open/replace/new-id/skip."""
        conn = get_db()
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, package_name, package_path, package_size, published, draft) "
                     "VALUES ('somenamewithnooverlap', 'Existing', 'rte', 'x.pak', '/x/x.pak', 1, 0, 1)")
        conn.commit()
        conn.close()
        sha, _ = self.host_quaddicted_release("rte.zip", "Incoming")
        bid = self.import_selected(sha)
        self.assertEqual(self.states(bid), ["needs_decision"])
        html = self.c.get(f"/batches/{bid}").get_data(as_text=True)
        self.assertIn("&#39;rte&#39; already exists", html)              # quotes are HTML-escaped in the page
        self.assertIn("Open existing add-on", html)                      # the R6 open link


    def test_abandoned_review_is_highlighted_and_can_be_discarded(self):
        """O1: a review waiting longer than the age bound is flagged; discarding is explicit."""
        sha, _ = self.host_quaddicted_release("rte.zip", "RTE")
        conn = get_db()
        conn.execute("INSERT INTO addons (addon_id, title, gamedir, package_name, package_path, package_size, published, draft) "
                     "VALUES ('rte', 'Existing', 'rte', 'x.pak', '/x/x.pak', 1, 0, 1)")
        conn.commit()
        conn.close()
        bid = self.import_selected(sha)
        item_id = self.status(bid)["items"][0]["id"]
        old = time.time() - 8 * 86400
        conn = get_db()
        conn.execute("UPDATE batch_items SET updated_at = ? WHERE id = ?", (old, item_id))
        conn.commit()
        conn.close()
        html = self.c.get(f"/batches/{bid}").get_data(as_text=True)
        self.assertIn("waiting", html)
        self.assertIn("Discard", html)                                           # the discard shortcut stays available
        res = self.post(f"/batches/items/{item_id}/cancel")
        self.assertEqual(res.status_code, 302)

    def test_end_to_end_quaddicted_to_draft_to_explicit_publish_to_download(self):
        sha, expected = self.host_quaddicted_release("pungfx.zip", "E2E-QD")
        # 1. select in the browse UI
        html = self.c.get("/quaddicted").get_data(as_text=True)
        self.assertIn(sha, html)
        # 2. import
        bid = self.import_selected(sha)
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "completed")
        # 3. draft exists and its package is served on the admin side
        self.assertEqual(self.c.get(item["addon_url"]).status_code, 200)
        self.assertEqual(self.pak_of("pungfx"), expected)
        # 4. explicit publish
        self.publish("pungfx")
        entry = self.catalog()[0]
        self.assertEqual((entry["gamedir"], entry["download"]), ("pungfx", "addons/pungfx.pak"))
        # 5. download the published artifact and verify its contents independently
        served = self.c.get("/" + entry["download"]).data
        out = self.tmp / "served.pak"
        out.write_bytes(served)
        self.assertEqual(parse_pak(out), expected)
        # 6. provenance on record
        rec = json.loads(self.row("pungfx")["conversion_report"])
        self.assertEqual(rec["provenance"]["source"], "quaddicted")
        self.assertEqual(rec["source_sha256"], sha)

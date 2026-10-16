import hashlib
import http.client
import io
import json
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import app as app_module
from app import app, get_db, jobs
from tests.test_convert_options import gradient, tga_bytes
from tests.test_pak_report import PUN_TXT, make_bsp, wav
from tests.test_publication import PublicationBase
from tests.test_zip_conversion import make_pak_bytes, parse_pak, write_zip
from tools.jobs import JobCancelled


def release_zip(path: Path, extra=()):
    pak0 = make_pak_bytes([("maps/a.bsp", make_bsp(worldspawn={"message": "A"})), ("sound/x.wav", wav())])
    write_zip(path, [("pak0.pak", pak0), ("maps/b.bsp", make_bsp()), ("maps/b.map", b"map source" * 100), ("pun.txt", PUN_TXT), *extra])
    return path


class FlowBase(PublicationBase):
    threaded = False

    def setUp(self):
        super().setUp()
        app.config["JOB_MODE"] = "thread" if self.threaded else "inline"
        self.addCleanup(lambda: jobs.wait_idle(20))

    def upload(self, *args, **kwargs):
        response = super().upload(*args, **kwargs)
        self.assertTrue(jobs.wait_idle(20))                                # in threaded mode the build outlives the request
        return response

    def publish(self, addon_id):
        response = super().publish(addon_id)
        self.assertTrue(jobs.wait_idle(20))
        return response

    def post(self, path, data=None, files=None, headers=None, follow=False):
        form = dict(data or {})
        form["csrf_token"] = self.csrf()
        if files:
            form.update(files)
        return self.c.post(path, data=form, content_type="multipart/form-data", headers=headers or {}, follow_redirects=follow)

    def new_addon_form(self, addon_id="rel", **extra):
        return {"title": addon_id, "addon_id": addon_id, "gamedir": addon_id, "author": "T", "date": "2026/10/16", "description": "d", **extra}

    def start_upload(self, zip_path, addon_id="rel", headers=None, follow=False, **extra):
        data = self.new_addon_form(addon_id, **extra)
        data["package"] = (zip_path.open("rb"), zip_path.name)
        return self.post("/addons/new", data, headers=headers, follow=follow)

    def job_id_from(self, response):
        return response.headers["Location"].rsplit("/", 1)[-1]

    def status(self, job_id):
        res = self.c.get(f"/jobs/{job_id}/status")
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def finish(self, job_id, timeout=20):
        self.assertTrue(jobs.wait_idle(timeout))
        return self.status(job_id)

    def last_import(self):
        conn = get_db()
        try:
            return conn.execute("SELECT * FROM imports ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()

    def import_count(self):
        conn = get_db()
        try:
            return conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        finally:
            conn.close()

    def stray_files(self):
        out = []
        for root in (Path(app.config["PERSIST_ROOT"]) / "packages", Path(app.config["PUBLIC_ROOT"]) / "addons"):
            if root.is_dir():
                out += [p.name for p in root.iterdir() if p.name.startswith(".") or p.name.endswith(".tmp")]
        up = Path(app.config["UPLOAD_FOLDER"])
        out += [p.name for p in up.iterdir() if p.name != ".work"]
        work = up / ".work"
        if work.is_dir():
            out += [f".work/{p.name}" for p in work.iterdir()]
        return sorted(out)


class ThreadedBuildTests(FlowBase):
    threaded = True

    def slow_build(self, gate, entered, loops=0):
        real = app_module.build_single_pak_from_zip

        def build(source, target, **kw):
            progress = kw["progress"]
            progress("Extracting files", 0, 100)
            entered.set()
            time.sleep(0.5)                                                # progress writes are throttled to one per 0.4 s
            progress("Extracting files", 40, 100)
            gate.wait(20)
            for i in range(loops):                                         # give a cancel request time to land
                progress("Extracting files", 40 + i, 100)
                time.sleep(0.02)
            return real(source, target, **kw)
        return build

    def test_post_returns_at_once_while_the_conversion_keeps_running(self):
        gate, entered = threading.Event(), threading.Event()
        app_module.build_single_pak_from_zip, real = self.slow_build(gate, entered), app_module.build_single_pak_from_zip
        self.addCleanup(setattr, app_module, "build_single_pak_from_zip", real)
        z = release_zip(self.tmp / "rel.zip")
        t0 = time.monotonic()
        res = self.start_upload(z, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(res.status_code, 200)
        target = res.get_json()["redirect"]
        job_id = target.rsplit("/", 1)[-1]
        self.assertTrue(entered.wait(10))
        time.sleep(0.9)
        st = self.status(job_id)                                           # the browser's poll while the work is in progress
        self.assertEqual((st["state"], st["stage"], st["done"], st["total"], st["finished"]), ("running", "Extracting files", 40, 100, False))
        self.assertTrue(0 < st["percent"] < 100)
        self.assertIsNone(self.row("rel"))                                  # nothing is installed before the build finishes
        page = self.c.get(target).get_data(as_text=True)
        self.assertIn('id="job-bar"', page)
        self.assertIn("http-equiv=\"refresh\"", page)                        # no-JavaScript fallback keeps polling
        self.assertIn("Running jobs", self.c.get("/dashboard").get_data(as_text=True))
        gate.set()
        st = self.finish(job_id)
        self.assertEqual((st["state"], st["percent"], st["level"]), ("done", 100.0, "success"))
        self.assertIsNotNone(self.row("rel"))
        self.assertEqual(self.stray_files(), [])

    def test_cancel_during_the_build_leaves_nothing_behind(self):
        gate, entered = threading.Event(), threading.Event()
        gate.set()
        app_module.build_single_pak_from_zip, real = self.slow_build(gate, entered, loops=500), app_module.build_single_pak_from_zip
        self.addCleanup(setattr, app_module, "build_single_pak_from_zip", real)
        res = self.start_upload(release_zip(self.tmp / "rel.zip"), headers={"X-Requested-With": "XMLHttpRequest"})
        job_id = res.get_json()["redirect"].rsplit("/", 1)[-1]
        self.assertTrue(entered.wait(10))
        self.assertEqual(self.post(f"/jobs/{job_id}/cancel", headers={"X-Requested-With": "XMLHttpRequest"}).get_json(), {"ok": True})
        st = self.finish(job_id)
        self.assertEqual((st["state"], st["level"]), ("cancelled", "info"))
        self.assertIsNone(self.row("rel"))
        self.assertEqual((self.stray_files(), self.import_count()), ([], 0))

    def test_real_builder_cancelled_mid_write_leaves_no_package_or_temp_files(self):
        real = app_module.build_single_pak_from_zip

        def build(source, target, **kw):
            progress = kw["progress"]

            def cancelling(stage, done, total):
                if stage == "Writing package" and done > 0:
                    raise JobCancelled()
                progress(stage, done, total)
            return real(source, target, **{**kw, "progress": cancelling})
        app_module.build_single_pak_from_zip = build
        self.addCleanup(setattr, app_module, "build_single_pak_from_zip", real)
        job_id = self.job_id_from(self.start_upload(release_zip(self.tmp / "rel.zip")))
        st = self.finish(job_id)
        self.assertEqual(st["state"], "cancelled")
        self.assertEqual((self.stray_files(), self.row("rel")), ([], None))

    def test_publish_is_a_job_and_the_copy_reports_progress(self):
        self.upload("pubjob", make_pak_bytes([("maps/a.bsp", make_bsp())]))
        gate, entered = threading.Event(), threading.Event()
        real = app_module._copy_hashing

        def slow_copy(src, dst, progress, stage):
            if "public" in stage:
                progress(stage, 10, 100)
                entered.set()
                gate.wait(20)
            return real(src, dst, progress, stage)
        app_module._copy_hashing = slow_copy
        self.addCleanup(setattr, app_module, "_copy_hashing", real)
        pk = self.row("pubjob")["id"]
        t0 = time.monotonic()
        res = self.post(f"/addons/{pk}/publish")
        self.assertLess(time.monotonic() - t0, 3)
        job_id = self.job_id_from(res)
        self.assertTrue(entered.wait(10))
        st = self.status(job_id)
        self.assertEqual((st["state"], st["stage"]), ("running", "Copying to the public folder"))
        self.assertEqual(self.c.get("/addons/pubjob.pak").status_code, 404)  # not live until the copy is verified and moved
        gate.set()
        self.assertEqual(self.finish(job_id)["state"], "done")
        self.assertEqual(self.c.get("/addons/pubjob.pak").status_code, 200)
        self.assertEqual(self.stray_files(), [])

    def test_publish_refuses_a_package_changed_on_disk_and_keeps_the_live_copy(self):
        good = make_pak_bytes([("maps/a.bsp", make_bsp())])
        self.upload("tamper", good)
        self.publish("tamper")
        live = self.c.get("/addons/tamper.pak").data
        package = Path(self.row("tamper")["package_path"])
        package.write_bytes(make_pak_bytes([("maps/evil.bsp", make_bsp())]))
        job_id = self.job_id_from(self.post(f"/addons/{self.row('tamper')['id']}/publish"))
        st = self.finish(job_id)
        self.assertEqual(st["state"], "failed")
        self.assertIn("Stored package hash does not match", st["message"])
        self.assertEqual(self.c.get("/addons/tamper.pak").data, live)
        self.assertEqual(self.stray_files(), [])


class ProxyTimeoutTests(FlowBase):
    """A reverse proxy that gives up when the upstream is silent for longer than UPSTREAM_TIMEOUT (like nginx proxy_read_timeout)."""
    threaded = True
    UPSTREAM_TIMEOUT = 1.5

    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        self.upstream = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.shutdown)
        test = self
        up_port = self.upstream.server_port

        class Proxy(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                conn = http.client.HTTPConnection("127.0.0.1", up_port, timeout=test.UPSTREAM_TIMEOUT)
                try:
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
                    conn.request(self.command, self.path, body, headers)
                    resp = conn.getresponse()
                    data = resp.read()
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                            self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (socket.timeout, TimeoutError):
                    test.proxy_timeouts += 1
                    self.send_response(504)
                    self.end_headers()
                finally:
                    conn.close()
            do_GET = do_POST = forward

        self.proxy_timeouts = 0
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.shutdown)
        self.base = f"http://127.0.0.1:{self.proxy.server_port}"

    def client(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from accept_zip import Client
        c = Client(self.base)
        c.post("/login", {"username": "admin", "password": "secret"})
        return c

    def test_conversion_longer_than_the_proxy_timeout_still_completes(self):
        real = app_module.build_single_pak_from_zip
        duration = self.UPSTREAM_TIMEOUT * 2.5

        def slow(source, target, **kw):
            end = time.monotonic() + duration
            progress = kw["progress"]
            while time.monotonic() < end:
                progress("Extracting files", int((time.monotonic() - end + duration) * 100), int(duration * 100))
                time.sleep(0.05)
            return real(source, target, **kw)
        app_module.build_single_pak_from_zip = slow
        self.addCleanup(setattr, app_module, "build_single_pak_from_zip", real)
        z = release_zip(self.tmp / "rel.zip")
        c = self.client()
        t0 = time.monotonic()
        raw = c.post("/addons/new", self.new_addon_form(), {"package": (z.name, z.read_bytes())}, csrf_from="/addons/new",
                     headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertLess(time.monotonic() - t0, self.UPSTREAM_TIMEOUT)      # the POST itself was quick
        job_id = json.loads(raw)["redirect"].rsplit("/", 1)[-1]
        states, last = [], None
        while True:
            last = json.loads(c.get(f"/jobs/{job_id}/status"))             # each poll is a fresh short request through the proxy
            states.append((last["state"], last["percent"]))
            if last["finished"]:
                break
            time.sleep(0.2)
        total = time.monotonic() - t0
        self.assertGreater(total, self.UPSTREAM_TIMEOUT * 1.5)             # the work really did outlast the proxy's patience
        self.assertEqual(self.proxy_timeouts, 0)                           # ...yet the proxy never timed out a request
        self.assertEqual(last["state"], "done")
        self.assertGreater(len({p for _, p in states}), 3)                 # and the user saw the bar move
        self.assertEqual([p for _, p in states], sorted(p for _, p in states))
        self.assertIsNotNone(self.row("rel"))

    def test_the_control_a_synchronous_request_that_long_would_have_timed_out(self):
        # Demonstrates the failure mode being avoided: any single request silent for longer than the proxy timeout gets a 504.
        real = app_module.generate_catalog
        app_module.generate_catalog = lambda: (time.sleep(self.UPSTREAM_TIMEOUT * 2), real())[1]
        self.addCleanup(setattr, app_module, "generate_catalog", real)
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        conn.request("GET", "/content.json")                               # builds the catalog on first request: slow on purpose
        self.assertEqual(conn.getresponse().status, 504)
        self.assertEqual(self.proxy_timeouts, 1)


class ReviewFlowTests(FlowBase):
    def build_review(self, extra_members=(), **extra):
        z = release_zip(self.tmp / "rel.zip", extra_members)
        res = self.start_upload(z, review="1", **extra)
        self.assertEqual(res.status_code, 302)
        job = self.status(self.job_id_from(res))
        self.assertEqual((job["state"], job["auto_redirect"]), ("done", True))
        return self.last_import()["token"], job

    def test_review_shows_the_plan_and_builds_nothing_yet(self):
        token, job = self.build_review()
        self.assertEqual(job["next_url"], f"/imports/{token}")
        self.assertIsNone(self.row("rel"))
        html = self.c.get(f"/imports/{token}").get_data(as_text=True)
        self.assertIn("Review the conversion", html)
        self.assertIn("5 of 2048 allowed", html)
        for preset in ("authoring", "junk", "docs"):
            self.assertIn(f'name="exclude_presets" value="{preset}" >', html)       # every option starts switched off
        for name in ("reencode_textures", "reencode_skyboxes", "generate_mapdb", "keep_source"):
            self.assertRegex(html, rf'name="{name}" value="1" >')
        self.assertEqual(self.stray_files(), [token])                      # staged upload, nothing else

    def test_options_flow_end_to_end(self):
        tga = tga_bytes(64, 64, gradient(64, 64, 3), channels=3)
        token, _ = self.build_review([("gfx/t.tga", tga), ("gfx/env/s_up.tga", tga)])
        # update the preview with an exclusion preset: the plan changes, still nothing built
        res = self.post(f"/imports/{token}/plan", {"exclude_presets": ["authoring"], "exclude_patterns": "pun.txt"})
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        plan = json.loads(self.last_import()["plan_json"])
        self.assertEqual((plan["excluded"]["total"], plan["blockers"]), (2, []))
        self.assertIsNone(self.row("rel"))
        # build with exclusions + texture re-encoding (not skyboxes)
        res = self.post(f"/imports/{token}/build", {"exclude_presets": ["authoring"], "exclude_patterns": "pun.txt", "reencode_textures": "1", "keep_source": "1"})
        st = self.status(self.job_id_from(res))
        self.assertEqual(st["state"], "done", st["message"])
        got = parse_pak(Path(self.row("rel")["package_path"]))
        self.assertEqual(sorted(got), ["gfx/env/s_up.tga", "gfx/t.png", "maps/a.bsp", "maps/b.bsp", "sound/x.wav"])
        rec = json.loads(self.row("rel")["conversion_report"])
        self.assertEqual(rec["options"]["exclude_presets"], ["authoring"])
        self.assertEqual((rec["excluded"]["total"], rec["reencoded"]["converted"]), (2, 1))
        self.assertEqual(self.import_count(), 0)
        html = self.c.get(f"/addons/{self.row('rel')['id']}").get_data(as_text=True)
        self.assertIn("Options used", html)
        self.assertIn("1 image(s) re-encoded", html)
        self.assertIn("2 file(s)", html)

    def test_bad_options_are_rejected_without_losing_the_upload(self):
        token, _ = self.build_review()
        res = self.post(f"/imports/{token}/build", {"exclude_presets": ["nonsense"]}, follow=True)
        self.assertIn("Options rejected", res.get_data(as_text=True))
        res = self.post(f"/imports/{token}/plan", {"exclude_patterns": "*"}, follow=True)
        self.assertIn("would match everything", res.get_data(as_text=True))
        self.assertIsNotNone(self.last_import())
        self.assertIsNone(self.row("rel"))

    def test_ambiguous_release_can_be_fixed_by_choosing_a_root(self):
        z = self.tmp / "two.zip"
        write_zip(z, [("moda/maps/a.bsp", make_bsp()), ("modb/maps/b.bsp", make_bsp())])
        res = self.start_upload(z, review="1")
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        token = self.last_import()["token"]
        html = self.c.get(f"/imports/{token}").get_data(as_text=True)
        self.assertIn("ambiguous root mapping", html)
        self.assertRegex(html, r'<button type="submit"[^>]*formaction="[^"]*build"[^>]*disabled')
        # building anyway is refused by the converter too, and the upload is kept for another try
        st = self.status(self.job_id_from(self.post(f"/imports/{token}/build", {})))
        self.assertEqual(st["state"], "failed")
        self.assertIn("ambiguous root mapping", st["message"])
        self.assertEqual(st["next_url"], f"/imports/{token}")
        self.assertIsNotNone(self.last_import())
        st = self.status(self.job_id_from(self.post(f"/imports/{token}/build", {"root": "modb"})))
        self.assertEqual(st["state"], "done", st["message"])
        self.assertEqual(sorted(parse_pak(Path(self.row("rel")["package_path"]))), ["maps/b.bsp"])

    def test_names_from_the_archive_are_escaped_on_the_plan_page(self):
        z = self.tmp / "evil.zip"
        write_zip(z, [("<b onmouseover=1>moda</b>/maps/a.bsp", make_bsp()), ('"><img src=x onerror=alert(3)>/maps/b.bsp', make_bsp()),
                      ("<img src=x onerror=alert(2)>.map", b"m")])
        self.assertEqual(self.status(self.job_id_from(self.start_upload(z, review="1")))["state"], "done")
        token = self.last_import()["token"]
        res = self.post(f"/imports/{token}/plan", {"exclude_patterns": "*.map", "root": '"><img src=x onerror=alert(3)>'})
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        html = self.c.get(f"/imports/{token}").get_data(as_text=True)
        for raw in ("<script>", "<b onmouseover", "<img src=x"):
            self.assertNotIn(raw, html, raw)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;.map", html)
        self.assertIn("&lt;b onmouseover=1&gt;moda&lt;/b&gt;/", html)
        self.assertIn("&#34;&gt;&lt;img src=x onerror=alert(3)&gt;", html)

    def test_over_the_entry_limit_is_fixable_with_a_preset(self):
        members = [(f"maps/m{i}.bsp", b"x") for i in range(2040)] + [(f"maps/s{i}.map", b"s") for i in range(30)]
        z = self.tmp / "big.zip"
        write_zip(z, members)
        self.assertEqual(self.status(self.job_id_from(self.start_upload(z, review="1")))["state"], "done")
        token = self.last_import()["token"]
        html = self.c.get(f"/imports/{token}").get_data(as_text=True)
        self.assertIn("2070 of 2048", html)
        self.assertIn("refuses PAK files with more entries", html)
        res = self.post(f"/imports/{token}/plan", {"exclude_presets": ["authoring"]})
        self.assertEqual(json.loads(self.last_import()["plan_json"])["entries"], 2040)

    def test_low_disk_space_is_flagged_on_the_plan_page(self):
        from collections import namedtuple
        from unittest import mock
        Usage = namedtuple("Usage", "total used free")
        token, _ = self.build_review()
        with mock.patch("app.shutil.disk_usage", return_value=Usage(10**12, 10**12 - 1000, 1000)):
            html = self.c.get(f"/imports/{token}").get_data(as_text=True)
        self.assertIn("Low disk space", html)
        with mock.patch("app.shutil.disk_usage", return_value=Usage(10**12, 0, 10**12)):
            html = self.c.get(f"/imports/{token}").get_data(as_text=True)
        self.assertNotIn("Low disk space", html)

    def test_cancel_discards_the_upload(self):
        token, _ = self.build_review()
        res = self.post(f"/imports/{token}/cancel", follow=True)
        self.assertEqual((res.status_code, self.import_count(), self.stray_files()), (200, 0, []))
        self.assertEqual(self.c.get(f"/imports/{token}").status_code, 302)   # sends you back to the form

    def test_pak_uploads_skip_the_review(self):
        pak = make_pak_bytes([("maps/a.bsp", make_bsp())])
        f = self.tmp / "m.pak"
        f.write_bytes(pak)
        data = self.new_addon_form("plain", review="1")
        data["package"] = (f.open("rb"), "m.pak")
        res = self.post("/addons/new", data)
        self.assertEqual(self.status(self.job_id_from(res))["kind"], "build")
        self.assertEqual(Path(self.row("plain")["package_path"]).read_bytes(), pak)

    def test_unauthenticated_access_is_refused_everywhere(self):
        token, _ = self.build_review()
        anon = app.test_client()
        anon.get("/login")
        with anon.session_transaction() as s:
            csrf = s["csrf_token"]
        for method, path in (("get", f"/imports/{token}"), ("post", f"/imports/{token}/build"), ("post", f"/imports/{token}/plan"),
                             ("post", f"/imports/{token}/cancel"), ("get", "/jobs/x/status"), ("get", "/jobs/x"), ("post", "/jobs/x/cancel"),
                             ("post", "/addons/1/rebuild"), ("post", "/addons/1/publish")):
            res = getattr(anon, method)(path, data={"csrf_token": csrf}) if method == "post" else anon.get(path)
            self.assertEqual(res.status_code, 403, path)

    def test_unknown_tokens_and_jobs_are_404(self):
        self.assertEqual(self.c.get("/jobs/doesnotexist").status_code, 404)
        self.assertEqual(self.c.get("/jobs/doesnotexist/status").status_code, 404)
        self.assertEqual(self.post("/imports/nope/build").status_code, 404)


class KeepSourceTests(FlowBase):
    def kept(self, addon_id="rel", **extra):
        z = release_zip(self.tmp / "rel.zip")
        res = self.start_upload(z, addon_id, keep_source="1", **extra)
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        return z, self.row(addon_id)

    def test_kept_source_is_stored_and_listed(self):
        z, row = self.kept()
        kept = Path(row["source_path"])
        self.assertEqual((kept.read_bytes(), row["source_name"]), (z.read_bytes(), "rel.zip"))
        self.assertEqual(kept.parent.parent, Path(app.config["PERSIST_ROOT"]) / "sources")
        self.assertEqual(self.import_count(), 0)
        self.assertIn("Rebuild with different options", self.c.get(f"/addons/{row['id']}").get_data(as_text=True))

    def test_not_kept_by_default(self):
        res = self.start_upload(release_zip(self.tmp / "rel.zip"))
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        row = self.row("rel")
        self.assertIsNone(row["source_path"])
        self.assertFalse((Path(app.config["PERSIST_ROOT"]) / "sources").exists())
        self.assertIn("not kept", self.c.get(f"/addons/{row['id']}").get_data(as_text=True))

    def test_rebuild_with_different_options_replaces_the_package_and_keeps_the_source(self):
        z, row = self.kept()
        before = Path(row["package_path"]).read_bytes()
        res = self.post(f"/addons/{row['id']}/rebuild")
        st = self.status(self.job_id_from(res))
        self.assertEqual((st["kind"], st["state"]), ("plan", "done"))
        token = self.last_import()["token"]
        imp = self.last_import()
        self.assertEqual((imp["owns_source"], imp["kind"], imp["addon_pk"]), (0, "replace", row["id"]))
        st = self.status(self.job_id_from(self.post(f"/imports/{token}/build", {"exclude_presets": ["authoring"]})))
        self.assertEqual(st["state"], "done", st["message"])
        after = self.row("rel")
        self.assertNotEqual(Path(after["package_path"]).read_bytes(), before)
        self.assertNotIn("maps/b.map", parse_pak(Path(after["package_path"])))
        self.assertEqual(after["source_path"], row["source_path"])
        self.assertTrue(Path(after["source_path"]).is_file())                 # the kept source survives a rebuild
        self.assertEqual(self.import_count(), 0)

    def test_rebuilding_a_published_addon_keeps_serving_the_old_package_until_republished(self):
        z, row = self.kept()
        self.publish("rel")
        live = self.c.get("/addons/rel.pak").data
        token = (self.post(f"/addons/{row['id']}/rebuild"), self.last_import()["token"])[1]
        st = self.status(self.job_id_from(self.post(f"/imports/{token}/build", {"exclude_presets": ["authoring"]})))
        self.assertIn("until you click Publish again", st["message"])
        self.assertEqual(self.c.get("/addons/rel.pak").data, live)
        self.publish("rel")
        self.assertNotEqual(self.c.get("/addons/rel.pak").data, live)

    def test_rebuild_without_a_kept_source_is_refused(self):
        self.upload("nosrc", make_pak_bytes([("maps/a.bsp", make_bsp())]))
        res = self.post(f"/addons/{self.row('nosrc')['id']}/rebuild", follow=True)
        self.assertIn("No source ZIP was kept", res.get_data(as_text=True))
        self.assertEqual(self.import_count(), 0)

    def test_rebuild_never_touches_the_source_when_the_build_is_cancelled_or_fails(self):
        z, row = self.kept()
        token = (self.post(f"/addons/{row['id']}/rebuild"), self.last_import()["token"])[1]
        st = self.status(self.job_id_from(self.post(f"/imports/{token}/build", {"root": "nonexistent"})))
        self.assertEqual(st["state"], "failed")
        self.assertEqual(Path(row["source_path"]).read_bytes(), z.read_bytes())
        self.post(f"/imports/{token}/cancel")
        self.assertEqual(Path(row["source_path"]).read_bytes(), z.read_bytes())    # cancelling a rebuild must not delete the kept ZIP

    def test_delete_removes_the_kept_source(self):
        z, row = self.kept()
        kept = Path(row["source_path"])
        self.post(f"/addons/{row['id']}/delete")
        self.assertFalse(kept.exists())
        self.assertFalse(kept.parent.exists())

    def test_replacing_with_a_new_zip_and_keeping_it_removes_the_old_source(self):
        z, row = self.kept()
        old = Path(row["source_path"])
        z2 = self.tmp / "new.zip"
        release_zip(z2, [("extra.cfg", b"x")])
        data = {"title": "rel", "addon_id": "rel", "gamedir": "rel", "keep_source": "1", "package": (z2.open("rb"), "new.zip")}
        self.assertEqual(self.status(self.job_id_from(self.post(f"/addons/{row['id']}/edit", data)))["state"], "done")
        new = Path(self.row("rel")["source_path"])
        self.assertTrue(new.is_file() and new != old)
        self.assertFalse(old.exists())


class LocalImportTests(FlowBase):
    def test_local_import_reads_in_place_and_leaves_the_directory_untouched(self):
        src_dir = self.tmp / "shared"
        src_dir.mkdir()
        z = release_zip(src_dir / "mod.zip")
        before = (z.read_bytes(), sorted(p.name for p in src_dir.iterdir()))
        data = self.new_addon_form("local", import_mode="local", local_source=str(z))
        res = self.post("/addons/new", data)
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        self.assertEqual((z.read_bytes(), sorted(p.name for p in src_dir.iterdir())), before)   # no temp files next to the source
        self.assertEqual(self.stray_files(), [])

    def test_local_import_validation_messages_are_unchanged(self):
        for path, message in (("", "Local source path is required"), ("relative.zip", "must be absolute"), (str(self.tmp / "missing.zip"), "does not exist"),
                              (str(self.tmp), "must be a .zip or .pak")):
            res = self.post("/addons/new", self.new_addon_form("x", import_mode="local", local_source=path), follow=True)
            self.assertIn(message, res.get_data(as_text=True), path)

    def test_cancelling_a_local_import_preview_never_deletes_the_users_file(self):
        z = release_zip(self.tmp / "mod.zip")
        data = self.new_addon_form("local", import_mode="local", local_source=str(z), review="1")
        self.status(self.job_id_from(self.post("/addons/new", data)))
        self.post(f"/imports/{self.last_import()['token']}/cancel")
        self.assertTrue(z.exists())


class EditMetadataTests(FlowBase):
    def make_existing(self, addon_id="editme"):
        self.upload(addon_id, make_pak_bytes([("maps/a.bsp", make_bsp())]))
        return self.row(addon_id)

    def edit_post(self, addon_id: int, data: dict, headers=None, follow=False):
        payload = dict(data)
        payload["csrf_token"] = self.csrf()
        return self.c.post(f"/addons/{addon_id}/edit", data=payload, content_type="multipart/form-data", headers=headers or {}, follow_redirects=follow)

    def test_metadata_only_edit_preserves_omitted_fields_and_allows_clearing_optional_ones(self):
        row = self.make_existing("keep")
        res = self.edit_post(row["id"], {"title": "Keep", "addon_id": row["addon_id"], "gamedir": row["gamedir"], "author": "", "description": ""})
        self.assertEqual(res.status_code, 302)
        updated = self.row("keep")
        self.assertEqual(updated["title"], "Keep")
        self.assertEqual((updated["author"], updated["description"]), ("", ""))
        self.assertEqual(updated["date"], row["date"])  # omitted field preserved

    def test_metadata_only_edit_does_not_invoke_conversion(self):
        row = self.make_existing("noconvert")
        import app as app_module
        real = app_module.build_single_pak_from_zip
        called = []
        def boom(*a, **kw):
            called.append(True)
            raise AssertionError("conversion should not run for metadata-only edits")
        app_module.build_single_pak_from_zip = boom
        self.addCleanup(setattr, app_module, "build_single_pak_from_zip", real)
        res = self.edit_post(row["id"], {"title": "No Convert", "addon_id": row["addon_id"], "gamedir": row["gamedir"], "author": "A"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(called, [])
        self.assertEqual(self.row("noconvert")["title"], "No Convert")

    def test_metadata_only_edit_keeps_the_existing_package_and_updates_fields(self):
        row = self.make_existing("rel")
        before = {
            "package_path": row["package_path"],
            "package_sha256": row["package_sha256"],
            "package_size": row["package_size"],
            "package_mtime": row["package_mtime"],
            "conversion_report": row["conversion_report"],
            "source_path": row["source_path"],
            "source_sha256": row["source_sha256"],
        }
        res = self.edit_post(row["id"], {"title": "Renamed", "addon_id": row["addon_id"], "gamedir": row["gamedir"], "author": "New Author", "date": row["date"], "description": "Updated description"}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(res.status_code, 302)
        updated = self.row("rel")
        self.assertEqual((updated["title"], updated["author"], updated["description"]), ("Renamed", "New Author", "Updated description"))
        for field, value in before.items():
            self.assertEqual(updated[field], value, field)
        self.assertEqual(self.stray_files(), [])

    def test_empty_file_input_means_no_replacement(self):
        row = self.make_existing("empty")
        with open(row["package_path"], "rb") as fh:
            res = self.edit_post(row["id"], {"title": "Still Empty", "addon_id": row["addon_id"], "gamedir": row["gamedir"], "package": (fh, "")})
        self.assertEqual(res.status_code, 302)
        updated = self.row("empty")
        self.assertEqual(updated["title"], "Still Empty")
        self.assertEqual(updated["package_sha256"], row["package_sha256"])
        self.assertEqual(updated["package_path"], row["package_path"])

    def test_edit_form_shows_replace_package_optional_and_current_package(self):
        row = self.make_existing("showme")
        html = self.c.get(f"/addons/{row['id']}/edit").get_data(as_text=True)
        self.assertIn("Replace package — optional", html)
        self.assertIn("Leave it empty to keep the current package.", html)
        self.assertIn(row["package_name"], html)
        self.assertIn("Metadata-only changes save without uploading a file.", html)
        self.assertIn("if ('edit' === 'edit' && !hasReplacement)", html)

    def test_published_metadata_only_edit_updates_catalog_without_replacing_the_package(self):
        row = self.make_existing("pubedit")
        self.publish("pubedit")
        before = self.c.get("/content.json").get_data(as_text=True)
        res = self.edit_post(row["id"], {"title": "Published Title", "addon_id": row["addon_id"], "gamedir": row["gamedir"], "author": "A", "date": row["date"], "description": "d"}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.row("pubedit")["title"], "Published Title")
        after = self.c.get("/content.json").get_data(as_text=True)
        self.assertNotEqual(before, after)
        self.assertIn("Published Title", after)
        self.assertEqual(Path(self.row("pubedit")["package_path"]).read_bytes(), Path(row["package_path"]).read_bytes())

    def test_optional_replacement_still_works_and_preserves_user_metadata(self):
        row = self.make_existing("replaceable")
        z2 = self.tmp / "new.zip"
        release_zip(z2, [("extra.cfg", b"x")])
        res = self.edit_post(row["id"], {"title": "User Title", "addon_id": row["addon_id"], "gamedir": row["gamedir"], "author": "User Author", "description": "User Desc", "package": (z2.open("rb"), "new.zip")})
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        updated = self.row("replaceable")
        self.assertEqual((updated["title"], updated["author"], updated["description"]), ("User Title", "User Author", "User Desc"))
        self.assertNotEqual(updated["package_sha256"], row["package_sha256"])

    def test_missing_stored_artifact_still_allows_metadata_edit_but_not_publish(self):
        row = self.make_existing("missing")
        Path(row["package_path"]).unlink()
        self.assertFalse(Path(row["package_path"]).exists())
        res = self.edit_post(row["id"], {"title": "Missing OK", "addon_id": row["addon_id"], "gamedir": row["gamedir"], "author": "A", "description": "d"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.row("missing")["title"], "Missing OK")
        pub = self.post(f"/addons/{row['id']}/publish", follow=True)
        self.assertIn("package file is missing", pub.get_data(as_text=True).lower())

    def test_required_fields_still_block_new_items(self):
        res = self.post("/addons/new", {"title": "", "addon_id": "", "gamedir": ""}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual((res.status_code, res.get_json()), (400, {"error": "Title, ID, and game dir are required."}))

    def test_auth_and_csrf_still_apply_to_edit_route(self):
        row = self.make_existing("secure")
        from app import app as flask_app
        anon = flask_app.test_client()
        self.assertEqual(anon.get(f"/addons/{row['id']}/edit").status_code, 403)
        self.assertEqual(self.c.post(f"/addons/{row['id']}/edit", data={"title": "x"}, content_type="multipart/form-data").status_code, 403)


class FormOptionsTests(FlowBase):
    """The options are on the upload/edit form itself, not only on the review page."""
    OPTION_INPUTS = ('name="exclude_presets" value="authoring"', 'name="exclude_presets" value="junk"', 'name="exclude_presets" value="docs"',
                     'name="exclude_patterns"', 'name="reencode_textures"', 'name="reencode_skyboxes"', 'name="generate_mapdb"',
                     'name="mapdb_name"', 'name="mapdb_dir"', 'name="keep_source"', 'name="review"')

    def zip_with_images(self):
        tga = tga_bytes(64, 64, gradient(64, 64, 3), channels=3)
        return release_zip(self.tmp / "rel.zip", [("gfx/t.tga", tga), ("gfx/env/s_up.tga", tga)])

    def test_every_option_is_on_the_new_add_on_form(self):
        html = self.c.get("/addons/new").get_data(as_text=True)
        for needle in self.OPTION_INPUTS:
            self.assertIn(needle, html, needle)
        self.assertEqual(html.count('name="keep_source"'), 1)               # one checkbox, not one per template
        self.assertIn("Conversion options", html)
        for preset in ("authoring", "junk", "docs"):
            self.assertIn(f'name="exclude_presets" value="{preset}" >', html)                      # every option starts switched off
        for name in ("reencode_textures", "reencode_skyboxes", "generate_mapdb", "keep_source"):
            self.assertIn(f'name="{name}" value="1" >', html)
        self.assertIn('name="review" value="1" checked', html)                                       # only the review step starts ticked

    def test_every_option_is_on_the_edit_form_too(self):
        self.upload("edit", make_pak_bytes([("maps/a.bsp", make_bsp())]))
        html = self.c.get(f"/addons/{self.row('edit')['id']}/edit").get_data(as_text=True)
        for needle in self.OPTION_INPUTS:
            self.assertIn(needle, html, needle)

    def test_options_chosen_on_the_form_apply_to_a_direct_build(self):
        z = self.zip_with_images()
        res = self.start_upload(z, "direct", exclude_presets=["authoring"], reencode_textures="1", generate_mapdb="1", keep_source="1")
        st = self.status(self.job_id_from(res))
        self.assertEqual((st["kind"], st["state"]), ("build", "done"))      # no review step: options are used immediately
        got = parse_pak(Path(self.row("direct")["package_path"]))
        self.assertEqual(sorted(got), ["gfx/env/s_up.tga", "gfx/t.png", "mapdb.json", "maps/a.bsp", "maps/b.bsp", "pun.txt", "sound/x.wav"])
        self.assertEqual(json.loads(got["mapdb.json"]), {"episodes": [{"dir": "direct", "name": "direct"}], "maps": []})   # blank fields default to title / directory
        rec = json.loads(self.row("direct")["conversion_report"])
        self.assertEqual((rec["options"]["exclude_presets"], rec["reencoded"]["converted"], rec["excluded"]["total"]), (["authoring"], 1, 1))
        self.assertTrue(Path(self.row("direct")["source_path"]).is_file())

    def test_explicit_mapdb_values_beat_the_defaults(self):
        res = self.start_upload(self.zip_with_images(), "mapx", generate_mapdb="1", mapdb_name="  The Real Name ", mapdb_dir="realdir")
        self.assertEqual(self.status(self.job_id_from(res))["state"], "done")
        got = parse_pak(Path(self.row("mapx")["package_path"]))
        self.assertEqual(json.loads(got["mapdb.json"])["episodes"], [{"dir": "realdir", "name": "The Real Name"}])

    def test_options_from_the_form_carry_over_to_the_review_page(self):
        res = self.start_upload(self.zip_with_images(), "carry", review="1", exclude_presets=["authoring", "junk"], exclude_patterns="*.txt",
                                reencode_skyboxes="1", keep_source="1")
        self.assertEqual(self.status(self.job_id_from(res))["kind"], "plan")
        token = self.last_import()["token"]
        plan = json.loads(self.last_import()["plan_json"])
        self.assertEqual(plan["excluded"]["total"], 2)                      # b.map (authoring) + pun.txt (custom), already reflected in the preview
        html = self.c.get(f"/imports/{token}").get_data(as_text=True)
        for preset in ("authoring", "junk"):
            self.assertIn(f'name="exclude_presets" value="{preset}" checked', html)
        self.assertIn('name="exclude_presets" value="docs" >', html)
        self.assertIn('name="reencode_skyboxes" value="1" checked', html)
        self.assertIn('name="reencode_textures" value="1" >', html)
        self.assertIn('name="keep_source" value="1" checked', html)
        self.assertIn("*.txt", html)
        # pressing Build without touching anything keeps those choices
        st = self.status(self.job_id_from(self.post(f"/imports/{token}/build", {"exclude_presets": ["authoring", "junk"], "exclude_patterns": "*.txt", "reencode_skyboxes": "1", "keep_source": "1"})))
        self.assertEqual(st["state"], "done", st["message"])
        self.assertEqual(sorted(parse_pak(Path(self.row("carry")["package_path"]))), ["gfx/env/s_up.png", "gfx/t.tga", "maps/a.bsp", "maps/b.bsp", "sound/x.wav"])

    def test_bad_options_are_rejected_before_anything_is_staged(self):
        z = self.zip_with_images()
        for extra, message in (({"exclude_presets": ["nonsense"]}, "Unknown exclusion preset"), ({"exclude_patterns": "*"}, "would match everything"),
                               ({"exclude_patterns": "../x"}, "'..'")):
            with self.subTest(extra=extra):
                res = self.start_upload(z, "bad", headers={"X-Requested-With": "XMLHttpRequest"}, **extra)
                self.assertEqual(res.status_code, 400)
                self.assertIn("Options rejected", res.get_json()["error"])
                self.assertIn(message, res.get_json()["error"])
                self.assertEqual((self.stray_files(), self.import_count(), self.row("bad")), ([], 0, None))
        res = self.start_upload(z, "bad", exclude_patterns="*", follow=True)                      # same message for a plain (non-JavaScript) post
        self.assertIn("Options rejected", res.get_data(as_text=True))

    def test_options_are_ignored_for_plain_pak_uploads(self):
        pak = make_pak_bytes([("maps/a.bsp", make_bsp()), ("src/a.qc", b"code")])
        f = self.tmp / "m.pak"
        f.write_bytes(pak)
        data = self.new_addon_form("plainpak", exclude_presets=["authoring"], reencode_textures="1", generate_mapdb="1", keep_source="1")
        data["package"] = (f.open("rb"), "m.pak")
        st = self.status(self.job_id_from(self.post("/addons/new", data)))
        self.assertEqual(st["state"], "done")
        self.assertEqual(Path(self.row("plainpak")["package_path"]).read_bytes(), pak)           # stored unchanged, nothing applied
        self.assertIsNone(self.row("plainpak")["source_path"])

    def test_replacing_a_package_from_the_edit_form_uses_the_chosen_options(self):
        self.upload("swap", make_pak_bytes([("maps/a.bsp", make_bsp())]))
        row = self.row("swap")
        z = self.zip_with_images()
        data = {"title": "swap", "addon_id": "swap", "gamedir": "swap", "exclude_presets": ["authoring"], "reencode_textures": "1", "package": (z.open("rb"), "rel.zip")}
        st = self.status(self.job_id_from(self.post(f"/addons/{row['id']}/edit", data)))
        self.assertEqual(st["state"], "done", st["message"])
        got = parse_pak(Path(self.row("swap")["package_path"]))
        self.assertIn("gfx/t.png", got)
        self.assertNotIn("maps/b.map", got)


class HousekeepingTests(FlowBase):
    def test_xhr_errors_are_json(self):
        res = self.post("/addons/new", {"title": "", "addon_id": "", "gamedir": ""}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual((res.status_code, res.get_json()), (400, {"error": "Title, ID, and game dir are required."}))
        f = self.tmp / "junk.pak"
        f.write_bytes(b"junkjunkjunk")
        data = self.new_addon_form("junk")
        data["package"] = (f.open("rb"), "junk.pak")
        res = self.post("/addons/new", data, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unsupported package format", res.get_json()["error"])
        self.assertEqual((self.stray_files(), self.import_count()), ([], 0))

    def test_duplicate_id_is_rejected_before_anything_is_staged(self):
        self.upload("dup", make_pak_bytes([("maps/a.bsp", make_bsp())]))
        z = release_zip(self.tmp / "rel.zip")
        res = self.start_upload(z, "dup", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual((res.status_code, "duplicate addon id" in res.get_json()["error"]), (400, True))
        self.assertEqual((self.stray_files(), self.import_count()), ([], 0))

    def test_two_pending_imports_for_one_id_cannot_both_install(self):
        z = release_zip(self.tmp / "rel.zip")
        first = self.job_id_from(self.start_upload(z, "twin", review="1"))
        second = self.job_id_from(self.start_upload(z, "twin", review="1"))
        conn = get_db()
        tokens = [r["token"] for r in conn.execute("SELECT token FROM imports ORDER BY created_at")]
        conn.close()
        self.assertEqual(len(tokens), 2)
        ok = self.status(self.job_id_from(self.post(f"/imports/{tokens[0]}/build", {})))
        bad = self.status(self.job_id_from(self.post(f"/imports/{tokens[1]}/build", {})))
        self.assertEqual((ok["state"], bad["state"]), ("done", "failed"))
        self.assertIn("duplicate addon id", bad["message"])
        conn = get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM addons WHERE addon_id='twin'").fetchone()[0], 1)
        conn.close()

    def test_stale_imports_expire_and_take_their_files_with_them(self):
        z = release_zip(self.tmp / "rel.zip")
        self.start_upload(z, review="1")
        imp = self.last_import()
        self.assertTrue(Path(imp["source_path"]).exists())
        conn = get_db()
        conn.execute("UPDATE imports SET created_at = created_at - 90000")
        conn.close()
        self.assertEqual(app_module.expire_stale_imports(), 1)
        self.assertEqual((self.import_count(), Path(imp["source_path"]).exists()), (0, False))

    def test_pending_uploads_are_listed_and_discardable_from_the_dashboard(self):
        self.start_upload(release_zip(self.tmp / "rel.zip"), "pend", review="1")
        token = self.last_import()["token"]
        html = self.c.get("/dashboard").get_data(as_text=True)
        self.assertIn("Pending uploads", html)
        self.assertIn(f"/imports/{token}", html)
        self.assertIn("min ago", html)
        self.post(f"/imports/{token}/cancel")
        self.assertNotIn("Pending uploads", self.c.get("/dashboard").get_data(as_text=True))

    def test_startup_discards_interrupted_direct_builds_but_keeps_previewed_uploads(self):
        z = release_zip(self.tmp / "rel.zip")
        self.start_upload(z, "reviewed", review="1")
        kept = self.last_import()
        # a direct build whose process "died": the import row exists but no job will ever finish it
        from app import _save_import
        direct_dir = Path(app.config["UPLOAD_FOLDER"]) / "directtoken"
        direct_dir.mkdir(parents=True)
        (direct_dir / "x.zip").write_bytes(z.read_bytes())
        _save_import("directtoken", "new", None, {"title": "d", "addon_id": "d", "gamedir": "d", "author": "", "date": "", "description": ""},
                     direct_dir / "x.zip", "x.zip", "zip", True, False, False)
        app_module.startup_cleanup()
        self.assertIsNone(app_module._load_import("directtoken"))
        self.assertFalse(direct_dir.exists())
        self.assertIsNotNone(app_module._load_import(kept["token"]))
        self.assertTrue(Path(kept["source_path"]).exists())

    def test_startup_cleanup_removes_what_a_killed_process_left(self):
        packages = Path(app.config["PERSIST_ROOT"]) / "packages"
        public = Path(app.config["PUBLIC_ROOT"]) / "addons"
        packages.mkdir(parents=True, exist_ok=True)
        public.mkdir(parents=True, exist_ok=True)
        (packages / ".staging-abc.pak").write_bytes(b"x")
        (public / ".publish-abc.tmp").write_bytes(b"x")
        (public / "keep.pak").write_bytes(b"live")
        (packages / "keep.pak").write_bytes(b"pkg")
        work = Path(app.config["UPLOAD_FOLDER"]) / ".work" / "quake-zip-dead"
        work.mkdir(parents=True)
        (work / "member-0.bin").write_bytes(b"x")
        conn = get_db()
        conn.execute("INSERT INTO jobs (id, kind, title, state, created_at, updated_at) VALUES ('stuck','build','t','running',0,0)")
        conn.close()
        app_module.startup_cleanup()
        self.assertEqual(sorted(p.name for p in packages.iterdir()), ["keep.pak"])
        self.assertEqual(sorted(p.name for p in public.iterdir()), ["keep.pak"])
        self.assertFalse(work.exists())
        self.assertEqual(jobs.get("stuck")["state"], "failed")

    def test_page_scripts_are_valid_javascript(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        token = None
        pages = {"form": self.c.get("/addons/new").get_data(as_text=True)}
        z = release_zip(self.tmp / "rel.zip")
        res = self.start_upload(z, review="1")
        pages["job"] = self.c.get(res.headers["Location"]).get_data(as_text=True)
        for name, html in pages.items():
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            self.assertTrue(scripts, name)
            for i, code in enumerate(scripts):
                f = self.tmp / f"{name}{i}.js"
                f.write_text(code)
                r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")

    def test_upload_form_has_progress_ui_and_fallback(self):
        html = self.c.get("/addons/new").get_data(as_text=True)
        for needle in ('id="upload-bar"', "xhr.upload.onprogress", 'name="review"', 'name="keep_source"', "X-Requested-With"):
            self.assertIn(needle, html)
        self.assertRegex(html, r'<form method="post" enctype="multipart/form-data" id="addon-form">')   # still a normal form without JS

    def test_job_page_renders_final_state_on_the_server(self):
        self.upload("page", make_pak_bytes([("maps/a.bsp", make_bsp())]))
        res = self.post(f"/addons/{self.row('page')['id']}/publish", follow=True)
        html = res.get_data(as_text=True)
        self.assertIn("Add-on published and catalog regenerated.", html)
        self.assertNotIn("http-equiv=\"refresh\"", html)
        self.assertIn('class="progress done"', html)


if __name__ == "__main__":
    unittest.main()

"""The browser-side progress code, executed in Node against DOM stubs (no browser needed)."""
import json
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from app import app, get_db
from tests.test_publication import PublicationBase

JS = Path(__file__).resolve().parent / "js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageScriptTests(PublicationBase):
    def script(self, html, name, index=-1):
        code = re.findall(r"<script>(.*?)</script>", html, re.S)[index]
        path = self.tmp / name
        path.write_text(code)
        return path

    _n = 0

    def insert_job(self, state, stage="", percent=0.0, message="", level="info", next_endpoint=None):
        conn = get_db()
        now = time.time()
        PageScriptTests._n += 1
        conn.execute("INSERT INTO jobs (id, kind, title, state, stage, percent, message, level, next_endpoint, next_values, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"jobtest{PageScriptTests._n}", "build", "Convert <b>x</b>.zip", state, stage, percent, message, level, next_endpoint, "{}", now, now))
        conn.close()
        return f"jobtest{PageScriptTests._n}"

    def run_job_page(self, statuses):
        job_id = self.insert_job("running", "Extracting files", 12.0)
        html = self.c.get(f"/jobs/{job_id}").get_data(as_text=True)
        script = self.script(html, "job.js")
        r = subprocess.run(["node", str(JS / "job_harness.js"), str(script), json.dumps(statuses)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout), html

    @staticmethod
    def status(**kw):
        base = {"state": "running", "stage": "Extracting files", "done": 0, "total": 0, "percent": 0, "message": "", "level": "info", "finished": False,
                "next_url": None, "auto_redirect": False, "elapsed": 0, "seconds_since_update": 0}
        base.update(kw)
        return base

    def test_job_page_shows_progress_then_the_result_and_redirects(self):
        S = self.status
        out, html = self.run_job_page([
            S(state="queued", stage="", percent=0),
            S(percent=37.4, stage="Extracting files", done=412 * 1048576, total=1648 * 1048576, elapsed=75),
            S(percent=62, stage="Re-encoding images", done=40, total=121, elapsed=120, seconds_since_update=25),
            S(state="done", finished=True, percent=100, message="Add-on saved as a draft.", level="success", next_url="/addons/5", auto_redirect=True, elapsed=130),
        ])
        log = out["log"]
        self.assertEqual(log[0]["state"], "Waiting to start…")
        self.assertEqual((log[1]["bar"], log[1]["aria"]), ("37.4%", 37))
        self.assertEqual(log[1]["detail"], "37% · 412 MiB of 1.6 GiB · 1:15 elapsed")
        self.assertEqual(log[2]["detail"], "62% · 40 of 121 images · 2:00 elapsed")        # item-count stages are not shown as bytes
        self.assertFalse(log[2]["stallHidden"])                                              # no update for 25 s: reassure the user
        self.assertTrue(log[1]["stallHidden"])
        f = out["final"]
        self.assertEqual((f["state"], f["bar"], f["message"], f["messageHidden"]), ("Finished", "100%", "Add-on saved as a draft.", False))
        self.assertEqual((f["messageClass"], f["progressClass"]), ("flash success", "progress done"))
        self.assertEqual((f["nextHref"], f["nextHidden"], f["redirect"]), ("/addons/5", False, "/addons/5"))
        self.assertTrue(out["intervalCleared"])                                              # polling stops once the job has finished
        self.assertTrue(f["stallHidden"])
        self.assertIn("Convert &lt;b&gt;x&lt;/b&gt;.zip", html)                               # untrusted title is escaped in the HTML

    def test_failures_and_cancellations_are_shown_without_redirecting(self):
        for state, level, message, cls in (("failed", "error", "Package rejected: ambiguous root mapping", "flash error"),
                                           ("cancelled", "info", "Cancelled. Nothing was changed.", "flash info")):
            with self.subTest(state):
                out, _ = self.run_job_page([self.status(percent=20), self.status(state=state, finished=True, percent=20, message=message, level=level,
                                                                                  next_url="/imports/abc" if state == "failed" else None, elapsed=3)])
                f = out["final"]
                self.assertEqual((f["message"], f["messageClass"], f["progressClass"]), (message, cls, "progress failed"))
                self.assertEqual(f["redirect"], "")                                         # only successful auto-redirect jobs move on by themselves
                self.assertEqual(f["nextHidden"], state != "failed")

    def test_the_markup_in_a_server_message_is_never_interpreted(self):
        out, _ = self.run_job_page([self.status(state="failed", finished=True, message="<img src=x onerror=alert(1)>", level="error")])
        self.assertEqual(out["final"]["message"], "<img src=x onerror=alert(1)>")            # set via textContent, shown literally

    def upload_form_script(self):
        html = self.c.get("/addons/new").get_data(as_text=True)
        return self.script(html, "upload.js", 0)

    def run_upload(self, scenario):
        r = subprocess.run(["node", str(JS / "upload_harness.js"), str(self.upload_form_script()), scenario], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_upload_progress_and_redirect(self):
        out = self.run_upload("progress")
        self.assertTrue(out["prevented"])                                                    # the browser's own submit is replaced by XHR
        self.assertEqual((out["method"], out["sent"]), ("POST", True))
        self.assertEqual(out["headers"], {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
        self.assertEqual(out["afterSubmit"], {"boxHidden": False, "buttonDisabled": True})
        self.assertEqual(out["half"]["bar"], "25%")
        self.assertTrue(out["half"]["text"].startswith("50.0 MiB of 200 MiB (25%)"))
        self.assertEqual(out["unknownTotal"]["text"], "3.0 MiB sent")
        self.assertEqual((out["uploaded"]["bar"], out["uploaded"]["text"]), ("100%", "Upload complete. Starting the job…"))
        self.assertEqual(out["redirect"], "/jobs/abc123")

    def test_upload_errors_are_reported_and_the_form_is_usable_again(self):
        cases = {"error-json": "Package rejected: duplicate addon id", "too-large": "larger than the configured limit", "network": "connection was interrupted",
                 "abort": "Upload cancelled.", "html-200": "The server rejected the upload (HTTP 200"}
        for scenario, needle in cases.items():
            with self.subTest(scenario):
                out = self.run_upload(scenario)
                self.assertIn(needle, out["error"]["text"])
                self.assertFalse(out["error"]["hidden"])
                if "buttonDisabled" in out["error"]:
                    self.assertFalse(out["error"]["buttonDisabled"])
        self.assertEqual(self.run_upload("html-200")["redirect"], "http://x/addons/new")   # an unexpected page (e.g. a login redirect) never navigates away


if __name__ == "__main__":
    unittest.main()

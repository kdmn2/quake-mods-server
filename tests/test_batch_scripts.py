"""Browser-side behaviour of the batch pages (selection summary, confirmations, double-submit protection, live refresh), run in Node."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from app import app
from tests.test_import_flow import FlowBase
from tests.test_batch_import import BatchBase, release

JS = Path(__file__).resolve().parent / "js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BatchScriptTests(BatchBase):
    def script(self, html, index=-1):
        path = self.tmp / "page.js"
        path.write_text(re.findall(r"<script>(.*?)</script>", html, re.S)[index])
        return path

    def run_js(self, html, scenario, index=-1):
        r = subprocess.run(["node", str(JS / "batch_harness.js"), str(self.script(html, index)), scenario], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_scan_page_selection_confirmation_and_double_submit_protection(self):
        for i in range(3):
            release(self.import_dir / f"m{i}.zip", f"t{i}")
        sid, _ = self.scan()
        html = self.c.get(f"/batches/scan/{sid}").get_data(as_text=True)
        out = self.run_js(html, "scan")
        self.assertEqual(out["initial"]["disabled"], True)                                        # nothing selected: "Import selected" is disabled
        self.assertTrue(out["initial"]["summary"].startswith("0 selected"))
        self.assertEqual(out["allSelected"], {"summary": "3 selected · 3.0 MiB", "disabled": False})
        self.assertEqual(out["cleared"]["disabled"], True)
        self.assertEqual(out["two"], "2 selected · 2.0 MiB")
        self.assertEqual(out["declined"], {"prevented": True, "text": "Import 2 selected package(s) (2.0 MiB) as drafts?", "buttonsDisabled": False, "appended": 0})
        self.assertEqual(out["accepted"]["prevented"], False)
        self.assertTrue(out["accepted"]["buttonsDisabled"])                                        # a second click cannot submit again
        self.assertEqual(out["accepted"]["hidden"], [["mode", "selected"]])                         # disabled buttons are not submitted, so the mode travels in a hidden field
        self.assertEqual(out["all"]["text"].startswith("Import ALL 25 eligible"), True)
        self.assertEqual(out["all"]["hidden"], [["mode", "all"]])

    def test_the_scan_page_embeds_the_real_confirmation_text_for_import_all(self):
        for i in range(4):
            release(self.import_dir / f"m{i}.zip", f"t{i}")
        sid, _ = self.scan()
        html = self.c.get(f"/batches/scan/{sid}").get_data(as_text=True)
        self.assertIn('data-confirm-text="Import ALL 4 eligible package(s)', html)
        self.assertIn('data-confirm-template="Import {n} selected package(s) ({size}) as drafts?"', html)

    def test_batch_page_confirms_bulk_actions_and_stops_polling_when_finished(self):
        self.host_release("/a", "A", "a.zip")
        bid = self.batch_id(self.submit_urls(self.url("/a")))
        html = self.c.get(f"/batches/{bid}").get_data(as_text=True)
        self.assertIn("data-confirm=", html)
        out = self.run_js(html, "batch")
        self.assertTrue(out["startedPolling"])
        self.assertEqual(out["declined"], {"prevented": True, "text": "Cancel every item?"})
        self.assertEqual(out["accepted"], {"prevented": False})
        self.assertIn("done", out["final"]["html"])
        self.assertTrue(out["final"]["stopped"])                                                    # no more polling once nothing is active
        self.assertEqual(out["final"]["polledTimes"], 3)

    def test_url_form_disables_its_buttons_after_the_first_submit(self):
        html = self.c.get("/batches").get_data(as_text=True)
        self.assertIn("form[data-once]", html)
        self.assertIn("b.disabled = true", html)
        self.assertIn('type="hidden" name="submit_key"', html)                                      # and the server de-duplicates by key anyway


if __name__ == "__main__":
    unittest.main()

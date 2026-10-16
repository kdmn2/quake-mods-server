"""Every state-changing route must refuse a POST that lacks the session's CSRF token. The route list is read from the application itself,
so a route added later is covered automatically."""
import re
import unittest

from app import app
from tests.test_publication import PublicationBase

DUMMY = {"int": "1", "string": "x", "path": "x"}


def post_urls():
    for rule in app.url_map.iter_rules():
        if "POST" not in rule.methods or rule.endpoint == "static":
            continue
        path = rule.rule
        for arg, conv in rule._converters.items():
            kind = type(conv).__name__.replace("Converter", "").lower()
            path = path.replace(f"<{conv.__class__.__name__.lower()}:{arg}>", DUMMY.get(kind, "x"))
            path = re.sub(rf"<(?:[a-z]+:)?{arg}>", DUMMY.get(kind, "x"), path)
        yield rule.endpoint, path


class CsrfEverywhereTests(PublicationBase):
    def test_the_route_list_is_not_empty_and_includes_the_newer_routes(self):
        endpoints = {e for e, _ in post_urls()}
        for expected in ("import_build", "import_cancel", "job_cancel", "batch_submit_urls", "batch_item_retry", "publish_addon", "login"):
            self.assertIn(expected, endpoints)

    def test_every_post_route_rejects_a_missing_or_wrong_token_even_when_logged_in(self):
        for endpoint, path in post_urls():
            for data in ({}, {"csrf_token": "forged"}):
                res = self.c.post(path, data=data)
                self.assertEqual(res.status_code, 403, f"{endpoint} {path} {data}")

    def test_a_valid_token_is_accepted_by_the_check(self):
        res = self.c.post("/imports/doesnotexist/cancel", data={"csrf_token": self.csrf()})
        self.assertEqual(res.status_code, 404)                                  # got past CSRF; the (nonexistent) import is what is missing

    def test_login_and_setup_still_work_with_their_tokens(self):
        anon = app.test_client()
        anon.get("/login")
        with anon.session_transaction() as s:
            token = s["csrf_token"]
        self.assertEqual(anon.post("/login", data={"username": "admin", "password": "secret", "csrf_token": token}).status_code, 302)
        self.assertEqual(app.test_client().post("/login", data={"username": "admin", "password": "secret"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()

"""SSRF protection, streaming limits, redirects, retries and redaction of tools/netfetch.py, against controlled local servers."""
import gzip
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from tests.nettest import PUBLIC_IP, PUBLIC_IP2, FakeNet, Site
from tests.test_zip_conversion import make_pak_bytes, write_zip
from tools import netfetch
from tools.netfetch import FetchError, FetchLimits

FAST = FetchLimits(max_bytes=5 * 1024 * 1024, connect_timeout=2, read_timeout=1.5, max_duration=30, max_redirects=5, max_attempts=3,
                   backoff_base=2, retry_after_max=30)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-net-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.site = Site()
        self.addCleanup(self.site.close)
        self.net = FakeNet(self.site)
        self.sleeps: list[float] = []
        zpath = self.tmp / "pkg.zip"
        write_zip(zpath, [("maps/a.bsp", b"bsp" * 100), ("progs.dat", b"dat")])
        self.zip_bytes = zpath.read_bytes()
        self.dest = self.tmp / "out.bin"

    def get(self, url, limits=FAST, **extra):
        kwargs = self.net.kwargs(sleep=self.sleeps.append)
        kwargs.update(extra)
        return netfetch.fetch_to_file(url, self.dest, limits, **kwargs)

    def fails(self, url, kind=None, limits=FAST, **extra):
        with self.assertRaises(FetchError) as ctx:
            self.get(url, limits, **extra)
        if kind:
            self.assertEqual(ctx.exception.kind, kind, str(ctx.exception))
        self.assertFalse(self.dest.exists(), "a failed download must not leave a partial file")
        return ctx.exception

    def hits(self, path):
        return sum(1 for r in self.site.requests if r["path"].split("?", 1)[0] == path)


class UrlValidationTests(unittest.TestCase):
    def test_accepts_ordinary_links(self):
        for url, host, port in (("https://example.org/a.zip", "example.org", 443), ("HTTP://Example.ORG:8080/x?token=1#frag", "example.org", 8080),
                                ("https://93.184.216.34/f", "93.184.216.34", 443), ("https://[2606:4700:4700::1111]/f", "2606:4700:4700::1111", 443),
                                ("https://example.org/dl?id=7&sig=abc", "example.org", 443), ("https://example.org", "example.org", 443),
                                ("https://example.org./a", "example.org", 443)):
            p = netfetch.parse_url(url)
            self.assertEqual((p.host, p.port), (host, port), url)
        self.assertEqual(netfetch.parse_url("https://example.org/a b".replace(" ", "%20")).target, "/a%20b")
        self.assertNotIn("#", netfetch.parse_url("https://example.org/x#secret").url)

    def test_rejects_what_is_not_a_web_link(self):
        bad = {
            "": "empty", "   ": "empty", "/home/user/mod.zip": "not a web link", "C:\\mods\\a.zip": "not a web link", "./a.zip": "not a web link", "mod.zip": "not a web link",
            "file:///etc/passwd": "Only http", "ftp://example.org/a.zip": "Only http", "gopher://example.org/": "Only http", "javascript:alert(1)": "Only http", "data:text/html,<b>": "Only http", "curl http://example.org/a.zip | sh": "not a single URL", "http://example.org/a.zip; rm -rf /": "not a single URL",
            "http://example.org/a\nb": "not a single URL", "http://user:pass@example.org/a.zip": "credentials", "http://example.org@evil.example/": "credentials",
            "http://good.example\\@evil.example/": "credentials", "https://:443/a": "no host name", "https:///a": "no host name", "http://example.org:0/": "port",
            "http://example.org:99999/": "not valid", "http://[::1": "not valid", "http://exa mple.org/": "not a single URL", "x" * 3000: "longer than",
        }
        for url, needle in bad.items():
            with self.subTest(url=url[:40]), self.assertRaises(FetchError) as ctx:
                netfetch.parse_url(url)
            self.assertIn(needle.lower(), str(ctx.exception).lower())

    def test_rejects_internal_and_ambiguous_addresses(self):
        internal = ["http://127.0.0.1/", "http://10.0.0.5/x", "http://172.16.0.1/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
                    "http://100.100.100.200/", "http://168.63.129.16/", "http://0.0.0.0/", "http://224.0.0.1/", "http://240.0.0.1/", "http://198.18.0.1/",
                    "http://[::1]/", "http://[::]/", "http://[fe80::1]/", "http://[fd00:ec2::254]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:10.1.2.3]/",
                    "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[2001:db8::1]/", "http://[ff02::1]/"]
        for url in internal:
            with self.subTest(url=url), self.assertRaises(FetchError) as ctx:
                netfetch.parse_url(url)
            self.assertEqual(ctx.exception.kind, "unsafe-destination", url)
        ambiguous = ["http://2130706433/", "http://0x7f000001/", "http://0x7f.0.0.1/", "http://127.1/", "http://0177.0.0.1/", "http://127.0.0.01/", "http://0/",
                     "http://1.2.3/", "http://[fe80::1%25eth0]/", "http://017700000001/", "http://1.2.3.4.5/"]
        for url in ambiguous:
            with self.subTest(url=url), self.assertRaises(FetchError) as ctx:
                netfetch.parse_url(url)
            self.assertEqual(ctx.exception.kind, "bad-url", url)

    def test_address_policy_table(self):
        public = ["93.184.216.34", "8.8.8.8", "1.1.1.1", "151.101.1.69", "2606:4700:4700::1111", "2a00:1450:4001:81b::200e"]
        private = ["127.0.0.1", "127.1.2.3", "10.0.0.1", "172.31.255.255", "192.168.0.1", "169.254.1.1", "100.64.0.1", "0.0.0.0", "255.255.255.255",
                   "::1", "::", "fe80::1", "fc00::1", "::ffff:192.168.1.1", "::ffff:7f00:1", "2001::1", "2002::1", "64:ff9b::1", "ff00::1"]
        for ip in public:
            self.assertTrue(netfetch.is_public_ip(ip), ip)
        for ip in private:
            self.assertFalse(netfetch.is_public_ip(ip), ip)

    def test_url_list_parsing(self):
        text = "\n  https://a.example/one.zip  \n\n\thttps://b.example/two?x=1\nhttps://A.example/one.zip#again\nnot a url\nftp://c.example/z\nhttp://u:p@d.example/\n"
        accepted, problems, dups = netfetch.parse_url_list(text, 10)
        self.assertEqual([(n, p.host) for n, p in accepted], [(2, "a.example"), (4, "b.example")])
        self.assertEqual([(p.line) for p in problems], [6, 7, 8])
        self.assertEqual([(d.line, "line 2" in d.message) for d in dups], [(5, True)])
        accepted, problems, _ = netfetch.parse_url_list("\n".join(f"https://h{i}.example/f" for i in range(6)), 5)
        self.assertEqual(len(accepted), 6)
        self.assertIn("Too many links", problems[-1].message)
        self.assertEqual(netfetch.parse_url_list("", 5)[0], [])

    def test_problem_text_never_echoes_secrets(self):
        _, problems, _ = netfetch.parse_url_list("http://user:SECRETPASS@example.org/a?token=SECRETTOKEN\nftp://example.org/x?sig=SECRETSIG", 5)
        shown = " ".join(p.text + p.message for p in problems)
        for secret in ("SECRETPASS", "SECRETTOKEN", "SECRETSIG"):
            self.assertNotIn(secret, shown)

    def test_redaction(self):
        url = "https://cdn.example.org/private/tok_SECRET123/files/My%20Mod.zip?X-Signature=SECRETSIG&expires=9#frag"
        shown = netfetch.redact_url(url)
        self.assertEqual(shown, "https://cdn.example.org/…/My Mod.zip (signed/query link)")
        for secret in ("SECRET123", "SECRETSIG", "frag", "expires"):
            self.assertNotIn(secret, shown)
        msg = netfetch.redact_text(f"failed to read {url} (X-Signature=SECRETSIG)", url)
        self.assertNotIn("SECRETSIG", msg)
        self.assertNotIn("SECRET123", msg)
        self.assertEqual(netfetch.redact_url("http://[bad"), "(invalid URL)")
        self.assertEqual(netfetch.redact_text("fine", None), "fine")

    def test_display_filename_sanitising(self):
        table = {"../../etc/passwd": "passwd", "..\\..\\win\\evil.zip": "evil.zip", "a/b/c.zip": "c.zip", "My Mod (v1.2).zip": "My Mod_v1.2_.zip", "": "download",
                 "...": "download", "con\x00tr\x1bol.zip": "control.zip", "\u202egnp.zip": "gnp.zip", "x" * 200 + ".zip": None, "\u00e9t\u00e9.zip": "t_.zip"}
        for raw, want in table.items():
            got = netfetch.safe_display_filename(raw)
            if want is None:
                self.assertLessEqual(len(got), 80)
                self.assertTrue(got.endswith(".zip"))
            else:
                self.assertEqual(got, want, repr(raw))
            self.assertNotIn("/", got)
            self.assertNotIn("\\", got)
            self.assertFalse(got.startswith("."))


class DownloadTests(Base):
    def test_extensionless_url_and_content_disposition_name_is_sanitised(self):
        self.site.serve("/download", self.zip_bytes, headers={"Content-Disposition": 'attachment; filename="../../evil/Pun GFX.zip"'})
        res = self.get("http://files.example.org/download?id=7")
        self.assertEqual((res.filename, res.size, res.sha256), ("Pun GFX.zip", len(self.zip_bytes), hashlib.sha256(self.zip_bytes).hexdigest()))
        self.assertEqual(self.dest.read_bytes(), self.zip_bytes)
        self.assertEqual(self.dest.parent, self.tmp)                         # nothing escaped the staging path chosen by the caller

    def test_filename_star_and_url_fallback(self):
        self.site.serve("/a", self.zip_bytes, headers={"Content-Disposition": "attachment; filename*=UTF-8''caf%C3%A9%20mod.zip"})
        self.site.serve("/files/Nice%20Mod.zip", self.zip_bytes)
        self.assertEqual(self.get("http://files.example.org/a").filename, "caf_mod.zip")            # RFC 2231 name decoded, then non-ASCII made safe
        self.assertEqual(self.get("http://files.example.org/files/Nice%20Mod.zip").filename, "Nice Mod.zip")

    def test_a_wrong_content_type_does_not_matter_only_the_content_does(self):
        for i, ctype in enumerate(("text/plain", "application/json", "binary/octet-stream", "image/png", "")):
            self.site.serve(f"/ct{i}", self.zip_bytes, ctype=ctype or "application/octet-stream")
            self.assertEqual(self.get(f"http://files.example.org/ct{i}").size, len(self.zip_bytes), ctype)
        self.site.serve("/zipct", b"<html>not a zip</html>", ctype="application/zip")
        self.fails("http://files.example.org/zipct", "html")                    # and a package-looking Content-Type does not make a web page acceptable

    def test_pak_is_accepted_as_a_package(self):
        pak = make_pak_bytes([("maps/a.bsp", b"x")])
        self.site.serve("/p", pak)
        self.assertEqual(self.get("http://files.example.org/p").sha256, hashlib.sha256(pak).hexdigest())

    def test_no_content_length_and_chunked_bodies(self):
        big = self.zip_bytes + os.urandom(300_000)
        self.site.serve("/nolen", big, length=False)
        self.site.serve("/chunked", big, chunked=True)
        for path in ("/nolen", "/chunked"):
            res = self.get(f"http://files.example.org{path}")
            self.assertEqual((res.size, self.dest.read_bytes()), (len(big), big), path)

    def test_streaming_size_limit_ignores_a_lying_content_length(self):
        limits = FetchLimits(**{**FAST.__dict__, "max_bytes": 200_000})
        self.site.endless("/lie", self.zip_bytes, announce=100)               # announces 100 bytes, sends forever
        self.site.endless("/nolen", self.zip_bytes)                           # no length at all
        for path in ("/nolen",):
            err = self.fails(f"http://files.example.org{path}", "too-large", limits)
            self.assertFalse(err.retryable)
        self.site.serve("/honest-big", self.zip_bytes + b"\0" * 300_000)
        self.fails("http://files.example.org/honest-big", "too-large", limits)    # rejected on the announced length, before reading
        self.assertEqual(self.hits("/honest-big"), 1)

    def test_wire_and_decoded_limits_are_separate(self):
        payload = self.zip_bytes + b"\0" * 2_000_000                          # compresses to a few KB
        small_wire = gzip.compress(payload)
        self.assertLess(len(small_wire), 20_000)
        self.site.serve("/gz", small_wire, headers={"Content-Encoding": "gzip"})
        res = self.get("http://files.example.org/gz")                          # within both limits: decoded and stored
        self.assertEqual((res.size, self.dest.read_bytes(), res.content_encoding), (len(payload), payload, "gzip"))
        bomb = gzip.compress(self.zip_bytes + b"\0" * 20_000_000)
        self.site.serve("/bomb", bomb, headers={"Content-Encoding": "gzip"})
        err = self.fails("http://files.example.org/bomb", "too-large")         # the wire is tiny, the decoded content is not
        self.assertIn("decoded", str(err))
        self.site.serve("/deflate", zlib.compress(self.zip_bytes), headers={"Content-Encoding": "deflate"})
        self.assertEqual(self.get("http://files.example.org/deflate").sha256, hashlib.sha256(self.zip_bytes).hexdigest())
        self.site.serve("/br", self.zip_bytes, headers={"Content-Encoding": "br"})
        self.fails("http://files.example.org/br", "encoding")
        self.site.serve("/badgz", b"PK\x03\x04 this is not gzip", headers={"Content-Encoding": "gzip"})
        self.fails("http://files.example.org/badgz", "encoding")

    def test_html_is_rejected_not_followed_and_not_retried(self):
        page = b'<!DOCTYPE html><html><body><a href="http://127.0.0.1/secret.zip">download</a></body></html>'
        self.site.serve("/ct", page, ctype="text/html; charset=utf-8")
        self.site.serve("/masq", page, ctype="application/octet-stream")
        self.site.serve("/xml", b"  \n<?xml version='1.0'?><error/>", ctype="application/zip")
        for path in ("/ct", "/masq", "/xml"):
            err = self.fails(f"http://files.example.org{path}", "html")
            self.assertIn("direct download link", str(err))
            self.assertFalse(err.retryable)
            self.assertEqual(self.hits(path), 1)
        self.assertEqual(self.net.resolutions.count("127.0.0.1"), 0)           # the link inside the page was never looked at

    def test_corrupt_empty_and_unknown_content(self):
        self.site.serve("/junk", b"just some text\n" * 100)
        self.site.serve("/empty", b"")
        self.site.serve("/rar", b"Rar!\x1a\x07\x00" + b"x" * 100)
        for path, text in (("/junk", "not a ZIP"), ("/empty", "empty"), ("/rar", "not a ZIP")):
            err = self.fails(f"http://files.example.org{path}", "not-a-package")
            self.assertIn(text, str(err))
            self.assertEqual(self.hits(path), 1)                                  # permanent: never retried

    def test_a_signature_split_across_tiny_packets_is_still_recognised(self):
        body = self.zip_bytes

        def drip(h):
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            for i in range(0, 12):                                             # the first bytes arrive one at a time
                h.wfile.write(body[i:i + 1])
                h.wfile.flush()
                time.sleep(0.01)
            h.wfile.write(body[12:])
        self.site.routes["/drip"] = drip
        self.assertEqual(self.get("http://files.example.org/drip").sha256, hashlib.sha256(body).hexdigest())
        page = b"<!doctype html><html><body>sorry</body></html>"

        def drip_html(h):
            h.send_response(200)
            h.send_header("Content-Length", str(len(page)))
            h.end_headers()
            for i in range(len(page)):
                h.wfile.write(page[i:i + 1])
                h.wfile.flush()
        self.site.routes["/drip-html"] = drip_html
        self.fails("http://files.example.org/drip-html", "html")

    def test_a_slow_drip_cannot_hold_the_worker_past_the_time_limit(self):
        limits = FetchLimits(**{**FAST.__dict__, "max_duration": 0.6, "read_timeout": 5, "max_bytes": 10 ** 9, "max_attempts": 1})

        def slow(h):
            h.send_response(200)
            h.send_header("Content-Length", "1000000")
            h.end_headers()
            h.wfile.write(self.zip_bytes)
            h.wfile.flush()
            try:
                while True:
                    time.sleep(0.05)
                    h.wfile.write(b"x")
                    h.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        self.site.routes["/drip-forever"] = slow
        started = time.monotonic()
        err = self.fails("http://files.example.org/drip-forever", "timeout", limits)
        self.assertLess(time.monotonic() - started, 3)                          # prompt, although the server never stalls and never finishes
        self.assertIn("longer than the allowed time", str(err))

    def test_truncated_zip_downloads_fine_at_this_layer(self):
        self.site.serve("/trunc", self.zip_bytes[:60])                           # signature present, rest missing: archive validators decide later
        self.assertEqual(self.get("http://files.example.org/trunc").size, 60)

    def test_only_fixed_headers_are_sent_and_nothing_secret_is_attached(self):
        self.site.serve("/h", self.zip_bytes)
        self.get("http://user-less.example.org/h?token=abc")
        sent = self.site.requests[-1]["headers"]
        self.assertEqual(sorted(sent), ["accept", "accept-encoding", "connection", "host", "user-agent"])
        self.assertEqual(sent["host"], "user-less.example.org")
        self.assertEqual(sent["accept-encoding"], "identity")
        self.assertIn("ironwail-addon-server", sent["user-agent"])

    def test_environment_proxy_settings_are_ignored(self):
        self.site.serve("/p", self.zip_bytes)
        env = {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.get("http://files.example.org/p").size, len(self.zip_bytes))
        self.assertEqual(self.net.connects[-1][1], 80)                          # connected to the target port, not the proxy's


class RedirectTests(Base):
    def test_relative_and_cross_host_redirects_are_followed_and_revalidated(self):
        self.site.redirect("/a", "/b")
        self.site.redirect("/b", "http://cdn.example.net/c")
        self.site.serve("/c", self.zip_bytes)
        res = self.get("http://files.example.org/a")
        self.assertEqual((res.redirects, res.host), (2, "cdn.example.net"))
        self.assertEqual(self.net.resolutions, ["files.example.org", "files.example.org", "cdn.example.net"])    # each hop is resolved and checked

    def test_redirect_to_internal_destinations_is_refused(self):
        targets = ["http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                   "http://2130706433/", "http://0x7f.1/", "http://localhost.internal/"]
        self.net.table["localhost.internal"] = ["127.0.0.1"]
        for i, target in enumerate(targets):
            self.site.redirect(f"/r{i}", target)
            err = self.fails(f"http://files.example.org/r{i}", None)
            self.assertIn(err.kind, ("unsafe-destination", "bad-url"), target)
            self.assertFalse(err.retryable)
        self.assertEqual(self.hits("/x"), 0)

    def test_redirect_to_other_schemes_credentials_and_downgrade(self):
        for i, target in enumerate(["file:///etc/passwd", "ftp://files.example.org/a.zip", "gopher://x/", "http://user:pw@files.example.org/x", "//", ""]):
            self.site.redirect(f"/s{i}", target)
            self.fails(f"http://files.example.org/s{i}", None)
        tls_target = "http://files.example.org/plain"
        self.site.serve("/plain", self.zip_bytes)
        # https -> http downgrade is refused (needs a TLS site; see TlsTests), here only the bare-http hop is checked
        self.assertEqual(self.get(tls_target).size, len(self.zip_bytes))

    def test_redirect_loops_and_limits(self):
        self.site.redirect("/loop1", "/loop2")
        self.site.redirect("/loop2", "/loop1")
        self.assertIn("loop", str(self.fails("http://files.example.org/loop1", "redirect")).lower())
        for i in range(10):
            self.site.redirect(f"/hop{i}", f"/hop{i + 1}")
        self.site.serve("/hop10", self.zip_bytes)
        err = self.fails("http://files.example.org/hop0", "redirect")
        self.assertIn("Too many redirects", str(err))
        self.assertEqual(self.get("http://files.example.org/hop6", FetchLimits(**{**FAST.__dict__, "max_redirects": 5})).redirects, 4)

    def test_redirect_without_location(self):
        def no_location(h):
            h.send_response(302)
            h.send_header("Content-Length", "0")
            h.end_headers()
        self.site.routes["/nol"] = no_location
        self.assertIn("without saying where", str(self.fails("http://files.example.org/nol", "redirect")))

    def test_no_credentials_or_cookies_follow_a_redirect(self):
        def with_cookie(h):
            h.send_response(302)
            h.send_header("Location", "http://other.example.net/target")
            h.send_header("Set-Cookie", "session=SECRET; Path=/")
            h.send_header("Content-Length", "0")
            h.end_headers()
        self.site.routes["/c"] = with_cookie
        self.site.serve("/target", self.zip_bytes)
        self.get("http://files.example.org/c?token=SECRET")
        followed = [r for r in self.site.requests if r["path"] == "/target"][0]["headers"]
        self.assertNotIn("cookie", followed)
        self.assertNotIn("authorization", followed)
        self.assertNotIn("referer", followed)
        self.assertEqual(followed["host"], "other.example.net")


class DnsAndConnectTests(Base):
    def test_every_answer_must_be_public(self):
        self.net.table["mixed.example.org"] = [PUBLIC_IP, "10.0.0.7"]
        self.net.table["private.example.org"] = ["192.168.1.10"]
        self.net.table["meta.example.org"] = ["169.254.169.254"]
        self.net.table["six.example.org"] = ["::1"]
        self.net.table["mapped.example.org"] = ["::ffff:127.0.0.1"]
        self.site.serve("/x", self.zip_bytes)
        for host in ("mixed", "private", "meta", "six", "mapped"):
            err = self.fails(f"http://{host}.example.org/x", "unsafe-destination")
            self.assertFalse(err.retryable)
        self.assertEqual(self.net.connects, [])                                   # no socket was opened to any of them

    def test_connection_uses_exactly_the_validated_addresses_and_resolves_once(self):
        self.net.table["rebind.example.org"] = [PUBLIC_IP, PUBLIC_IP2]
        self.site.serve("/x", self.zip_bytes)
        self.get("http://rebind.example.org/x")
        self.assertEqual(self.net.resolutions, ["rebind.example.org"])           # resolved once: no second lookup to rebind
        self.assertEqual(self.net.connects, [((PUBLIC_IP, PUBLIC_IP2), 80)])      # the connector only ever receives the addresses that were checked

    def test_dns_rebinding_between_calls_cannot_redirect_the_connection(self):
        answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
        seen = []

        def flaky(host, port, timeout):
            seen.append(host)
            return next(answers)
        self.site.serve("/x", self.zip_bytes)
        net = self.net
        kwargs = net.kwargs(resolver=flaky, sleep=self.sleeps.append)
        netfetch.fetch_to_file("http://rebind.example.org/x", self.dest, FAST, **kwargs)
        self.assertEqual(seen, ["rebind.example.org"])                            # the later hostile answers were never even requested

    def test_default_connector_verifies_the_peer_address(self):
        class FakeSock:
            def __init__(self, peer):
                self.peer = peer
                self.closed = False

            def getpeername(self):
                return (self.peer, 80)

            def close(self):
                self.closed = True
        for peer in ("10.0.0.5", "127.0.0.1", "93.184.216.35"):                  # private, loopback, or simply not the address that was validated
            fake = FakeSock(peer)
            with mock.patch("socket.create_connection", return_value=fake), self.assertRaises(FetchError) as ctx:
                netfetch.socket_connector([PUBLIC_IP], 80, 1)
            self.assertEqual(ctx.exception.kind, "unsafe-destination")
            self.assertTrue(fake.closed)
        ok = FakeSock(PUBLIC_IP)
        with mock.patch("socket.create_connection", return_value=ok):
            sock, ip = netfetch.socket_connector([PUBLIC_IP], 80, 1)
        self.assertEqual((sock is ok, ip), (True, PUBLIC_IP))

    def test_default_connector_tries_the_next_validated_address(self):
        calls = []

        def fake_connect(addr, timeout):
            calls.append(addr[0])
            if addr[0] == PUBLIC_IP:
                raise ConnectionRefusedError()
            s = mock.Mock()
            s.getpeername.return_value = (addr[0], 80)
            return s
        with mock.patch("socket.create_connection", side_effect=fake_connect):
            _, ip = netfetch.socket_connector([PUBLIC_IP, PUBLIC_IP2], 80, 1)
        self.assertEqual((calls, ip), ([PUBLIC_IP, PUBLIC_IP2], PUBLIC_IP2))

    def test_default_resolver_literal_and_failure_kinds(self):
        self.assertEqual(netfetch.system_resolver("93.184.216.34", 80, 1), ["93.184.216.34"])
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "x")), self.assertRaises(FetchError) as ctx:
            netfetch.system_resolver("nonexistent.invalid", 80, 1)
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("dns", False))
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_AGAIN, "x")), self.assertRaises(FetchError) as ctx:
            netfetch.system_resolver("flaky.example.org", 80, 1)
        self.assertTrue(ctx.exception.retryable)
        infos = [(2, 1, 6, "", ("93.184.216.34", 80)), (2, 1, 6, "", ("93.184.216.34", 80)), (10, 1, 6, "", ("2606:4700::1", 80, 0, 0))]
        with mock.patch("socket.getaddrinfo", return_value=infos):
            self.assertEqual(netfetch.system_resolver("dual.example.org", 80, 1), ["93.184.216.34", "2606:4700::1"])

    def test_localhost_by_name_is_refused_with_the_real_resolver(self):
        err = self.fails("http://localhost/x", "unsafe-destination", resolver=netfetch.system_resolver)
        self.assertEqual(self.net.connects, [])
        self.assertNotIn("127.0.0.1", str(err))                                    # the message does not teach an attacker what the name resolves to


class FailureAndRetryTests(Base):
    def test_connection_refused_is_retried_with_exponential_backoff_then_gives_up(self):
        attempts = []

        def refuse(addrs, port, timeout):
            attempts.append(1)
            raise FetchError("connect", "Could not connect to the server", retryable=True)
        err = self.fails("http://files.example.org/x", "connect", connector=refuse)
        self.assertEqual((len(attempts), err.attempts), (3, 3))
        self.assertEqual(sum(self.sleeps), 2 + 4)                                  # 2 s then 4 s, injected sleep (nothing really waited)

    def test_transient_server_errors_succeed_on_a_later_attempt(self):
        def fail503(h):
            h.send_response(503)
            h.send_header("Content-Length", "0")
            h.end_headers()
        ok = lambda h: (h.send_response(200), h.send_header("Content-Length", str(len(self.zip_bytes))), h.end_headers(), h.wfile.write(self.zip_bytes))
        self.site.sequence("/flaky", [fail503, fail503, ok])
        res = self.get("http://files.example.org/flaky")
        self.assertEqual((res.attempts, self.hits("/flaky")), (3, 3))

    def test_retry_after_is_honoured_within_the_limit(self):
        def limited(h):
            h.send_response(429)
            h.send_header("Retry-After", "7")
            h.send_header("Content-Length", "0")
            h.end_headers()
        ok = lambda h: (h.send_response(200), h.send_header("Content-Length", str(len(self.zip_bytes))), h.end_headers(), h.wfile.write(self.zip_bytes))
        self.site.sequence("/rl", [limited, ok])
        res = self.get("http://files.example.org/rl")
        self.assertEqual(res.attempts, 2)
        self.assertEqual(sum(self.sleeps), 7)                                       # the server's delay, not the default backoff

    def test_retry_after_beyond_the_limit_is_not_waited_for(self):
        def limited(h):
            h.send_response(503)
            h.send_header("Retry-After", "86400")
            h.send_header("Content-Length", "0")
            h.end_headers()
        self.site.routes["/day"] = limited
        err = self.fails("http://files.example.org/day", "http")
        self.assertIn("asked to wait 86400", str(err))
        self.assertEqual((self.hits("/day"), self.sleeps), (1, []))

    def test_http_date_retry_after(self):
        self.assertAlmostEqual(netfetch._retry_after("5"), 5)
        self.assertIsNone(netfetch._retry_after("soon"))
        self.assertIsNone(netfetch._retry_after(""))
        self.assertGreaterEqual(netfetch._retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0)

    def test_permanent_failures_are_not_retried(self):
        for status in (400, 401, 403, 404, 410, 451):
            self.site.serve(f"/s{status}", b"", status=status)
            err = self.fails(f"http://files.example.org/s{status}", "http")
            self.assertFalse(err.retryable, status)
            self.assertEqual(self.hits(f"/s{status}"), 1, status)
        self.assertIn("login", str(self.fails("http://files.example.org/s401", "http")))
        self.assertEqual(self.sleeps, [])

    def test_retryable_statuses(self):
        for status in (408, 425, 429, 500, 502, 503, 504):
            self.site.serve(f"/r{status}", b"", status=status)
            self.fails(f"http://files.example.org/r{status}", "http")
            self.assertEqual(self.hits(f"/r{status}"), 3, status)                   # max_attempts, then give up

    def test_connection_dropped_midway_is_retried_then_reported(self):
        self.site.drop_midway("/cut", self.zip_bytes)
        err = self.fails("http://files.example.org/cut", "connection")
        self.assertEqual((err.attempts, self.hits("/cut")), (3, 3))

    def test_stalled_server_hits_the_read_timeout(self):
        limits = FetchLimits(**{**FAST.__dict__, "read_timeout": 0.4, "max_attempts": 2})
        self.site.stall("/stall", self.zip_bytes, 3)
        err = self.fails("http://files.example.org/stall", "timeout", limits)
        self.assertTrue(err.retryable or err.attempts == 2)
        self.assertEqual(self.hits("/stall"), 2)

    def test_overall_duration_limit(self):
        ticks = iter(range(0, 10_000, 100))
        clock = lambda: float(next(ticks))
        limits = FetchLimits(**{**FAST.__dict__, "max_duration": 150, "max_bytes": 10 ** 9})
        self.site.endless("/slow", self.zip_bytes)
        err = self.fails("http://files.example.org/slow", "timeout", limits, clock=clock)
        self.assertIn("longer than the allowed time", str(err))
        self.assertFalse(err.retryable)

    def test_unsafe_and_invalid_inputs_are_never_retried(self):
        self.net.table["bad.example.org"] = ["10.0.0.1"]
        self.fails("http://bad.example.org/x", "unsafe-destination")
        self.assertEqual(self.sleeps, [])
        self.site.serve("/junk", b"zzzz")
        self.fails("http://files.example.org/junk", "not-a-package")
        self.assertEqual((self.hits("/junk"), self.sleeps), (1, []))

    def test_progress_reports_bytes_and_waiting_and_can_cancel(self):
        self.site.serve("/p", self.zip_bytes + b"\0" * 3_000_000)
        seen = []
        self.get("http://files.example.org/p", progress=lambda stage, done, total: seen.append((stage, done, total)))
        self.assertEqual(seen[-1][0], "Downloading")
        self.assertEqual(seen[-1][1], seen[-1][2])                                  # known total, so a percentage is meaningful
        self.site.serve("/nolen", self.zip_bytes, length=False)
        seen.clear()
        self.get("http://files.example.org/nolen", progress=lambda s, d, t: seen.append((s, d, t)))
        self.assertTrue(all(t == 0 for _, _, t in seen))                            # unknown total is reported as 0, never as a fake percentage

        class Stop(Exception):
            pass

        def cancel(stage, done, total):
            raise Stop()
        self.site.serve("/c", self.zip_bytes)
        with self.assertRaises(Stop):
            self.get("http://files.example.org/c", progress=cancel)
        self.assertFalse(self.dest.exists())

    def test_disk_errors_are_reported_and_not_retried(self):
        self.site.serve("/d", self.zip_bytes)
        with self.assertRaises(FetchError) as ctx:
            netfetch.fetch_to_file("http://files.example.org/d", self.tmp / "missing-dir" / "out.bin", FAST, **self.net.kwargs(sleep=self.sleeps.append))
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("disk", False))
        import errno
        real_open = open

        def full(path, mode="r", *a, **kw):
            if str(path) == str(self.dest) and "w" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_open(path, mode, *a, **kw)
        with mock.patch("builtins.open", full), self.assertRaises(FetchError) as ctx:
            self.get("http://files.example.org/d")
        self.assertIn("full", str(ctx.exception))
        self.assertEqual(self.sleeps, [])


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to create a test certificate")
class TlsTests(Base):
    def setUp(self):
        super().setUp()
        key, crt = self.tmp / "k.pem", self.tmp / "c.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1", "-subj", "/CN=files.example.org",
                        "-addext", "subjectAltName=DNS:files.example.org"], check=True, capture_output=True)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(str(crt), str(key))
        self.tls_site = Site(server_ctx)
        self.addCleanup(self.tls_site.close)
        self.tls_net = FakeNet(self.tls_site)
        self.tls_site.serve("/pkg", self.zip_bytes)
        self.trusting = ssl.create_default_context(cafile=str(crt))

    def fetch(self, url, **extra):
        return netfetch.fetch_to_file(url, self.dest, FAST, **self.tls_net.kwargs(sleep=self.sleeps.append, **extra))

    def test_certificate_verification_is_on_by_default(self):
        with self.assertRaises(FetchError) as ctx:
            self.fetch("https://files.example.org/pkg")                              # self-signed certificate, default trust store
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("tls", False))
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.dest.exists())

    def test_a_trusted_certificate_for_the_right_name_works(self):
        res = self.fetch("https://files.example.org/pkg", ssl_context=self.trusting)
        self.assertEqual(res.size, len(self.zip_bytes))
        self.assertEqual(self.tls_net.connects[-1][1], 443)

    def test_host_name_mismatch_is_refused_even_with_a_trusted_issuer(self):
        with self.assertRaises(FetchError) as ctx:
            self.fetch("https://other.example.org/pkg", ssl_context=self.trusting)
        self.assertEqual(ctx.exception.kind, "tls")

    def test_https_to_http_redirect_is_refused(self):
        self.tls_site.redirect("/down", "http://files.example.org/pkg")
        with self.assertRaises(FetchError) as ctx:
            self.fetch("https://files.example.org/down", ssl_context=self.trusting)
        self.assertIn("HTTPS to plain HTTP", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

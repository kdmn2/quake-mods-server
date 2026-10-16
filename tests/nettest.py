"""Controlled network infrastructure for tests. No public servers, no sockets leave the machine.

`Site` is a real HTTP(S) server on 127.0.0.1 whose behaviour is scripted per path. `FakeNet` supplies the resolver and connector that
netfetch accepts as arguments: names resolve to a *public-looking* address (so the production address checks run for real) and the
connector then dials the local server. The production default resolver/connector are never involved, and nothing in the application reads
these hooks from configuration or the environment.
"""
from __future__ import annotations

import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

PUBLIC_IP = "93.184.216.34"
PUBLIC_IP2 = "151.101.1.69"


class Site:
    def __init__(self, tls_context: ssl.SSLContext | None = None):
        self.routes: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                with site.lock:
                    site.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "time": time.monotonic()})
                handler = site.routes.get(self.path.split("?", 1)[0])
                if handler is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    handler(self)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        if tls_context is not None:
            self.httpd.socket = tls_context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- scripted behaviours -------------------------------------------------------------------------------------------
    def serve(self, path: str, body: bytes, *, status: int = 200, ctype: str = "application/octet-stream", headers: dict | None = None,
              length: bool = True, chunked: bool = False) -> None:
        def handler(h: BaseHTTPRequestHandler) -> None:
            h.send_response(status)
            h.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            if chunked:
                h.send_header("Transfer-Encoding", "chunked")
                h.end_headers()
                for i in range(0, len(body), 4096):
                    piece = body[i:i + 4096]
                    h.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                h.wfile.write(b"0\r\n\r\n")
            elif length:
                h.send_header("Content-Length", str(len(body)))
                h.end_headers()
                h.wfile.write(body)
            else:                                                     # no Content-Length: the body ends when the connection closes
                h.send_header("Connection", "close")
                h.end_headers()
                h.wfile.write(body)
                h.close_connection = True
        self.routes[path] = handler

    def redirect(self, path: str, location: str, status: int = 302) -> None:
        def handler(h: BaseHTTPRequestHandler) -> None:
            h.send_response(status)
            h.send_header("Location", location)
            h.send_header("Content-Length", "0")
            h.end_headers()
        self.routes[path] = handler

    def sequence(self, path: str, responses: list[Callable[[BaseHTTPRequestHandler], None]]) -> None:
        """Different behaviour on each successive request (the last repeats)."""
        state = {"n": 0}

        def handler(h: BaseHTTPRequestHandler) -> None:
            fn = responses[min(state["n"], len(responses) - 1)]
            state["n"] += 1
            fn(h)
        self.routes[path] = handler

    def stall(self, path: str, first_bytes: bytes, seconds: float) -> None:
        def handler(h: BaseHTTPRequestHandler) -> None:
            h.send_response(200)
            h.send_header("Content-Length", str(len(first_bytes) + 1000))
            h.end_headers()
            h.wfile.write(first_bytes)
            h.wfile.flush()
            time.sleep(seconds)
        self.routes[path] = handler

    def endless(self, path: str, first: bytes, chunk: bytes = b"x" * 65536, announce: int | None = None) -> None:
        def handler(h: BaseHTTPRequestHandler) -> None:
            h.send_response(200)
            if announce is not None:
                h.send_header("Content-Length", str(announce))
            else:
                h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(first)
            try:
                while True:
                    h.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            h.close_connection = True
        self.routes[path] = handler

    def drop_midway(self, path: str, body: bytes) -> None:
        def handler(h: BaseHTTPRequestHandler) -> None:
            h.send_response(200)
            h.send_header("Content-Length", str(len(body) + 5000))
            h.end_headers()
            h.wfile.write(body)
            h.wfile.flush()
            h.close_connection = True                                 # promises more bytes than it sends, then hangs up
        self.routes[path] = handler


class FakeNet:
    """resolver + connector for netfetch. `table` maps host name -> list of answers; unknown names get one public address."""

    def __init__(self, site: Site, table: dict[str, list[str]] | None = None):
        self.site = site
        self.table = dict(table or {})
        self.resolutions: list[str] = []
        self.connects: list[tuple[tuple[str, ...], int]] = []

    def resolver(self, host: str, port: int, timeout: float) -> list[str]:
        self.resolutions.append(host)
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        return list(self.table.get(host, [PUBLIC_IP]))

    def connector(self, addrs: list[str], port: int, timeout: float):
        self.connects.append((tuple(addrs), port))
        sock = socket.create_connection(("127.0.0.1", self.site.port), timeout=timeout)
        return sock, addrs[0]

    def kwargs(self, **extra) -> dict:
        return {"resolver": self.resolver, "connector": self.connector, **extra}

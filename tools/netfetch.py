"""Download a package from an untrusted URL without letting the URL, DNS, redirects or the server reach anything internal.

Everything the remote side controls is treated as hostile: the URL text, DNS answers, redirect targets, headers and body.

Threat model and how it is handled
* SSRF: only public destinations. Every address a name resolves to must be public (a mixed answer is refused, not filtered),
  and the connection goes to one of those exact validated addresses. The name is never resolved a second time, so there is no
  window for DNS rebinding between "check" and "connect". After connecting the peer address is verified again.
  Every redirect target goes through the same checks. Numeric hosts must be canonical dotted-quad IPv4 or bracketed IPv6;
  forms such as 2130706433, 0x7f.1, 127.1 or 0177.0.0.1 are refused as ambiguous instead of being interpreted.
* No proxies: http.client is used directly, so HTTP(S)_PROXY and friends in the environment are never consulted.
* TLS: the default context (certificate and hostname verification on, TLS >= 1.2). A failed verification is never retried.
* No secrets leave with the request: the only headers sent are fixed ones. No cookies, no Authorization, nothing forwarded
  across redirects, because nothing is ever attached.
* Resource limits are enforced while streaming (never trusting Content-Length): bytes on the wire, bytes after HTTP content
  decoding (a gzip bomb is cut off), connect time, silence between reads, total time, number of redirects.
* The first bytes decide whether it is a package (ZIP or PAK signature) so an HTML error page or login form is rejected after
  one chunk instead of being downloaded, and is never rendered or followed.

Tests inject `resolver`, `connector` and `ssl_context` as function arguments to talk to a local server. There is no environment
variable, configuration key or global switch that turns the protections off.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit

USER_AGENT = "ironwail-addon-server/1 (package import)"
CHUNK = 1024 * 1024
READ_SIZE = 256 * 1024          # read1(): returns what has arrived, so progress, cancellation and the time limit stay responsive on slow links
SNIFF_BYTES = 512
ZIP_MAGIC = b"PK\x03\x04"
PAK_MAGIC = b"PACK"


class FetchError(Exception):
    """A download problem with a stable `kind`, whether trying again could help, and an optional server-requested delay."""

    def __init__(self, kind: str, message: str, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after
        self.attempts = 1


@dataclass(frozen=True)
class FetchLimits:
    max_bytes: int = 2 * 1024 ** 3
    connect_timeout: float = 10.0
    read_timeout: float = 30.0          # silence allowed between two reads
    max_duration: float = 3600.0        # one attempt, start to finish
    max_redirects: int = 5
    max_attempts: int = 3
    backoff_base: float = 2.0
    retry_after_max: float = 120.0
    max_url_length: int = 2048


@dataclass
class FetchResult:
    size: int
    sha256: str
    filename: str
    host: str
    content_type: str
    attempts: int
    redirects: int
    content_encoding: str


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    host: str                # ASCII; IPv6 without brackets
    port: int
    target: str              # path + query as sent on the request line
    url: str                 # normalised absolute URL (no fragment)

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
            return True
        except ValueError:
            return False


# ---- address policy ----------------------------------------------------------------------------------------------------
_EXTRA_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "198.18.0.0/15", "240.0.0.0/4",
    "168.63.129.16/32",                                   # Azure wire server (a public-looking address that is really infrastructure)
    "::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16", "fec0::/10",
))


def is_public_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = ipaddress.ip_address(value) if isinstance(value, str) else value
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped                              # ::ffff:127.0.0.1 is 127.0.0.1
    # is_global alone is not enough (it does not exclude multicast, for example), so every special class is named explicitly
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_private or not ip.is_global:
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED)


_NUMERICISH = re.compile(r"^(0[xX][0-9a-fA-F]+|[0-9]+)(\.(0[xX][0-9a-fA-F]+|[0-9]+))*$")
_DOTTED_QUAD = re.compile(r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$")
_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


def _canonical_host(host: str) -> str:
    if ":" in host:                                      # IPv6 literal (urlsplit already removed the brackets)
        if "%" in host:
            raise FetchError("bad-url", "IPv6 zone identifiers are not allowed")
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            raise FetchError("bad-url", "Invalid IPv6 address") from None
    host = host[:-1] if host.endswith(".") else host
    if not host:
        raise FetchError("bad-url", "The URL has no host name")
    if _NUMERICISH.match(host):
        if _DOTTED_QUAD.match(host):
            return host
        raise FetchError("bad-url", "Numeric hosts must be written as a plain dotted IPv4 address; shortened, hex, octal or integer forms are refused")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise FetchError("bad-url", "The host name is not valid") from None
    if len(ascii_host) > 253 or not all(_LABEL.match(label) for label in ascii_host.split(".")):
        raise FetchError("bad-url", "The host name is not valid")
    return ascii_host


_DEFAULT_PORT = {"http": 80, "https": 443}
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"


def parse_url(url: str, max_length: int = 2048) -> ParsedUrl:
    """Syntax and literal-address checks only (no DNS). Raises FetchError('bad-url'|'unsafe-destination')."""
    if not isinstance(url, str) or not url.strip():
        raise FetchError("bad-url", "The URL is empty")
    if len(url) > max_length:
        raise FetchError("bad-url", f"The URL is longer than {max_length} characters")
    if any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise FetchError("bad-url", "This is not a single URL (it contains spaces or control characters)")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise FetchError("bad-url", "The URL is not valid") from None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORT:
        if len(scheme) <= 1:                             # no scheme at all, or a Windows drive letter such as C:\
            raise FetchError("bad-url", "This is not a web link. Use a full http:// or https:// URL (local paths and commands are not accepted)")
        raise FetchError("bad-url", f"Only http:// and https:// links are supported (not {scheme[:20]}:)")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise FetchError("bad-url", "URLs with embedded credentials are not accepted")
    if not host:
        raise FetchError("bad-url", "The URL has no host name")
    if port == 0:
        raise FetchError("bad-url", "Invalid port")
    canon = _canonical_host(host)
    try:
        literal = ipaddress.ip_address(canon)
    except ValueError:
        literal = None
    if literal is not None and not is_public_ip(literal):
        raise FetchError("unsafe-destination", "The address is not a public internet address")
    path = quote(parts.path or "/", safe=_PATH_SAFE)
    query = quote(parts.query, safe=_PATH_SAFE + "?") if parts.query else ""
    target = path + ("?" + query if parts.query else "")
    port = port or _DEFAULT_PORT[scheme]
    shown_host = f"[{canon}]" if ":" in canon else canon
    default = port == _DEFAULT_PORT[scheme]
    normalised = f"{scheme}://{shown_host}{'' if default else ':' + str(port)}{target}"
    return ParsedUrl(scheme, canon, port, target, normalised)


# ---- display helpers (what may be shown or logged) ---------------------------------------------------------------------
def safe_display_filename(name: str | None, fallback: str = "download") -> str:
    """A name safe to display and to use as a label. Never used to build a filesystem path."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c if (c.isalnum() and c.isascii()) or c in "._- " else "_" for c in name if ord(c) >= 32)
    name = re.sub(r"[ _]{2,}", "_", name).strip(" ._")
    if len(name) > 80:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:70] + dot + ext[:8]) if dot and len(ext) <= 8 else name[:80]
    return name or fallback


def redact_url(url: str) -> str:
    """Host plus the last path segment. Query strings, fragments, credentials and the rest of the path (where signed tokens live) are dropped."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        base = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]) if parts.path else ""
    except ValueError:
        return "(invalid URL)"
    host = f"[{host}]" if ":" in host else host
    shown = safe_display_filename(base, "") if base else ""
    more = "/…/" if parts.path.count("/") > 1 else "/"
    return f"{parts.scheme or 'url'}://{host}{more}{shown}" + (" (signed/query link)" if parts.query else "")


def redact_text(text: str, url: str | None) -> str:
    """Remove a URL (and its query string) from an error message before it is stored, logged or shown."""
    if not text or not url:
        return text
    out = text.replace(url, redact_url(url))
    try:
        query = urlsplit(url).query
    except ValueError:
        query = ""
    if query:
        out = out.replace(query, "…")
        for key, value in parse_qsl(query, keep_blank_values=True):      # parts of the query that appear on their own in a message
            if value and len(value) >= 3:
                out = out.replace(f"{key}={value}", f"{key}=…").replace(value, "…")
    return out


def url_key(url: str) -> str:
    """Stable identity of a link for duplicate detection (case-insensitive host, default port and fragment removed)."""
    return hashlib.sha256(parse_url(url, 10_000).url.encode()).hexdigest()


@dataclass
class UrlProblem:
    line: int
    text: str
    message: str


def parse_url_list(text: str, max_items: int, max_length: int = 2048) -> tuple[list[tuple[int, ParsedUrl]], list[UrlProblem], list[UrlProblem]]:
    """One URL per line. Returns (accepted, problems, duplicates). Blank lines and surrounding whitespace are ignored."""
    accepted: list[tuple[int, ParsedUrl]] = []
    problems: list[UrlProblem] = []
    dups: list[UrlProblem] = []
    seen: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = parse_url(line, max_length)
        except FetchError as exc:
            problems.append(UrlProblem(number, _snippet(line), str(exc)))
            continue
        if parsed.url in seen:
            dups.append(UrlProblem(number, redact_url(parsed.url), f"Same link as line {seen[parsed.url]}; imported once"))
            continue
        seen[parsed.url] = number
        accepted.append((number, parsed))
    if len(accepted) > max_items:
        problems.append(UrlProblem(0, "", f"Too many links: {len(accepted)} (at most {max_items} per batch)"))
    return accepted, problems, dups


def mask_credentials(text: str) -> str:
    """Hide `user:password@` in any URLs inside free text (used when a rejected list is shown again for correction)."""
    return re.sub(r"(?i)(\bhttps?://)[^/@\s]*@", r"\1…@", text)


def _snippet(line: str) -> str:
    line = re.sub(r"//[^/@\s]*@", "//…@", line)           # credentials
    line = re.split(r"[?#]", line, maxsplit=1)[0]                   # query / fragment
    return line[:60] + ("…" if len(line) > 60 else "")


# ---- network primitives (injectable for tests) -----------------------------------------------------------------------
Resolver = Callable[[str, int, float], "list[str]"]
Connector = Callable[["list[str]", int, float], "tuple[socket.socket, str]"]


def system_resolver(host: str, port: int, timeout: float) -> list[str]:
    if _is_ip(host):
        return [host]
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        infos = future.result(timeout=timeout)
    except FutureTimeout:
        raise FetchError("dns", "Looking up the host name timed out", retryable=True) from None
    except socket.gaierror as exc:
        temporary = exc.errno in (socket.EAI_AGAIN, getattr(socket, "EAI_FAIL", -4))
        raise FetchError("dns", "The host name could not be resolved" + (" (temporary failure)" if temporary else ""), retryable=temporary) from None
    finally:
        pool.shutdown(wait=False)
    seen: list[str] = []
    for info in infos:
        addr = info[4][0].split("%", 1)[0]
        if addr not in seen:
            seen.append(addr)
    return seen


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def socket_connector(addrs: list[str], port: int, timeout: float) -> tuple[socket.socket, str]:
    """Connect to one of the already-validated addresses and verify the peer really is that address and still public."""
    last: Exception | None = None
    for ip in addrs:
        try:
            sock = socket.create_connection((ip, port), timeout=timeout)
        except socket.timeout as exc:
            last = exc
            continue
        except OSError as exc:
            last = exc
            continue
        peer = sock.getpeername()[0].split("%", 1)[0]
        if ipaddress.ip_address(peer) != ipaddress.ip_address(ip) or not is_public_ip(peer):
            sock.close()
            raise FetchError("unsafe-destination", "The connection went to an unexpected address and was dropped")
        return sock, ip
    if isinstance(last, socket.timeout):
        raise FetchError("timeout", "Connecting timed out", retryable=True)
    raise FetchError("connect", "Could not connect to the server", retryable=True)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, *, addrs, connector, connect_timeout, read_timeout):
        super().__init__(host, port, timeout=read_timeout)
        self._addrs, self._connector, self._connect_timeout, self._read_timeout = addrs, connector, connect_timeout, read_timeout

    def connect(self) -> None:
        sock, _ = self._connector(self._addrs, self.port, self._connect_timeout)
        sock.settimeout(self._read_timeout)
        self.sock = sock


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, *, addrs, connector, connect_timeout, read_timeout, context):
        super().__init__(host, port, timeout=read_timeout, context=context)
        self._addrs, self._connector, self._connect_timeout, self._read_timeout = addrs, connector, connect_timeout, read_timeout

    def connect(self) -> None:
        sock, _ = self._connector(self._addrs, self.port, self._connect_timeout)
        try:
            tls = self._context.wrap_socket(sock, server_hostname=self.host)     # certificate and host name are verified here
        except Exception:
            sock.close()
            raise
        tls.settimeout(self._read_timeout)
        self.sock = tls


# ---- the download --------------------------------------------------------------------------------------------------------
_REDIRECTS = (301, 302, 303, 307, 308)
_RETRYABLE_STATUS = (408, 425, 429, 500, 502, 503, 504)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(int(value))
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _filename_from(resp: http.client.HTTPResponse, final: ParsedUrl) -> str:
    disposition = resp.getheader("Content-Disposition")
    name = None
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        try:
            name = msg.get_filename()
        except Exception:
            name = None
    if not name:
        name = unquote(final.target.split("?", 1)[0].rsplit("/", 1)[-1])
    return safe_display_filename(name)


def _classify_start(first: bytes) -> None:
    if first.startswith(ZIP_MAGIC) or first.startswith(PAK_MAGIC):
        return
    head = first[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml")) or b"<html" in head:
        raise FetchError("html", "The link returned a web page, not a package. A direct download link to the ZIP or PAK file is required "
                                  "(pages that need a login, a click-through or JavaScript are not supported).")
    if not first:
        raise FetchError("not-a-package", "The server sent an empty response")
    raise FetchError("not-a-package", "The download is not a ZIP archive or a Quake PAK file")


def _fetch_once(url: str, dest: Path, limits: FetchLimits, progress, resolver: Resolver, connector: Connector,
                ssl_context: ssl.SSLContext | None, clock) -> FetchResult:
    started = clock()
    current = parse_url(url, limits.max_url_length)
    seen = {current.url}
    redirects = 0
    while True:
        addrs = resolver(current.host, current.port, limits.connect_timeout)
        if not addrs:
            raise FetchError("dns", "The host name has no addresses", retryable=False)
        if not all(is_public_ip(a) for a in addrs):
            raise FetchError("unsafe-destination", "The host name points to a private or internal address; only public internet destinations are allowed")
        if current.scheme == "https":
            conn: http.client.HTTPConnection = _PinnedHTTPS(current.host, current.port, addrs=addrs, connector=connector, connect_timeout=limits.connect_timeout,
                                                            read_timeout=limits.read_timeout, context=ssl_context or ssl.create_default_context())
        else:
            conn = _PinnedHTTP(current.host, current.port, addrs=addrs, connector=connector, connect_timeout=limits.connect_timeout, read_timeout=limits.read_timeout)
        try:
            try:
                conn.request("GET", current.target, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity", "Connection": "close"})
                resp = conn.getresponse()
            except ssl.SSLCertVerificationError:
                raise FetchError("tls", "The server's TLS certificate could not be verified (untrusted, expired or for another host name)") from None
            except ssl.SSLError as exc:
                raise FetchError("tls", f"TLS error: {exc.__class__.__name__}") from None
            except socket.timeout:
                raise FetchError("timeout", "The server did not respond in time", retryable=True) from None
            except (ConnectionError, http.client.HTTPException, OSError) as exc:
                if isinstance(exc, FetchError):
                    raise
                raise FetchError("connection", "The connection failed or was interrupted", retryable=True) from None

            status = resp.status
            if status in _REDIRECTS:
                location = resp.getheader("Location")
                resp.read(0)
                if not location:
                    raise FetchError("redirect", f"The server redirected (HTTP {status}) without saying where to")
                redirects += 1
                if redirects > limits.max_redirects:
                    raise FetchError("redirect", f"Too many redirects (more than {limits.max_redirects})")
                nxt = parse_url(urljoin(current.url, location), limits.max_url_length)       # same checks as the first URL
                if current.scheme == "https" and nxt.scheme == "http":
                    raise FetchError("redirect", "The server redirected from HTTPS to plain HTTP, which is refused")
                if nxt.url in seen:
                    raise FetchError("redirect", "Redirect loop")
                seen.add(nxt.url)
                current = nxt
                continue
            if status != 200:
                wait = _retry_after(resp.getheader("Retry-After"))
                if status in _RETRYABLE_STATUS:
                    raise FetchError("http", f"The server answered HTTP {status}" + (" (rate limited)" if status == 429 else ""), retryable=True, retry_after=wait)
                if status in (401, 403):
                    raise FetchError("http", f"The server refused access (HTTP {status}). The link may have expired or need a login, which is not supported")
                if status in (404, 410):
                    raise FetchError("http", f"The file was not found (HTTP {status})")
                raise FetchError("http", f"The server answered HTTP {status}")

            ctype = (resp.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype in ("text/html", "application/xhtml+xml"):
                raise FetchError("html", "The link returned a web page, not a package. A direct download link to the ZIP or PAK file is required.")
            length = resp.getheader("Content-Length")
            total = int(length) if length and length.isdigit() else 0
            if total > limits.max_bytes:
                raise FetchError("too-large", f"The file is larger than the allowed {limits.max_bytes} bytes")
            encoding = (resp.getheader("Content-Encoding") or "identity").strip().lower()
            if encoding in ("", "identity"):
                decoder = None
            elif encoding in ("gzip", "x-gzip"):
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif encoding == "deflate":
                decoder = zlib.decompressobj()
            else:
                raise FetchError("encoding", f"The server used an unsupported Content-Encoding ({encoding[:20]})")

            digest = hashlib.sha256()
            wire = decoded = 0
            head: list[bytes] = []          # until enough bytes have arrived to recognise (or reject) the content
            head_len = 0
            classified = False
            try:
                with open(dest, "wb") as out:
                    def emit(piece: bytes) -> None:
                        nonlocal head_len, classified
                        if not piece:
                            return
                        if classified:
                            digest.update(piece)
                            out.write(piece)
                            return
                        head.append(piece)
                        head_len += len(piece)
                        if head_len >= SNIFF_BYTES:
                            joined = b"".join(head)
                            _classify_start(joined)
                            classified = True
                            digest.update(joined)
                            out.write(joined)
                            head.clear()

                    while True:
                        if clock() - started > limits.max_duration:
                            raise FetchError("timeout", "The download took longer than the allowed time")
                        try:
                            chunk = resp.read1(READ_SIZE)
                        except socket.timeout:
                            raise FetchError("timeout", "The server stopped sending data", retryable=True) from None
                        except (http.client.IncompleteRead, ConnectionError, OSError, http.client.HTTPException):
                            raise FetchError("connection", "The connection was interrupted during the download", retryable=True) from None
                        if not chunk:
                            break
                        wire += len(chunk)
                        if wire > limits.max_bytes:
                            raise FetchError("too-large", f"The download exceeded the allowed {limits.max_bytes} bytes")
                        if decoder is None:
                            emit(chunk)
                        else:
                            pending = chunk
                            while pending:
                                try:
                                    piece = decoder.decompress(pending, CHUNK)
                                except zlib.error:
                                    raise FetchError("encoding", "The response body could not be decoded") from None
                                decoded += len(piece)
                                if decoded > limits.max_bytes:
                                    raise FetchError("too-large", f"The decoded content exceeded the allowed {limits.max_bytes} bytes")
                                emit(piece)
                                pending = decoder.unconsumed_tail
                                if not piece:
                                    break
                        if progress:
                            progress("Downloading", wire, total)
                    if decoder is not None:
                        tail = decoder.flush()
                        if tail:
                            decoded += len(tail)
                            if decoded > limits.max_bytes:
                                raise FetchError("too-large", "The decoded content exceeded the allowed size")
                            emit(tail)
                    if not classified:                       # short body: decide on what there is
                        joined = b"".join(head)
                        _classify_start(joined)
                        digest.update(joined)
                        out.write(joined)
            except OSError as exc:
                if isinstance(exc, FetchError):
                    raise
                if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise FetchError("disk", "The staging area is full") from None
                raise FetchError("disk", "The staging area could not be written (permissions or a read-only volume)") from None
            if total and wire != total and decoder is None:
                raise FetchError("connection", "The download ended before the announced size", retryable=True)
            size = decoded if decoder is not None else wire
            return FetchResult(size=size, sha256=digest.hexdigest(), filename=_filename_from(resp, current), host=current.host,
                               content_type=ctype, attempts=1, redirects=redirects, content_encoding=encoding or "identity")
        finally:
            conn.close()


@dataclass
class MemoryResult:
    body: bytes
    content_type: str
    url: str                      # final URL after redirects
    attempts: int
    redirects: int


def _fetch_memory_once(url: str, limits: FetchLimits, accepted_types: tuple[str, ...], max_bytes: int, progress,
                       resolver: Resolver, connector: Connector, ssl_context: ssl.SSLContext | None, clock) -> MemoryResult:
    """One GET into memory with the same destination, redirect and transport checks as _fetch_once, but no package sniffing:
    the response must match `accepted_types` (empty tuple accepts anything but HTML) and fit `max_bytes` after decoding."""
    started = clock()
    current = parse_url(url, limits.max_url_length)
    seen = {current.url}
    redirects = 0
    while True:
        addrs = resolver(current.host, current.port, limits.connect_timeout)
        if not addrs:
            raise FetchError("dns", "The host name has no addresses", retryable=False)
        if not all(is_public_ip(a) for a in addrs):
            raise FetchError("unsafe-destination", "The host name points to a private or internal address; only public internet destinations are allowed")
        if current.scheme == "https":
            conn: http.client.HTTPConnection = _PinnedHTTPS(current.host, current.port, addrs=addrs, connector=connector, connect_timeout=limits.connect_timeout,
                                                            read_timeout=limits.read_timeout, context=ssl_context or ssl.create_default_context())
        else:
            conn = _PinnedHTTP(current.host, current.port, addrs=addrs, connector=connector, connect_timeout=limits.connect_timeout, read_timeout=limits.read_timeout)
        try:
            try:
                conn.request("GET", current.target, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity", "Connection": "close"})
                resp = conn.getresponse()
            except ssl.SSLCertVerificationError:
                raise FetchError("tls", "The server's TLS certificate could not be verified (untrusted, expired or for another host name)") from None
            except ssl.SSLError as exc:
                raise FetchError("tls", f"TLS error: {exc.__class__.__name__}") from None
            except socket.timeout:
                raise FetchError("timeout", "The server did not respond in time", retryable=True) from None
            except (ConnectionError, http.client.HTTPException, OSError) as exc:
                if isinstance(exc, FetchError):
                    raise
                raise FetchError("connection", "The connection failed or was interrupted", retryable=True) from None

            status = resp.status
            if status in _REDIRECTS:
                location = resp.getheader("Location")
                resp.read(0)
                if not location:
                    raise FetchError("redirect", f"The server redirected (HTTP {status}) without saying where to")
                redirects += 1
                if redirects > limits.max_redirects:
                    raise FetchError("redirect", f"Too many redirects (more than {limits.max_redirects})")
                nxt = parse_url(urljoin(current.url, location), limits.max_url_length)       # same checks as the first URL
                if current.scheme == "https" and nxt.scheme == "http":
                    raise FetchError("redirect", "The server redirected from HTTPS to plain HTTP, which is refused")
                if nxt.url in seen:
                    raise FetchError("redirect", "Redirect loop")
                seen.add(nxt.url)
                current = nxt
                continue
            if status != 200:
                wait = _retry_after(resp.getheader("Retry-After"))
                if status in _RETRYABLE_STATUS:
                    raise FetchError("http", f"The server answered HTTP {status}" + (" (rate limited)" if status == 429 else ""), retryable=True, retry_after=wait)
                if status in (401, 403):
                    raise FetchError("http", f"The server refused access (HTTP {status})")
                if status in (404, 410):
                    raise FetchError("http", f"The file was not found (HTTP {status})")
                raise FetchError("http", f"The server answered HTTP {status}")

            ctype = (resp.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype in ("text/html", "application/xhtml+xml"):
                raise FetchError("html", "The link returned a web page, not structured data.")
            if accepted_types and ctype not in accepted_types:
                raise FetchError("content", f"The answer is {ctype or 'of an unknown type'}, not {accepted_types[0]}")
            length = resp.getheader("Content-Length")
            total = int(length) if length and length.isdigit() else 0
            if total > max_bytes:
                raise FetchError("too-large", f"The response is larger than the allowed {max_bytes} bytes")
            encoding = (resp.getheader("Content-Encoding") or "identity").strip().lower()
            if encoding in ("", "identity"):
                decoder = None
            elif encoding in ("gzip", "x-gzip"):
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif encoding == "deflate":
                decoder = zlib.decompressobj()
            else:
                raise FetchError("encoding", f"The server used an unsupported Content-Encoding ({encoding[:20]})")

            body = bytearray()
            wire = decoded = 0
            try:
                while True:
                    if clock() - started > limits.max_duration:
                        raise FetchError("timeout", "The download took longer than the allowed time")
                    try:
                        chunk = resp.read1(READ_SIZE)
                    except socket.timeout:
                        raise FetchError("timeout", "The server stopped sending data", retryable=True) from None
                    except (http.client.IncompleteRead, ConnectionError, OSError, http.client.HTTPException):
                        raise FetchError("connection", "The connection was interrupted", retryable=True) from None
                    if not chunk:
                        break
                    wire += len(chunk)
                    if wire > max_bytes:
                        raise FetchError("too-large", f"The response exceeded the allowed {max_bytes} bytes")
                    if decoder is None:
                        body += chunk
                    else:
                        pending = chunk
                        while pending:
                            try:
                                piece = decoder.decompress(pending, CHUNK)
                            except zlib.error:
                                raise FetchError("encoding", "The response body could not be decoded") from None
                            decoded += len(piece)
                            if decoded > max_bytes:
                                raise FetchError("too-large", f"The decoded content exceeded the allowed {max_bytes} bytes")
                            body += piece
                            pending = decoder.unconsumed_tail
                            if not piece:
                                break
                    if progress:
                        progress("Fetching", wire, total)
                if decoder is not None:
                    tail = decoder.flush()
                    decoded += len(tail)
                    if decoded > max_bytes:
                        raise FetchError("too-large", "The decoded content exceeded the allowed size")
                    body += tail
            except MemoryError:
                raise FetchError("too-large", f"The response exceeded the allowed {max_bytes} bytes") from None
            return MemoryResult(body=bytes(body), content_type=ctype, url=current.url, attempts=1, redirects=redirects)
        finally:
            conn.close()


def fetch_to_memory(url: str, limits: FetchLimits | None = None, *, accepted_types: tuple[str, ...] = (), max_bytes: int = 32 * 1024 ** 2,
                    progress=None, resolver: Resolver | None = None, connector: Connector | None = None, ssl_context: ssl.SSLContext | None = None,
                    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> MemoryResult:
    """GET a bounded response into memory (metadata, not packages). Same destination policy, TLS, redirect and retry rules as
    fetch_to_file; the body is capped while streaming, never trusted from Content-Length."""
    limits = limits or FetchLimits()
    resolver = resolver or system_resolver
    connector = connector or socket_connector
    attempt = 0
    while True:
        attempt += 1
        try:
            result = _fetch_memory_once(url, limits, accepted_types, max_bytes, progress, resolver, connector, ssl_context, clock)
            result.attempts = attempt
            return result
        except FetchError as exc:
            exc.attempts = attempt
            if not exc.retryable or attempt >= limits.max_attempts:
                raise
            if exc.retry_after is not None and exc.retry_after > limits.retry_after_max:
                raise FetchError(exc.kind, f"{exc} The server asked to wait {int(exc.retry_after)} s, longer than the allowed {int(limits.retry_after_max)} s.",
                                 retryable=False) from None
            delay = exc.retry_after if exc.retry_after is not None else limits.backoff_base * (2 ** (attempt - 1))
            delay = min(delay, limits.retry_after_max)
            waited = 0.0
            while waited < delay:
                step = min(0.5, delay - waited)
                if progress:
                    progress(f"Waiting to retry (attempt {attempt + 1} of {limits.max_attempts})", 0, 0)
                sleep(step)
                waited += step
        except BaseException:
            raise


def fetch_to_file(url: str, dest: Path, limits: FetchLimits | None = None, progress=None, *, resolver: Resolver | None = None,
                  connector: Connector | None = None, ssl_context: ssl.SSLContext | None = None,
                  sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> FetchResult:
    """Download `url` into `dest` (created/overwritten). Retries only failures that can plausibly succeed later, with bounded backoff.

    `progress(stage, done, total)` is called per chunk and while waiting between attempts; it may raise to cancel.
    On any failure `dest` is removed."""
    limits = limits or FetchLimits()
    resolver = resolver or system_resolver
    connector = connector or socket_connector
    attempt = 0
    while True:
        attempt += 1
        try:
            result = _fetch_once(url, dest, limits, progress, resolver, connector, ssl_context, clock)
            result.attempts = attempt
            return result
        except FetchError as exc:
            dest.unlink(missing_ok=True)
            exc.attempts = attempt
            if not exc.retryable or attempt >= limits.max_attempts:
                raise
            if exc.retry_after is not None and exc.retry_after > limits.retry_after_max:
                raise FetchError(exc.kind, f"{exc} The server asked to wait {int(exc.retry_after)} s, longer than the allowed {int(limits.retry_after_max)} s.",
                                 retryable=False) from None
            delay = exc.retry_after if exc.retry_after is not None else limits.backoff_base * (2 ** (attempt - 1))
            delay = min(delay, limits.retry_after_max)
            waited = 0.0
            while waited < delay:
                step = min(0.5, delay - waited)
                if progress:
                    progress(f"Waiting to retry (attempt {attempt + 1} of {limits.max_attempts})", 0, 0)
                sleep(step)
                waited += step
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

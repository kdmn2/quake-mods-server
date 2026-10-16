#!/usr/bin/env python3
"""Exact-input acceptance over real HTTP against a running instance (local process or container).

    upload ZIP -> conversion -> draft -> publish -> fetch /content.json -> download artifact
    -> parse the downloaded PAK and compare it with an expectation computed independently from the ZIP.

The expectation code below deliberately imports nothing from tools.catalog. It encodes the Ironwail search order
(common.c): pakN ... pak1, pak0, then loose files; first duplicate inside a PAK wins; a single non-content
top-level directory is a wrapper. Exit status 0 only if the manifest matches exactly.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import re
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

CONTENT = {"maps", "progs", "sound", "music", "gfx", "models", "textures", "sprites", "env", "demos", "demo",
           "video", "glsl", "particles", "skins", "cdtracks", "locs"}
AUX = {"doc", "docs", "documentation", "source", "src", "extras", "screenshots", "images"}


def parse_pak(raw: bytes) -> list[tuple[str, bytes]]:
    assert raw[:4] == b"PACK", "not a PAK"
    off, length = struct.unpack("<II", raw[4:12])
    out = []
    for i in range(length // 64):
        rec = raw[off + i * 64: off + i * 64 + 64]
        pos, size = struct.unpack("<II", rec[56:])
        out.append((rec[:56].split(b"\0")[0].decode(), raw[pos:pos + size]))
    return out


def zip_bytes(zf: zipfile.ZipFile, zpath: Path, info: zipfile.ZipInfo) -> bytes:
    try:
        return zf.read(info)
    except NotImplementedError:
        return subprocess.run(["unzip", "-p", str(zpath), re.sub(r"([\\*?\[\]])", r"\\\1", info.filename)], check=True, capture_output=True).stdout


def expected_manifest(zpath: Path) -> tuple[dict[str, bytes], dict]:
    with zipfile.ZipFile(zpath) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        names = [i.filename for i in infos]
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        other = [t for t in tops if t.lower() not in CONTENT | AUX]
        wrapper = other[0] + "/" if len(other) == 1 and not any(t.lower() in CONTENT for t in tops) and not any(
            re.fullmatch(r"pak\d+\.pak|progs\.dat", n) for n in names if "/" not in n) else ""
        files = {i.filename[len(wrapper):] if wrapper and i.filename.startswith(wrapper) else i.filename: i for i in infos}
        paks = {int(m.group(1)): n for n in files if (m := re.fullmatch(r"pak(0|[1-9]\d*)\.pak", n))}
        result: dict[str, bytes] = {}
        source: dict[str, str] = {}
        for n, i in files.items():
            if n not in paks.values():
                result[n], source[n] = zip_bytes(zf, zpath, i), "loose"
        overridden = 0
        for idx in sorted(paks):
            seen = set()
            for name, data in parse_pak(zip_bytes(zf, zpath, files[paks[idx]])):
                if name in seen:
                    continue
                seen.add(name)
                overridden += name in result
                result[name], source[name] = data, f"pak{idx}"
    return result, {"wrapper": wrapper.rstrip("/") or None, "paks": sorted(paks), "overridden": overridden,
                    "loose_files": sum(1 for s in source.values() if s == "loose")}


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path: str, timeout=600) -> bytes:
        return self.opener.open(self.base + path, timeout=timeout).read()

    def csrf(self, path: str) -> str:
        return re.search(r'name="csrf_token" value="([^"]+)"', self.get(path).decode()).group(1)

    def post(self, path: str, fields: dict, files: dict | None = None, csrf_from: str | None = None) -> str:
        fields = dict(fields, csrf_token=self.csrf(csrf_from or path))
        boundary = uuid.uuid4().hex
        body = bytearray()
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (fname, data) in (files or {}).items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.base + path, bytes(body), {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self.opener.open(req, timeout=1800).read().decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--zip", required=True, type=Path)
    ap.add_argument("--id", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    c = Client(a.base)
    if c.opener.open(a.base + "/setup").geturl().endswith("/setup"):
        c.post("/setup", {"username": a.user, "password": a.password})
    c.post("/login", {"username": a.user, "password": a.password})
    page = c.post("/addons/new", {"title": a.id, "addon_id": a.id, "gamedir": a.id, "author": "acceptance", "date": "2026/10/16", "description": "acceptance run"},
                  {"package": (a.zip.name, a.zip.read_bytes())}, csrf_from="/addons/new")
    flash = re.search(r'flash (?:success|error)">([^<]+)', page)
    print("upload:", flash.group(1) if flash else "no flash")
    dash = c.get("/dashboard").decode()
    m = re.search(r'/addons/(\d+)"[^<]*</a>', dash)
    row = re.findall(r'<tr>.*?</tr>', dash, re.S)
    addon_pk = next((re.search(r'/addons/(\d+)', r).group(1) for r in row if f">{a.id}<" in r), None)
    if not addon_pk:
        print("FAIL: add-on not created (draft missing)")
        return 2
    c.post(f"/addons/{addon_pk}/publish", {}, csrf_from="/dashboard")
    catalog = json.loads(c.get("/content.json"))
    entry = next((e for e in catalog["addons"] if e["gamedir"] == a.id), None)
    if not entry:
        print("FAIL: not in catalog")
        return 2
    data = c.get("/" + entry["download"])
    a.out.mkdir(parents=True, exist_ok=True)
    artifact = a.out / f"{a.id}-acceptance.pak"
    artifact.write_bytes(data)

    got_list = parse_pak(data)
    got = dict(got_list)
    exp, meta = expected_manifest(a.zip)
    missing = sorted(set(exp) - set(got))
    unexpected = sorted(set(got) - set(exp))
    mismatched = sorted(n for n in set(exp) & set(got) if hashlib.sha256(exp[n]).digest() != hashlib.sha256(got[n]).digest())
    report = {
        "zip_sha256": hashlib.sha256(a.zip.read_bytes()).hexdigest(), "catalog_entry": entry,
        "catalog_size_matches_download": entry["size"] == len(data),
        "artifact": str(artifact), "artifact_sha256": hashlib.sha256(data).hexdigest(), "artifact_size": len(data),
        "entries": len(got_list), "duplicate_entries": len(got_list) - len(got), "expected_entries": len(exp),
        "missing": missing, "unexpected": unexpected, "mismatched": mismatched, **meta,
    }
    print(json.dumps(report, indent=2))
    ok = not (missing or unexpected or mismatched) and report["duplicate_entries"] == 0 and report["catalog_size_matches_download"]
    print("ACCEPTANCE", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

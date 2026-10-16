#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import zipfile
import tempfile
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
VALID_GAMEDIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
VALID_DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _reject_value(label: str, value: str) -> None:
    if value is None:
        raise ValueError(f"Missing {label}")
    if any(sep in value for sep in ("/", "\\")):
        raise ValueError(f"{label} cannot contain path separators: {value!r}")
    if value in (".", "..") or value.startswith("../") or value.startswith("./"):
        raise ValueError(f"{label} contains invalid path syntax: {value!r}")


def _validate_id(identifier: str) -> str:
    if not identifier or not VALID_ID_RE.fullmatch(identifier):
        raise ValueError(f"Invalid addon id {identifier!r}; use letters, numbers, '-' or '_' only")
    return identifier


def _validate_gamedir(name: str) -> str:
    if not name or not VALID_GAMEDIR_RE.fullmatch(name):
        raise ValueError(f"Invalid gamedir {name!r}; use letters, numbers, '-' or '_' only")
    return name


def _validate_date(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not VALID_DATE_RE.fullmatch(value):
        raise ValueError(f"Invalid date {value!r}; expected YYYY/MM/DD")
    return value


def _normalize_download(download: str) -> str:
    if not download:
        raise ValueError("download path is required")
    if download.startswith("/") or download.startswith("\\"):
        raise ValueError(f"download path must be relative, not absolute: {download!r}")
    if ".." in download.split("/"):
        raise ValueError(f"download path contains traversal: {download!r}")
    return download.replace("\\", "/")


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_pak_header(path: Path) -> Dict[str, Any]:
    if path.suffix.lower() != ".pak":
        raise ValueError(f"Expected .pak file, got {path.name!r}")
    with open(path, "rb") as fh:
        sig = fh.read(4)
        if sig != b"PACK":
            raise ValueError(f"{path.name!r} is not a valid Quake PAK file")
        dir_offset = int.from_bytes(fh.read(4), "little")
        dir_size = int.from_bytes(fh.read(4), "little")
    if dir_offset < 12 or dir_size < 0:
        raise ValueError(f"{path.name!r} has an invalid PAK directory header")
    return {"size": path.stat().st_size, "sha256": _compute_sha256(path), "dir_offset": dir_offset, "dir_size": dir_size}


def validate_pak_paths(path: Path) -> None:
    with open(path, "rb") as fh:
        sig = fh.read(4)
        if sig != b"PACK":
            raise ValueError(f"{path.name!r} is not a valid Quake PAK file")
        dir_offset = int.from_bytes(fh.read(4), "little")
        dir_size = int.from_bytes(fh.read(4), "little")
        if dir_offset < 12 or dir_size < 0:
            raise ValueError(f"{path.name!r} has an invalid PAK structure")
        fh.seek(dir_offset)
        data = fh.read(dir_size)
    if not data:
        # Empty PAKs are not useful as a real add-on, but the protocol does not forbid them.
        # We reject them to avoid broken installs.
        raise ValueError(f"{path.name!r} has an empty directory table")
    # Walk the directory table as a sequence of 64-byte filename records; stop gracefully on trailing zeros.
    pos = 0
    entries = 0
    while pos + 64 <= len(data):
        chunk = data[pos:pos + 64]
        name = chunk[:56].split(b"\0", 1)[0].decode("utf-8", errors="strict")
        if not name:
            break
        if name.startswith("/") or name.startswith("\\"):
            raise ValueError(f"Unsafe archive path in PAK: {name!r}")
        if "/../" in name or name.startswith("../") or name.startswith("..\\"):
            raise ValueError(f"Archive path traversal detected in PAK: {name!r}")
        if ".." in name.split("/") or ".." in name.split("\\"):
            raise ValueError(f"Archive path traversal detected in PAK: {name!r}")
        entries += 1
        pos += 64
        # The directory table format stores a 4-byte offset and 4-byte size after the filename; we ignore them.
        if pos + 8 > len(data):
            break
        pos += 8
    if entries == 0:
        raise ValueError(f"{path.name!r} contains no archive entries")


def detect_package_format(path: Path) -> str:
    with open(path, "rb") as fh:
        sig = fh.read(4)
    if sig == b"PACK":
        return "pak"
    if sig == b"PK\x03\x04":
        return "zip"
    return "unknown"


def _validate_zip_member_name(name: str) -> str:
    name = name.replace("\\", "/")
    if not name or name.endswith("/"):
        raise ValueError(f"Invalid ZIP entry name: {name!r}")
    if name.startswith("/") or name.startswith("../") or "/../" in name or name == ".." or "/.." in name.split("/"):
        raise ValueError(f"Unsafe ZIP entry path: {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"Unsafe ZIP entry path: {name!r}")
    return name


def _read_zip_entries_with_unzip(source: Path) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    print(f"[zip-import] using system unzip fallback for {source.name}", flush=True)
    with tempfile.TemporaryDirectory(prefix="quake-zip-", dir=str(source.parent)) as tmpdir:
        proc = subprocess.run(["unzip", "-qq", "-o", str(source), "-d", tmpdir], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise ValueError(f"unzip failed for {source.name!r}: {proc.stderr.strip() or proc.stdout.strip() or 'unknown error'}")
        root = Path(tmpdir)
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(root).as_posix()
            _validate_zip_member_name(rel)
            entries.append((rel, path.read_bytes()))
    print(f"[zip-import] unzip fallback extracted {len(entries)} files from {source.name}", flush=True)
    return entries


def repack_zip_to_pak(source: Path, target: Path, max_files: int = 5000, max_uncompressed_size: int = 2 * 1024 * 1024 * 1024) -> Dict[str, Any]:
    source = Path(source)
    target = Path(target)
    if zipfile.is_zipfile(source) is False:
        raise ValueError(f"{source.name!r} is not a valid ZIP archive")

    entries: list[tuple[str, bytes]] = []
    total_uncompressed = 0
    with zipfile.ZipFile(source, "r") as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if not infos:
            raise ValueError(f"{source.name!r} does not contain any files")
        if len(infos) > max_files:
            raise ValueError(f"ZIP has too many files ({len(infos)} > {max_files})")
        print(f"[zip-import] python zipfile saw {len(infos)} files in {source.name}", flush=True)
        for info in infos:
            if info.flag_bits & 0x1:
                raise ValueError(f"Encrypted ZIP entries are not supported: {info.filename!r}")
            name = _validate_zip_member_name(info.filename)
            try:
                with zf.open(info, "r") as fh:
                    data = fh.read()
            except NotImplementedError as exc:
                print(f"[zip-import] python zipfile could not decode {info.filename}; falling back to unzip", flush=True)
                entries = _read_zip_entries_with_unzip(source)
                return repack_zip_to_pak_from_entries(source, target, entries, max_files, max_uncompressed_size)
            except RuntimeError as exc:
                raise ValueError(f"Unable to read ZIP entry {info.filename!r}: {exc}") from exc
            total_uncompressed += len(data)
            if total_uncompressed > max_uncompressed_size:
                raise ValueError(f"ZIP uncompressed data exceeds limit ({total_uncompressed} > {max_uncompressed_size})")
            entries.append((name, data))

    dir_offset = 12
    file_blobs = bytearray()
    dir_records = bytearray()
    for name, data in entries:
        file_offset = dir_offset + len(file_blobs)
        file_blobs.extend(data)
        name_bytes = name.encode("utf-8")
        if len(name_bytes) > 56:
            raise ValueError(f"Entry name too long for PAK: {name!r}")
        dir_records.extend(name_bytes.ljust(56, b"\0"))
        dir_records.extend(struct.pack("<II", file_offset, len(data)))

    header = b"PACK" + struct.pack("<II", dir_offset + len(file_blobs), len(dir_records))
    atomic_write(target, header + file_blobs + dir_records)
    print(f"[zip-import] repacked {len(entries)} files from {source.name} into {target.name} ({target.stat().st_size} bytes)", flush=True)
    return {"entries": len(entries), "size": target.stat().st_size, "sha256": _compute_sha256(target)}


def repack_zip_to_pak_from_entries(source: Path, target: Path, entries: list[tuple[str, bytes]], max_files: int, max_uncompressed_size: int) -> Dict[str, Any]:
    if len(entries) > max_files:
        raise ValueError(f"ZIP has too many files ({len(entries)} > {max_files})")
    total_uncompressed = 0
    cleaned: list[tuple[str, bytes]] = []
    for name, data in entries:
        name = _validate_zip_member_name(name)
        total_uncompressed += len(data)
        if total_uncompressed > max_uncompressed_size:
            raise ValueError(f"ZIP uncompressed data exceeds limit ({total_uncompressed} > {max_uncompressed_size})")
        cleaned.append((name, data))
    dir_offset = 12
    file_blobs = bytearray()
    dir_records = bytearray()
    for name, data in cleaned:
        file_offset = dir_offset + len(file_blobs)
        file_blobs.extend(data)
        name_bytes = name.encode("utf-8")
        if len(name_bytes) > 56:
            raise ValueError(f"Entry name too long for PAK: {name!r}")
        dir_records.extend(name_bytes.ljust(56, b"\0"))
        dir_records.extend(struct.pack("<II", file_offset, len(data)))
    header = b"PACK" + struct.pack("<II", dir_offset + len(file_blobs), len(dir_records))
    atomic_write(target, header + file_blobs + dir_records)
    print(f"[zip-import] repacked {len(cleaned)} files from {source.name} via unzip into {target.name} ({target.stat().st_size} bytes)", flush=True)
    return {"entries": len(cleaned), "size": target.stat().st_size, "sha256": _compute_sha256(target)}


def validate_package(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Package file not found: {path}")
    info = parse_pak_header(path)
    validate_pak_paths(path)
    return {
        "size": info["size"],
        "sha256": info["sha256"],
        "mtime": int(path.stat().st_mtime),
        "filename": path.name,
    }


def _read_metadata_file(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Metadata file must be a JSON object: {path}")
    return data


def load_addon_metadata(path: Path) -> Dict[str, Any]:
    data = _read_metadata_file(path)
    identifier = _validate_id(str(data.get("id") or path.stem))
    gamedir = _validate_gamedir(str(data.get("gamedir") or identifier))
    title = str(data.get("title") or data.get("name") or identifier)
    if not title:
        raise ValueError(f"Missing title for addon {identifier!r}")
    author = str(data.get("author") or "Unknown")
    date = _validate_date(str(data.get("date") or ""))
    description = data.get("description") or ""
    if description is not None and not isinstance(description, str):
        raise ValueError(f"description for {identifier!r} must be a string")

    package_name = str(data.get("package") or f"{identifier}.pak")
    package_dir = path.parent.parent / "packages"
    full_path = (package_dir / package_name).resolve()
    if not full_path.exists():
        raise ValueError(f"Package file does not exist for {identifier!r}: {full_path}")
    validate_pak_paths(full_path)
    size = full_path.stat().st_size
    return {
        "id": identifier,
        "title": title,
        "gamedir": gamedir,
        "download": f"addons/{identifier}.pak",
        "package": str(full_path.relative_to(path.parent.parent)),
        "author": author,
        "date": date,
        "size": size,
        "description": description,
    }


def collect_metadata(metadata_dir: Path) -> List[Dict[str, Any]]:
    metadata_dir = Path(metadata_dir)
    if not metadata_dir.exists():
        return []
    addons: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_dirs: set[str] = set()
    for meta_path in sorted(metadata_dir.glob("*.json")):
        addon = load_addon_metadata(meta_path)
        if addon["id"] in seen_ids:
            raise ValueError(f"Duplicate addon id: {addon['id']}")
        if addon["gamedir"] in seen_dirs:
            raise ValueError(f"Duplicate gamedir for addon {addon['id']!r}: {addon['gamedir']!r}")
        seen_ids.add(addon["id"])
        seen_dirs.add(addon["gamedir"])
        addons.append(addon)
    return addons


def build_catalog(metadata_dir: Path, public_dir: Path, base_url: str) -> Dict[str, Any]:
    metadata_dir = Path(metadata_dir)
    public_dir = Path(public_dir)
    addons = collect_metadata(metadata_dir)
    manifest = {"addons": []}
    public_dir.mkdir(parents=True, exist_ok=True)
    addons_dir = public_dir / "addons"
    addons_dir.mkdir(parents=True, exist_ok=True)
    for addon in addons:
        source_package = metadata_dir.parent / "packages" / f"{addon['id']}.pak"
        if not source_package.exists():
            raise ValueError(f"Missing package for {addon['id']!r}: {source_package}")
        target = addons_dir / f"{addon['id']}.pak"
        if source_package.resolve() != target.resolve():
            atomic_write(target, source_package.read_bytes())
        entry = {
            "id": addon["id"],
            "download": f"addons/{addon['id']}.pak",
            "gamedir": addon["gamedir"],
            "name": addon["title"],
            "author": addon["author"],
            "date": addon["date"],
            "size": addon["size"],
        }
        if addon["description"]:
            entry["description"] = {"en": addon["description"]}
        manifest["addons"].append(entry)
    catalog_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write(public_dir / "content.json", catalog_bytes)
    return manifest


def validate_catalog(catalog_path: Path) -> Dict[str, Any]:
    catalog_path = Path(catalog_path)
    if not catalog_path.exists():
        raise FileNotFoundError(f"Catalog file not found: {catalog_path}")
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "addons" not in data or not isinstance(data["addons"], list):
        raise ValueError("Catalog JSON must contain an 'addons' array")
    seen = set()
    for index, entry in enumerate(data["addons"]):
        if not isinstance(entry, dict):
            raise ValueError(f"Catalog entry #{index} is not an object")
        download = entry.get("download")
        gamedir = entry.get("gamedir")
        if not download or not gamedir:
            raise ValueError(f"Catalog entry #{index} is missing required 'download' or 'gamedir'")
        if ".." in download.split("/"):
            raise ValueError(f"Catalog entry #{index} has unsafe download path: {download!r}")
        _validate_gamedir(str(gamedir))
        file_path = catalog_path.parent / download
        if not file_path.exists():
            raise FileNotFoundError(f"Package file not found for {download!r}: {file_path}")
        addon_id = str(entry.get("id") or entry.get("name") or download)
        if addon_id in seen:
            raise ValueError(f"Duplicate addon id in catalog: {addon_id!r}")
        seen.add(addon_id)
    return data


def compute_manifest(base_url: str, metadata_dir: Path, public_dir: Path) -> Dict[str, Any]:
    base_url = str(base_url).rstrip("/")
    addons = collect_metadata(metadata_dir)
    manifest = {"addons": []}
    for addon in addons:
        entry = {
            "download": addon["download"],
            "gamedir": addon["gamedir"],
            "name": addon["title"],
            "author": addon["author"],
            "date": addon["date"],
            "size": addon["size"],
        }
        if addon["description"]:
            entry["description"] = {"en": addon["description"]}
        manifest["addons"].append(entry)
    return manifest

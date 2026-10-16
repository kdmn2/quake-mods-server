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


# --- PAK directory parsing (Ironwail: common.c COM_LoadPackFile) -------------
MAX_PAK_ENTRIES = 2048          # MAX_FILES_IN_PACK: engine calls Sys_Error above this
MAX_PAK_NAME_BYTES = 55         # dpackfile_t.name is char[56]; q_strlcpy keeps 55 + NUL
MAX_PAK_BYTES = 2**31 - 1       # filepos/filelen are signed 32-bit in the engine
CONTENT_DIRS = frozenset({
    "maps", "progs", "sound", "music", "gfx", "models", "textures", "sprites", "env",
    "demos", "demo", "video", "glsl", "particles", "skins", "cdtracks", "locs",
})
AUX_DIRS = frozenset({"doc", "docs", "documentation", "source", "src", "extras", "screenshots", "images"})
NUMBERED_PAK_RE = re.compile(r"^pak(0|[1-9][0-9]*)\.pak$", re.IGNORECASE)
_COPY_CHUNK = 1024 * 1024


def _check_pak_entry_name(name: str, context: str = "PAK") -> None:
    if not name:
        raise ValueError(f"{context} contains an entry with an empty name")
    if name.startswith(("/", "\\")) or "\\" in name:
        raise ValueError(f"Unsafe archive path in {context}: {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"Archive path traversal detected in {context}: {name!r}")
    if len(name.encode("utf-8")) > MAX_PAK_NAME_BYTES:
        raise ValueError(f"Entry name too long for PAK ({len(name.encode('utf-8'))} > {MAX_PAK_NAME_BYTES} bytes): {name!r}")


def read_pak_directory(path: Path) -> list[dict[str, Any]]:
    """Parse and validate a PAK directory. Returns entries in directory order (duplicates preserved)."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        header = fh.read(12)
        if len(header) < 12 or header[:4] != b"PACK":
            raise ValueError(f"{path.name!r} is not a valid Quake PAK file")
        dir_offset, dir_size = struct.unpack("<II", header[4:12])
        if dir_offset < 12 or dir_offset + dir_size > size or dir_size % 64 != 0:
            raise ValueError(f"{path.name!r} has an invalid PAK structure")
        count = dir_size // 64
        if count == 0:
            raise ValueError(f"{path.name!r} has an empty directory table")
        if count > MAX_PAK_ENTRIES:
            raise ValueError(f"{path.name!r} has {count} entries; Ironwail refuses PAKs with more than {MAX_PAK_ENTRIES}")
        fh.seek(dir_offset)
        table = fh.read(dir_size)
    entries: list[dict[str, Any]] = []
    for idx in range(count):
        rec = table[idx * 64:(idx + 1) * 64]
        raw = rec[:56]
        if b"\0" not in raw:
            raise ValueError(f"{path.name!r} entry {idx} has an unterminated name")
        try:
            name = raw.split(b"\0", 1)[0].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path.name!r} entry {idx} has a non-UTF-8 name") from exc
        _check_pak_entry_name(name, f"PAK {path.name!r}")
        offset, length = struct.unpack("<II", rec[56:64])
        if offset < 12 or offset + length > size:
            raise ValueError(f"PAK entry out of bounds: {name!r}")
        entries.append({"path": name, "offset": offset, "size": length})
    return entries


def validate_pak_paths(path: Path) -> None:
    read_pak_directory(Path(path))


def detect_package_format(path: Path) -> str:
    with open(path, "rb") as fh:
        sig = fh.read(4)
    if sig == b"PACK":
        return "pak"
    if sig == b"PK\x03\x04":
        return "zip"
    return "unknown"


def _normalize_zip_member_path(name: str) -> str:
    raw = name.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or "\0" in raw:
        raise ValueError(f"Unsafe ZIP entry path: {name!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe ZIP entry path: {name!r}")
    return "/".join(parts)


def classify_release_layout(paths: Iterable[str]) -> dict[str, Any]:
    """Infer the release root from member paths only. File inclusion is decided elsewhere.

    Top-level directories are content (maps/, sound/, ...), auxiliary (docs/, src/, ...) or other.
    rooted    - no "other" directory; content, auxiliary dirs and root-level files are kept as they are.
    wrapper   - exactly one "other" directory and no root-level game files/content dirs; it is stripped.
    ambiguous - several "other" directories, or one "other" directory mixed with game content beside it.
    """
    paths = list(paths)
    root_files = sorted(p for p in paths if "/" not in p)
    top_dirs = sorted({p.split("/", 1)[0] for p in paths if "/" in p})
    other = [d for d in top_dirs if d.lower() not in CONTENT_DIRS and d.lower() not in AUX_DIRS]
    has_game_root = any(d.lower() in CONTENT_DIRS for d in top_dirs) or any(
        NUMBERED_PAK_RE.match(f) or f.lower() == "progs.dat" for f in root_files)
    result: dict[str, Any] = {"root_mapping": "ambiguous", "wrapper_dirs": [], "has_wrapper": False,
                              "root_level_files": root_files, "top_level_dirs": top_dirs}
    if not paths:
        return result
    if not other:
        result["root_mapping"] = "rooted"
    elif len(other) == 1 and not has_game_root:
        result.update(root_mapping="wrapper", wrapper_dirs=other, has_wrapper=True)
    return result


def _strip_wrapper(layout: dict[str, Any], members: list[tuple[zipfile.ZipInfo, str]]) -> list[tuple[zipfile.ZipInfo, str]]:
    prefix = layout["wrapper_dirs"][0] + "/" if layout["has_wrapper"] else ""
    result: list[tuple[zipfile.ZipInfo, str]] = []
    seen: dict[str, str] = {}
    for info, name in members:
        new = name[len(prefix):] if prefix and name.startswith(prefix) else name
        if new in seen:
            raise ValueError(f"Paths collide after removing wrapper directory {prefix!r}: {seen[new]!r} and {name!r}")
        seen[new] = name
        result.append((info, new))
    return result


def _zip_release_members(zf: zipfile.ZipFile, max_files: int, max_total: int) -> list[tuple[zipfile.ZipInfo, str]]:
    members: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = _normalize_zip_member_path(info.filename)
        if name.split("/", 1)[0] == "__MACOSX":
            continue
        if info.flag_bits & 0x1:
            raise ValueError(f"Encrypted ZIP entries are not supported: {info.filename!r}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"ZIP symlink entries are not supported: {info.filename!r}")
        if name in seen:
            raise ValueError(f"ZIP contains duplicate path: {name!r}")
        seen.add(name)
        total += info.file_size
        members.append((info, name))
        if len(members) > max_files:
            raise ValueError(f"ZIP has too many files ({len(members)} > {max_files})")
        if total > max_total:
            raise ValueError(f"ZIP uncompressed data exceeds limit ({total} > {max_total})")
    return members


def inspect_zip_release(source: Path, max_files: int = 5000, max_uncompressed_size: int = 2 * 1024 * 1024 * 1024) -> dict[str, Any]:
    """Describe a release ZIP from its central directory (no decompression, so any method is inspectable)."""
    source = Path(source)
    if not zipfile.is_zipfile(source):
        raise ValueError(f"{source.name!r} is not a valid ZIP archive")
    with zipfile.ZipFile(source, "r") as zf:
        members = _zip_release_members(zf, max_files, max_uncompressed_size)
    layout = classify_release_layout(name for _, name in members)
    effective = _strip_wrapper(layout, members) if layout["root_mapping"] != "ambiguous" else [(i, n) for i, n in members]
    layout["entry_count"] = len(members)
    layout["embedded_paks"] = [{"path": n, "size": i.file_size} for i, n in effective if NUMBERED_PAK_RE.match(n)]
    layout["loose"] = [{"path": n, "size": i.file_size} for i, n in effective if not NUMBERED_PAK_RE.match(n)]
    layout["root_level_files"] = sorted(n for _, n in effective if "/" not in n)
    return layout


def _copy_limited(reader, out, expected: int, label: str) -> str:
    digest = hashlib.sha256()
    written = 0
    while True:
        chunk = reader.read(_COPY_CHUNK)
        if not chunk:
            break
        written += len(chunk)
        if written > expected:
            raise ValueError(f"ZIP entry {label!r} is larger than its declared size")
        digest.update(chunk)
        out.write(chunk)
    if written != expected:
        raise ValueError(f"ZIP entry {label!r} is truncated ({written} of {expected} bytes)")
    return digest.hexdigest()


def _extract_member(zf: zipfile.ZipFile, source: Path, info: zipfile.ZipInfo, dest: Path) -> str:
    """Stream one member to dest. Falls back to system unzip for methods Python cannot decode (e.g. Deflate64)."""
    try:
        with zf.open(info, "r") as reader, open(dest, "wb") as out:
            return _copy_limited(reader, out, info.file_size, info.filename)
    except NotImplementedError:
        pass
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f"Unable to read ZIP entry {info.filename!r}: {exc}") from exc
    escaped = re.sub(r"([\\*?\[\]])", r"\\\1", info.filename)
    try:
        proc = subprocess.Popen(["unzip", "-p", str(source), escaped], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise ValueError(f"ZIP entry {info.filename!r} uses a compression method Python cannot read and 'unzip' is not installed") from exc
    try:
        with open(dest, "wb") as out:
            digest = _copy_limited(proc.stdout, out, info.file_size, info.filename)
    except ValueError:
        proc.kill()
        raise
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0:
        raise ValueError(f"unzip failed for {info.filename!r}: {err or f'exit code {rc}'}")
    return digest


def build_single_pak_from_zip(source: Path, target: Path, max_files: int = 5000, max_uncompressed_size: int = 2 * 1024 * 1024 * 1024) -> dict[str, Any]:
    """Convert a release ZIP into one PAK holding the effective game assets.

    Precedence mirrors Ironwail's search path (common.c: COM_AddGameDirectory/COM_FindFile): the game
    directory (loose files) is searched LAST, pak0..pakN are pushed on the front in order, so the
    highest-numbered PAK wins, any PAK beats a loose file, and within one PAK the first entry wins.
    Nothing depends on ZIP member order.
    """
    source = Path(source)
    target = Path(target)
    if not zipfile.is_zipfile(source):
        raise ValueError(f"{source.name!r} is not a valid ZIP archive")
    with tempfile.TemporaryDirectory(prefix="quake-zip-", dir=str(source.parent)) as tmp, zipfile.ZipFile(source, "r") as zf:
        tmpdir = Path(tmp)
        members = _zip_release_members(zf, max_files, max_uncompressed_size)
        if not members:
            raise ValueError(f"{source.name!r} does not contain any representable files")
        layout = classify_release_layout(name for _, name in members)
        if layout["root_mapping"] == "ambiguous":
            raise ValueError("ZIP release has ambiguous root mapping; provide a single wrapper directory or already-rooted game content")
        effective = _strip_wrapper(layout, members)
        eff_names = [n for _, n in effective]
        inner_dirs = {n.split("/", 1)[0] for n in eff_names if "/" in n}
        if layout["has_wrapper"] and len(inner_dirs) == 1 and not any("/" not in n for n in eff_names) \
                and next(iter(inner_dirs)).lower() not in CONTENT_DIRS | AUX_DIRS:
            raise ValueError(f"ZIP release has nested wrapper directories ({layout['wrapper_dirs'][0]}/{next(iter(inner_dirs))}/); ambiguous root mapping")

        stored: dict[str, Path] = {}
        total = 0
        for idx, (info, name) in enumerate(effective):
            total += info.file_size
            dest = tmpdir / f"member-{idx}.bin"
            _extract_member(zf, source, info, dest)
            stored[name] = dest

        pak_files: dict[int, Path] = {}
        loose: dict[str, Path] = {}
        for name, path in stored.items():
            m = NUMBERED_PAK_RE.match(name)
            if m:
                pak_files[int(m.group(1))] = path
            else:
                loose[name] = path
        if pak_files and sorted(pak_files) != list(range(len(pak_files))):
            raise ValueError(f"Numbered PAKs must be contiguous from pak0.pak (found {sorted(pak_files)}); Ironwail stops loading at the first gap")

        # winner[path] = (source label, file path, offset, length); ascending priority so later assignments override
        winner: dict[str, tuple[str, Path, int, int]] = {}
        overrides: list[dict[str, str]] = []

        def assign(path_name: str, candidate: tuple[str, Path, int, int]) -> None:
            if path_name in winner:
                overrides.append({"path": path_name, "overridden": winner[path_name][0], "by": candidate[0]})
            winner[path_name] = candidate

        for name, path in loose.items():
            assign(name, (f"loose:{name}", path, 0, path.stat().st_size))
        for index in sorted(pak_files):
            label = f"pak{index}.pak"
            seen_here: set[str] = set()
            for entry in read_pak_directory(pak_files[index]):
                if entry["path"] in seen_here:
                    continue  # engine scans linearly and takes the first match within a PAK
                seen_here.add(entry["path"])
                assign(entry["path"], (label, pak_files[index], entry["offset"], entry["size"]))

        if not winner:
            raise ValueError(f"{source.name!r} does not contain any representable files")
        folded: dict[str, str] = {}
        for name in winner:
            _check_pak_entry_name(name, "release")
            if name.lower() in folded:
                raise ValueError(f"Paths differ only by letter case: {folded[name.lower()]!r} and {name!r}")
            folded[name.lower()] = name
        if len(winner) > MAX_PAK_ENTRIES:
            raise ValueError(f"Release resolves to {len(winner)} files; a single PAK may hold at most {MAX_PAK_ENTRIES} (Ironwail limit)")

        order = sorted(winner)
        manifest: list[dict[str, Any]] = []
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = target.with_name(target.name + ".tmp")
        try:
            with open(tmp_out, "wb") as out:
                out.write(b"PACK" + b"\0" * 8)
                records = bytearray()
                for name in order:
                    label, path, offset, length = winner[name]
                    file_pos = out.tell()
                    digest = hashlib.sha256()
                    with open(path, "rb") as fh:
                        fh.seek(offset)
                        remaining = length
                        while remaining:
                            chunk = fh.read(min(_COPY_CHUNK, remaining))
                            if not chunk:
                                raise ValueError(f"Source for {name!r} is truncated")
                            digest.update(chunk)
                            out.write(chunk)
                            remaining -= len(chunk)
                    if out.tell() > MAX_PAK_BYTES:
                        raise ValueError(f"Resulting PAK exceeds {MAX_PAK_BYTES} bytes (32-bit PAK offsets)")
                    records += name.encode("utf-8").ljust(56, b"\0") + struct.pack("<II", file_pos, length)
                    manifest.append({"path": name, "size": length, "sha256": digest.hexdigest(), "source": label})
                dir_offset = out.tell()
                out.write(records)
                if out.tell() > MAX_PAK_BYTES:
                    raise ValueError(f"Resulting PAK exceeds {MAX_PAK_BYTES} bytes (32-bit PAK offsets)")
                out.seek(4)
                out.write(struct.pack("<II", dir_offset, len(records)))
            os.replace(tmp_out, target)
        finally:
            tmp_out.unlink(missing_ok=True)
    return {
        "entries": len(order), "size": target.stat().st_size, "sha256": _compute_sha256(target), "paths": order,
        "manifest": manifest, "overrides": overrides, "root_mapping": layout["root_mapping"],
        "wrapper": layout["wrapper_dirs"][0] if layout["has_wrapper"] else None,
        "embedded_paks": [f"pak{i}.pak" for i in sorted(pak_files)], "loose_files": len(loose),
    }


repack_zip_to_pak = build_single_pak_from_zip


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

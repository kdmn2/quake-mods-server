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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

from tools.convert_options import PRESETS, ConvertOptions, first_match

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


def _compute_sha256(path: Path, progress: Callable[[str, int, int], None] | None = None, stage: str = "Checking package") -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(stage, done, total)
    return digest.hexdigest()


def parse_pak_header(path: Path, sha256: str | None = None) -> Dict[str, Any]:
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
    return {"size": path.stat().st_size, "sha256": sha256 or _compute_sha256(path), "dir_offset": dir_offset, "dir_size": dir_size}


# --- PAK directory parsing (Ironwail: common.c COM_LoadPackFile) -------------
MAX_PAK_ENTRIES = 2048          # MAX_FILES_IN_PACK: engine calls Sys_Error above this
MAX_PAK_NAME_BYTES = 55         # dpackfile_t.name is char[56]; q_strlcpy keeps 55 + NUL
MAX_PAK_BYTES = 2**31 - 1       # filepos/filelen are signed 32-bit in the engine
CONTENT_DIRS = frozenset({
    "maps", "progs", "sound", "music", "gfx", "models", "textures", "sprites", "env",
    "demos", "demo", "video", "glsl", "particles", "skins", "cdtracks", "locs", "localization",
})
AUX_DIRS = frozenset({"doc", "docs", "documentation", "source", "sources", "src", "mapsrc", "extras", "screenshots", "images"})
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


def _copy_limited(reader, out, expected: int, label: str, on_bytes=None) -> str:
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
        if on_bytes:
            on_bytes(len(chunk))
    if written != expected:
        raise ValueError(f"ZIP entry {label!r} is truncated ({written} of {expected} bytes)")
    return digest.hexdigest()


def _extract_member(zf: zipfile.ZipFile, source: Path, info: zipfile.ZipInfo, dest: Path, on_bytes=None) -> str:
    """Stream one member to dest. Falls back to system unzip for methods Python cannot decode (e.g. Deflate64)."""
    try:
        with zf.open(info, "r") as reader, open(dest, "wb") as out:
            return _copy_limited(reader, out, info.file_size, info.filename, on_bytes)
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
            digest = _copy_limited(proc.stdout, out, info.file_size, info.filename, on_bytes)
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


Progress = Callable[[str, int, int], None]
EXCLUDED_SAMPLE = 100


@dataclass
class _Win:
    """The copy of a path that will end up in the PAK. Nothing is extracted until it has won."""
    label: str
    size: int
    kind: str                                  # "loose" | "pak" | "file"
    info: zipfile.ZipInfo | None = None        # kind == "loose": member to extract
    file: Path | None = None                   # kind == "pak"/"file": where the bytes are
    offset: int = 0


@dataclass
class _Resolved:
    layout: dict[str, Any]
    effective_count: int = 0
    loose_count: int = 0
    paks: dict[int, Path] = field(default_factory=dict)
    pak_sizes: dict[int, int] = field(default_factory=dict)
    pak_entry_counts: dict[int, int] = field(default_factory=dict)
    winners: dict[str, _Win] = field(default_factory=dict)
    overrides: list[dict[str, str]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[tuple[str, int]] = field(default_factory=list)      # every effective path before exclusion
    blockers: list[str] = field(default_factory=list)


def _block(res: _Resolved, strict: bool, message: str) -> None:
    if strict:
        raise ValueError(message)
    if message not in res.blockers and len(res.blockers) < 20:
        res.blockers.append(message)


def _summarise_excluded(excluded: list[dict[str, Any]]) -> dict[str, Any]:
    by_rule: dict[str, int] = {}
    for e in excluded:
        by_rule[e["reason"]] = by_rule.get(e["reason"], 0) + 1
    return {"total": len(excluded), "bytes": sum(e.get("size", 0) for e in excluded), "by_rule": by_rule,
            "sample": [{"path": e["path"], "reason": e["reason"]} for e in excluded[:EXCLUDED_SAMPLE]]}


def _resolve_release(source: Path, zf: zipfile.ZipFile, tmpdir: Path, options: ConvertOptions, strict: bool,
                     max_files: int, max_total: int, progress: Progress | None) -> _Resolved:
    """Decide what the PAK will contain. Reads the central directory and the embedded PAK directories only;
    loose files are not extracted here (so a preview is cheap and shadowed/excluded files are never read)."""
    members = _zip_release_members(zf, max_files, max_total)
    if not members:
        raise ValueError(f"{source.name!r} does not contain any representable files")
    res = _Resolved(layout={})
    eff: list[tuple[zipfile.ZipInfo, str]]
    if options.root:
        prefix = options.root + "/"
        top = {n.split("/", 1)[0] for _, n in members if "/" in n}
        if options.root not in top:
            raise ValueError(f"The selected root directory {options.root!r} is not in the archive")
        eff = [(i, n[len(prefix):]) for i, n in members if n.startswith(prefix)]
        res.excluded += [{"path": n, "reason": "outside the selected root directory", "size": i.file_size}
                         for i, n in members if not n.startswith(prefix)]
        res.layout = {"root_mapping": "selected", "wrapper_dirs": [options.root], "has_wrapper": True, "top_level_dirs": sorted(top)}
    else:
        layout = classify_release_layout(name for _, name in members)
        res.layout = layout
        eff = list(members)
        if layout["root_mapping"] == "ambiguous":
            _block(res, strict, "ZIP release has ambiguous root mapping; provide a single wrapper directory or already-rooted game content")
        else:
            try:
                eff = _strip_wrapper(layout, members)
            except ValueError as exc:
                _block(res, strict, str(exc))
            else:
                names = [n for _, n in eff]
                inner_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
                if layout["has_wrapper"] and len(inner_dirs) == 1 and not any("/" not in n for n in names) \
                        and next(iter(inner_dirs)).lower() not in CONTENT_DIRS | AUX_DIRS:
                    _block(res, strict, f"ZIP release has nested wrapper directories ({layout['wrapper_dirs'][0]}/{next(iter(inner_dirs))}/); ambiguous root mapping")
    res.effective_count = len(eff)
    patterns = options.patterns()

    pak_members: dict[int, tuple[zipfile.ZipInfo, str]] = {}
    loose: list[tuple[zipfile.ZipInfo, str]] = []
    for info, name in eff:
        m = NUMBERED_PAK_RE.match(name)
        if m:
            pak_members[int(m.group(1))] = (info, name)
            continue
        res.candidates.append((name, info.file_size))
        hit = first_match(patterns, name) if patterns else None
        if hit:
            res.excluded.append({"path": name, "reason": f"{hit[1]} ({hit[0]})", "size": info.file_size})
        else:
            loose.append((info, name))
    res.loose_count = len(loose)

    if pak_members and sorted(pak_members) != list(range(len(pak_members))):
        _block(res, strict, f"Numbered PAKs must be contiguous from pak0.pak (found {sorted(pak_members)}); Ironwail stops loading at the first gap")

    pak_entries: dict[int, list[dict[str, Any]]] = {}
    total_pak_bytes = sum(i.file_size for i, _ in pak_members.values())
    done_bytes = 0
    for idx in sorted(pak_members):
        info, name = pak_members[idx]
        dest = tmpdir / f"pak-{idx}.bin"

        def tick(n: int, _base=done_bytes) -> None:
            nonlocal done_bytes
            done_bytes += n
            if progress:
                progress("Reading embedded PAKs", done_bytes, total_pak_bytes)

        _extract_member(zf, source, info, dest, tick)
        res.paks[idx] = dest
        res.pak_sizes[idx] = info.file_size
        entries = read_pak_directory(dest)
        res.pak_entry_counts[idx] = len(entries)
        kept = []
        for e in entries:
            res.candidates.append((e["path"], e["size"]))
            hit = first_match(patterns, e["path"]) if patterns else None
            if hit:
                res.excluded.append({"path": e["path"], "reason": f"{hit[1]} ({hit[0]}) in pak{idx}.pak", "size": e["size"]})
            else:
                kept.append(e)
        pak_entries[idx] = kept

    for info, name in loose:
        res.winners[name] = _Win(f"loose:{name}", info.file_size, "loose", info=info)
    for idx in sorted(pak_entries):
        label = f"pak{idx}.pak"
        seen_here: set[str] = set()
        for e in pak_entries[idx]:
            if e["path"] in seen_here:
                continue                            # engine scans linearly and takes the first match within a PAK
            seen_here.add(e["path"])
            if e["path"] in res.winners:
                res.overrides.append({"path": e["path"], "overridden": res.winners[e["path"]].label, "by": label})
            res.winners[e["path"]] = _Win(label, e["size"], "pak", file=res.paks[idx], offset=e["offset"])

    if not res.winners:
        _block(res, strict, f"{source.name!r} does not contain any representable files")
    _check_winners(res, strict)
    return res


def _check_winners(res: _Resolved, strict: bool) -> None:
    folded: dict[str, str] = {}
    for name in sorted(res.winners):
        try:
            _check_pak_entry_name(name, "release")
        except ValueError as exc:
            _block(res, strict, str(exc))
        if name.lower() in folded:
            _block(res, strict, f"Paths differ only by letter case: {folded[name.lower()]!r} and {name!r}")
        folded[name.lower()] = name
    if len(res.winners) > MAX_PAK_ENTRIES:
        _block(res, strict, f"Release resolves to {len(res.winners)} files; a single PAK may hold at most {MAX_PAK_ENTRIES} (Ironwail limit)")


def _image_candidates(winners: dict[str, _Win], options: ConvertOptions | None = None) -> dict[str, list[str]]:
    """TGA files that could be re-encoded. A TGA is dead weight when a PNG with the same name exists (Image_LoadImage tries png first)."""
    out: dict[str, list[str]] = {"textures": [], "skyboxes": []}
    for name in sorted(winners):
        if not name.lower().endswith(".tga"):
            continue
        if f"{name[:-4]}.png" in winners or f"{name[:-4]}.PNG" in winners:
            continue
        out["skyboxes" if name.startswith("gfx/env/") else "textures"].append(name)
    return out


def plan_zip_release(source: Path, options: ConvertOptions | None = None, max_files: int = 5000,
                     max_uncompressed_size: int = 2 * 1024 * 1024 * 1024, progress: Progress | None = None,
                     workdir: Path | None = None) -> dict[str, Any]:
    """Dry run: what building this ZIP with these options would produce. Writes nothing outside a temp directory."""
    from tools.pak_report import classify

    options = options or ConvertOptions()
    source = Path(source)
    if not zipfile.is_zipfile(source):
        raise ValueError(f"{source.name!r} is not a valid ZIP archive")
    with tempfile.TemporaryDirectory(prefix="quake-plan-", dir=str(workdir or source.parent)) as tmp, zipfile.ZipFile(source, "r") as zf:
        res = _resolve_release(source, zf, Path(tmp), options, False, max_files, max_uncompressed_size, progress)
        patterns_all = {name: [(p, f"preset:{name}") for p in PRESETS[name]["patterns"]] for name in PRESETS}
        preset_stats = {}
        for name, pats in patterns_all.items():
            hits = [(n, s) for n, s in res.candidates if first_match(pats, n)]
            preset_stats[name] = {"files": len(hits), "bytes": sum(s for _, s in hits), "label": PRESETS[name]["label"], "help": PRESETS[name]["help"]}
        images = _image_candidates(res.winners)
        engine_read = sorted({classify(e["path"]) for e in res.excluded} - {"authoring", "docs", "other", "metadata"})
        top_dirs = res.layout.get("top_level_dirs") or sorted({n.split("/", 1)[0] for n, _ in res.candidates if "/" in n})
        total_bytes = sum(w.size for w in res.winners.values())
        mapdb_present = "mapdb.json" in res.winners
        return {
            "root_mapping": res.layout.get("root_mapping"),
            "wrapper": res.layout["wrapper_dirs"][0] if res.layout.get("has_wrapper") else None,
            "top_level_dirs": top_dirs,
            "entries": len(res.winners), "entry_limit": MAX_PAK_ENTRIES,
            "payload_bytes": total_bytes, "estimated_pak_bytes": 12 + total_bytes + 64 * len(res.winners),
            "loose_files": res.loose_count,
            "embedded_paks": [{"name": f"pak{i}.pak", "bytes": res.pak_sizes[i], "entries": res.pak_entry_counts[i]} for i in sorted(res.paks)],
            "overrides_total": len(res.overrides), "overrides": res.overrides[:50],
            "excluded": _summarise_excluded(res.excluded),
            "excluded_engine_read_types": engine_read,
            "preset_stats": preset_stats,
            "image_candidates": {k: {"files": len(v), "bytes": sum(res.winners[n].size for n in v)} for k, v in images.items()},
            "mapdb_present": mapdb_present,
            "blockers": res.blockers,
            "options": options.to_dict(),
        }


def build_single_pak_from_zip(source: Path, target: Path, max_files: int = 5000, max_uncompressed_size: int = 2 * 1024 * 1024 * 1024,
                              options: ConvertOptions | None = None, progress: Progress | None = None,
                              workdir: Path | None = None) -> dict[str, Any]:
    """Convert a release ZIP into one PAK holding the effective game assets.

    Precedence mirrors Ironwail's search path (common.c: COM_AddGameDirectory/COM_FindFile): the game
    directory (loose files) is searched LAST, pak0..pakN are pushed on the front in order, so the
    highest-numbered PAK wins, any PAK beats a loose file, and within one PAK the first entry wins.
    Nothing depends on ZIP member order. With no options the output is exactly the plain conversion.

    `options` are opt-in and each use is recorded in the returned info (excluded files, re-encoded images, generated files).
    `progress(stage, done, total)` may raise to cancel; the target is only replaced after everything succeeded.
    """
    options = options or ConvertOptions()
    source = Path(source)
    target = Path(target)
    if not zipfile.is_zipfile(source):
        raise ValueError(f"{source.name!r} is not a valid ZIP archive")
    emit = progress or (lambda *_: None)
    with tempfile.TemporaryDirectory(prefix="quake-zip-", dir=str(workdir or source.parent)) as tmp, zipfile.ZipFile(source, "r") as zf:
        tmpdir = Path(tmp)
        emit("Reading archive", 0, 1)
        res = _resolve_release(source, zf, tmpdir, options, True, max_files, max_uncompressed_size, progress)
        winners = res.winners

        loose_wins = sorted((n, w) for n, w in winners.items() if w.kind == "loose")
        total = sum(w.size for _, w in loose_wins)
        done = 0

        def tick(n: int) -> None:
            nonlocal done
            done += n
            emit("Extracting files", done, total)

        emit("Extracting files", 0, total)
        for idx, (name, w) in enumerate(loose_wins):
            dest = tmpdir / f"member-{idx}.bin"
            _extract_member(zf, source, w.info, dest, tick)
            w.kind, w.file, w.offset, w.info = "file", dest, 0, None

        reencoded = _reencode_images(winners, options, tmpdir, emit)
        generated: list[dict[str, Any]] = []
        if options.mapdb and "mapdb.json" not in winners:
            name, mod_dir = options.mapdb
            body = (json.dumps({"episodes": [{"dir": mod_dir, "name": name}], "maps": []}, indent="\t", ensure_ascii=False) + "\n").encode("utf-8")
            gen = tmpdir / "generated-mapdb.json"
            gen.write_bytes(body)
            winners["mapdb.json"] = _Win("generated", len(body), "file", file=gen)
            generated.append({"path": "mapdb.json", "note": "added by option: episode name and directory only", "size": len(body)})
        _check_winners(res, True)

        order = sorted(winners)
        out_total = sum(winners[n].size for n in order)
        written = 0
        manifest: list[dict[str, Any]] = []
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = target.with_name(target.name + ".tmp")
        try:
            with open(tmp_out, "wb") as out:
                out.write(b"PACK" + b"\0" * 8)
                records = bytearray()
                emit("Writing package", 0, out_total)
                for name in order:
                    w = winners[name]
                    file_pos = out.tell()
                    digest = hashlib.sha256()
                    with open(w.file, "rb") as fh:
                        fh.seek(w.offset)
                        remaining = w.size
                        while remaining:
                            chunk = fh.read(min(_COPY_CHUNK, remaining))
                            if not chunk:
                                raise ValueError(f"Source for {name!r} is truncated")
                            digest.update(chunk)
                            out.write(chunk)
                            remaining -= len(chunk)
                            written += len(chunk)
                            emit("Writing package", written, out_total)
                    if out.tell() > MAX_PAK_BYTES:
                        raise ValueError(f"Resulting PAK exceeds {MAX_PAK_BYTES} bytes (32-bit PAK offsets)")
                    records += name.encode("utf-8").ljust(56, b"\0") + struct.pack("<II", file_pos, w.size)
                    manifest.append({"path": name, "size": w.size, "sha256": digest.hexdigest(), "source": w.label})
                dir_offset = out.tell()
                out.write(records)
                if out.tell() > MAX_PAK_BYTES:
                    raise ValueError(f"Resulting PAK exceeds {MAX_PAK_BYTES} bytes (32-bit PAK offsets)")
                out.seek(4)
                out.write(struct.pack("<II", dir_offset, len(records)))
            os.replace(tmp_out, target)
        finally:
            tmp_out.unlink(missing_ok=True)
    layout = res.layout
    return {
        "entries": len(order), "size": target.stat().st_size, "sha256": _compute_sha256(target, progress, "Checking package"), "paths": order,
        "manifest": manifest, "overrides": res.overrides, "root_mapping": layout.get("root_mapping"),
        "wrapper": layout["wrapper_dirs"][0] if layout.get("has_wrapper") else None,
        "embedded_paks": [f"pak{i}.pak" for i in sorted(res.paks)], "loose_files": res.loose_count,
        "options": options.to_dict() if not options.is_default else None,
        "excluded": _summarise_excluded(res.excluded) if res.excluded else None,
        "reencoded": reencoded, "generated": generated or None,
    }


def _reencode_images(winners: dict[str, _Win], options: ConvertOptions, tmpdir: Path, emit: Progress) -> dict[str, Any] | None:
    """Opt-in lossless TGA -> PNG. Each result is verified pixel-for-pixel (see tools/imagepack.py); anything else stays a TGA."""
    if not (options.reencode_textures or options.reencode_skyboxes):
        return None
    from tools import imagepack

    cands = _image_candidates(winners)
    todo = (cands["textures"] if options.reencode_textures else []) + (cands["skyboxes"] if options.reencode_skyboxes else [])
    done_files: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    before = after = 0
    emit("Re-encoding images", 0, len(todo))
    for i, name in enumerate(sorted(todo)):
        w = winners[name]
        with open(w.file, "rb") as fh:
            fh.seek(w.offset)
            data = fh.read(w.size)
        try:
            png, info = imagepack.tga_to_png(data, compress_level=6)
        except imagepack.Skip as skip:
            skipped[skip.reason] = skipped.get(skip.reason, 0) + 1
        else:
            new_name = name[:-4] + ".png"
            dest = tmpdir / f"png-{i}.bin"
            dest.write_bytes(png)
            del winners[name]
            winners[new_name] = _Win(f"reencoded:{w.label}", len(png), "file", file=dest)
            before += len(data)
            after += len(png)
            done_files.append({"path": name, "to": new_name, "before": len(data), "after": len(png)})
        emit("Re-encoding images", i + 1, len(todo))
    return {"converted": len(done_files), "bytes_before": before, "bytes_after": after, "skipped": skipped,
            "textures": options.reencode_textures, "skyboxes": options.reencode_skyboxes, "libraries": imagepack.versions(),
            "files": done_files[:200], "files_total": len(done_files)}


repack_zip_to_pak = build_single_pak_from_zip


def validate_package(path: Path, sha256: str | None = None) -> Dict[str, Any]:
    """Structural validation. Pass `sha256` when the caller already hashed these exact bytes to avoid reading them again."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Package file not found: {path}")
    info = parse_pak_header(path, sha256)
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

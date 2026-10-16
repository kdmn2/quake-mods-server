"""Read-only inspection of a finished PAK: what Ironwail will see in it, and what looks wrong.

Nothing here writes, renames or drops content. Every rule mirrors a concrete engine behaviour
(Ironwail Quake/ sources, referenced next to each rule) and is bounded in time and memory:
only the PAK directory, BSP headers, a prefix of each worldspawn entity lump, and a few small
text/JSON files are read.

Findings are advisory ("warning" = the engine will visibly ignore or reject something,
"info" = worth knowing). None of them blocks an upload.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import struct
from pathlib import Path, PurePosixPath
from typing import Any

from tools.catalog import read_pak_directory

REPORT_VERSION = 1

MAX_MAPS = 128                  # maps inspected in detail
MAX_ENTITY_BYTES = 64 * 1024    # worldspawn is the first entity; its prefix is enough
MAX_JSON_BYTES = 256 * 1024
MAX_TEXT_BYTES = 16 * 1024
MAX_FINDINGS = 200
MAX_PATHS_PER_FINDING = 12

# host_cmd.c ExtraMaps_Init: only maps/<name>.bsp (no subdirectory) larger than 32 KiB are listed
MAP_LIST_MIN_BYTES = 32 * 1024
# host_cmd.c ExtraMaps_Init: char mapname[32]
MAP_NAME_MAX = 31

# bspfile.h
BSP_VERSIONS = {
    29: "BSP29",
    int.from_bytes(b"BSP2", "little"): "BSP2",       # BSP2VERSION_BSP2
    int.from_bytes(b"2PSB", "little"): "2PSB",        # BSP2VERSION_2PSB
    int.from_bytes(b" 46Q", "little"): "Quake64",     # BSPVERSION_QUAKE64 (('Q'<<24)|('6'<<16)|('4'<<8)|' ')
}
LUMP_ENTITIES, LUMP_LIGHTING, HEADER_LUMPS = 0, 8, 15
BSP_HEADER_BYTES = 4 + 8 * HEADER_LUMPS

# pr_comp.h PROG_VERSION / progdefs PROGHEADER_CRC, pr_edict.c PR_LoadProgs
PROG_VERSION = 6
PROGHEADER_CRC = 5927
FOREIGN_PROGS_CRC = {
    22390: "full CSQC", 52195: "obsolete DP CSQC", 54730: "QuakeWorld", 26940: "prerelease",
    32401: "Tenebrae", 38488: "Hexen II", 26905: "Hexen II mission pack", 14046: "Hexen II demo",
}

# gl_sky.c Sky_LoadSkyBox / Sky_NewMap, image.c Image_LoadImage
SKY_KEYS = ("sky", "skyname", "qlsky")
SKY_SIDES = ("rt", "bk", "lf", "ft", "up", "dn")
IMAGE_EXTS = ("png", "tga", "jpg", "pcx", "lmp")

# modelgen.h / spritegn.h / gl_model.c Mod_LoadModel dispatch on the first four bytes, not on the file name
MODEL_IDENTS = {b"IDPO": ("alias model", 6), b"IDSP": ("sprite", 1)}
WAV_SCAN_BYTES = 64 * 1024
WAV_FORMAT_PCM = 1

# bgmusic.c wanted_handlers
BASE_GAME_TRACKS = 11
MUSIC_EXTS = ("ogg", "opus", "mp3", "flac", "wav", "it", "s3m", "xm", "mod", "umx")

AUTHORING_EXTS = {".map", ".qc", ".src", ".fgd", ".def", ".psd", ".xcf", ".blend", ".db"}
AUTHORING_DIRS = {"mapsrc", "src", "source", "sources"}
DOC_EXTS = {".txt", ".md", ".htm", ".html", ".pdf", ".nfo", ".diz", ".rtf"}
CONFIG_EXTS = {".rc", ".cfg"}
MODEL_EXTS = {".mdl", ".spr", ".md3", ".iqm"}
GAMECODE_NAMES = {"progs.dat", "csprogs.dat"}

CATEGORY_LABELS = {
    "maps": "Maps", "map_lighting": "Map lighting (.lit)", "models": "Models and sprites", "sounds": "Sounds",
    "music": "Music", "graphics": "Graphics and skyboxes", "gamecode": "Game code (progs.dat)",
    "config": "Config / startup scripts", "metadata": "Metadata files", "docs": "Documentation",
    "authoring": "Authoring files (map sources, QuakeC, editor defs)", "other": "Other",
}


def _finding(findings: list[dict[str, Any]], severity: str, code: str, message: str, paths: list[str] | None = None) -> None:
    if len(findings) >= MAX_FINDINGS:
        return
    item: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if paths:
        item["paths"] = sorted(paths)[:MAX_PATHS_PER_FINDING]
        if len(paths) > MAX_PATHS_PER_FINDING:
            item["more"] = len(paths) - MAX_PATHS_PER_FINDING
    findings.append(item)


def classify(path: str) -> str:
    """Display-only grouping. Never affects what is packed."""
    p = PurePosixPath(path)
    parts = p.parts
    ext = p.suffix.lower()
    name = p.name.lower()
    top = parts[0].lower() if len(parts) > 1 else ""
    if top in AUTHORING_DIRS or ext in AUTHORING_EXTS:
        return "authoring"
    if top == "maps":
        return "map_lighting" if ext in (".lit", ".lux") else "maps" if ext == ".bsp" else "other"
    if top == "music":
        return "music"
    if top == "sound":
        return "sounds"
    if top in ("gfx", "textures", "env") or (len(parts) == 1 and ext in (".tga", ".png", ".jpg", ".pcx", ".lmp")):
        return "graphics"
    if top in ("progs", "models", "sprites") and ext in MODEL_EXTS:
        return "models"
    if len(parts) == 1:
        if name in GAMECODE_NAMES:
            return "gamecode"
        if ext in CONFIG_EXTS:
            return "config"
        if name in ("mapdb.json", "descript.ion"):
            return "metadata"
        if ext in DOC_EXTS:
            return "docs"
    return "other"


def _read_entry(fh, entry: dict[str, Any], limit: int | None = None, offset: int = 0) -> bytes:
    fh.seek(entry["offset"] + offset)
    return fh.read(max(0, min(entry["size"] - offset, limit if limit is not None else entry["size"])))


def parse_worldspawn(text: str) -> dict[str, str]:
    """First {...} block as key->value; quote-aware. Leading '_' on keys is stripped like gl_sky.c does."""
    result: dict[str, str] = {}
    pos, n = 0, len(text)
    while pos < n and text[pos] != "{":
        pos += 1
    pos += 1
    tokens: list[str] = []
    while pos < n:
        c = text[pos]
        if c == "}":
            break
        if c == '"':
            end = text.find('"', pos + 1)
            if end < 0:
                break
            tokens.append(text[pos + 1:end])
            pos = end + 1
            continue
        pos += 1
    for i in range(0, len(tokens) - 1, 2):
        key = tokens[i][1:] if tokens[i].startswith("_") else tokens[i]
        result[key.rstrip(" ")] = tokens[i + 1]
    return result


def inspect_bsp(fh, entry: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {"version": None, "version_name": None, "lighting_bytes": None, "worldspawn": {}, "error": None}
    header = _read_entry(fh, entry, BSP_HEADER_BYTES)
    if len(header) < BSP_HEADER_BYTES:
        info["error"] = "file is shorter than a BSP header"
        return info
    version = struct.unpack("<i", header[:4])[0]
    info["version"] = version
    info["version_name"] = BSP_VERSIONS.get(version)
    if info["version_name"] is None:
        return info
    lumps = [struct.unpack("<ii", header[4 + 8 * i:12 + 8 * i]) for i in range(HEADER_LUMPS)]
    for idx in (LUMP_ENTITIES, LUMP_LIGHTING):
        off, length = lumps[idx]
        if off < 0 or length < 0 or off + length > entry["size"]:
            info["error"] = f"lump {idx} lies outside the file"
            return info
    info["lighting_bytes"] = lumps[LUMP_LIGHTING][1]
    ent_off, ent_len = lumps[LUMP_ENTITIES]
    raw = _read_entry(fh, entry, min(ent_len, MAX_ENTITY_BYTES), ent_off)
    info["worldspawn"] = parse_worldspawn(raw.split(b"\0", 1)[0].decode("latin-1"))
    return info


def map_kind(stem: str) -> str:
    """host_cmd.c ExtraMaps_Categorize for a mod's own maps."""
    if stem.startswith("start") or stem.endswith("start") or stem.endswith("intro"):
        return "start"
    if stem.endswith("end"):
        return "end"
    digits = stem.rstrip("0123456789")
    if len(digits) >= 2 and digits.endswith("dm"):
        return "deathmatch"
    return "level"


def check_model_header(head: bytes) -> str | None:
    """None if Ironwail can load this .mdl/.spr, else why not (gl_model.c Mod_LoadModel: unsupported versions are Sys_Error)."""
    if len(head) < 8:
        return "is shorter than a model header"
    ident = head[:4]
    version = struct.unpack("<i", head[4:8])[0]
    if ident in MODEL_IDENTS:
        kind, want = MODEL_IDENTS[ident]
        return None if version == want else f"is an {kind} with version {version} (Ironwail needs {want})"
    if ident == b"IDP3":
        return "is an MD3 file; Ironwail refuses to load MD3 directly"
    if struct.unpack("<i", ident)[0] in BSP_VERSIONS:
        return None                                         # a brush model
    return "does not start with a known model signature"


def check_wav(buf: bytes, total: int) -> str | None:
    """Mirror snd_wave.c WAV_ReadRIFFHeader. None = loads (or inconclusive); otherwise the reason it will not play."""
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return "is missing the RIFF/WAVE header"

    def find(pos: int, name: bytes) -> tuple[int, int] | None:
        while pos + 8 <= len(buf):
            cid, clen = buf[pos:pos + 4], struct.unpack("<i", buf[pos + 4:pos + 8])[0]
            if clen < 0:
                return None
            if cid == name:
                return pos + 8, clen
            pos += 8 + ((clen + 1) & ~1)
        return None

    fmt = find(12, b"fmt ")
    if fmt is None:
        return None if len(buf) < total else "is missing its fmt chunk"
    fpos, flen = fmt
    if fpos + 16 > len(buf):
        return None if len(buf) < total else "has a truncated fmt chunk"
    tag, channels, _rate, _byterate, _align, bits = struct.unpack("<HHIIHH", buf[fpos:fpos + 16])
    if tag != WAV_FORMAT_PCM:
        return f"is not plain PCM (format tag {tag}); Ironwail only plays PCM WAV"
    if bits not in (8, 16):
        return f"is {bits}-bit; Ironwail only plays 8 or 16-bit WAV"
    data = find(fpos + 16 + max(0, flen - 16), b"data")
    if data is None:
        return None if len(buf) < total else "is missing its data chunk"
    if channels not in (1, 2):
        return f"has {channels} channels; Ironwail only plays mono or stereo"
    if (data[1] // (bits // 8)) // channels == 0:
        return "contains no samples"
    return None


def _lit_status(lit_head: bytes, lit_size: int, lighting_bytes: int | None) -> str | None:
    """None if gl_model.c Mod_LoadLighting would accept it, otherwise the reason it won't."""
    if len(lit_head) < 8 or lit_head[:4] != b"QLIT":
        return "does not start with the QLIT signature"
    version = struct.unpack("<i", lit_head[4:8])[0]
    if version != 1:
        return f"has unsupported version {version} (only 1 is read)"
    if lighting_bytes is None:
        return None
    want = 8 + 3 * lighting_bytes
    if lit_size != want:
        return f"is {lit_size} bytes but its map needs exactly {want} (it was built for a different version of the map)"
    return None


def parse_mapdb_name(data: bytes, gamedir: str | None) -> tuple[str | None, str | None]:
    """Mirror host_cmd.c Modlist_Add. Returns (episode name Ironwail would use, problem)."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return None, f"is not valid JSON ({type(exc).__name__})"
    episodes = doc.get("episodes") if isinstance(doc, dict) else None
    if not isinstance(episodes, list):
        return None, 'has no "episodes" array'
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        name, mod_dir = ep.get("name"), ep.get("dir")
        if not isinstance(name, str) or not isinstance(mod_dir, str):
            continue
        # unmodified Copper mapdb.json in derived mods is ignored unless the gamedir really is copper
        if mod_dir.lower() == "copper" and (gamedir or "").lower() != "copper":
            continue
        return name, None
    return None, None


_FIELD_RE = re.compile(r"^\s*(title|author|release date|date|description)\s*:\s*(.*)$", re.IGNORECASE)
_STOP_RE = re.compile(r"^\s*[A-Za-z][A-Za-z ./'-]{1,30}\s*:\s*\S*")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", _CTRL_RE.sub("", text)).strip()[:limit]


def _normalise_date(value: str) -> str | None:
    m = re.fullmatch(r"\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*", value)
    if not m:
        return None
    try:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return d.strftime("%Y/%m/%d")


def parse_readme_fields(data: bytes) -> dict[str, str]:
    """Fields from the common 'Title : ... / Author : ...' text-file template. Only what is literally written."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    fields: dict[str, str] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _FIELD_RE.match(line)
        if not m:
            continue
        key = "date" if m.group(1).lower() in ("release date", "date") else m.group(1).lower()
        if key in fields:
            continue
        value = m.group(2)
        if key == "description":
            parts = [value] if value.strip() else []
            for nxt in lines[i + 1:i + 40]:
                if not nxt.strip() and parts:
                    break
                if nxt.strip() and _STOP_RE.match(nxt) and not nxt.startswith((" ", "\t")):
                    break
                if nxt.strip():
                    parts.append(nxt)
            value = " ".join(parts)
        cleaned = _clean(value, 600 if key == "description" else 200)
        if key == "date":
            normalised = _normalise_date(cleaned)
            if normalised:
                fields["date"] = normalised
        elif cleaned:
            fields[key] = cleaned
    return fields


def analyze_pak(path: Path, gamedir: str | None = None, current: dict[str, str] | None = None) -> dict[str, Any]:
    """Inspect a PAK. `current` holds the catalog values (title/author/date/description) to compare suggestions with."""
    path = Path(path)
    current = current or {}
    entries_all = read_pak_directory(path)
    entries: dict[str, dict[str, Any]] = {}
    for e in entries_all:
        entries.setdefault(e["path"], e)            # engine: first match in a PAK wins
    findings: list[dict[str, Any]] = []
    categories: dict[str, dict[str, int]] = {}
    for name, e in entries.items():
        c = categories.setdefault(classify(name), {"count": 0, "bytes": 0})
        c["count"] += 1
        c["bytes"] += e["size"]

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "entries": len(entries), "duplicate_directory_entries": len(entries_all) - len(entries),
        "payload_bytes": sum(e["size"] for e in entries.values()),
        "categories": {k: {**v, "label": CATEGORY_LABELS[k]} for k, v in sorted(categories.items())},
        "maps": [], "findings": findings, "suggestions": {}, "notable": [], "effective_name": None,
    }
    if report["duplicate_directory_entries"]:
        _finding(findings, "info", "pak.duplicate_entries",
                 "The PAK directory repeats some names; Ironwail uses the first of each (common.c COM_FindFile).")

    with open(path, "rb") as fh:
        # --- maps: menu listing, BSP validity, companion files -----------------------------------
        bsp_names = sorted(n for n in entries if n.startswith("maps/") and "/" not in n[5:] and n.lower().endswith(".bsp"))
        lit_names = {n for n in entries if n.startswith("maps/") and n.lower().endswith(".lit")}
        used_lits: set[str] = set()
        cd_tracks: dict[int, list[str]] = {}
        skies_checked: dict[str, int] = {}
        for bsp in bsp_names[:MAX_MAPS]:
            e = entries[bsp]
            stem = bsp[5:-4]
            listed = e["size"] > MAP_LIST_MIN_BYTES
            row: dict[str, Any] = {"name": stem, "size": e["size"], "listed": listed, "kind": map_kind(stem),
                                   "title": None, "bsp": None, "sky": None, "sky_sides": None, "lit": "none", "cdtrack": None}
            info = inspect_bsp(fh, e)
            row["bsp"] = info["version_name"] or (f"unknown ({info['version']})" if info["version"] is not None else None)
            if info["error"]:
                _finding(findings, "warning", "map.corrupt", f"maps/{stem}.bsp: {info['error']}.", [bsp])
            elif listed and info["version_name"] is None:
                row["listed"] = False
                _finding(findings, "warning", "map.unsupported_bsp",
                         f"maps/{stem}.bsp has BSP version {info['version']}, which Ironwail does not load; it will not appear in the map list "
                         "(host_cmd.c ExtraMaps_ParseDescriptions / gl_model.c Mod_LoadMapDescription).", [bsp])
            if listed and len(stem) > MAP_NAME_MAX:
                _finding(findings, "warning", "map.name_too_long",
                         f"Map name '{stem}' is longer than {MAP_NAME_MAX} characters; Ironwail truncates list names to that length (host_cmd.c ExtraMaps_Init), so it cannot be selected from the menu.", [bsp])
            ws = info["worldspawn"]
            title = _clean(ws.get("message", ""), 120)
            row["title"] = title or None
            # last sky key wins (gl_sky.c Sky_NewMap loads in key order)
            sky_entries = [v for k, v in ws.items() if k in SKY_KEYS]
            sky = sky_entries[-1] if sky_entries else ""
            if sky:
                found = [s for s in SKY_SIDES if any(f"gfx/env/{sky}{s}.{x}" in entries for x in IMAGE_EXTS)]
                row["sky"], row["sky_sides"] = sky, len(found)
                skies_checked[sky] = len(found)
                missing = [s for s in SKY_SIDES if s not in found]
                if 0 < len(found) < 6:
                    _finding(findings, "warning", "sky.partial",
                             f"Skybox '{sky}' (used by maps/{stem}.bsp) has {len(found)} of 6 faces in the package; missing: {', '.join(missing)}. "
                             "Ironwail prints \"Couldn't load\" for each and renders the sky incompletely (gl_sky.c Sky_LoadSkyBox).", [bsp])
                elif not found:
                    _finding(findings, "info", "sky.absent",
                             f"Skybox '{sky}' (used by maps/{stem}.bsp) is not in the package. Ironwail falls back to the scrolling sky unless another game directory provides it.", [bsp])
            lit_name = f"maps/{stem}.lit"
            if lit_name in entries:
                used_lits.add(lit_name)
                lit = entries[lit_name]
                problem = _lit_status(_read_entry(fh, lit, 8), lit["size"], info["lighting_bytes"] if not info["error"] else None)
                row["lit"] = "ok" if problem is None else "rejected"
                if problem:
                    _finding(findings, "warning", "lit.rejected",
                             f"{lit_name} {problem}. Ironwail ignores it and uses plain white lighting (gl_model.c Mod_LoadLighting).", [lit_name])
            sounds = ws.get("sounds", "").strip()
            if sounds.isdigit() and int(sounds) > 0:
                row["cdtrack"] = int(sounds)
                cd_tracks.setdefault(int(sounds), []).append(bsp)
            report["maps"].append(row)
        if len(bsp_names) > MAX_MAPS:
            _finding(findings, "info", "map.limit", f"Only the first {MAX_MAPS} of {len(bsp_names)} maps were inspected in detail.")
        orphans = sorted(lit_names - used_lits)
        if orphans and len(bsp_names) <= MAX_MAPS:
            _finding(findings, "info", "lit.orphan", "Some .lit files have no matching .bsp in the package and are never loaded.", orphans)

        # Tracks 1-11 are the original game's own soundtrack (assumption, not from the engine source): a mod using them
        # relies on the base install, so only higher track numbers are reported.
        missing_music = sorted(t for t in cd_tracks
                               if t > BASE_GAME_TRACKS and not any(f"music/track{t:02d}.{x}" in entries for x in MUSIC_EXTS))
        if missing_music:
            _finding(findings, "info", "music.absent",
                     "Maps set worldspawn 'sounds' to CD track(s) " + ", ".join(map(str, missing_music)) +
                     " but the package has no matching music/trackNN file. With a standard progs.dat that means no music unless another game directory supplies it "
                     "(bgmusic.c BGM_PlayCDtrack).")

        # --- game code ------------------------------------------------------------------------------
        if "progs.dat" in entries and entries["progs.dat"]["size"] >= 8:
            head = _read_entry(fh, entries["progs.dat"], 8)
            version, crc = struct.unpack("<ii", head)
            report["notable"].append({"path": "progs.dat", "note": f"QuakeC progs version {version}, header CRC {crc}"})
            if version != PROG_VERSION:
                _finding(findings, "warning", "progs.version",
                         f"progs.dat has version {version}; Ironwail requires {PROG_VERSION} and stops with an error when a map loads (pr_edict.c PR_LoadProgs).", ["progs.dat"])
            elif crc != PROGHEADER_CRC:
                kind = FOREIGN_PROGS_CRC.get(crc)
                _finding(findings, "warning", "progs.crc",
                         f"progs.dat header CRC is {crc}" + (f" ({kind} game code)" if kind else "") +
                         f", not the standard {PROGHEADER_CRC}; Ironwail rejects it when a map loads (pr_edict.c PR_LoadProgs).", ["progs.dat"])
        elif "progs.dat" in entries:
            _finding(findings, "warning", "progs.truncated", "progs.dat is too small to be a QuakeC program.", ["progs.dat"])

        # --- models, sprites and sounds: headers the engine rejects (fatal for models, silent for sounds) -------------------
        bad_models: dict[str, list[str]] = {}
        bad_sounds: dict[str, list[str]] = {}
        for name in sorted(entries):
            low = name.lower()
            if low.endswith((".mdl", ".spr")):
                reason = check_model_header(_read_entry(fh, entries[name], 8))
                if reason:
                    bad_models.setdefault(reason, []).append(name)
            elif low.startswith("sound/") and low.endswith(".wav"):
                e = entries[name]
                reason = check_wav(_read_entry(fh, e, WAV_SCAN_BYTES), e["size"])
                if reason:
                    bad_sounds.setdefault(reason, []).append(name)
        for reason, paths in sorted(bad_models.items()):
            _finding(findings, "warning", "model.rejected",
                     f"{len(paths)} model/sprite file(s) {reason}. Ironwail stops with a fatal error when a map or the game loads such a model "
                     "(gl_model.c Mod_LoadModel, Mod_LoadAliasModel, Mod_LoadSpriteModel).", paths)
        for reason, paths in sorted(bad_sounds.items()):
            _finding(findings, "warning", "sound.unplayable",
                     f"{len(paths)} sound file(s) {reason}; Ironwail skips them, so they will be silent (snd_wave.c WAV_ReadRIFFHeader).", paths)

        # --- startup / metadata files ---------------------------------------------------------------
        if "quake.rc" in entries:
            report["notable"].append({"path": "quake.rc", "note": "replaces the base game's startup script while this add-on is active (common.c / host.c: exec quake.rc)"})
            _finding(findings, "info", "quake.rc.present",
                     "The package carries its own quake.rc, which Ironwail runs instead of the base game's. It is preserved byte for byte; check it still executes default.cfg/config.cfg if the mod relies on them.", ["quake.rc"])
        if "descript.ion" in entries:
            _finding(findings, "warning", "descript.ion.inert",
                     "descript.ion is only read from the installed mod folder on disk (host_cmd.c Modlist_Add), never from inside a PAK, so it has no effect here. "
                     "Set the display name in the catalog title instead.", ["descript.ion"])
        mapdb_name = None
        if "mapdb.json" in entries:
            e = entries["mapdb.json"]
            if e["size"] > MAX_JSON_BYTES:
                _finding(findings, "info", "mapdb.large", "mapdb.json is too large to inspect.", ["mapdb.json"])
            else:
                mapdb_name, problem = parse_mapdb_name(_read_entry(fh, e), gamedir)
                if problem:
                    _finding(findings, "warning", "mapdb.invalid", f"mapdb.json {problem}; Ironwail ignores it (host_cmd.c Modlist_Add).", ["mapdb.json"])
                elif mapdb_name is None:
                    _finding(findings, "info", "mapdb.no_name",
                             "mapdb.json gives no episode name Ironwail would use here (an unmodified Copper mapdb.json is deliberately ignored unless the gamedir is 'copper').", ["mapdb.json"])
        report["effective_name"] = mapdb_name

        # --- suggestions from the package itself (administrator confirms) ---------------------------
        suggestions: dict[str, dict[str, str]] = {}
        if mapdb_name:
            suggestions["title"] = {"value": _clean(mapdb_name, 200), "source": "mapdb.json episode name (what Ironwail shows for the installed add-on)"}
        text_files = sorted(n for n in entries if "/" not in n and n.lower().endswith(".txt") and entries[n]["size"] <= MAX_TEXT_BYTES * 8)
        for tf in text_files[:8]:
            fields = parse_readme_fields(_read_entry(fh, entries[tf], MAX_TEXT_BYTES))
            if "title" not in fields:
                continue            # only a file that identifies itself with a Title; other readmes may describe a bundled component
            for key in ("title", "author", "date", "description"):
                if key in fields and key not in suggestions:
                    suggestions[key] = {"value": fields[key], "source": f"{tf} ({key})"}
            break
        report["suggestions"] = {
            k: v for k, v in suggestions.items() if v["value"] and v["value"] != (current.get(k) or "")
        }
        if mapdb_name and current.get("title") and _clean(mapdb_name, 200) != current["title"]:
            _finding(findings, "info", "name.mismatch",
                     f"The catalog title is '{current['title']}' but once installed Ironwail lists this add-on as '{_clean(mapdb_name, 200)}' (from mapdb.json).", ["mapdb.json"])

    authoring = categories.get("authoring", {"count": 0, "bytes": 0})
    docs = categories.get("docs", {"count": 0, "bytes": 0})
    extra = authoring["bytes"] + docs["bytes"]
    if authoring["count"] and extra >= 1024 * 1024:
        _finding(findings, "info", "size.extras",
                 f"{authoring['count']} authoring file(s) ({authoring['bytes'] / 1048576:.1f} MiB) plus documentation are packed but never read by Ironwail. "
                 "They are kept as supplied; they only add to the download size.")
    report["findings"] = sorted(findings, key=lambda f: (f["severity"] != "warning", f["code"], f["message"]))
    report["warning_count"] = sum(1 for f in findings if f["severity"] == "warning")
    return report


def summarize_conversion(info: dict[str, Any], source_name: str, source_sha256: str | None) -> dict[str, Any]:
    """Compact, bounded record of how a package was produced (stored with the add-on, never read back into the PAK)."""
    overrides = info.get("overrides", [])
    return {
        "report_version": REPORT_VERSION, "source_name": source_name, "source_sha256": source_sha256,
        "kind": "zip-release", "root_mapping": info.get("root_mapping"), "wrapper": info.get("wrapper"),
        "embedded_paks": info.get("embedded_paks", []), "loose_files": info.get("loose_files"),
        "entries": info.get("entries"), "output_sha256": info.get("sha256"), "output_size": info.get("size"),
        "overrides_total": len(overrides), "overrides": overrides[:200],
        "options": info.get("options"), "excluded": info.get("excluded"), "reencoded": info.get("reencoded"), "generated": info.get("generated"),
    }

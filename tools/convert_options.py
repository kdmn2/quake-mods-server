"""Opt-in conversion options. The default (no options) reproduces the plain conversion byte for byte.

Every option is explicit, is validated here, and is recorded in the add-on's conversion record when used.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Iterable

MAX_PATTERNS = 60
MAX_PATTERN_LEN = 120

# Presets are plain pattern lists so the preview can show exactly what each one would remove.
# A leading "/" anchors to the package root, a trailing "/" means "any directory with this name", a pattern with no "/" matches
# the file name at any depth; matching is case-insensitive and '*' never crosses a '/'.
PRESETS: dict[str, dict[str, Any]] = {
    "authoring": {
        "label": "Map sources, QuakeC sources and editor files",
        "help": "mapsrc/, src/, *.map, *.qc, *.src, *.fgd, *.def, *.psd, *.xcf, *.blend. Ironwail never reads these.",
        "patterns": ["mapsrc/", "src/", "source/", "sources/", "*.map", "*.qc", "*.src", "*.fgd", "*.def", "*.psd", "*.xcf", "*.blend"],
    },
    "junk": {
        "label": "Operating-system leftovers",
        "help": "Thumbs.db, desktop.ini, .DS_Store.",
        "patterns": ["thumbs.db", "desktop.ini", ".ds_store"],
    },
    "docs": {
        "label": "Documentation at the package root",
        "help": "Root-level .txt, .md, .htm(l), .pdf, .nfo, .diz, .rtf. Files inside folders (for example localization/*.txt, which Ironwail reads) are kept. "
                "Metadata suggestions are read from the built package, so removing the readme removes them too.",
        "patterns": [f"/*.{e}" for e in ("txt", "md", "htm", "html", "pdf", "nfo", "diz", "rtf")],
    },
}


@dataclass(frozen=True)
class ConvertOptions:
    exclude_presets: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    root: str | None = None
    reencode_textures: bool = False
    reencode_skyboxes: bool = False
    mapdb: tuple[str, str] | None = None            # (episode name, episode dir) written only if the release has no mapdb.json

    @property
    def is_default(self) -> bool:
        return self == ConvertOptions()

    def patterns(self) -> list[tuple[str, str]]:
        """(pattern, rule label) pairs: presets first, then custom patterns."""
        out = [(p, f"preset:{name}") for name in self.exclude_presets for p in PRESETS[name]["patterns"]]
        out += [(p, "custom") for p in self.exclude_patterns]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclude_presets": list(self.exclude_presets), "exclude_patterns": list(self.exclude_patterns), "root": self.root,
            "reencode_textures": self.reencode_textures, "reencode_skyboxes": self.reencode_skyboxes,
            "mapdb": list(self.mapdb) if self.mapdb else None,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ConvertOptions":
        """Strict, defensive parse of untrusted input (form data or stored JSON)."""
        if not isinstance(raw, dict):
            return cls()
        presets = _str_list(raw.get("exclude_presets"))
        unknown = [p for p in presets if p not in PRESETS]
        if unknown:
            raise ValueError(f"Unknown exclusion preset: {unknown[0]!r}")
        patterns = [normalise_pattern(p) for p in _str_list(raw.get("exclude_patterns")) if p.strip()]
        if len(patterns) > MAX_PATTERNS:
            raise ValueError(f"At most {MAX_PATTERNS} custom exclusion patterns are allowed")
        root = raw.get("root")
        if root in (None, ""):
            root = None
        elif not isinstance(root, str) or "/" in root or root in (".", "..") or "\0" in root or len(root) > 120:
            raise ValueError("Invalid root directory selection")
        mapdb = raw.get("mapdb")
        mapdb_t: tuple[str, str] | None = None
        if mapdb:
            if not (isinstance(mapdb, (list, tuple)) and len(mapdb) == 2 and all(isinstance(x, str) for x in mapdb)):
                raise ValueError("Invalid mapdb.json settings")
            name, d = mapdb[0].strip(), mapdb[1].strip()
            if not name or not d or len(name) > 200 or "\0" in name + d:
                raise ValueError("The generated mapdb.json needs a non-empty name and directory")
            mapdb_t = (name, d)
        return cls(tuple(dict.fromkeys(presets)), tuple(dict.fromkeys(patterns)), root,
                   bool(raw.get("reencode_textures")), bool(raw.get("reencode_skyboxes")), mapdb_t)


def _str_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [line for line in value.splitlines()]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ValueError("Expected a list of strings")


def normalise_pattern(pattern: str) -> str:
    p = pattern.strip().replace("\\", "/")
    if not p or len(p) > MAX_PATTERN_LEN or "\0" in p:
        raise ValueError(f"Invalid exclusion pattern: {pattern[:40]!r}")
    if ".." in p.split("/"):
        raise ValueError(f"Exclusion patterns may not contain '..': {pattern[:40]!r}")
    if p.strip("/") == "" or p in ("*", "**", "/*", "*/"):
        raise ValueError(f"Exclusion pattern would match everything: {pattern[:40]!r}")
    return p


def matches(pattern: str, path: str) -> bool:
    """Case-insensitive match of an exclusion pattern against a package-relative path (see PRESETS for the syntax)."""
    p, s = pattern.lower(), path.lower()
    segs = s.split("/")
    if p.endswith("/"):
        return p[:-1] in segs[:-1]
    if p.startswith("/"):
        pseg = p[1:].split("/")
        return len(pseg) == len(segs) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(segs, pseg))
    if "/" in p:
        pseg = p.split("/")
        if len(pseg) > len(segs):
            return False
        return all(fnmatch.fnmatchcase(a, b) for a, b in zip(segs[-len(pseg):], pseg))
    return fnmatch.fnmatchcase(segs[-1], p)


def first_match(patterns: Iterable[tuple[str, str]], path: str) -> tuple[str, str] | None:
    for pattern, rule in patterns:
        if matches(pattern, path):
            return pattern, rule
    return None


def options_from_form(form: Any) -> ConvertOptions:
    """Build options from a Flask form (multi-valued checkboxes + textarea)."""
    mapdb = None
    if form.get("generate_mapdb"):
        mapdb = [form.get("mapdb_name", ""), form.get("mapdb_dir", "")]
    return ConvertOptions.from_dict({
        "exclude_presets": form.getlist("exclude_presets"),
        "exclude_patterns": form.get("exclude_patterns", ""),
        "root": form.get("root", ""),
        "reencode_textures": form.get("reencode_textures"),
        "reencode_skyboxes": form.get("reencode_skyboxes"),
        "mapdb": mapdb,
    })

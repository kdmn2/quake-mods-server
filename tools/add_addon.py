#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from catalog import _validate_date, _validate_gamedir, _validate_id, atomic_write, parse_pak_header, validate_pak_paths


def _load_or_create_store(root: Path) -> tuple[Path, Path, Path]:
    root = root.resolve()
    packages_dir = root / "packages"
    metadata_dir = root / "metadata"
    packages_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    return root, packages_dir, metadata_dir


def add_addon(source_path: str, addon_id: str, title: str, gamedir: str | None, author: str, date: str | None, description: str | None, store_root: str) -> Path:
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Input file not found: {source}")
    if source.suffix.lower() != ".pak":
        raise ValueError(f"Unsupported file type {source.suffix!r}; only .pak is supported for Ironwail downloads")

    _validate_id(addon_id)
    if gamedir is None:
        gamedir = addon_id
    gamedir = _validate_gamedir(gamedir)
    _validate_date(date)

    # Reject unsafe archive content before publishing.
    validate_pak_paths(source)
    stat = source.stat()
    digest = parse_pak_header(source)
    if stat.st_size <= 0:
        raise ValueError(f"Input package is empty: {source}")

    root, packages_dir, metadata_dir = _load_or_create_store(Path(store_root))
    existing = list(metadata_dir.glob("*.json"))
    for meta in existing:
        with open(meta, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("id") == addon_id:
            raise ValueError(f"Duplicate addon id: {addon_id}")
        if data.get("gamedir") == gamedir:
            raise ValueError(f"Conflicting installation directory: {gamedir}")

    package_target = packages_dir / f"{addon_id}.pak"
    atomic_write(package_target, source.read_bytes())

    metadata = {
        "id": addon_id,
        "title": title or addon_id,
        "gamedir": gamedir,
        "author": author or "Unknown",
        "date": date,
        "description": description or "",
        "package": f"{addon_id}.pak",
        "size": stat.st_size,
        "sha256": digest["sha256"],
    }

    meta_path = metadata_dir / f"{addon_id}.json"
    atomic_write(meta_path, (json.dumps(metadata, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return meta_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage a Quake add-on package for an Ironwail-compatible add-on server")
    parser.add_argument("source", help="Path to a Quake .pak file to publish")
    parser.add_argument("--id", required=True, help="Unique add-on identifier")
    parser.add_argument("--title", required=True, help="Display title")
    parser.add_argument("--gamedir", help="Game directory name to install into (defaults to --id)")
    parser.add_argument("--author", default="Unknown", help="Author name")
    parser.add_argument("--date", help="Date in YYYY/MM/DD format")
    parser.add_argument("--description", help="Optional English description")
    parser.add_argument("--store-root", default="store", help="Directory to stage packages and metadata")
    args = parser.parse_args(argv)

    try:
        meta_path = add_addon(
            source_path=args.source,
            addon_id=args.id,
            title=args.title,
            gamedir=args.gamedir,
            author=args.author,
            date=args.date,
            description=args.description,
            store_root=args.store_root,
        )
        print(f"Added add-on {args.id!r} ({meta_path})")
        return 0
    except Exception as exc:  # pragma: no cover - CLI safety
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

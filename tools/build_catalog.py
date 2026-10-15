#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from catalog import atomic_write, build_catalog, collect_metadata, compute_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a static Ironwail add-on catalog from staged metadata")
    parser.add_argument("--store-root", default="store", help="Directory containing staged packages and metadata")
    parser.add_argument("--public-dir", default="public", help="Directory to write the published static catalog")
    parser.add_argument("--base-url", default="http://localhost:8080", help="Public base URL used for the add-on server")
    args = parser.parse_args(argv)

    try:
        store_root = Path(args.store_root).resolve()
        public_dir = Path(args.public_dir).resolve()
        metadata_dir = store_root / "metadata"
        if not metadata_dir.exists():
            raise FileNotFoundError(f"No staged metadata directory: {metadata_dir}")

        manifest = build_catalog(metadata_dir, public_dir, args.base_url)
        print(f"Published {len(manifest['addons'])} add-ons to {public_dir}")
        return 0
    except Exception as exc:  # pragma: no cover - CLI safety
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

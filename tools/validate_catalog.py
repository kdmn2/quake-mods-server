#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from catalog import validate_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an Ironwail-compatible content.json file")
    parser.add_argument("catalog", help="Path to content.json")
    args = parser.parse_args(argv)
    try:
        data = validate_catalog(Path(args.catalog))
        print(f"Validated {len(data['addons'])} add-on entries")
        return 0
    except Exception as exc:  # pragma: no cover - CLI safety
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

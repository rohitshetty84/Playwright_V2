"""
ci/export_goldens.py — materialize golden/*.json into runnable .ts specs.

Each golden JSON stores its TypeScript source under the `code` key.
Playwright can't execute JSON, so before running tests in CI we dump that
source out to `tests/<safe-name>.spec.ts`.

Usage (from .github/workflows/playwright.yml):
    python ci/export_goldens.py --from golden --to tests
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def safe_filename(raw: str, fallback: str) -> str:
    """Turn a golden's `name` field into a filesystem-safe filename."""
    name = (raw or fallback).strip()
    # Strip an existing extension so we control it.
    name = re.sub(r"\.(spec\.)?ts$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not name:
        name = fallback
    return f"{name}.spec.ts"


def export(src: Path, dst: Path) -> int:
    if not src.is_dir():
        print(f"[export_goldens] source dir not found: {src}", file=sys.stderr)
        return 1

    dst.mkdir(parents=True, exist_ok=True)

    count = 0
    for golden_file in sorted(src.glob("*.json")):
        try:
            data = json.loads(golden_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[export_goldens] skip {golden_file.name}: {exc}", file=sys.stderr)
            continue

        code = data.get("code")
        if not code:
            print(f"[export_goldens] skip {golden_file.name}: no `code` field")
            continue

        out_name = safe_filename(data.get("name", ""), fallback=data.get("id") or golden_file.stem)
        out_path = dst / out_name
        out_path.write_text(code, encoding="utf-8")
        print(f"[export_goldens] {golden_file.name}  ->  {out_path}")
        count += 1

    if count == 0:
        print("[export_goldens] no goldens exported — nothing to test", file=sys.stderr)
        return 1

    print(f"[export_goldens] {count} golden(s) exported to {dst}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export golden JSON -> .ts specs")
    parser.add_argument("--from", dest="src", default="golden", help="source directory of golden JSON files")
    parser.add_argument("--to", dest="dst", default="tests", help="destination directory for .spec.ts files")
    args = parser.parse_args()
    return export(Path(args.src), Path(args.dst))


if __name__ == "__main__":
    raise SystemExit(main())

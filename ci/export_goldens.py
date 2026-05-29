"""
ci/export_goldens.py — materialize golden/*.json into runnable .ts specs.

Each golden JSON stores its TypeScript source under the `code` key.
Playwright can't execute JSON, so before running tests in CI we dump that
source out to `tests/<safe-name>.spec.ts`.

Usage (from .github/workflows/playwright.yml):
    python ci/export_goldens.py --from studio/golden --to tests
    python ci/export_goldens.py --from studio/golden --to tests --ids "seed-g1,4217f745"

When --ids is omitted (or empty), every golden in --from is exported.
When --ids is provided, only goldens whose `id` field OR filename stem
matches one of the given IDs are exported. Matching is case-insensitive.
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


def parse_ids(raw: str | None) -> set[str]:
    """Comma- or whitespace-separated string -> lowercase set of IDs."""
    if not raw:
        return set()
    parts = re.split(r"[,\s]+", raw.strip())
    return {p.lower() for p in parts if p}


def export(src: Path, dst: Path, wanted_ids: set[str]) -> int:
    if not src.is_dir():
        print(f"[export_goldens] source dir not found: {src}", file=sys.stderr)
        return 1

    dst.mkdir(parents=True, exist_ok=True)

    if wanted_ids:
        print(f"[export_goldens] filtering to IDs: {sorted(wanted_ids)}")
    else:
        print("[export_goldens] no --ids filter — exporting every golden")

    exported = 0
    skipped_filtered = 0
    matched_ids: set[str] = set()

    for golden_file in sorted(src.glob("*.json")):
        try:
            data = json.loads(golden_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[export_goldens] skip {golden_file.name}: {exc}", file=sys.stderr)
            continue

        golden_id = (data.get("id") or "").lower()
        stem_id = golden_file.stem.lower()

        if wanted_ids and golden_id not in wanted_ids and stem_id not in wanted_ids:
            print(f"[export_goldens] skip {golden_file.name}: id '{data.get('id', '?')}' not in --ids")
            skipped_filtered += 1
            continue

        matched_ids.add(golden_id or stem_id)

        code = data.get("code")
        if not code:
            print(f"[export_goldens] skip {golden_file.name}: no `code` field")
            continue

        out_name = safe_filename(data.get("name", ""), fallback=data.get("id") or golden_file.stem)
        out_path = dst / out_name
        out_path.write_text(code, encoding="utf-8")
        print(f"[export_goldens] {golden_file.name}  ->  {out_path}")
        exported += 1

    # Warn loudly if the user asked for IDs that don't exist on disk —
    # otherwise this is silent and confusing.
    unknown_ids = wanted_ids - matched_ids
    if unknown_ids:
        print(
            f"[export_goldens] WARNING: requested ID(s) not found in {src}: "
            f"{sorted(unknown_ids)}",
            file=sys.stderr,
        )

    if exported == 0:
        if wanted_ids:
            print(
                f"[export_goldens] no goldens matched the --ids filter "
                f"({skipped_filtered} skipped) — nothing to test",
                file=sys.stderr,
            )
        else:
            print("[export_goldens] no goldens exported — nothing to test", file=sys.stderr)
        return 1

    print(f"[export_goldens] {exported} golden(s) exported to {dst}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export golden JSON -> .ts specs")
    parser.add_argument("--from", dest="src", default="studio/golden", help="source directory of golden JSON files")
    parser.add_argument("--to", dest="dst", default="tests", help="destination directory for .spec.ts files")
    parser.add_argument(
        "--ids",
        dest="ids",
        default="",
        help='Comma-separated list of golden IDs to include (e.g. "seed-g1,4217f745"). '
             "Empty/omitted means export all.",
    )
    args = parser.parse_args()
    return export(Path(args.src), Path(args.dst), parse_ids(args.ids))


if __name__ == "__main__":
    raise SystemExit(main())

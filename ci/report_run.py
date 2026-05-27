"""
ci/report_run.py — POST a Playwright JSON report back to /api/runs.

Reads Playwright's JSON reporter output and translates it into the payload
shape that server.py expects:

    {
      "golden_id":   "<id>",
      "browser":     "msedge",
      "candidates":  [{ "name", "path", "status", "duration", "error?" }, ...]
    }

Env vars (set as GitHub repository secrets/variables and exposed via the
workflow's `env:` block):
    PLAYWRIGHT_AI_STUDIO_URL    base URL of a deployed Studio
                                e.g. https://studio.example.com
    PLAYWRIGHT_AI_STUDIO_TOKEN  optional bearer token

Usage:
    python3 ci/report_run.py results.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests


PATH_TAG = re.compile(r"\bPath\s+([AB])\b", re.IGNORECASE)


def secs(ms: float | int) -> str:
    return f"{(ms or 0) / 1000:.1f}s"


def derive_path(title: str) -> str:
    """Pull Path A / Path B out of the test title; default to 'A'."""
    m = PATH_TAG.search(title or "")
    return m.group(1).upper() if m else "A"


def candidates_from_report(report: dict) -> list[dict]:
    """Walk Playwright's JSON report tree and emit one candidate per test."""
    out: list[dict] = []

    def walk(suite: dict):
        for spec in suite.get("specs", []) or []:
            for test in spec.get("tests", []) or []:
                last = (test.get("results") or [{}])[-1]
                status = last.get("status", "unknown")
                row = {
                    "name": spec.get("title", "<unnamed>"),
                    "path": derive_path(spec.get("title", "")),
                    "status": "pass" if status in ("passed", "expected") else "fail",
                    "duration": secs(last.get("duration", 0)),
                }
                if row["status"] == "fail":
                    err = last.get("error") or {}
                    row["error"] = err.get("message") or err.get("stack") or "Unknown failure"
                out.append(row)
        for child in suite.get("suites", []) or []:
            walk(child)

    for suite in report.get("suites", []) or []:
        walk(suite)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: report_run.py <playwright-json-results>", file=sys.stderr)
        return 2

    results_file = Path(sys.argv[1])
    if not results_file.exists():
        print(f"[report_run] {results_file} not found — nothing to report", file=sys.stderr)
        return 1

    base = os.environ.get("PLAYWRIGHT_AI_STUDIO_URL", "").rstrip("/")
    if not base:
        print("[report_run] PLAYWRIGHT_AI_STUDIO_URL not set — skipping POST")
        return 0

    report = json.loads(results_file.read_text(encoding="utf-8"))
    candidates = candidates_from_report(report)
    if not candidates:
        print("[report_run] no candidates parsed from report — nothing to send")
        return 0

    # GOLDEN_ID is set by the workflow (either from repository variable or auto-detected).
    # It identifies which golden test the results belong to.
    # Auto-Heal uses GOLDEN_ID to find errors specific to that golden.
    # Workflow auto-detects first available golden if not explicitly set.
    golden_id = os.environ.get("GOLDEN_ID")
    if not golden_id:
        print("[report_run] ERROR: GOLDEN_ID not set by workflow", file=sys.stderr)
        return 1
    browser = os.environ.get("BROWSER", "msedge")

    payload = {"golden_id": golden_id, "browser": browser, "candidates": candidates}

    headers = {"Content-Type": "application/json"}
    token = os.environ.get("PLAYWRIGHT_AI_STUDIO_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{base}/api/runs"
    print(f"[report_run] POST {url}  ({len(candidates)} candidate(s))")
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if not resp.ok:
        print(f"[report_run] FAILED  HTTP {resp.status_code}\n{resp.text}", file=sys.stderr)
        return 1

    print(f"[report_run] OK  -> {resp.json().get('id', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

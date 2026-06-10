"""
scripts/smoke_deploy.py
=======================
Post-deploy smoke test for the Playwright AI Studio on Azure Container Apps.

Checks that the app is running and key APIs respond correctly.
Does NOT trigger a real exploration (that dispatches to GitHub Actions and
is too heavy / has side-effects for a deploy gate).

Exit codes:
  0 — all checks passed
  1 — one or more checks failed

Usage:
    python scripts/smoke_deploy.py
    python scripts/smoke_deploy.py --url https://ca-playwright-studio.victoriousglacier-a63c9b2a.eastus.azurecontainerapps.io
    python scripts/smoke_deploy.py --ci
"""

import argparse
import json
import os
import sys

import httpx

DEFAULT_URL = os.environ.get(
    "STUDIO_URL",
    "https://ca-playwright-studio.victoriousglacier-a63c9b2a.eastus.azurecontainerapps.io",
)


def run(base_url: str, ci: bool) -> bool:
    SEP = "─" * 64
    print(f"\n{SEP}")
    print("  Playwright AI Studio — Deploy Smoke Test")
    print(f"  Target : {base_url}")
    print(SEP)

    failures: list[str] = []

    def check(label: str, fn):
        try:
            fn()
            print(f"  ✅  {label}")
        except Exception as e:
            print(f"  ❌  {label}: {e}")
            failures.append(f"{label}: {e}")

    # ── 1. Health (serves the UI) ────────────────────────────────────────────
    def health():
        r = httpx.get(f"{base_url}/", timeout=30)
        r.raise_for_status()
        assert "text/html" in r.headers.get("content-type", ""), \
            f"Expected HTML, got {r.headers.get('content-type')}"

    check("GET / returns HTML", health)

    # ── 2. Explorations list ─────────────────────────────────────────────────
    def explorations():
        r = httpx.get(f"{base_url}/api/explorations", timeout=15)
        r.raise_for_status()
        data = r.json()
        assert isinstance(data, (list, dict)), f"Unexpected response shape: {data!r:.100}"

    check("GET /api/explorations responds", explorations)

    # ── 3. Selector memory endpoint ──────────────────────────────────────────
    def selector_memory():
        r = httpx.get(f"{base_url}/api/selector-memory", timeout=15)
        r.raise_for_status()

    check("GET /api/selector-memory responds", selector_memory)

    # ── 4. Memory export (runner uses this) ──────────────────────────────────
    def memory_export():
        r = httpx.get(f"{base_url}/api/memory/export", timeout=15)
        r.raise_for_status()
        data = r.json()
        assert "selector_memory" in data, f"Missing selector_memory key: {list(data)}"

    check("GET /api/memory/export responds", memory_export)

    # ── 5. GitHub health (explore/start would fail without this) ────────────
    def github_health():
        r = httpx.get(f"{base_url}/api/health/github", timeout=15)
        assert r.status_code != 404, "Endpoint not found (404)"
        data = r.json()
        assert r.status_code == 200 and data.get("status") == "healthy", \
            f"GitHub not healthy: {r.status_code} {data}"

    check("GET /api/health/github — GitHub credentials present", github_health)

    # ── Result ────────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    if not failures:
        print("  RESULT: ✅  PASS")
        print(SEP)
        return True
    else:
        print("  RESULT: ❌  FAIL")
        for f in failures:
            print(f"    • {f}")
        print(SEP)
        if ci:
            sys.exit(1)
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args()
    ok = run(base_url=args.url.rstrip("/"), ci=args.ci)
    sys.exit(0 if ok else 1)

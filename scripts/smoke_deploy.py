"""
scripts/smoke_deploy.py
=======================
Post-deploy smoke test for the Browser Explorer running on Azure Container Apps
(or any hosted instance).  Uses a public website — no auth session required.

The test is intentionally minimal: it validates the complete path from
HTTP API → LLM → MCP bridge → browser launch → step execution.  If ANY layer
is broken the test fails, which is all we need to gate a deployment.

Exit codes
----------
  0  — all assertions passed
  1  — one or more assertions failed (or server unreachable)

Usage
-----
    # Against the deployed Azure app (reads STUDIO_URL env var or defaults):
    python scripts/smoke_deploy.py

    # Explicit URL:
    python scripts/smoke_deploy.py --url https://ca-playwright-studio.victoriousglacier-a63c9b2a.eastus.azurecontainerapps.io

    # Local server (for parity testing):
    python scripts/smoke_deploy.py --url http://localhost:8000

    # CI — exits 1 on failure, prints no extra prompts:
    python scripts/smoke_deploy.py --ci

Requirements
------------
    pip install httpx
"""

import argparse
import json
import os
import sys
import time
from typing import Iterator, Optional

import httpx

# ── Smoke test case — public site, no auth ────────────────────────────────────
# Deliberately simple: navigate → read title → done.
# Success proves: browser launched, MCP bridge live, LLM responded, step ran.
TEST_CASE = """
1. Navigate to https://example.com
2. Wait for the page to fully load
3. Read the text of the h1 heading on the page
4. Confirm the heading contains the word "Example"
""".strip()

# ── Acceptance thresholds ─────────────────────────────────────────────────────
MIN_STEPS_EXECUTED = 1        # at least 1 step must actually run (not just be planned)
MIN_SUCCESS_RATE   = 0.25     # lenient — even a partial run proves infrastructure works
MAX_STEPS          = 12       # cap so the smoke test stays fast
TIMEOUT_SECONDS    = 180      # 3 min wall-clock limit

# Keyword that must appear in at least one successful step description —
# confirms we actually navigated somewhere rather than erroring on launch.
MUST_PASS_KEYWORD  = "navigate"

DEFAULT_URL = os.environ.get(
    "STUDIO_URL",
    "https://ca-playwright-studio.victoriousglacier-a63c9b2a.eastus.azurecontainerapps.io",
)


# ── SSE streaming ─────────────────────────────────────────────────────────────

def stream_sse(url: str, timeout: int) -> Iterator[dict]:
    with httpx.stream("GET", url, timeout=timeout) as resp:
        resp.raise_for_status()
        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                for line in block.splitlines():
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload:
                            try:
                                yield json.loads(payload)
                            except json.JSONDecodeError:
                                pass


# ── Main ──────────────────────────────────────────────────────────────────────

def run(base_url: str, ci: bool) -> bool:
    SEP = "─" * 64
    print(f"\n{SEP}")
    print("  Browser Explorer — Deploy Smoke Test")
    print(f"  Target : {base_url}")
    print(f"  Steps  : {MAX_STEPS}   Timeout: {TIMEOUT_SECONDS}s")
    print(SEP)

    failures: list[str] = []

    # ── 1. Server reachable ───────────────────────────────────────────────────
    print("\n[1/4] Health check…")
    try:
        r = httpx.get(f"{base_url}/", timeout=30)
        r.raise_for_status()
        print("  ✅ Server reachable")
    except Exception as e:
        _fail(f"Server not reachable: {e}", ci)
        return False

    # ── 2. Start exploration (no storage_state → no auth needed) ─────────────
    print("\n[2/4] Starting exploration…")
    try:
        r = httpx.post(
            f"{base_url}/api/explore/start",
            json={
                "test_case":  TEST_CASE,
                "max_steps":  MAX_STEPS,
                "headless":   True,
            },
            timeout=30,
        )
        r.raise_for_status()
        eid = r.json()["explorationId"]
        print(f"  ✅ Exploration started — ID: {eid}")
    except Exception as e:
        _fail(f"Failed to start exploration: {e}", ci)
        return False

    # ── 3. Stream events ──────────────────────────────────────────────────────
    print("\n[3/4] Streaming events…")
    stream_url = f"{base_url}/api/explorations/{eid}/stream"

    steps: list[dict] = []
    fatal: Optional[str] = None
    start_t = time.time()

    try:
        for event in stream_sse(stream_url, timeout=TIMEOUT_SECONDS):
            etype = event.get("type", "")

            if etype == "log":
                msg = event.get("message", "")
                lvl = event.get("level", "info")
                icon = "  💥" if lvl == "error" else ("  ⚠️ " if lvl == "warn" else "     ")
                print(f"{icon} {msg}")
                if lvl == "error" and any(w in msg.lower() for w in ("fatal", "not installed", "not found", "chromium")):
                    fatal = msg

            elif etype == "step_result":
                steps.append(event)
                icon = "  ✅" if event.get("success") else "  ❌"
                num  = event.get("step_num", "?")
                desc = (event.get("description") or "")[:60]
                print(f"{icon} Step {num}: {desc}")

            elif etype in ("complete", "done", "error"):
                break

            if time.time() - start_t > TIMEOUT_SECONDS:
                failures.append(f"Timed out after {TIMEOUT_SECONDS}s")
                break

    except Exception as e:
        failures.append(f"Stream error: {e}")

    elapsed = time.time() - start_t
    print(f"\n  Elapsed: {elapsed:.1f}s")

    # ── 4. Assertions ─────────────────────────────────────────────────────────
    print(f"\n[4/4] Assertions…")

    if fatal:
        failures.append(f"Fatal infrastructure error: {fatal}")

    executed  = [s for s in steps if s.get("action") not in ("blocked", "", None)]
    succeeded = [s for s in executed if s.get("success")]

    print(f"  Steps planned  : {len(steps)}")
    print(f"  Steps executed : {len(executed)}")
    print(f"  Steps passed   : {len(succeeded)}")

    if len(executed) < MIN_STEPS_EXECUTED:
        failures.append(
            f"Too few steps executed ({len(executed)} < {MIN_STEPS_EXECUTED}) — "
            "browser may have failed to launch"
        )

    if executed:
        rate = len(succeeded) / len(executed)
        print(f"  Success rate   : {rate:.0%}  (min {MIN_SUCCESS_RATE:.0%})")
        if rate < MIN_SUCCESS_RATE:
            failures.append(f"Success rate {rate:.0%} below threshold {MIN_SUCCESS_RATE:.0%}")

    nav_passed = any(
        MUST_PASS_KEYWORD in (s.get("description") or "").lower() and s.get("success")
        for s in steps
    )
    if not nav_passed:
        failures.append(
            f"No step containing '{MUST_PASS_KEYWORD}' succeeded — "
            "browser launched but could not navigate"
        )

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


def _fail(msg: str, ci: bool):
    print(f"\n  ❌  {msg}")
    if ci:
        sys.exit(1)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post-deploy smoke test")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="Studio base URL (default: STUDIO_URL env or Azure ACA URL)")
    parser.add_argument("--ci",  action="store_true",
                        help="Exit 1 on failure (for CI pipelines)")
    args = parser.parse_args()

    ok = run(base_url=args.url.rstrip("/"), ci=args.ci)
    sys.exit(0 if ok else 1)

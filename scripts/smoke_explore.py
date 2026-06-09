"""
scripts/smoke_explore.py
========================
Smoke test for the Browser Explorer (MCP) pipeline.

Starts an exploration via the studio API, streams SSE events, and asserts
that the run meets minimum quality thresholds — all without touching the
browser frontend.

Usage
-----
    # server must already be running:
    python scripts/smoke_explore.py

    # auto-start the server for you:
    python scripts/smoke_explore.py --start-server

    # non-interactive CI mode (exits 0=pass, 1=fail):
    python scripts/smoke_explore.py --ci

Options
-------
    --url      Studio base URL (default: http://localhost:7860)
    --steps    Max steps to allow (default: 25)
    --timeout  Max wall-clock seconds to wait for completion (default: 300)
    --headless Run browser headless (default: true)
    --start-server  Launch uvicorn before running, kill it after
    --ci       Exit with code 1 on failure instead of printing and continuing
"""

import argparse
import json
import re
import subprocess
import sys
import time
from typing import Iterator, Optional

import httpx   # pip install httpx


# ── Test case ────────────────────────────────────────────────────────────────

TEST_CASE = """
1. Navigate to https://performancemanager8.successfactors.com/sf/start
2. Click the "Home" icon in the top navigation bar
3. Click the "Onboarding" menu item from the dropdown
4. Wait for the Onboarding dashboard page (/onb2Dashboard) to load
5. Locate the input field with placeholder "Search for new recruit"
6. Fill the input field with the candidate name 'Matthew Moraga'
7. Click the typeahead suggestion for 'Matthew Moraga'
8. Click the "Go" button (role=button[name="Go"])
9. Wait for ui5-table-row to appear in the candidate table
10. Read the value from ui5-table-row >> ui5-table-cell:nth-child(4) (Data Collection status)
11. Read the value from ui5-table-row >> ui5-table-cell:nth-child(5) (Compliance Forms status)
12. If BOTH statuses read "Completed", mark the test as passed
""".strip()

STORAGE_STATE = "successfactors"

# ── Assertions ────────────────────────────────────────────────────────────────
# Adjust these thresholds to your acceptance bar.
MIN_STEPS_ATTEMPTED   = 3    # at least this many steps must execute (not just be planned)
MIN_SUCCESS_RATE      = 0.4  # >= 40% of executed steps must succeed
MUST_REACH_STEPS      = [    # at least one of these step descriptions must pass
    "navigate",
    "login",
    "home",
]
# If you want a hard check on a specific step, add its exact description here:
REQUIRED_STEP_PASS: Optional[str] = None   # e.g. "Navigate to https://..."


# ── SSE streaming ─────────────────────────────────────────────────────────────

def stream_sse(url: str, timeout: int) -> Iterator[dict]:
    """Yield parsed SSE data objects from a text/event-stream endpoint."""
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


# ── Runner ────────────────────────────────────────────────────────────────────

def run_smoke(base_url: str, max_steps: int, timeout: int, headless: bool, ci: bool,
              restarts: int = 0) -> bool:
    sep = "─" * 60

    print(f"\n{sep}")
    print("  Browser Explorer Smoke Test")
    print(f"  Server : {base_url}")
    print(f"  Steps  : {max_steps}   Timeout: {timeout}s   Restarts: {restarts}")
    print(sep)

    # 1. Health check
    try:
        r = httpx.get(f"{base_url}/", timeout=10)
        r.raise_for_status()
        print("✅ Server reachable")
    except Exception as e:
        _fail(f"Server not reachable at {base_url}: {e}", ci)
        return False

    # 2. Start exploration
    payload = {
        "test_case":     TEST_CASE,
        "storage_state": STORAGE_STATE,
        "max_steps":     max_steps,
        "headless":      headless,
        "max_restarts":  args.restarts,
    }
    try:
        r = httpx.post(f"{base_url}/api/explore/start", json=payload, timeout=30)
        r.raise_for_status()
        eid = r.json()["explorationId"]
        stream_url = f"{base_url}/api/explorations/{eid}/stream"
        print(f"✅ Exploration started — ID: {eid}")
    except Exception as e:
        _fail(f"Failed to start exploration: {e}", ci)
        return False

    # 3. Stream events
    steps: list[dict] = []
    log_lines: list[str] = []
    final_event: Optional[dict] = None
    fatal = None
    start_t = time.time()

    print(f"\n{sep}")
    print("  Live log")
    print(sep)

    try:
        for event in stream_sse(stream_url, timeout=timeout):
            etype = event.get("type", "")

            if etype == "log":
                msg = event.get("message", "")
                lvl = event.get("level", "info")
                prefix = "💥" if lvl == "error" else ("⚠️ " if lvl == "warn" else "   ")
                print(f"  {prefix} {msg}")
                log_lines.append(msg)
                if lvl == "error" and "fatal" in msg.lower():
                    fatal = msg

            elif etype == "step_result":
                steps.append(event)
                icon = "✅" if event.get("success") else "❌"
                num  = event.get("step_num", "?")
                desc = event.get("description", "")[:55]
                act  = event.get("action", "")
                print(f"  {icon} Step {num}: {desc} [{act}]")

            elif etype in ("complete", "done", "error"):
                final_event = event
                break

            if time.time() - start_t > timeout:
                _fail(f"Timed out after {timeout}s", ci)
                return False

    except httpx.HTTPStatusError as e:
        _fail(f"Stream error: {e}", ci)
        return False

    elapsed = time.time() - start_t
    print(f"\n{sep}")
    print(f"  Exploration finished in {elapsed:.1f}s")
    print(sep)

    # 4. Assertions
    failures: list[str] = []

    if fatal:
        failures.append(f"Fatal error during run: {fatal}")

    executed  = [s for s in steps if s.get("action") not in ("blocked", "")]
    succeeded = [s for s in executed if s.get("success")]

    print(f"\n  Steps planned   : {len(steps)}")
    print(f"  Steps executed  : {len(executed)}")
    print(f"  Steps succeeded : {len(succeeded)}")

    if len(executed) < MIN_STEPS_ATTEMPTED:
        failures.append(
            f"Too few steps executed: {len(executed)} < {MIN_STEPS_ATTEMPTED}"
        )

    if executed:
        rate = len(succeeded) / len(executed)
        print(f"  Success rate    : {rate:.0%} (min {MIN_SUCCESS_RATE:.0%})")
        if rate < MIN_SUCCESS_RATE:
            failures.append(
                f"Success rate {rate:.0%} below threshold {MIN_SUCCESS_RATE:.0%}"
            )

    if MUST_REACH_STEPS:
        reached = any(
            any(kw in s.get("description", "").lower() for kw in MUST_REACH_STEPS)
            and s.get("success")
            for s in steps
        )
        if not reached:
            failures.append(
                f"None of the required step keywords were passed: {MUST_REACH_STEPS}"
            )

    if REQUIRED_STEP_PASS:
        found = any(
            REQUIRED_STEP_PASS.lower() in s.get("description", "").lower()
            and s.get("success")
            for s in steps
        )
        if not found:
            failures.append(f"Required step not passed: '{REQUIRED_STEP_PASS}'")

    # 5. Report
    print(f"\n{sep}")
    if not failures:
        print("  RESULT: ✅  PASS")
        print(sep)
        return True
    else:
        print("  RESULT: ❌  FAIL")
        for f in failures:
            print(f"    • {f}")
        print(sep)
        if ci:
            sys.exit(1)
        return False


def _fail(msg: str, ci: bool):
    print(f"\n❌  {msg}")
    if ci:
        sys.exit(1)


# ── Server management ─────────────────────────────────────────────────────────

def start_server(studio_dir: str) -> subprocess.Popen:
    print("🚀 Starting studio server…")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "0.0.0.0", "--port", "7860"],
        cwd=studio_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until it responds
    for _ in range(20):
        time.sleep(1)
        try:
            httpx.get("http://localhost:7860/", timeout=2).raise_for_status()
            print("✅ Server ready")
            return proc
        except Exception:
            pass
    raise RuntimeError("Server did not start within 20s")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pathlib

    parser = argparse.ArgumentParser(description="Browser Explorer smoke test")
    parser.add_argument("--url",          default="http://localhost:7860")
    parser.add_argument("--steps",        type=int, default=25)
    parser.add_argument("--timeout",      type=int, default=300)
    parser.add_argument("--no-headless",  action="store_true")
    parser.add_argument("--restarts",     type=int, default=0,
                        help="Full-run retries on cascade failure (default: 0)")
    parser.add_argument("--start-server", action="store_true")
    parser.add_argument("--ci",           action="store_true",
                        help="Exit 1 on failure (for CI pipelines)")
    args = parser.parse_args()

    server_proc: Optional[subprocess.Popen] = None
    studio_dir = str(pathlib.Path(__file__).parent.parent / "studio")

    try:
        if args.start_server:
            server_proc = start_server(studio_dir)

        result = run_smoke(
            base_url=args.url,
            max_steps=args.steps,
            timeout=args.timeout,
            headless=not args.no_headless,
            ci=args.ci,
            restarts=args.restarts,
        )
        sys.exit(0 if result else 1)

    finally:
        if server_proc:
            server_proc.terminate()
            print("Server stopped.")

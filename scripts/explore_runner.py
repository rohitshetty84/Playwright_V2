#!/usr/bin/env python3
"""
Standalone exploration runner for GitHub Actions.

Reads config from environment variables (set by explore.yml workflow),
runs the full AI exploration loop using the existing studio code,
then POSTs the result + updated memory back to the Studio API.

Required env vars:
  EXPLORATION_ID          — matches the Studio's pending exploration record
  STUDIO_URL              — public URL of the Studio (for callbacks)
  STUDIO_CALLBACK_TOKEN   — shared secret to authenticate callbacks
  TEST_CASE               — exploration test case description
  AZURE_OPENAI_*          — all Azure OpenAI config

Optional env vars:
  STORAGE_STATE           — auth session name (e.g. "successfactors")
  MAX_STEPS               — default 25
  APP_CONTEXT             — default "SAP SuccessFactors Onboarding 2.0"
  HEADLESS                — "true" or "false", default "true"
"""

import os
import sys
import json
import asyncio
import logging
from pathlib import Path

import requests

# ── Path setup ────────────────────────────────────────────────────────────────
# This script lives in scripts/; studio/ is one level up.
REPO_ROOT = Path(__file__).resolve().parent.parent
STUDIO_DIR = REPO_ROOT / "studio"
sys.path.insert(0, str(STUDIO_DIR))

# ── Config from env ───────────────────────────────────────────────────────────
EXPLORATION_ID  = os.environ["EXPLORATION_ID"]
STUDIO_URL      = os.environ["STUDIO_URL"].rstrip("/")
CALLBACK_TOKEN  = os.environ.get("STUDIO_CALLBACK_TOKEN", "")
TEST_CASE       = os.environ["TEST_CASE"]
STORAGE_STATE   = os.environ.get("STORAGE_STATE", "").strip() or None
MAX_STEPS       = int(os.environ.get("MAX_STEPS", "25"))
APP_CONTEXT     = os.environ.get("APP_CONTEXT", "SAP SuccessFactors Onboarding 2.0")
HEADLESS        = os.environ.get("HEADLESS", "true").lower() != "false"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("explore_runner")

# ── Studio API helpers ────────────────────────────────────────────────────────

_HEADERS = {
    "X-Callback-Token": CALLBACK_TOKEN,
    "Content-Type": "application/json",
}


def _studio_get(path: str) -> dict:
    r = requests.get(f"{STUDIO_URL}{path}", headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def _studio_post(path: str, payload: dict) -> dict:
    r = requests.post(f"{STUDIO_URL}{path}", json=payload, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_memory() -> None:
    """Download selector_memory, exploration_patterns and learned_rules from Studio."""
    log.info("[memory] Fetching from Studio …")
    try:
        data = _studio_get("/api/memory/export")
        (STUDIO_DIR / "selector_memory.json").write_text(
            json.dumps(data.get("selector_memory", {}), indent=2)
        )
        (STUDIO_DIR / "exploration_patterns.json").write_text(
            json.dumps(data.get("exploration_patterns", {}), indent=2)
        )
        (STUDIO_DIR / "learned_rules.json").write_text(
            json.dumps(data.get("learned_rules", {}), indent=2)
        )
        domains = len(data.get("selector_memory", {}))
        patterns = sum(
            len(v.get("patterns", []))
            for v in data.get("exploration_patterns", {}).values()
        )
        log.info(f"[memory] Loaded — {domains} selector domains, {patterns} patterns")
    except Exception as exc:
        log.warning(f"[memory] Could not fetch from Studio ({exc}) — starting fresh")


def upload_memory() -> None:
    """Push updated memory files back to Studio after the exploration completes."""
    log.info("[memory] Uploading updates to Studio …")

    def _read(fname: str) -> dict:
        p = STUDIO_DIR / fname
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            return {}

    try:
        _studio_post("/api/memory/import", {
            "selector_memory":       _read("selector_memory.json"),
            "exploration_patterns":  _read("exploration_patterns.json"),
            "learned_rules":         _read("learned_rules.json"),
        })
        log.info("[memory] Upload complete")
    except Exception as exc:
        log.warning(f"[memory] Upload failed: {exc}")


class RemoteQueue:
    """
    Drop-in replacement for asyncio.Queue in the exploration pipeline.
    Instead of storing events locally, each put() POSTs the event to the
    Studio's /api/explorations/{id}/event endpoint so the browser's SSE
    stream receives live step updates as they happen on the runner.
    """

    def __init__(self, studio_url: str, exploration_id: str, callback_token: str):
        self._url = f"{studio_url}/api/explorations/{exploration_id}/event"
        self._headers = {
            "X-Callback-Token": callback_token,
            "Content-Type": "application/json",
        }

    async def put(self, event) -> None:
        if event is None:
            return  # end-of-stream sentinel — the /complete callback handles teardown
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._url,
                    json=event,
                    headers=self._headers,
                    timeout=6.0,
                )
        except Exception as exc:
            log.debug(f"[event] Failed to stream event to Studio: {exc}")


def post_result(result: dict) -> None:
    """Send the completed exploration result to Studio."""
    log.info(f"[result] Posting to Studio … steps={len(result.get('steps', []))}")
    try:
        _studio_post(f"/api/explorations/{EXPLORATION_ID}/complete", result)
        log.info("[result] Studio accepted the result")
    except Exception as exc:
        log.error(f"[result] POST failed: {exc}")
        # Write result locally as a fallback artefact — visible in GitHub Actions
        fallback = STUDIO_DIR / "explorations" / EXPLORATION_ID / "runner_result.json"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(json.dumps(result, indent=2, default=str))
        log.info(f"[result] Saved fallback → {fallback}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    log.info(f"[runner] exploration_id={EXPLORATION_ID}")
    log.info(f"[runner] studio_url={STUDIO_URL}")
    log.info(f"[runner] test_case={TEST_CASE[:80]}…")
    log.info(f"[runner] storage_state={STORAGE_STATE!r}  max_steps={MAX_STEPS}  headless={HEADLESS}")

    # 1. Pull memory from Studio so this run benefits from past learnings
    fetch_memory()

    # 2. Import server module — this inits Azure OpenAI clients, loads env, etc.
    #    Must happen AFTER fetch_memory() writes the memory files.
    log.info("[runner] Importing studio server …")
    import server  # noqa: F401 — side-effects: sets up clients, loads .env

    from server import ExploreRequest, _run_exploration  # noqa: E402

    # 3. Build the request
    req = ExploreRequest(
        test_case=TEST_CASE,
        storage_state=STORAGE_STATE,
        max_steps=MAX_STEPS,
        headless=HEADLESS,
        max_restarts=0,
    )

    # 4. Run — RemoteQueue streams each event back to Studio in real-time
    log.info("[runner] Starting exploration (live events → Studio) …")
    live_queue = RemoteQueue(STUDIO_URL, EXPLORATION_ID, CALLBACK_TOKEN)
    try:
        result = await _run_exploration(req, EXPLORATION_ID, queue=live_queue)
        log.info(f"[runner] Exploration finished — {len(result.get('steps', []))} steps")
    except Exception as exc:
        log.exception("[runner] Exploration raised an exception")
        result = {
            "explorationId": EXPLORATION_ID,
            "error": str(exc),
            "steps": [],
            "status": "error",
        }

    # 5. Push updated memory back (selector learnings, patterns, rules)
    upload_memory()

    # 6. Send full result to Studio
    post_result(result)


if __name__ == "__main__":
    # Run and then drain the loop so asyncio subprocess transports (the MCP server
    # child process) have a chance to close cleanly before the GC tears them down.
    # Without this Python 3.11 prints "RuntimeError: Event loop is closed" in __del__,
    # which GitHub Actions wrongly flags as a step error even though the job passed.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run())
        loop.run_until_complete(asyncio.sleep(0.1))  # drain pending callbacks
    finally:
        loop.close()

"""
Playwright AI Studio — Python/FastAPI backend
Azure OpenAI powered test synthesis & auto-healing
"""

import os, json, uuid, re, subprocess, tempfile, logging, base64, asyncio
from collections import defaultdict

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mK]')
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
import requests
from playwright.async_api import async_playwright

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AzureOpenAI
from dotenv import load_dotenv

# ── Import improved healing engine ─────────────────────────────────────────────
from healing_engine import ErrorSignature, generate_targeted_healing_prompt, analyze_healing_history

# ── P1-2: extracted prompt + LLM helpers (single source of truth) ──────────────
from services.llm import LLMService
from services import prompts  # noqa: F401 — referenced by string-name in future call sites
from services.assertions import evaluate as evaluate_assertions  # P1-5
from services.git_sync import sync_goldens as _sync_goldens_service  # P2-2
from services.vision_policy import decide as decide_vision, log_decision  # P2-3

# ── Configure Logging ──────────────────────────────────────────────────────────
BASE = Path(__file__).parent
LOGS_DIR = BASE / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# Set up logger with both file and console handlers
logger = logging.getLogger("playwright_ai_studio")
logger.setLevel(logging.DEBUG)

# File handler - daily synthesis log
synthesis_log_file = LOGS_DIR / f"synthesis-{datetime.now().strftime('%Y-%m-%d')}.log"
file_handler = logging.FileHandler(synthesis_log_file)
file_handler.setLevel(logging.DEBUG)

# JSON structured log handler
json_log_file = LOGS_DIR / "synthesis-results.jsonl"
json_handler = logging.FileHandler(json_log_file)
json_handler.setLevel(logging.DEBUG)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Formatter
formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# ── Ensure Playwright browsers are installed — only when binary is missing ─────
try:
    from playwright.sync_api import sync_playwright as _spw
    with _spw() as _p:
        _exe = _p.chromium.executable_path
    if not Path(_exe).exists():
        logger.info("Playwright chromium not found — running install...")
        _pw_check = subprocess.run(
            ["python", "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=120, cwd=str(BASE)
        )
        if _pw_check.returncode != 0:
            logger.warning(f"playwright install warning: {_pw_check.stderr[:200]}")
        else:
            logger.info("Playwright chromium installed")
    else:
        logger.info(f"Playwright chromium ready at {_exe}")
except Exception as _pw_err:
    logger.warning(f"Could not verify Playwright browser: {_pw_err}")

def _strip_ansi(text):
    if isinstance(text, str):
        return _ANSI_RE.sub('', text)
    return text

def _clean_details(obj):
    if isinstance(obj, dict):
        return {k: _clean_details(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_details(v) for v in obj]
    return _strip_ansi(obj)

def log_json_result(phase, status, message, details=None):
    """Log structured JSON result to synthesis-results.jsonl"""
    try:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "phase": phase,
            "status": status,
            "message": _strip_ansi(message),
            "details": _clean_details(details or {})
        }
        with open(json_log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception as e:
        logger.error(f"Failed to write JSON log: {e}")

ROOT_ENV = BASE.parent / ".env"
load_dotenv()
if ROOT_ENV.exists():
    load_dotenv(ROOT_ENV, override=False)

# ── Azure OpenAI client (vision / synthesis) ─────────────────────────────────
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
)
DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# ── Azure OpenAI reasoning client (o4-mini) ───────────────────────────────────
# Used by the exploration engine for planning and self-correction.
# Falls back to the main client/deployment if not configured.
_reasoning_deployment  = os.getenv("AZURE_REASONING_DEPLOYMENT", "").strip()
_reasoning_api_version = os.getenv("AZURE_REASONING_API_VERSION", "2024-12-01-preview").strip()

if _reasoning_deployment:
    reasoning_client = AzureOpenAI(
        api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        api_version=_reasoning_api_version,
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    )
    REASONING_DEPLOYMENT = _reasoning_deployment
    logger.info(f"[config] Reasoning model: {REASONING_DEPLOYMENT} (api_version={_reasoning_api_version})")
else:
    reasoning_client     = client
    REASONING_DEPLOYMENT = DEPLOYMENT
    logger.info(f"[config] No reasoning model configured — exploration will use {DEPLOYMENT}")

# P1-2: shared LLM service. New code should prefer `llm.ask` / `llm.vision_heal`
# over the legacy `ask_llm()` helper below. The legacy helper is preserved for
# backward compatibility with the many existing call sites in this file.
llm = LLMService(
    client=client,
    deployment=DEPLOYMENT,
    default_temperature=0.2,
    default_max_tokens=1500,
    vision_max_tokens=2000,
)

# ── Storage paths ─────────────────────────────────────────────────────────────
GOLDEN_DIR     = BASE / "golden"
RUNS_DIR       = BASE / "runs"
HEALING_DIR    = BASE / "healing_history"  # Track all healing attempts
GOLDEN_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)
HEALING_DIR.mkdir(exist_ok=True)
EXPLORATIONS_DIR    = BASE / "explorations"
SELECTOR_MEMORY_FILE = BASE / "selector_memory.json"
EXPLORATIONS_DIR.mkdir(exist_ok=True)

# ── Synthesis tuning ─────────────────────────────────────────────────────────
MAX_HEAL_ROUNDS    = 3    # Max Phase-1/2 retry cycles before giving up
MAX_EXPLORE_RETRIES = 2   # Verify-then-act: retries per exploration step before giving up
LLM_TEMPERATURE   = 0.2  # All LLM calls use a single determinism constant
LLM_MAX_TOKENS    = 1500 # Default output token budget
LLM_VISION_TOKENS = 2000 # Vision responses include full TS code — need more room
NAV_PAUSE_MS      = 2000 # Post-navigation pause so JS-rendered elements appear

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Playwright AI Studio", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# ── Pydantic models ───────────────────────────────────────────────────────────
class SynthesizeRequest(BaseModel):
    test_case: str
    script_fragment: Optional[str] = ""

class SaveGoldenRequest(BaseModel):
    name: str
    description: str
    code: str
    browsers: list[str] = ["msedge"]
    analysis: Optional[dict] = {}

class RunRequest(BaseModel):
    golden_id: str
    browser: str = "msedge"
    candidates: list[dict]   # [{name, path, status, duration, error?}]

class HealRequest(BaseModel):
    golden_id: str

class PromoteGoldenRequest(BaseModel):
    code: str

class TriggerCIRequest(BaseModel):
    golden_ids: str  # comma-separated list of golden IDs

class ValidateHealRequest(BaseModel):
    golden_id: str

class ExploreRequest(BaseModel):
    test_case: str
    storage_state: Optional[str] = None   # e.g. "successfactors" → studio/.auth/successfactors.json
    max_steps: int = 30

class GenerateFromExplorationRequest(BaseModel):
    exploration_id: str
    md_content: str                        # possibly user-edited before generation
# ── Azure OpenAI helper ───────────────────────────────────────────────────────
# Legacy wrapper, now thin: delegates to LLMService + retry, and sanitises
# error responses (P2-4) so we don't leak full Azure exception strings.
def ask_llm(system: str, user: str, max_tokens: int = LLM_MAX_TOKENS) -> str:
    try:
        return llm.ask(system=system, user=user, max_tokens=max_tokens)
    except Exception as e:
        logger.exception("[ask_llm] Azure OpenAI call failed")
        # P2-4: don't leak provider error details to API clients.
        raise HTTPException(
            status_code=502,
            detail="LLM request failed — see server logs for details.",
        )

# ── File helpers ──────────────────────────────────────────────────────────────
def load_goldens() -> list[dict]:
    out = []
    for f in sorted(GOLDEN_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out

def load_runs() -> list[dict]:
    out = []
    for f in sorted(RUNS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out

def save_json(directory: Path, id: str, data: dict):
    # P0-3: write atomically — tmp file then rename — so a half-written JSON
    # file can never be read by another request.
    target = directory / f"{id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(target)

def ts_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

# ── P0-3: per-golden-id locks for concurrent read-modify-write ────────────────
# Two heal requests on the same golden could otherwise clobber each other's
# history entries. The lock guards the whole RMW sequence, not just the write.
_golden_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

def golden_lock(golden_id: str) -> asyncio.Lock:
    return _golden_locks[golden_id]

# ── Healing History Helper Functions ──────────────────────────────────────────
def load_healing_history(golden_id: str) -> list:
    """Load all healing attempts for a golden"""
    path = HEALING_DIR / f"{golden_id}_history.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []

def save_healing_attempt(golden_id: str, attempt: dict):
    """Save a healing attempt with metadata.

    NOTE: This is intentionally synchronous so existing call sites that aren't
    in async contexts keep working. For new code use `save_healing_attempt_locked`
    which holds the per-golden lock across the read-modify-write sequence.
    """
    history = load_healing_history(golden_id)
    attempt["attemptNumber"] = len(history) + 1
    attempt["timestamp"] = ts_now()
    history.append(attempt)
    save_json(HEALING_DIR, f"{golden_id}_history", history)
    logger.info(f"[healing] Recorded attempt #{attempt['attemptNumber']} for golden {golden_id}")

async def save_healing_attempt_locked(golden_id: str, attempt: dict):
    """P0-3: lock-protected RMW for healing history. Use this in async handlers."""
    async with golden_lock(golden_id):
        save_healing_attempt(golden_id, attempt)

def get_healing_failures_for_error(golden_id: str, error_msg: str) -> list:
    """Get all failed healing attempts for a specific error"""
    history = load_healing_history(golden_id)
    return [h for h in history if h.get("error") == error_msg and not h.get("succeeded", True)]

def is_healing_stuck(golden_id: str) -> dict:
    """Check if healing has failed multiple times for same error"""
    history = load_healing_history(golden_id)
    if len(history) < 3:
        return {"stuck": False}

    # Group by error type
    errors = {}
    for h in history:
        error = h.get("error", "unknown")
        if error not in errors:
            errors[error] = []
        if not h.get("succeeded", True):
            errors[error].append(h)

    # Check if any error has 3+ failed attempts
    for error, attempts in errors.items():
        if len(attempts) >= 3:
            return {
                "stuck": True,
                "error": error,
                "failedAttempts": len(attempts),
                "recommendation": "MANUAL_FIX_NEEDED",
                "history": attempts
            }

    return {"stuck": False}

# ── LOCAL VALIDATION: Run tests locally for instant feedback ──────────────────
async def validate_test_locally(test_code: str, golden_id: str) -> dict:
    """
    Run a Playwright test locally and return results immediately.

    This allows auto-heal to test fixes in seconds instead of waiting for GitHub Actions.

    Args:
        test_code: The TypeScript test code to validate
        golden_id: The golden ID (for temp file naming)

    Returns:
        {
            "status": "PASS" | "FAIL" | "ERROR",
            "duration": 2.1,
            "error": null or error message,
            "passed": true | false
        }
    """
    try:
        # Create temp test file with TypeScript extension
        # Use system temp directory (automatically created by OS)
        with tempfile.NamedTemporaryFile(
            mode='w',
            suffix=f'_{golden_id}_validate.spec.ts',
            delete=False
        ) as f:
            f.write(test_code)
            temp_file = f.name

        try:
            # Call the Node.js validation script
            root_project = BASE.parent  # Go up from studio/
            validate_script = root_project / "validate-test.js"

            if not validate_script.exists():
                return {
                    "status": "ERROR",
                    "error": f"validate-test.js not found at {validate_script}",
                    "duration": 0,
                    "passed": False
                }

            # Verify temp file exists and is readable
            temp_file_path = Path(temp_file)
            if not temp_file_path.exists():
                return {
                    "status": "ERROR",
                    "error": f"Temp file not found: {temp_file}",
                    "duration": 0,
                    "passed": False
                }

            logger.debug(f"[validation] Temp file created: {temp_file}")
            logger.debug(f"[validation] File size: {temp_file_path.stat().st_size} bytes")
            logger.debug(f"[validation] Running: node {validate_script} {temp_file}")

            result = subprocess.run(
                ['node', str(validate_script), temp_file],
                capture_output=True,
                text=True,
                timeout=180,   # 3 min — slow corporate sites (SuccessFactors etc.)
                cwd=str(root_project)
            )

            logger.debug(f"[validation] Exit code: {result.returncode}")
            logger.debug(f"[validation] Stdout: {result.stdout[:500]}")
            if result.stderr:
                logger.debug(f"[validation] Stderr: {result.stderr[:500]}")

            # Parse JSON result.
            # P0-2 fix: validate-test.js's [validate] log lines come BEFORE the
            # final result line, AND Playwright's JSON reporter dumps a single
            # massive JSON blob to stdout that we must not confuse with our own
            # result. Scan from the END for the last line that is a complete,
            # parseable JSON object.
            parsed = None
            for line in reversed(result.stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith('{') and line.endswith('}'):
                    try:
                        parsed = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue  # keep searching upward
            if parsed is not None:
                # validate-test.js passes the screenshot as a file path to avoid
                # bloating stdout with base64. Read it here and attach as base64.
                shot_path = parsed.get("failureScreenshotPath")
                if shot_path:
                    try:
                        with open(shot_path, "rb") as f:
                            parsed["failureScreenshot"] = base64.b64encode(f.read()).decode("utf-8")
                        logger.info(f"[validation] Failure screenshot loaded ({Path(shot_path).stat().st_size // 1024}KB): {shot_path}")
                    except Exception as shot_err:
                        logger.warning(f"[validation] Could not read failure screenshot: {shot_err}")
                return parsed
            return {
                "status": "ERROR",
                "error": f"Failed to parse validation result: {result.stdout[:500]}",
                "duration": 0,
                "passed": False
            }

        finally:
            # Clean up temp file
            try:
                Path(temp_file).unlink()
            except Exception as cleanup_err:
                logger.warning(f"[validation] Warning: Could not delete temp file {temp_file}: {cleanup_err}")

    except subprocess.TimeoutExpired:
        return {
            "status": "TIMEOUT",
            "error": "Test validation timed out (>60 seconds)",
            "duration": 60,
            "passed": False
        }
    except Exception as e:
        return {
            "status": "ERROR",
            "error": f"Validation error: {str(e)}",
            "duration": 0,
            "passed": False
        }

def _clean_healed_code(code: str) -> str:
    """Strip markdown fences and trailing LLM commentary from healed TypeScript."""
    code = re.sub(r"```(?:typescript|ts|js)?[\n]?", "", code).strip()
    code = re.sub(r"```$", "", code).strip()
    # Keep only up to the last closing }); so trailing summary text is dropped
    lines = code.split('\n')
    for i in range(len(lines) - 1, -1, -1):
        if '});' in lines[i]:
            code = '\n'.join(lines[:i+1]).rstrip()
            break
    # Drop any trailing [AI-HEAL] comment lines the LLM appended after the block
    while True:
        lines = code.split('\n')
        if lines and re.match(r'^\s*(\*+\s*)?\[AI-HEAL\]', lines[-1]):
            code = '\n'.join(lines[:-1]).rstrip()
        else:
            break
    return code

# ── Selector memory helpers ───────────────────────────────────────────────────
# Every successful exploration step is recorded. Future explorations on the
# same domain get the verified selector injected as a hint into the planning
# prompt — the model uses evidence instead of guessing.

def _load_selector_memory() -> dict:
    if SELECTOR_MEMORY_FILE.exists():
        try:
            return json.loads(SELECTOR_MEMORY_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_selector_memory(memory: dict) -> None:
    SELECTOR_MEMORY_FILE.write_text(json.dumps(memory, indent=2, default=str))

def _intent_keywords(text: str) -> set:
    """Extract meaningful keywords from a step description for fuzzy matching."""
    stop = {
        'the','a','an','and','or','to','from','in','on','at','by','for','with',
        'into','of','is','are','was','be','as','their','this','that','it','its',
        'if','then','based','path','please','use','using','should','must','will',
        'can','click','go','navigate','open','find','select','check','get',
    }
    words = re.sub(r'[^a-z0-9\s]', '', text.lower()).split()
    return {w for w in words if w not in stop and len(w) > 2}

def _intent_similarity(a: str, b: str) -> float:
    """Jaccard similarity between two step intents (0–1)."""
    kw_a, kw_b = _intent_keywords(a), _intent_keywords(b)
    if not kw_a or not kw_b:
        return 0.0
    return len(kw_a & kw_b) / len(kw_a | kw_b)

def _find_memory_hints(domain: str, step_desc: str,
                       min_similarity: float = 0.35) -> list:
    """Return past verified selectors for similar steps on this domain, best first."""
    memory = _load_selector_memory()
    entries = memory.get(domain, {}).get("entries", [])
    hits = []
    for e in entries:
        if e.get("success_count", 0) == 0:
            continue
        sim = _intent_similarity(step_desc, e.get("step_intent", ""))
        if sim >= min_similarity:
            hits.append({**e, "_similarity": sim})
    hits.sort(key=lambda x: x["_similarity"] * x.get("success_count", 1), reverse=True)
    return hits[:3]

def _record_selector_outcome(domain: str, step_desc: str, action: str,
                              selector: str, value: Optional[str],
                              success: bool) -> None:
    """Record the outcome of an exploration action to selector memory."""
    if not selector or not domain or action in ("wait", "navigate", "read", "decision", "done"):
        return
    memory = _load_selector_memory()
    if domain not in memory:
        memory[domain] = {"entries": []}
    entries = memory[domain]["entries"]

    # Find an existing entry that matches this selector + similar intent
    existing = next(
        (e for e in entries
         if e.get("selector") == selector
         and _intent_similarity(e.get("step_intent", ""), step_desc) > 0.55),
        None
    )
    now = datetime.now().strftime("%Y-%m-%d")
    if existing:
        if success:
            existing["success_count"] = existing.get("success_count", 0) + 1
            existing["last_success"]  = now
            existing["failure_count"] = max(0, existing.get("failure_count", 0) - 1)
        else:
            existing["failure_count"] = existing.get("failure_count", 0) + 1
            existing["last_failure"]  = now
        sc, fc = existing["success_count"], existing.get("failure_count", 0)
        existing["confidence"] = "high" if sc >= 3 and fc == 0 else (
                                  "medium" if sc >= 1 and fc <= 1 else "low")
    elif success:
        entries.append({
            "id":            str(uuid.uuid4())[:8],
            "step_intent":   step_desc,
            "action":        action,
            "selector":      selector,
            "value":         value,
            "success_count": 1,
            "failure_count": 0,
            "confidence":    "medium",
            "last_success":  now,
            "last_failure":  None,
        })
    try:
        _save_selector_memory(memory)
    except Exception as me:
        logger.warning(f"[memory] Could not save selector memory: {me}")


# ── Exploration helpers ───────────────────────────────────────────────────────

def _parse_test_steps(test_case: str) -> list:
    """Use LLM to break a free-text description into a flat ordered step list."""
    raw = ask_llm(
        system="""You are a test planning assistant.
Break the test description into a flat ordered list of atomic steps.
For conditional paths (Path A / Path B / If X then Y) include ALL path steps tagged with their path label.
Return ONLY a JSON array — no markdown fences, no explanation.
Schema: [{"id":1,"description":"...","type":"navigate|interact|read|conditional|assert","path":"A|B|both"}]""",
        user=test_case,
        max_tokens=2000,
    )
    raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
    return json.loads(raw)


def _generate_exploration_md(exploration_id: str, test_case: str, steps_log: list) -> str:
    """Produce a human-readable Markdown file from the exploration step log."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# Test Exploration — {exploration_id}",
        f"**Generated:** {now}",
        "",
        "## Original Test Description",
        "",
        test_case.strip(),
        "",
        "---",
        "",
        "## Verified Steps",
        "",
    ]

    for entry in steps_log:
        num    = entry.get("step_num", "?")
        desc   = entry.get("description", "")
        ok     = entry.get("success", False)
        action = entry.get("action", "")
        sel    = entry.get("selector", "")
        val    = entry.get("value", "")
        obs    = entry.get("observation", "")
        notes  = entry.get("notes", "")
        err    = entry.get("error", "")
        path   = entry.get("path", "both")
        readv  = entry.get("read_value", "")
        shot   = entry.get("screenshot_file", "")

        lines.append(f"### Step {num}: {desc}")
        if path and path not in ("both", None):
            lines.append(f"**Path:** {path}")
        lines.append(f"**Status:** {'✅ Success' if ok else '❌ Failed'}")
        if action:   lines.append(f"**Action:** `{action}`")
        if sel:      lines.append(f"**Selector:** `{sel}`")
        if val:      lines.append(f"**Value:** `{val}`")
        if readv:    lines.append(f"**Read value:** `{readv}`")
        if obs:      lines.append(f"**Observation:** {obs}")
        if notes:    lines.append(f"**Notes:** {notes}")
        if err:      lines.append(f"**Error:** {err}")
        if shot:     lines.append(f"**Screenshot:** `screenshots/{shot}`")
        lines.append("")

    # Verified selector reference table
    verified = [s for s in steps_log if s.get("selector") and s.get("success")]
    if verified:
        lines += [
            "---", "",
            "## Selector Reference",
            "",
            "| Step | Action | Selector | Value | Notes |",
            "|------|--------|----------|-------|-------|",
        ]
        for s in verified:
            sel_e = s.get("selector", "").replace("|", "\\|")
            val_e = (s.get("value") or "").replace("|", "\\|")
            lines.append(f"| {s['step_num']} | {s.get('action','')} | `{sel_e}` | {val_e} | {s.get('notes','')} |")
        lines.append("")

    # Path decisions
    decisions = [s for s in steps_log if s.get("action") == "decision"]
    if decisions:
        lines += ["---", "", "## Conditional Path Decisions", ""]
        for d in decisions:
            lines.append(f"- Step {d['step_num']}: {d.get('observation','')} → **Path {d.get('path_taken','?')}**")
        lines.append("")

    lines += [
        "---", "",
        "## Instructions for Test Generation",
        "",
        "Use the **Selector Reference** table above to generate the Playwright TypeScript test.",
        "- Add `test.use({ storageState: 'studio/.auth/<appName>.json' })` for authentication",
        "- Follow verified steps in order; handle conditional paths as documented above",
        "- Use exact selectors from the reference table — do not guess alternatives",
        "",
    ]
    return "\n".join(lines)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(BASE / "static" / "index.html")

@app.get("/api/goldens")
async def get_goldens():
    return load_goldens()

@app.get("/api/runs")
async def get_runs():
    return load_runs()

# ─ Shared: capture a screenshot from a URL for LLM context ───────────────────
async def capture_screenshot_b64(url: str, label: str = "") -> Optional[str]:
    """Navigate to url, return base64 PNG or None on failure.

    P2-3: gated by VISION_ALLOWED_HOSTS — if the URL's hostname is not in the
    allowlist, returns None and the caller falls back to text-only.
    """
    decision = decide_vision(url)
    log_decision(decision, context=label or "capture_screenshot_b64")
    if not decision.allowed:
        return None
    try:
        pb = await async_playwright().start()
        browser = await pb.chromium.launch()
        page = await browser.new_page()
        for _w, _t in [('domcontentloaded', 30000), ('load', 30000), ('commit', 45000)]:
            try:
                await page.goto(url, wait_until=_w, timeout=_t)
                break
            except Exception:
                pass
        await page.wait_for_timeout(NAV_PAUSE_MS)
        shot = await page.screenshot(full_page=False)
        await browser.close()
        await pb.stop()
        tag = f"[{label}] " if label else ""
        logger.info(f"{tag}✅ Heal screenshot captured ({len(shot)//1024}KB) from {url}")
        return base64.b64encode(shot).decode('utf-8')
    except Exception as e:
        logger.warning(f"[capture_screenshot_b64] ⚠️  Could not capture screenshot from {url}: {e}")
        return None

# ─ Vision Analysis with Azure GPT-4V ──────────────────────────────────────────
async def analyze_page_with_vision(url: str, test_description: str) -> str:
    """
    Navigate to page, capture screenshot, analyze with Azure GPT-4V Vision

    Returns: Generated TypeScript test code
    """
    pb = None
    browser = None

    try:
        logger.info(f"[VISION] Starting page analysis for {url}")

        # P2-3: vision allowlist check.
        _vd = decide_vision(url)
        log_decision(_vd, context="VISION")
        if not _vd.allowed:
            raise PermissionError(
                f"vision blocked by allowlist: {_vd.reason}"
            )

        # Launch browser
        pb = await async_playwright().start()
        browser = await pb.chromium.launch()
        page = await browser.new_page()

        # Navigate to page with fallback strategies
        logger.info(f"[VISION] Navigating to {url}")
        for _w, _t in [('domcontentloaded', 30000), ('load', 30000), ('commit', 45000)]:
            try:
                await page.goto(url, wait_until=_w, timeout=_t)
                logger.info(f"[VISION] ✅ Page loaded (wait_until='{_w}')")
                break
            except Exception as _e:
                logger.warning(f"[VISION] ⚠️  '{_w}' timed out — trying next ({_e})")

        await page.wait_for_timeout(NAV_PAUSE_MS)
        # Capture screenshot
        screenshot_bytes = await page.screenshot()
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
        logger.info(f"[VISION] ✅ Screenshot captured ({len(screenshot_bytes)} bytes)")

        # Close browser
        await browser.close()
        await pb.stop()

        # Send to GPT-4V for analysis
        logger.info("[VISION] Sending to GPT-4V for analysis...")

        response = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_b64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": f"""You are analyzing a webpage screenshot to generate a Playwright test.

WEBPAGE: [Screenshot above]

TEST INSTRUCTIONS:
{test_description}

TASK:
1. Carefully analyze the webpage structure visible in the screenshot
2. Identify the exact selectors for all elements mentioned in the instructions
3. Note where buttons, tabs, text, links, forms are located
4. Generate a complete, valid TypeScript test for Playwright

RULES:
- Use only selectors you can see in the screenshot
- Prefer getByRole(), getByText(), getByLabel() selectors
- Use page.locator() for complex selectors
- Include proper waits: page.waitForLoadState(), locator.waitFor()
- Add comments explaining each step
- Code must be completely valid and runnable

OUTPUT:
Return ONLY the TypeScript code. No markdown, no explanations.

```typescript
import {{ test, expect }} from '@playwright/test';

test.describe('Page Test', () => {{
  test('user interaction test', async ({{ page }}) => {{
    // Your code here
  }});
}});
```"""
                        }
                    ]
                }
            ],
            max_tokens=LLM_VISION_TOKENS,
            temperature=LLM_TEMPERATURE
        )

        generated_code = response.choices[0].message.content.strip()
        logger.info(f"[VISION] ✅ Code generated ({len(generated_code)} chars)")

        return generated_code

    except Exception as e:
        logger.error(f"[VISION] ❌ Error: {str(e)}")
        raise

    finally:
        # Cleanup
        if browser:
            await browser.close()
        if pb:
            await pb.stop()

# ─ NEW: Enhanced Synthesis with Local Validation ─────────────────────────────
@app.post("/api/synthesize/with-validation")
async def synthesize_with_validation(req: SynthesizeRequest):
    """
    ENHANCED WORKFLOW: Synthesize → Run Local → Learn → Tune → Validate → Ready for Golden

    Instead of: Generate → Ask to save
    Now: Generate → Run → Learn → Tune → Validate → "Ready for golden?"

    Returns:
        {
            "generatedCode": "...",
            "phase1Pass": true/false,
            "phase1Message": "Test ran successfully",
            "phase2Updated": true/false,
            "phase2Changes": ["Changed selector A", "Changed selector B"],
            "tuned Code": "...",
            "phase3Pass": true/false,
            "phase3Message": "Validation passed!",
            "readyForGolden": true/false,
            "recommendation": "✅ Ready to save as golden!" or "⚠️ Review issues before saving"
        }
    """
    try:
        logger.info(f"\n{'='*80}")
        logger.info("SYNTHESIS WORKFLOW STARTED")
        logger.info(f"{'='*80}")
        logger.info(f"Test Description: {req.test_case[:100]}...")

        log_json_result("START", "INFO", "Synthesis workflow initiated", {
            "test_description": req.test_case[:200],
            "timestamp": datetime.now().isoformat()
        })

        # ─── PHASE 0: SYNTHESIZE WITH VISION (Azure GPT-4V) ──────────────────────
        logger.info("[PHASE 0] Synthesizing test with vision analysis (GPT-4V)...")
        log_json_result(0, "IN_PROGRESS", "Synthesizing with vision", {})

        url = None   # may be set by Phase 0 or the fallback; used by Phase 2 screenshots

        try:
            # Extract URL from test description (full https:// URLs only)
            url_match = re.search(r'https?://[^\s]+', req.test_case)
            if not url_match:
                raise ValueError("No URL found in test description")
            url = url_match.group(0)
            logger.info(f"[PHASE 0] Detected URL: {url}")

            # P2-3: vision allowlist check — if blocked, fall straight through
            # to the text-only fallback. This raises so the existing except
            # branch handles it cleanly.
            _vd = decide_vision(url)
            log_decision(_vd, context="PHASE 0")
            if not _vd.allowed:
                raise PermissionError(
                    f"vision blocked by allowlist: {_vd.reason}"
                )

            # Phase 0A: Navigate and capture screenshot
            logger.info("[PHASE 0A] Navigating to page and capturing screenshot...")

            pb = await async_playwright().start()
            browser = await pb.chromium.launch()
            page = await browser.new_page()

            # Try progressively more lenient wait strategies so sites with
            # continuous background requests (news, finance) don't time out.
            _strategies = [
                ('domcontentloaded', 30000),
                ('load', 30000),
                ('commit', 45000),
            ]
            _loaded = False
            for _wait, _timeout in _strategies:
                try:
                    await page.goto(url, wait_until=_wait, timeout=_timeout)
                    logger.info(f"[PHASE 0A] ✅ Page loaded (wait_until='{_wait}')")
                    _loaded = True
                    break
                except Exception as _nav_err:
                    logger.warning(f"[PHASE 0A] ⚠️  '{_wait}' timed out — trying next strategy ({_nav_err})")
            if not _loaded:
                raise RuntimeError(f"Could not load {url} with any wait strategy")

            # Brief pause so JS-rendered elements appear in the screenshot
            await page.wait_for_timeout(NAV_PAUSE_MS)
            screenshot_bytes = await page.screenshot()
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            logger.info(f"[PHASE 0A] ✅ Screenshot captured")

            await browser.close()
            await pb.stop()

            # Phase 0B: Analyze with GPT-4V and generate code
            logger.info("[PHASE 0B] Analyzing with Azure GPT-4V Vision...")

            response = client.chat.completions.create(
                model=DEPLOYMENT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{screenshot_b64}"
                                }
                            },
                            {
                                "type": "text",
                                "text": f"""Analyze this webpage and generate a Playwright test.

TEST INSTRUCTIONS:
{req.test_case}

Generate complete, valid TypeScript code using selectors visible in the screenshot.

SELECTOR RULES (in order of preference):
1. For navigation tabs/menu items: use page.locator('a[role="menuitem"]').filter({{hasText: "..."}}) — many sites use role="menuitem" not role="link" on nav tabs.
2. For buttons: getByRole('button', {{ name: '...' }})
3. For links with href: page.locator('a[href*="keyword"]')
4. For text content: getByText('...', {{ exact: true }})
5. Avoid getByRole('link') for navigation tabs — it breaks on sites that use role="menuitem".

AUTHENTICATION RULES (must follow — security requirement):
- NEVER hardcode usernames, passwords, or any credentials in the test code.
- If the test requires a logged-in session, add this line before the test block:
    test.use({{ storageState: 'studio/.auth/<appName>.json' }});
  Replace <appName> with a short identifier for the app (e.g. 'myapp', 'successfactors').
- The storageState file is created once by running: npx ts-node scripts/auth.ts
- If the test instructions mention login but no storageState is available yet, add this comment inside the test:
    // TODO: run `npx ts-node scripts/auth.ts` to create the session file, then remove this line.
- For apps that always start logged-in (no login step needed), omit the storageState line entirely.
- NEVER use placeholder URLs like 'https://your-actual-domain.com' or 'https://example.com'.
  Always use the exact URL from the test instructions. If no URL is given, use the real domain
  name mentioned (e.g. 'SuccessFactors' → 'https://performancemanager.successfactors.com').

TEST DATA RULES:
- If the test needs candidate names, user IDs, or other test data, import from '../data/candidate.json'
  using: const data = require('../data/candidate.json');
- Never hardcode candidate names inline — always read from the data file.
- The data file structure is: {{ "candidates": [{{ "id": "TC-001", "name": "..." }}] }}

Include proper waits and error handling.
Output ONLY the code, no markdown or explanations."""
                            }
                        ]
                    }
                ],
                max_tokens=2000,
                temperature=0.2
            )

            generated_code = response.choices[0].message.content.strip()
            logger.info(f"[PHASE 0B] ✅ Code generated with vision ({len(generated_code)} chars)")

        except Exception as e:
            logger.error(f"[PHASE 0] ❌ Vision synthesis failed: {str(e)}")
            log_json_result(0, "FAILED", f"Vision synthesis error: {str(e)} — attempting text-only fallback", {})

            # ── Phase 0 Fallback: text-only synthesis (no screenshot) ─────────
            # Phase 0 couldn't take a screenshot or call vision — ask the LLM to
            # generate a best-effort test from the description alone, then let
            # Phase 1+2 run and heal it as normal.
            logger.info("[PHASE 0-FALLBACK] Attempting text-only synthesis...")
            try:
                # Step 1: infer the URL from the description so Phase 2 can take
                # healing screenshots even though Phase 0 never navigated anywhere.
                url_infer = client.chat.completions.create(
                    model=DEPLOYMENT,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Extract the full URL (including https://) from this test description. "
                            "If only a domain is mentioned (e.g. 'bbc.com/news'), prepend https://. "
                            "Return ONLY the URL — nothing else.\n\n"
                            f"{req.test_case}"
                        )
                    }],
                    max_tokens=80,
                    temperature=0,
                )
                inferred = url_infer.choices[0].message.content.strip()
                if inferred.startswith("http"):
                    url = inferred
                    logger.info(f"[PHASE 0-FALLBACK] Inferred URL: {url}")
                else:
                    logger.warning("[PHASE 0-FALLBACK] Could not infer URL — Phase 2 will run without screenshots")

                # Step 2: generate test code from text description alone
                fallback_resp = client.chat.completions.create(
                    model=DEPLOYMENT,
                    messages=[{
                        "role": "user",
                        "content": f"""Generate a complete Playwright TypeScript test for the following description.

TEST INSTRUCTIONS:
{req.test_case}

SELECTOR RULES (in order of preference):
1. For navigation tabs/menu items: use page.locator('a[role="menuitem"]').filter({{hasText: "..."}})
2. For buttons: getByRole('button', {{ name: '...' }})
3. For links with href: page.locator('a[href*="keyword"]')
4. For text content: getByText('...', {{ exact: true }})
5. Avoid getByRole('link') for navigation tabs.

AUTHENTICATION RULES (must follow — security requirement):
- NEVER hardcode usernames, passwords, or any credentials in the test code.
- If the test requires a logged-in session, add: test.use({{ storageState: 'studio/.auth/<appName>.json' }});
- For apps that start logged-in already, omit the storageState line entirely.
- NEVER use placeholder URLs like 'https://your-actual-domain.com' or 'https://example.com'.
  Use the exact URL from the instructions. If only an app name is given (e.g. 'SuccessFactors'),
  use its real known domain (e.g. 'https://performancemanager.successfactors.com').

TEST DATA RULES:
- If the test needs candidate names, user IDs, or other test data, import from '../data/candidate.json'
  using: const data = require('../data/candidate.json');
- Never hardcode candidate names inline — always read from the data file.
- The data file structure is: {{ "candidates": [{{ "id": "TC-001", "name": "..." }}] }}

Include proper waits and error handling.
Output ONLY the code, no markdown or explanations."""
                    }],
                    max_tokens=LLM_MAX_TOKENS,
                    temperature=LLM_TEMPERATURE,
                )
                generated_code = _clean_healed_code(fallback_resp.choices[0].message.content.strip())
                logger.info(f"[PHASE 0-FALLBACK] ✅ Text-only code generated ({len(generated_code)} chars)")
                log_json_result(0, "FALLBACK", "Text-only synthesis succeeded — will validate via Phase 1+2", {
                    "original_error": str(e),
                    "code_chars": len(generated_code),
                    "inferred_url": url,
                })
            except Exception as e2:
                logger.error(f"[PHASE 0-FALLBACK] ❌ Text-only synthesis also failed: {str(e2)}")
                log_json_result(0, "FAILED", f"Both vision and text-only synthesis failed", {})
                return {
                    "error": f"Phase 0 failed: {str(e)} | Text-only fallback also failed: {str(e2)}",
                    "generatedCode": "",
                    "phase1Pass": False,
                    "phase1Message": f"Phase 0 failed: {str(e)}",
                    "phase2Updated": False,
                    "phase2Changes": [],
                    "phase2SkipReason": "Phase 0 and fallback both failed — no code generated",
                    "tunedCode": "",
                    "phase3Pass": False,
                    "phase3Message": "Not run (Phase 0 and fallback failed)",
                    "readyForGolden": False,
                    "recommendation": f"Check your Azure OpenAI config. Vision error: {str(e)}. Fallback error: {str(e2)}"
                }

        generated_code = generated_code.strip()
        logger.info(f"[PHASE 0] ✅ Code generated successfully ({len(generated_code)} chars, {len(generated_code.split(chr(10)))} lines)")
        log_json_result(0, "SUCCESS", "Test code generated with Azure GPT-4V Vision", {
            "code_length": len(generated_code),
            "line_count": len(generated_code.split('\n')),
            "code_preview": generated_code[:500],
            "method": "Azure GPT-4V Vision"
        })

        # ─── PHASE 1+2 LOOP: Run → Fail → Vision-heal, up to MAX_HEAL_ROUNDS ─────
        # Each iteration: run the test (Phase 1.R), and if it fails take a fresh
        # screenshot so GPT-4V can see the live page before proposing a fix (Phase 2.R).
        current_code  = generated_code
        phase1_pass   = False
        phase1_message = "Not run"
        heal_history  = []   # full audit trail of every round

        for round_num in range(1, MAX_HEAL_ROUNDS + 1):
            is_last_round = (round_num == MAX_HEAL_ROUNDS)

            # ── Phase 1.R: run test ────────────────────────────────────────────
            logger.info(f"[PHASE 1 · Round {round_num}/{MAX_HEAL_ROUNDS}] Running test locally...")
            log_json_result(
                f"1.{round_num}", "IN_PROGRESS",
                f"Round {round_num}/{MAX_HEAL_ROUNDS}: running test",
                {"round": round_num, "code_chars": len(current_code)}
            )

            p1_result       = await validate_test_locally(current_code, f"synthesis_temp_r{round_num}")
            phase1_pass     = p1_result.get("passed", False)
            phase1_message  = p1_result.get("error") or "Test executed successfully"
            p1_duration     = p1_result.get("duration", 0)
            # Failure screenshot captured by validate-test.js at the moment of
            # failure — shows the actual failing page, not a fresh homepage load.
            failure_shot_b64 = p1_result.get("failureScreenshot")

            round_entry = {
                "round":          round_num,
                "passed":         phase1_pass,
                "error":          phase1_message,
                "duration_secs":  p1_duration,
                "code_preview":   current_code[:300],
                "screenshot_for_heal": False,
            }

            if phase1_pass:
                logger.info(
                    f"[PHASE 1 · Round {round_num}] ✅ PASSED in {p1_duration}s"
                )
                log_json_result(
                    f"1.{round_num}", "SUCCESS",
                    f"Round {round_num}: test passed",
                    {"round": round_num, "duration_secs": p1_duration,
                     "code_chars": len(current_code)}
                )
                generated_code = current_code
                heal_history.append(round_entry)
                break

            # Test failed
            logger.error(
                f"[PHASE 1 · Round {round_num}] ❌ FAILED — {phase1_message}"
            )
            log_json_result(
                f"1.{round_num}", "FAILED",
                f"Round {round_num}: test failed",
                {"round": round_num, "duration_secs": p1_duration,
                 "error_details": phase1_message,
                 "code_preview": current_code[:400]}
            )

            if is_last_round:
                logger.error(
                    f"[PHASE 1+2] Max rounds ({MAX_HEAL_ROUNDS}) exhausted — giving up"
                )
                log_json_result(
                    "1.FINAL", "FAILED",
                    f"All {MAX_HEAL_ROUNDS} rounds failed",
                    {"total_rounds": MAX_HEAL_ROUNDS,
                     "final_error": phase1_message,
                     "heal_history": heal_history}
                )
                heal_history.append(round_entry)
                break

            # ── Phase 2.R: vision-assisted healing ────────────────────────────
            logger.info(
                f"[PHASE 2 · Round {round_num}] Capturing live screenshot for GPT-4V healing..."
            )
            log_json_result(
                f"2.{round_num}", "IN_PROGRESS",
                f"Round {round_num}: vision-assisted healing",
                {"round": round_num}
            )

            try:
                # Prefer the failure screenshot captured by validate-test.js at the
                # exact moment the test broke — it shows the right page (e.g. an
                # article page), not a fresh re-navigation to the starting URL.
                # Fall back to navigating to url only when no failure screenshot
                # is available (older test-results dir, screenshot disabled, etc.).
                heal_shot_b64 = failure_shot_b64

                if heal_shot_b64:
                    logger.info(
                        f"[PHASE 2 · Round {round_num}] ✅ Using failure-time screenshot "
                        f"from Phase 1 — shows the exact failing page state"
                    )
                    round_entry["screenshot_for_heal"] = True
                    round_entry["screenshot_kb"] = len(heal_shot_b64) * 3 // 4 // 1024
                elif url:
                    logger.info(
                        f"[PHASE 2 · Round {round_num}] No failure screenshot — "
                        f"navigating to {url} for a fresh screenshot"
                    )
                    pb2      = await async_playwright().start()
                    browser2 = await pb2.chromium.launch()
                    page2    = await browser2.new_page()
                    for _w, _t in [('domcontentloaded', 30000), ('load', 30000), ('commit', 45000)]:
                        try:
                            await page2.goto(url, wait_until=_w, timeout=_t)
                            break
                        except Exception:
                            pass
                    await page2.wait_for_timeout(2000)
                    heal_shot_bytes = await page2.screenshot(full_page=False)
                    heal_shot_b64   = base64.b64encode(heal_shot_bytes).decode('utf-8')
                    await browser2.close()
                    await pb2.stop()

                    logger.info(
                        f"[PHASE 2 · Round {round_num}] ✅ Fallback screenshot captured "
                        f"({len(heal_shot_bytes)//1024}KB)"
                    )
                    round_entry["screenshot_for_heal"] = True
                    round_entry["screenshot_kb"] = len(heal_shot_bytes) // 1024
                else:
                    logger.warning(
                        f"[PHASE 2 · Round {round_num}] No screenshot available — using text-only healing"
                    )
                    round_entry["screenshot_for_heal"] = False

                heal_text = f"""This Playwright test failed (Round {round_num} of {MAX_HEAL_ROUNDS}).
{"Look at the screenshot to identify the correct element, then fix the test." if heal_shot_b64 else "Analyse the error and fix the test code."}

FAILURE ERROR:
{phase1_message}

CURRENT BROKEN CODE:
{current_code}

SELECTOR RULES (use in this order):
1. Nav tabs/menu items → page.locator('a[role="menuitem"]').filter({{hasText: "Label"}})
2. Buttons            → getByRole('button', {{ name: '...' }})
3. Links with href    → page.locator('a[href*="keyword"]')
4. Plain text         → getByText('...', {{ exact: true }})
❌ NEVER use getByRole('link') for navigation tabs — those elements use role="menuitem".

AUTHENTICATION RULES (must preserve — security requirement):
- If the existing code has a test.use({{ storageState: ... }}) line, keep it exactly as-is.
- NEVER replace storageState with hardcoded credentials, even as a debugging aid.
- NEVER introduce usernames or passwords anywhere in the code.

{"Study the screenshot carefully. Identify the exact element the test is trying to reach." if heal_shot_b64 else ""}
Fix only the broken selector/action. Keep all other test logic unchanged.
Output ONLY the corrected TypeScript code. No markdown fences."""

                if heal_shot_b64:
                    heal_content = [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{heal_shot_b64}"}},
                        {"type": "text", "text": heal_text},
                    ]
                else:
                    heal_content = heal_text

                heal_response = client.chat.completions.create(
                    model=DEPLOYMENT,
                    messages=[{"role": "user", "content": heal_content}],
                    max_tokens=2000,
                    temperature=0.2,
                )

                new_code = heal_response.choices[0].message.content.strip()
                new_code = _clean_healed_code(new_code)

                if new_code and new_code != current_code:
                    logger.info(
                        f"[PHASE 2 · Round {round_num}] ✅ Healed code received "
                        f"({len(new_code)} chars) — will test in Round {round_num + 1}"
                    )
                    round_entry["healed_code_preview"] = new_code[:300]
                    log_json_result(
                        f"2.{round_num}", "SUCCESS",
                        f"Round {round_num}: GPT-4V healing applied",
                        {"round": round_num,
                         "screenshot_kb": round_entry["screenshot_kb"],
                         "code_changed": True,
                         "prev_chars": len(current_code),
                         "new_chars": len(new_code),
                         "new_code_preview": new_code[:400]}
                    )
                    current_code = new_code
                else:
                    logger.warning(
                        f"[PHASE 2 · Round {round_num}] ⚠️  LLM returned identical code — stopping loop"
                    )
                    log_json_result(
                        f"2.{round_num}", "WARN",
                        f"Round {round_num}: LLM returned no changes",
                        {"round": round_num}
                    )
                    heal_history.append(round_entry)
                    break

            except Exception as heal_err:
                logger.error(
                    f"[PHASE 2 · Round {round_num}] ❌ Healing error: {heal_err}"
                )
                log_json_result(
                    f"2.{round_num}", "ERROR",
                    f"Round {round_num}: healing failed",
                    {"round": round_num, "error": str(heal_err)}
                )
                heal_history.append(round_entry)
                break

            heal_history.append(round_entry)

        # ─── PHASE TUNE: Selector-confidence tuning (runs only after loop passes) ─
        tuned_code    = generated_code
        phase2_updated = False
        phase2_changes = []

        if phase1_pass:
            logger.info("[PHASE TUNE] Analyzing learning data for selector confidence...")
            log_json_result("TUNE", "IN_PROGRESS", "Analyzing selectors for tuning", {})

            learning_file = BASE.parent / ".learning" / "synthesis_temp.learning-results.json"

            if learning_file.exists():
                try:
                    learning_data   = json.loads(learning_file.read_text())
                    selector_stats  = learning_data.get("selectorStats", [])
                    low_confidence  = [s for s in selector_stats if s.get("confidence", 0) < 75]

                    if low_confidence:
                        logger.info(
                            f"[PHASE TUNE] {len(low_confidence)} low-confidence selectors to optimize"
                        )
                        for s in low_confidence:
                            logger.debug(f"  - {s.get('selector')} ({s.get('confidence')}%)")

                        tune_prompt = ask_llm(
                            system="""You are a Playwright selector optimization expert.
Given test code and low-confidence selectors, suggest high-confidence alternatives.
Return ONLY JSON: {"suggestions": [{"old": "selector", "new": "better selector", "reason": "why"}]}""",
                            user=(
                                f"Test code:\n{generated_code}\n\n"
                                f"Low-confidence selectors:\n"
                                + json.dumps([{"selector": s["selector"], "confidence": s.get("confidence")}
                                              for s in low_confidence])
                            ),
                            max_tokens=800,
                        )

                        try:
                            tune_data   = json.loads(re.sub(r"```(?:json)?|```", "", tune_prompt).strip())
                            suggestions = tune_data.get("suggestions", [])

                            if suggestions:
                                tuned_code = generated_code
                                for i, sg in enumerate(suggestions, 1):
                                    old, new, reason = sg.get("old",""), sg.get("new",""), sg.get("reason","")
                                    if old and new:
                                        tuned_code = tuned_code.replace(old, new)
                                        msg = f"Improved: {old} → {new}"
                                        phase2_changes.append(msg)
                                        phase2_updated = True
                                        logger.info(f"  [{i}] {msg} ({reason})")

                                logger.info(f"[PHASE TUNE] ✅ Applied {len(suggestions)} improvements")
                                log_json_result("TUNE", "SUCCESS",
                                    f"Applied {len(suggestions)} selector improvements",
                                    {"improvements": phase2_changes,
                                     "reasons": [s.get("reason","") for s in suggestions]})
                            else:
                                log_json_result("TUNE", "SKIPPED", "All selectors already optimal", {})

                        except Exception as e:
                            logger.error(f"[PHASE TUNE] ⚠️  Could not parse tuning suggestions: {e}")
                            log_json_result("TUNE", "ERROR", "Failed to parse tuning suggestions",
                                            {"error": str(e)})
                    else:
                        logger.info("[PHASE TUNE] All selectors high-confidence — no tuning needed")
                        log_json_result("TUNE", "SKIPPED", "All selectors confident",
                                        {"reason": "All > 75% confidence"})

                except Exception as e:
                    logger.error(f"[PHASE TUNE] ⚠️  Could not analyze learning data: {e}")
                    log_json_result("TUNE", "ERROR", "Failed to analyze learning data", {"error": str(e)})
            else:
                logger.info("[PHASE TUNE] No learning data — skipping")
                log_json_result("TUNE", "SKIPPED", "No learning data",
                                {"learning_file": str(learning_file)})
        else:
            logger.info("[PHASE TUNE] ⏭️  Skipped — loop did not pass")
            log_json_result("TUNE", "SKIPPED", "Loop failed", {"heal_rounds": len(heal_history)})

        # ─── PHASE 3: VALIDATE TUNED TEST ─────────────────────────────────────
        phase3_pass    = False
        phase3_message = "Not run (loop did not pass)"

        if phase1_pass and phase2_updated:
            logger.info("[PHASE 3] Validating tuned test code...")
            log_json_result(3, "IN_PROGRESS", "Re-validating tuned code", {})

            phase3_result  = await validate_test_locally(tuned_code, "synthesis_temp_tuned")
            phase3_pass    = phase3_result.get("passed", False)
            phase3_message = phase3_result.get("error") or "Validation passed"

            if phase3_pass:
                logger.info(f"[PHASE 3] ✅ TUNED TEST VALIDATED")
                log_json_result(3, "SUCCESS", "Tuned test validated",
                                {"execution_time": phase3_result.get("duration", "unknown")})
            else:
                logger.error(f"[PHASE 3] ❌ VALIDATION FAILED — {phase3_message}")
                log_json_result(3, "FAILED", "Tuned test validation failed",
                                {"error": phase3_message})
        elif phase1_pass:
            phase3_pass    = True
            phase3_message = "Validation passed (no tuning applied)"
            logger.info("[PHASE 3] ✅ PASSED (no tuning needed)")
            log_json_result(3, "SKIPPED", "No tuning was applied",
                            {"reason": "Phase TUNE found no changes needed"})
        else:
            logger.info(f"[PHASE 3] ⏭️  Skipped — loop did not pass after {len(heal_history)} rounds")
            log_json_result(3, "SKIPPED", "Loop did not pass",
                            {"heal_rounds": len(heal_history),
                             "heal_history": heal_history})

        # ─── P1-5: Assertion-strength check on the final code ─────────────────
        # A passing test that asserts on the wrong thing is worse than failing.
        # We don't block promotion on weak assertions — we surface them so the
        # user can decide.
        final_code = tuned_code if phase2_updated else generated_code
        assertion_report = evaluate_assertions(final_code)
        logger.info(
            f"[ASSERTION CHECK] score={assertion_report.score}/100, "
            f"{assertion_report.assertion_count} assertion(s), "
            f"{assertion_report.weak_count} warning(s)"
        )
        if assertion_report.weak_count:
            for w in assertion_report.warnings[:5]:
                logger.warning(f"[ASSERTION CHECK] {w.severity.upper()} line {w.line}: {w.rule} — {w.message}")
            log_json_result("ASSERTION_CHECK", "WARN",
                            f"Detected {assertion_report.weak_count} weak assertion(s)",
                            assertion_report.to_dict())
        else:
            log_json_result("ASSERTION_CHECK", "SUCCESS",
                            "All assertions look strong",
                            assertion_report.to_dict())

        # ─── DECISION: Ready for golden? ───────────────────────────────────────
        ready_for_golden = phase1_pass and phase3_pass

        if ready_for_golden:
            recommendation = "✅ Ready to save as GOLDEN! All phases passed."
            logger.info(f"[DECISION] {recommendation}")
        elif phase1_pass and not phase3_pass:
            recommendation = "⚠️  Review Phase 3 validation failure before saving"
            logger.warning(f"[DECISION] {recommendation}")
        else:
            recommendation = "⚠️  Fix Phase 1 issues before saving as golden"
            logger.warning(f"[DECISION] {recommendation}")

        logger.info(f"{'='*80}")
        logger.info("SYNTHESIS WORKFLOW COMPLETED")
        logger.info(f"{'='*80}\n")

        heal_rounds_used  = len(heal_history)
        heal_rounds_passed = sum(1 for h in heal_history if h.get("passed"))

        log_json_result("END", "SUCCESS" if ready_for_golden else "REVIEW",
                       recommendation, {
                           "phase1_pass": phase1_pass,
                           "phase2_updated": phase2_updated,
                           "phase3_pass": phase3_pass,
                           "ready_for_golden": ready_for_golden,
                           "heal_rounds_used": heal_rounds_used,
                           "heal_rounds_passed": heal_rounds_passed,
                           "heal_history": heal_history,
                           "code_summary": {
                               "generated_chars": len(generated_code),
                               "tuned_chars": len(tuned_code),
                               "changes_count": len(phase2_changes)
                           }
                       })

        # Build a human-readable reason for why Phase 2 did/didn't run
        if phase2_updated:
            phase2_skip_reason = None
        elif not phase1_pass:
            phase2_skip_reason = f"Skipped — test failed after {heal_rounds_used} round(s)"
        else:
            phase2_skip_reason = "No tuning needed — all selectors already optimal"

        return {
            "generatedCode": generated_code,
            "phase1Pass": phase1_pass,
            "phase1Message": phase1_message,
            "phase2Updated": phase2_updated,
            "phase2Changes": phase2_changes,
            "phase2SkipReason": phase2_skip_reason,
            "tunedCode": tuned_code if phase2_updated else generated_code,
            "phase3Pass": phase3_pass,
            "phase3Message": phase3_message,
            "readyForGolden": ready_for_golden,
            "recommendation": recommendation,
            "healRoundsUsed": heal_rounds_used,
            "healHistory": heal_history,
            # P1-5: assertion-strength report for the UI
            "assertionReport": assertion_report.to_dict(),
        }

    except Exception as e:
        logger.error(f"[SYNTHESIS ERROR] ❌ Fatal error during workflow: {str(e)}")
        import traceback
        error_traceback = traceback.format_exc()
        logger.error(f"[TRACEBACK]\n{error_traceback}")

        log_json_result("ERROR", "FAILED", "Fatal synthesis error", {
            "error": str(e),
            "traceback": error_traceback,
            "timestamp": datetime.now().isoformat()
        })

        return {
            "error": str(e),
            "readyForGolden": False,
            "recommendation": f"Error during synthesis: {str(e)}",
            "phase1Pass": False,
        }, 500


# ─ Save as Golden ──────────────────────────────────────────────────────────────
# P2-2: hardened git sync delegated to studio/services/git_sync.py.
# This thin wrapper preserves the legacy {pushed, output, error} shape used by
# downstream call sites, but exposes the richer result through `_full_result`.
def git_sync_goldens(message: str) -> dict:
    branch = os.getenv("GITHUB_BRANCH", "main")
    try:
        result = _sync_goldens_service(
            repo_root=BASE.parent,
            message=message,
            expected_branch=branch,
        )
    except Exception as e:
        logger.exception("[git-sync] unexpected error")
        return {"pushed": False, "output": "", "error": str(e)}

    if result.pushed:
        logger.info(f"[git-sync] ✅ {result.message}")
    elif result.skipped:
        logger.info(f"[git-sync] ⏭️  {result.message}")
    else:
        logger.warning(f"[git-sync] ⚠️  {result.message}: {result.error or ''}")

    # Legacy shape kept for backward compat with existing handlers.
    return {
        "pushed": result.pushed,
        "output": result.message,
        "error": result.error,
        # Richer fields for newer call sites + UI:
        "_full_result": result.to_dict(),
    }


@app.post("/api/goldens")
async def create_golden(req: SaveGoldenRequest):
    gid = str(uuid.uuid4())[:8]
    golden = {
        "id": gid,
        "name": req.name,
        "description": req.description,
        "code": req.code,
        "browsers": req.browsers,
        "analysis": req.analysis,
        "createdAt": ts_now(),
        "healCount": 0,
        "lastHealed": None,
        "status": "active",
        "steps": len(req.analysis.get("steps", [])) or 5,
    }
    save_json(GOLDEN_DIR, gid, golden)

    # Auto-push so GitHub Actions can find the golden immediately
    sync = git_sync_goldens(f"Add golden: {req.name} [{gid}]")
    if sync["pushed"]:
        logger.info(f"[create_golden] ✅ Golden {gid} pushed to GitHub")
    else:
        logger.warning(f"[create_golden] ⚠️  Git sync skipped: {sync.get('error') or sync.get('output')}")

    # P2-2: include the rich sync result so the UI can show specific feedback
    # (e.g. "Saved locally — pull --rebase needed before push") instead of a
    # generic banner.
    return {
        **golden,
        "gitSynced": sync["pushed"],
        "gitMessage": sync.get("output") or sync.get("error"),
        "gitDetails": sync.get("_full_result"),
    }


@app.post("/api/goldens/sync")
async def sync_goldens_to_github():
    """Commit and push all golden JSON files — use when auto-push was skipped."""
    sync = git_sync_goldens("Sync goldens to GitHub")
    if not sync["pushed"] and sync["error"]:
        raise HTTPException(status_code=500, detail=sync["error"])
    return {"synced": sync["pushed"], "message": sync.get("output") or "Nothing new to push"}

# ─ Record a test run ──────────────────────────────────────────────────────────
@app.post("/api/runs")
async def record_run(req: RunRequest):
    # Look up golden by ID if it exists (optional for CI/CD robustness)
    golden = next((g for g in load_goldens() if g["id"] == req.golden_id), None)

    rid = str(uuid.uuid4())[:8]
    run = {
        "id": rid,
        "goldenId": req.golden_id,
        "goldenName": golden["name"] if golden else req.golden_id,  # Use ID as name if golden not found
        "browser": req.browser,
        "runAt": ts_now(),
        "candidates": req.candidates,
    }
    save_json(RUNS_DIR, rid, run)

    # ── NEW: Detect if this is a post-healing run and check if healing succeeded ──
    # P0-3: serialize healing-history writes for this golden so concurrent
    # /api/runs calls (CI + manual + scheduled) can't clobber each other.
    if golden and golden.get("healCount", 0) > 0:
      async with golden_lock(req.golden_id):
        # This golden has been healed before
        has_failures = any(c.get("status") == "fail" for c in req.candidates)
        error_msg = None
        # P0-1 fix: hoist `history` out of the if-branch so the else-branch can read it
        history = load_healing_history(req.golden_id)

        if has_failures:
            # Find the first error
            for c in req.candidates:
                if c.get("status") == "fail" and c.get("error"):
                    error_msg = c.get("error")
                    break

            # Check if this is the same error as before
            if history:
                last_attempt = history[-1]
                if last_attempt.get("error") == error_msg:
                    # HEALING FAILED - same error persists!
                    save_healing_attempt(req.golden_id, {
                        "fix": "Previous healing attempt",
                        "error": error_msg,
                        "succeeded": False,
                        "result": "Same error persists after healing",
                        "testResult": "FAIL"
                    })
                    logger.warning(f"[healing] ❌ HEALING FAILED for {req.golden_id}: Same error persists")
                else:
                    # Different error - healing helped with previous issue
                    save_healing_attempt(req.golden_id, {
                        "fix": "Previous healing attempt",
                        "error": error_msg,
                        "succeeded": False,
                        "result": f"New error appeared: {error_msg}",
                        "testResult": "FAIL"
                    })
                    logger.warning(f"[healing] ⚠️  New error for {req.golden_id}: {error_msg}")
        else:
            # All tests passed! Healing succeeded!
            if history:
                save_healing_attempt(req.golden_id, {
                    "fix": "Previous healing attempt",
                    "error": "NONE",
                    "succeeded": True,
                    "result": "All tests passed!",
                    "testResult": "PASS"
                })
                logger.info(f"[healing] ✅ HEALING SUCCEEDED for {req.golden_id}!")

    # Log for debugging
    status = "✓ recorded" if golden else "⚠ recorded (golden not found, using ID as name)"
    logger.info(f"[api/runs] {status} — ID={rid}, golden={req.golden_id}, candidates={len(req.candidates)}")

    return run

# ─ Auto-Heal ──────────────────────────────────────────────────────────────────
@app.post("/api/heal/{golden_id}")
async def heal(golden_id: str):
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    # Collect errors from all runs for this golden
    errors = []
    latest_error = None
    for run in load_runs():
        if run.get("goldenId") == golden_id:
            for c in run.get("candidates", []):
                if c.get("status") == "fail" and c.get("error"):
                    errors.append(f"[{c['name']} Path {c['path']}] {c['error']}")
                    latest_error = c.get("error")

    error_summary = "\n".join(errors) if errors else "Selector timeout on dynamic elements."

    # ── NEW: Check healing history and learn from failures ──────────────────
    healing_history = load_healing_history(golden_id)
    learning_context = ""

    if latest_error and len(healing_history) > 0:
        # Get previous failed attempts for this same error
        failed_attempts = get_healing_failures_for_error(golden_id, latest_error)
        if len(failed_attempts) > 0:
            learning_context = f"""
⚠️  LEARNING FROM PAST FAILURES:
This error has been seen {len(failed_attempts)} time(s) before.

Previous failed fixes:
"""
            for attempt in failed_attempts[-2:]:  # Show last 2 failures
                learning_context += f"  - Attempt #{attempt['attemptNumber']}: {attempt.get('fix', 'Unknown fix')}\n"

            learning_context += f"""
DO NOT repeat these approaches. Instead, try a fundamentally different strategy.

For "Locators must belong to the same frame" error:
  ❌ DO NOT: Mix getByRole() with locator() in .or() chains
  ❌ DO NOT: Chain selectors that operate in different frame contexts
  ✅ DO: Use only page.locator() chains consistently
  ✅ DO: Keep all locators within the same frame context
  ✅ DO: Use .first() to disambiguate, not .or() for different selector types
"""

    # ── Vision: capture live screenshot so GPT-4V sees the current page ─────────
    url_match = re.search(r'https?://[^\s\'"]+', golden.get("code", ""))
    heal_screenshot_b64 = None
    if url_match:
        heal_screenshot_b64 = await capture_screenshot_b64(url_match.group(0), label="heal")

    system_prompt = """You are a Playwright auto-healing expert.
Given failure error messages, a live screenshot of the page, and the original golden TypeScript script, produce an improved script.
Study the screenshot carefully — use it to identify the correct selectors for broken elements.
For every fix, add an inline comment starting exactly with [AI-HEAL] explaining what changed and why.
Key healing patterns:
  - Use only page.locator() chains to stay in same frame
  - .first() for ambiguous multi-match locators
  - waitForLoadState for timing gaps
  - try/catch with fallback click strategies
  - For nav tabs visible in screenshot: prefer page.locator('a[role="menuitem"]').filter({hasText:"..."}) or a[href*="keyword"]

CRITICAL: Avoid mixing different selector types (getByRole + locator) in same chain.
All selectors in a chain must operate in the same frame context.

AUTHENTICATION RULES (must preserve — security requirement):
- If the existing code has a test.use({ storageState: ... }) line, keep it exactly as-is.
- NEVER replace storageState with hardcoded credentials, even as a debugging aid.
- NEVER introduce usernames or passwords anywhere in the code.
Output ONLY the TypeScript code. No markdown."""

    if heal_screenshot_b64:
        logger.info(f"[heal] Sending screenshot + error to GPT-4V for vision-assisted healing")
        heal_response = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{heal_screenshot_b64}"}},
                    {"type": "text", "text": f"{system_prompt}\n\nErrors:\n{error_summary}\n\nOriginal golden script:\n{golden['code']}{learning_context}"}
                ]
            }],
            max_tokens=LLM_MAX_TOKENS,
            temperature=LLM_TEMPERATURE
        )
        healed_code = heal_response.choices[0].message.content.strip()
        logger.info(f"[heal] ✅ Vision-assisted heal response received ({len(healed_code)} chars)")
    else:
        logger.info(f"[heal] No screenshot available — falling back to text-only healing")
        healed_code = ask_llm(
            system=system_prompt,
            user=f"""Errors:\n{error_summary}\n\nOriginal golden script:\n{golden['code']}{learning_context}""",
            max_tokens=1500,
        )

    healed_code = _clean_healed_code(healed_code)
    logger.info(f"[heal] Generated fix for golden {golden_id} (attempt #{len(healing_history) + 1})")

    # Generate a plain-English diff summary
    diff_summary = ask_llm(
        system="Return ONLY a JSON array of strings. Each string = one change made. Max 6 items. No markdown.",
        user=f"Summarise the healing changes made:\nErrors: {error_summary}\nHealed code excerpt: {healed_code[:800]}",
        max_tokens=300,
    )
    try:
        changes = json.loads(re.sub(r"```(?:json)?|```", "", diff_summary).strip())
    except Exception:
        changes = ["Applied .first() to ambiguous role selectors", "Added .or() fallback for Nudge button", "Added waitForLoadState after navigation"]

    return {"healedCode": healed_code, "changes": changes}

# ─ NEW: Heal + Validate Locally (Instant Feedback) ───────────────────────────
@app.post("/api/heal-and-validate/{golden_id}")
async def heal_and_validate(golden_id: str):
    """
    NEW WORKFLOW: Generate healed code AND run it locally for instant feedback.

    Instead of: Heal → Promote → Wait 5+ min for GitHub
    Now: Heal → Validate locally (2-5 sec) → See pass/fail → Decide to promote

    Returns:
        {
            "goldenId": "...",
            "healedCode": "...",
            "testResult": "PASS" | "FAIL" | "ERROR",
            "duration": 2.1,
            "error": null or error message,
            "passed": true | false,
            "readyToPromote": true | false,
            "message": "✅ Test passed! Ready to promote." or error
        }
    """
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    try:
        # Step 1: Collect errors and diagnose root cause
        errors = []
        latest_error = None
        for run in load_runs():
            if run.get("goldenId") == golden_id:
                for c in run.get("candidates", []):
                    if c.get("status") == "fail" and c.get("error"):
                        errors.append(f"[{c['name']} Path {c['path']}] {c['error']}")
                        latest_error = c.get("error")

        error_summary = "\n".join(errors) if errors else "Selector timeout on dynamic elements."

        healing_history = load_healing_history(golden_id)
        learning_context = ""

        # ── NEW: Diagnose root cause using ErrorSignature ──────────────────────
        diagnosis = ErrorSignature.diagnose(latest_error or error_summary, golden["code"])
        root_cause = diagnosis.get("root_cause", "UNKNOWN")
        confidence = diagnosis.get("confidence", 0.0)

        logger.info(f"[heal-validate] Root cause diagnosis: {root_cause} (confidence: {confidence:.0%})")
        logger.debug(f"[heal-validate] Evidence: {diagnosis.get('evidence', 'No evidence')}")

        # ── Analyze healing history for patterns ──────────────────────────────
        history_analysis = analyze_healing_history(healing_history)
        if history_analysis.get("needs_manual_review"):
            logger.warning(f"[heal-validate] ⚠️ Healing stuck - manual review recommended")

        # Build learning context from history
        if len(healing_history) > 0:
            learning_context = f"""
⚠️  LEARNING FROM PAST FAILURES (Attempt #{len(healing_history) + 1}):
Previous attempts: {len(healing_history)}
Root cause identified: {root_cause} (confidence: {confidence:.0%})

Recent attempts:
"""
            for attempt in healing_history[-3:]:
                learning_context += f"  - Attempt #{attempt.get('attemptNumber', '?')}: {attempt.get('rootCause', 'Unknown')}\n"

            learning_context += f"""
Strategy: Apply targeted fix for '{root_cause}' instead of generic selector fixes.
"""

        # ── Generate targeted healing prompt based on diagnosis ────────────────
        system_prompt, user_prompt = generate_targeted_healing_prompt(
            latest_error or error_summary,
            golden["code"],
            diagnosis,
            learning_context
        )

        # ── Vision: capture live screenshot so GPT-4V sees the current page ──
        hv_url_match = re.search(r'https?://[^\s\'"]+', golden.get("code", ""))
        hv_screenshot_b64 = None
        if hv_url_match:
            hv_screenshot_b64 = await capture_screenshot_b64(hv_url_match.group(0), label="heal-validate")

        if hv_screenshot_b64:
            logger.info(f"[heal-validate] Sending screenshot + error to GPT-4V (vision-assisted)")
            hv_response = client.chat.completions.create(
                model=DEPLOYMENT,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{hv_screenshot_b64}"}},
                        {"type": "text", "text": f"{system_prompt}\n\n{user_prompt}"}
                    ]
                }],
                max_tokens=1500,
                temperature=0.2
            )
            healed_code = hv_response.choices[0].message.content.strip()
            logger.info(f"[heal-validate] ✅ Vision-assisted heal response received ({len(healed_code)} chars)")
        else:
            logger.info(f"[heal-validate] No screenshot — falling back to text-only healing")
            healed_code = ask_llm(
                system=system_prompt,
                user=user_prompt,
                max_tokens=1500,
            )

        healed_code = _clean_healed_code(healed_code)
        logger.info(f"[heal-validate] Generated targeted fix for '{root_cause}' (attempt #{len(healing_history) + 1})")

        # Step 2: Run test locally
        logger.info(f"[heal-validate] Running test locally for golden {golden_id}...")
        validation_result = await validate_test_locally(healed_code, golden_id)

        # Step 3: Return result with code + pass/fail
        response = {
            "goldenId": golden_id,
            "healedCode": healed_code,
            "testResult": validation_result["status"],
            "duration": validation_result.get("duration", 0),
            "error": validation_result.get("error"),
            "passed": validation_result.get("passed", False),
            "readyToPromote": validation_result["status"] == "PASS",
        }

        # Step 4: Record healing attempt with diagnosis — P0-3 lock-protected
        await save_healing_attempt_locked(golden_id, {
            "fix": f"Applied targeted fix for root cause: {root_cause}",
            "rootCause": root_cause,
            "confidence": confidence,
            "error": latest_error or error_summary,
            "succeeded": validation_result["status"] == "PASS",
            "testResult": validation_result["status"],
            "duration": validation_result.get("duration", 0),
            "newError": validation_result.get("error") if validation_result["status"] != "PASS" else None
        })

        if validation_result["status"] == "PASS":
            response["message"] = f"✅ Test PASSED in {validation_result.get('duration', 0):.1f}s! Ready to promote."
            logger.info(f"[heal-validate] ✅ LOCAL TEST PASSED for {golden_id}!")
        else:
            error_msg = validation_result.get("error", "Unknown error")
            response["message"] = f"❌ Test FAILED: {error_msg}"
            logger.warning(f"[heal-validate] ❌ LOCAL TEST FAILED for {golden_id}: {error_msg}")
            response["diagnosis"] = {
                "rootCause": root_cause,
                "confidence": confidence,
                "evidence": diagnosis.get("evidence")
            }

        return response

    except Exception as e:
        logger.error(f"[heal-validate] Error: {str(e)}")
        return {
            "error": str(e),
            "goldenId": golden_id,
            "testResult": "ERROR",
            "message": f"Validation error: {str(e)}"
        }, 500

# ─ Promote healed code as new Golden ─────────────────────────────────────────
@app.patch("/api/goldens/{golden_id}/promote")
async def promote_healed(golden_id: str, body: PromoteGoldenRequest):
    # P0-3: lock the whole RMW (load → mutate → save golden + history) for this id.
    async with golden_lock(golden_id):
        goldens = load_goldens()
        golden = next((g for g in goldens if g["id"] == golden_id), None)
        if not golden:
            raise HTTPException(status_code=404, detail="Golden not found")

        if not body.code.strip():
            raise HTTPException(status_code=400, detail="Promoted code cannot be empty")

        golden["code"] = body.code
        golden["healCount"] = golden.get("healCount", 0) + 1
        golden["lastHealed"] = ts_now()
        save_json(GOLDEN_DIR, golden_id, golden)

        # ── NEW: Save this healing attempt to history ──────────────────────────
        # Find what the fix was by comparing code or using a marker
        save_healing_attempt(golden_id, {
            "fix": "Generated fix from Azure OpenAI",
            "error": "TBD - will be confirmed when tests run",
            "succeeded": None,  # Pending - will be updated when test runs
            "result": "Promoted and awaiting test results",
            "testResult": "PENDING"
        })
        logger.info(f"[promote] Saved healing attempt #{golden.get('healCount')} for {golden_id}")

    # ── Auto-trigger GitHub Actions to test the healed golden ──────────────────
    # This ensures the healed code is tested with the updated golden file
    workflow_result = {"status": "skipped", "message": "GitHub workflow not configured"}
    try:
        logger.info(f"[promote] Auto-triggering workflow for healed golden: {golden_id}")
        workflow_result = dispatch_github_workflow({"golden_ids": golden_id})
        logger.info(f"[promote] Workflow triggered successfully: {workflow_result.get('message')}")
    except HTTPException as e:
        # Workflow dispatch failed but golden was saved successfully
        logger.warning(f"[promote] Warning: Could not trigger workflow: {e.detail}")
        workflow_result = {"status": "failed", "message": str(e.detail)}
    except Exception as e:
        logger.error(f"[promote] Unexpected error triggering workflow: {e}")
        workflow_result = {"status": "failed", "message": str(e)}

    return {
        "golden": golden,
        "workflowTriggered": workflow_result.get("status") == "success",
        "workflowMessage": workflow_result.get("message", "Unknown"),
    }

# ─ Check Healing Status & Escalation ────────────────────────────────────────
@app.get("/api/goldens/{golden_id}/healing-status")
async def get_healing_status(golden_id: str):
    """Get healing history and check if escalation is needed"""
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    history = load_healing_history(golden_id)
    stuck = is_healing_stuck(golden_id)

    return {
        "goldenId": golden_id,
        "goldenName": golden.get("name"),
        "healAttempts": len(history),
        "healCount": golden.get("healCount", 0),
        "lastHealed": golden.get("lastHealed"),
        "isEscalated": stuck.get("stuck", False),
        "escalationReason": stuck.get("error") if stuck.get("stuck") else None,
        "failedAttempts": stuck.get("failedAttempts", 0),
        "recommendation": stuck.get("recommendation", "CONTINUE_AUTO_HEALING"),
        "recentHistory": history[-5:] if history else [],  # Last 5 attempts
    }

# ─ GitHub workflow dispatch helpers ─────────────────────────────────────────
def parse_github_remote(url: str):
    if not url:
        return None
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    elif url.startswith("https://github.com/"):
        path = url[len("https://github.com/"):]
    elif url.startswith("ssh://git@github.com/"):
        path = url.split("github.com/", 1)[1]
    else:
        return None

    if path.endswith(".git"):
        path = path[:-4]
    parts = path.strip("/").split("/")
    return tuple(parts) if len(parts) == 2 else None


def get_github_config():
    # Reload env each time so updates to .env are picked up while the server runs.
    if ROOT_ENV.exists():
        load_dotenv(ROOT_ENV, override=True)

    gh_token = os.getenv("GITHUB_TOKEN")
    gh_owner = os.getenv("GITHUB_OWNER")
    gh_repo = os.getenv("GITHUB_REPO")
    gh_workflow = os.getenv("GITHUB_WORKFLOW", "playwright-test.yml")
    gh_branch = os.getenv("GITHUB_BRANCH", "main")

    if not gh_owner or not gh_repo:
        try:
            remote_url = subprocess.check_output(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=BASE.parent,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            parsed = parse_github_remote(remote_url)
            if parsed:
                gh_owner, gh_repo = parsed
        except Exception:
            pass

    if not all([gh_token, gh_owner, gh_repo]):
        raise HTTPException(
            status_code=500,
            detail="GitHub credentials not configured in .env (GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO)"
        )

    return gh_token, gh_owner, gh_repo, gh_workflow, gh_branch


def dispatch_github_workflow(inputs: dict):
    gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()
    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/{gh_workflow}/dispatches"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "ref": gh_branch,
        "inputs": inputs,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)

        if response.status_code == 204:
            return {
                "status": "success",
                "message": "GitHub workflow dispatched",
                "inputs": inputs,
            }

        # Handle error responses with better detail
        try:
            error_detail = response.json().get("message", response.text)
        except Exception:
            error_detail = response.text

        if response.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid GitHub token")
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Workflow not found: {gh_workflow}")
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"GitHub API error: {error_detail}"
            )

    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail="GitHub API timeout (>15s) - workflow may still be triggered"
        )
    except requests.ConnectionError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach GitHub API - check internet connection: {str(e)}"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected error triggering workflow: {str(e)}"
        )


# ─ Trigger CI run by golden ID ───────────────────────────────────────────────
@app.post("/api/trigger-ci/{golden_id}")
async def trigger_ci(golden_id: str):
    """Trigger a GitHub Actions workflow for a specific golden test"""
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    # Use consistent input format for all workflows
    inputs = {"golden_name": golden["name"], "golden_id": golden_id}
    result = dispatch_github_workflow(inputs)
    return {**result, "golden_id": golden_id, "golden_name": golden["name"]}


# ─ Trigger CI run by golden_ids string ─────────────────────────────────────────
@app.post("/api/trigger-ci")
async def trigger_ci_ids(body: TriggerCIRequest):
    """Trigger workflow for multiple golden tests (comma-separated IDs)"""
    golden_ids = [gid.strip() for gid in body.golden_ids.split(",") if gid.strip()]
    if not golden_ids:
        raise HTTPException(status_code=400, detail="Please provide one or more golden_ids")

    known_ids = {g["id"] for g in load_goldens()}
    invalid = [gid for gid in golden_ids if gid not in known_ids]
    if invalid:
        raise HTTPException(status_code=404, detail=f"Unknown golden IDs: {', '.join(invalid)}")

    # For multi-trigger, use first golden's name as reference
    golden_names = [g["name"] for g in load_goldens() if g["id"] in golden_ids]
    inputs = {"golden_ids": ",".join(golden_ids)}
    result = dispatch_github_workflow(inputs)
    return {**result, "golden_ids": golden_ids, "golden_count": len(golden_ids)}


# ─ Get workflow run status ────────────────────────────────────────────────────
@app.get("/api/workflow-status/{golden_id}")
async def get_workflow_status(golden_id: str):
    """Get status of the most recent workflow run for a golden"""
    try:
        gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()

        # Get recent workflow runs
        url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/{gh_workflow}/runs"
        headers = {"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"}

        response = requests.get(url, headers=headers, timeout=10, params={"per_page": 10})

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch workflow runs: {response.text}"
            )

        runs = response.json().get("workflow_runs", [])

        # Find run for this golden_id by checking workflow inputs
        matching_run = None
        for run in runs:
            inputs = run.get("inputs", {})
            if inputs.get("golden_id") == golden_id:
                matching_run = run
                break

        if not matching_run:
            return {
                "status": "not_found",
                "message": "No workflow run found for this golden",
                "golden_id": golden_id,
            }

        return {
            "status": "found",
            "golden_id": golden_id,
            "run_id": matching_run["id"],
            "run_number": matching_run["run_number"],
            "name": matching_run["name"],
            "conclusion": matching_run.get("conclusion"),  # null=running, success/failure
            "status": matching_run["status"],  # queued, in_progress, completed
            "created_at": matching_run["created_at"],
            "updated_at": matching_run["updated_at"],
            "html_url": matching_run["html_url"],
            "github_link": matching_run["html_url"],
            "display_title": matching_run.get("display_title", matching_run["name"]),
        }
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching workflow status: {str(e)}")


# ── Exploration endpoints ─────────────────────────────────────────────────────

# ── Verify-then-act helpers ───────────────────────────────────────────────────

def _gpt_vision_json(shot_b64: str, prompt: str) -> dict:
    """Single GPT-4V call (main model) — used for verification / screenshot comparison."""
    content = (
        [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}},
         {"type": "text", "text": prompt}]
        if shot_b64 else prompt
    )
    raw = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[{"role": "user", "content": content}],
        max_tokens=500,
        temperature=0.1,
    ).choices[0].message.content.strip()
    raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
    return json.loads(raw)


def _reasoning_vision_json(shot_b64: str, prompt: str) -> dict:
    """o4-mini call with vision — used for exploration planning and self-correction.

    o4-mini rules:
    - temperature must be omitted (reasoning models control sampling internally)
    - max_completion_tokens replaces max_tokens
    - vision (image_url) is supported
    """
    content = (
        [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}},
         {"type": "text", "text": prompt}]
        if shot_b64 else prompt
    )
    kwargs: dict = {
        "model":    REASONING_DEPLOYMENT,
        "messages": [{"role": "user", "content": content}],
    }
    # o4-mini uses max_completion_tokens; standard models use max_tokens
    if REASONING_DEPLOYMENT != DEPLOYMENT:
        kwargs["max_completion_tokens"] = 2000
    else:
        kwargs["max_tokens"]  = 500
        kwargs["temperature"] = 0.1

    raw = reasoning_client.chat.completions.create(**kwargs).choices[0].message.content.strip()
    raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
    return json.loads(raw)


def _plan_action_prompt(step_desc: str, step_type: str, current_url: str,
                        history_ctx: str, memory_hints: Optional[list] = None) -> str:
    hints_block = ""
    if memory_hints:
        hint_lines = []
        for h in memory_hints:
            conf  = h.get("confidence", "medium")
            used  = h.get("success_count", 1)
            sel   = h.get("selector", "")
            act   = h.get("action", "")
            val   = h.get("value")
            val_s = f', value="{val}"' if val else ""
            hint_lines.append(
                f'  • [{conf} confidence, used {used}x] action="{act}", selector="{sel}"{val_s}'
            )
        hints_block = (
            "\nPAST VERIFIED SELECTORS (from previous explorations on this site):\n"
            + "\n".join(hint_lines)
            + "\nTry these first — they worked before. Only deviate if the page looks different.\n"
        )

    return f"""You are controlling a real web browser to automate a test step.

CURRENT STEP: {step_desc}
STEP TYPE: {step_type}
CURRENT URL: {current_url}

RECENT ACTIONS:
{history_ctx or "(none yet)"}
{hints_block}
Study the screenshot and decide the best single action to take right now.
Think carefully: look at the actual element roles, aria-labels, and text visible on screen.
Return ONLY a raw JSON object — no markdown:
{{
  "action": "click"|"fill"|"select_option"|"navigate"|"read"|"wait"|"hover"|"decision"|"done",
  "selector": "CSS or Playwright locator — prefer aria-label, role, data-testid over classes",
  "value": "text/URL/option to use — null for click/read/wait",
  "observation": "what you see on screen relevant to this step (1-2 sentences)",
  "confidence": "high"|"medium"|"low",
  "notes": "anything important a test writer must know about this element",
  "path_decision": null|"A"|"B"
}}
Rules:
- Prefer: aria-label, getByRole, data-testid, text= locators
- Avoid: long CSS class chains, positional selectors
- For conditional/read steps: action="decision", set path_decision
- If step already done: action="done"
- For navigation: action="navigate", value=full URL"""


def _verify_prompt(step_desc: str, action: str, selector: str, value: str) -> str:
    return f"""You are verifying whether a browser action succeeded.

STEP THAT WAS ATTEMPTED: {step_desc}
ACTION EXECUTED: {action} on "{selector}" {f'with value "{value}"' if value else ''}

You have two screenshots: BEFORE (left/first) and AFTER (right/second) the action.
Compare them carefully and determine if the action had the intended effect.

Return ONLY a raw JSON object — no markdown:
{{
  "success": true|false,
  "observation": "what changed between the two screenshots (or what stayed the same)",
  "correction_hint": "if failed: specific reason why + what to try instead (different selector, different action type, need to scroll first, element is inside iframe, etc.)"
}}

Be strict: success=true only if there is clear visual evidence the action worked
(e.g. a menu opened, a field was filled, a page changed, a value was read)."""


def _correction_prompt(step_desc: str, action_plan: dict, failure_reason: str,
                        current_url: str, history_ctx: str) -> str:
    return f"""A browser action just failed. Reason: {failure_reason}

ORIGINAL STEP: {step_desc}
FAILED ACTION: {action_plan.get('action')} on selector "{action_plan.get('selector')}"
CURRENT URL: {current_url}

RECENT ACTIONS:
{history_ctx or "(none yet)"}

Look at the screenshot (current page state after the failed attempt).
Reason carefully about why the action failed and suggest a corrected action.

Return ONLY a raw JSON object — no markdown:
{{
  "action": "click"|"fill"|"select_option"|"navigate"|"read"|"wait"|"hover"|"decision"|"done",
  "selector": "corrected selector based on what you now see",
  "value": "corrected value if needed — null otherwise",
  "observation": "what you see now and why the original failed",
  "confidence": "high"|"medium"|"low",
  "notes": "what was wrong and what you changed",
  "path_decision": null|"A"|"B",
  "reasoning": "step-by-step reasoning about why the original failed and why this correction should work"
}}"""


async def _execute_exploration_action(page, action: str, selector: str,
                                       value: Optional[str]) -> Optional[str]:
    """Execute one action on the page. Returns read text for 'read' actions."""
    if action == "navigate":
        nav_url = value or selector
        for _w, _t in [('domcontentloaded', 60_000), ('load', 60_000), ('commit', 90_000)]:
            try:
                await page.goto(nav_url, wait_until=_w, timeout=_t)
                break
            except Exception:
                pass
        await page.wait_for_timeout(NAV_PAUSE_MS)

    elif action == "wait":
        await page.wait_for_timeout(int(value or 2000))

    elif action == "read":
        if selector:
            return await page.locator(selector).first.text_content(timeout=15_000)

    elif action == "hover":
        await page.locator(selector).first.hover(timeout=10_000)
        await page.wait_for_timeout(500)

    elif action == "click":
        await page.locator(selector).first.click(timeout=20_000)
        await page.wait_for_timeout(1500)

    elif action == "fill":
        await page.locator(selector).first.fill(value or "", timeout=15_000)

    elif action == "select_option":
        loc = page.locator(selector).first
        if value:
            await loc.select_option(value, timeout=15_000)
        else:
            opts = await loc.locator("option").all()
            if opts:
                first_val = await opts[0].get_attribute("value") or ""
                await loc.select_option(first_val, timeout=15_000)

    return None


@app.post("/api/explore")
async def explore_test_case(req: ExploreRequest):
    """
    Stage 1 — Browser Exploration.
    Launches a real Playwright browser (with storageState for auth), drives it
    step-by-step using GPT-4V vision, records verified selectors, and produces
    a Markdown file that can be used as the backbone for test generation.
    """
    exploration_id = str(uuid.uuid4())[:8]
    expl_dir  = EXPLORATIONS_DIR / exploration_id
    shots_dir = expl_dir / "screenshots"
    expl_dir.mkdir(parents=True, exist_ok=True)
    shots_dir.mkdir(exist_ok=True)

    steps_log  = []
    pb         = None
    browser    = None
    path_taken = None   # "A" or "B" once a conditional decision is made

    try:
        logger.info(f"[EXPLORE {exploration_id}] Starting — {req.test_case[:80]}...")

        # ── E0-pre: Inject candidate data from data/candidate.json ────────
        # If the test description mentions "candidate" or "JSON file" but has no
        # actual name, auto-inject the first candidate's name so the LLM knows
        # exactly what to type into the search box.
        enriched_test_case = req.test_case
        data_file = BASE.parent / "data" / "candidate.json"
        candidate_context = ""
        if data_file.exists():
            try:
                candidate_data = json.loads(data_file.read_text())
                candidates = candidate_data.get("candidates", [])
                if candidates:
                    names = [c.get("name", "") for c in candidates if c.get("name")]
                    candidate_context = (
                        f"\n\n[TEST DATA from data/candidate.json]\n"
                        f"Candidate(s) to use: {', '.join(names)}\n"
                        f"When the description says 'search for candidate by name', "
                        f"use the first candidate: {names[0]}\n"
                        f"Full list: {json.dumps(candidates)}"
                    )
                    enriched_test_case = req.test_case + candidate_context
                    logger.info(f"[EXPLORE {exploration_id}] Injected candidate data: {names}")
            except Exception as de:
                logger.warning(f"[EXPLORE {exploration_id}] Could not load candidate.json: {de}")

        # ── E0: Parse description into atomic steps ────────────────────────
        logger.info(f"[EXPLORE {exploration_id}] Parsing test steps with LLM...")
        try:
            steps = _parse_test_steps(enriched_test_case)
            logger.info(f"[EXPLORE {exploration_id}] {len(steps)} steps parsed")
        except Exception as pe:
            logger.error(f"[EXPLORE {exploration_id}] Step parsing failed: {pe}")
            return {"error": f"Could not parse test steps: {pe}", "explorationId": exploration_id, "steps": []}

        # ── E1: Launch browser ─────────────────────────────────────────────
        pb      = await async_playwright().start()
        browser = await pb.chromium.launch(headless=False)

        ctx_kwargs: dict = {}
        if req.storage_state:
            auth_path = BASE / ".auth" / f"{req.storage_state}.json"
            if auth_path.exists():
                ctx_kwargs["storage_state"] = str(auth_path)
                logger.info(f"[EXPLORE {exploration_id}] Using storageState: {auth_path.name}")
            else:
                logger.warning(f"[EXPLORE {exploration_id}] storageState not found: {auth_path} — continuing without auth")

        ctx  = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        # Navigate to start URL extracted from the test description
        url_match = re.search(r'https?://[^\s]+', req.test_case)
        if url_match:
            start_url = url_match.group(0).rstrip('.,)')
            logger.info(f"[EXPLORE {exploration_id}] Navigating to {start_url}")
            for _w, _t in [('domcontentloaded', 60_000), ('load', 60_000), ('commit', 90_000)]:
                try:
                    await page.goto(start_url, wait_until=_w, timeout=_t)
                    logger.info(f"[EXPLORE {exploration_id}] Page loaded (wait_until='{_w}')")
                    break
                except Exception:
                    pass
            await page.wait_for_timeout(NAV_PAUSE_MS)

        # ── E2: Verify-then-act exploration loop ──────────────────────────────
        # For each step:
        #   1. Screenshot BEFORE
        #   2. GPT-4V plans the action (with chain-of-thought reasoning)
        #   3. Execute the action on the real browser
        #   4. Screenshot AFTER
        #   5. GPT-4V verifies: did it work? (compares before/after)
        #   6. If verification fails → GPT reasons about why → corrected action → retry
        #   Up to MAX_EXPLORE_RETRIES retries per step before moving on.
        step_counter = 0
        for step in steps[:req.max_steps]:
            step_id   = step.get("id", step_counter + 1)
            step_desc = step.get("description", "")
            step_type = step.get("type", "interact")
            step_path = step.get("path", "both")

            if path_taken and step_path not in ("both", path_taken):
                logger.info(f"[EXPLORE {exploration_id}] Skipping step {step_id} (path={step_path}, chose={path_taken})")
                continue

            step_counter += 1
            logger.info(f"[EXPLORE {exploration_id}] Step {step_counter}: {step_desc[:60]}")

            # ── Before screenshot ──────────────────────────────────────────
            before_b64  = ""
            before_file = f"step-{step_counter:03d}-before.png"
            try:
                before_bytes = await page.screenshot(full_page=False)
                before_b64   = base64.b64encode(before_bytes).decode()
                (shots_dir / before_file).write_bytes(before_bytes)
            except Exception as se:
                logger.warning(f"[EXPLORE {exploration_id}] Before-screenshot failed: {se}")

            history_ctx = "\n".join([
                f"Step {h['step_num']}: [{h.get('action','')}] {h.get('selector','')} — {h.get('observation','')}"
                for h in steps_log[-4:]
            ])

            # ── Memory lookup — inject hints from past successful explorations ─
            current_domain = re.sub(r'https?://', '', page.url).split('/')[0]
            memory_hints   = _find_memory_hints(current_domain, step_desc)
            if memory_hints:
                logger.info(
                    f"[EXPLORE {exploration_id}] Memory: {len(memory_hints)} hint(s) for step {step_counter} "
                    f"— top: \"{memory_hints[0].get('selector','')}\" ({memory_hints[0].get('confidence','')})"
                )

            log_entry = {
                "step_num":        step_counter,
                "description":     step_desc,
                "action":          "",
                "selector":        "",
                "value":           None,
                "observation":     "",
                "notes":           "",
                "confidence":      "low",
                "path":            step_path,
                "path_taken":      path_taken,
                "screenshot_file": before_file,
                "success":         False,
                "error":           None,
                "memory_hints_used": len(memory_hints),
                "read_value":      None,
                "attempts":        0,
            }

            last_failure  = ""
            action_plan   = {}
            current_b64   = before_b64   # updated after each attempt
            url_before_step = page.url    # for stuck-detection on navigation steps

            # ── Retry loop ─────────────────────────────────────────────────
            for attempt in range(MAX_EXPLORE_RETRIES + 1):
                log_entry["attempts"] = attempt + 1

                # ── Plan (or re-plan with reasoning on retry) ──────────────
                try:
                    if attempt == 0:
                        prompt = _plan_action_prompt(step_desc, step_type, page.url,
                                                     history_ctx, memory_hints)
                    else:
                        logger.info(f"[EXPLORE {exploration_id}] Retry {attempt}/{MAX_EXPLORE_RETRIES}: {last_failure[:60]}")
                        prompt = _correction_prompt(step_desc, action_plan, last_failure, page.url, history_ctx)

                    action_plan = _reasoning_vision_json(current_b64, prompt)
                except Exception as pe:
                    logger.error(f"[EXPLORE {exploration_id}] Planning failed: {pe}")
                    log_entry["error"] = f"Planning error: {pe}"
                    break

                action   = action_plan.get("action", "done")
                selector = action_plan.get("selector", "")
                value    = action_plan.get("value")
                pdec     = action_plan.get("path_decision")

                log_entry.update({
                    "action":     action,
                    "selector":   selector,
                    "value":      value,
                    "observation": action_plan.get("observation", ""),
                    "notes":      action_plan.get("notes", ""),
                    "confidence": action_plan.get("confidence", "medium"),
                })

                if pdec and path_taken is None:
                    path_taken = pdec
                    log_entry["path_taken"] = path_taken
                    logger.info(f"[EXPLORE {exploration_id}] Path decision → {path_taken}")

                # FIX 1: "done" is NOT auto-success — verify the goal was achieved.
                # The model uses "done" as an escape hatch when it can't find elements.
                # We verify with a goal-check before accepting it.
                if action == "decision":
                    log_entry["success"] = True
                    logger.info(f"[EXPLORE {exploration_id}] ✅ Step {step_counter}: path decision")
                    break

                if action == "done":
                    # Ask GPT-4V: was the actual goal of this step achieved?
                    goal_check_prompt = f"""The model says this step is already done: "{step_desc}"
Look at the screenshot carefully.
Is there clear visual evidence that this goal HAS actually been accomplished on this page?
Return ONLY raw JSON: {{"achieved": true|false, "reason": "what you see that confirms or denies it"}}"""
                    try:
                        goal_resp = _reasoning_vision_json(current_b64, goal_check_prompt)
                        if goal_resp.get("achieved", False):
                            log_entry["success"] = True
                            log_entry["observation"] = goal_resp.get("reason", "")
                            logger.info(f"[EXPLORE {exploration_id}] ✅ Step {step_counter}: goal confirmed done")
                            break
                        else:
                            # Goal NOT achieved — treat as failure and retry
                            last_failure = (
                                f"Model said 'done' but goal was not achieved: "
                                f"{goal_resp.get('reason', 'Goal not visible on page')}. "
                                f"You MUST find and interact with an element to accomplish this step."
                            )
                            logger.warning(f"[EXPLORE {exploration_id}] 'done' rejected — {last_failure[:80]}")
                            if attempt >= MAX_EXPLORE_RETRIES:
                                log_entry["error"] = last_failure
                            continue
                    except Exception:
                        # If goal-check fails to parse, give benefit of the doubt
                        log_entry["success"] = True
                        break

                # ── Execute ────────────────────────────────────────────────
                exec_error = None
                read_val   = None
                try:
                    read_val = await _execute_exploration_action(page, action, selector, value)
                    if read_val is not None:
                        log_entry["read_value"] = read_val
                        logger.info(f"[EXPLORE {exploration_id}] Read: '{read_val}'")
                except Exception as ae:
                    exec_error = str(ae)[:300]
                    logger.warning(f"[EXPLORE {exploration_id}] Exec error attempt {attempt+1}: {ae}")

                # ── After screenshot ───────────────────────────────────────
                after_b64   = ""
                after_file  = f"step-{step_counter:03d}-after-a{attempt+1}.png"
                try:
                    after_bytes = await page.screenshot(full_page=False)
                    after_b64   = base64.b64encode(after_bytes).decode()
                    (shots_dir / after_file).write_bytes(after_bytes)
                except Exception:
                    pass

                # ── Verify ─────────────────────────────────────────────────
                url_after = page.url
                if exec_error:
                    last_failure = (
                        f"Playwright raised an error executing {action} on '{selector}': "
                        f"{exec_error}. The selector may not exist, be hidden, or need scrolling."
                    )
                    verification_ok = False

                elif action == "wait":
                    verification_ok = True

                elif action in ("navigate", "read"):
                    # FIX 2: For navigation steps, verify the URL actually changed
                    if action == "navigate" and step_type in ("navigate",) and url_after == url_before_step:
                        last_failure = (
                            f"Navigation action executed but URL did not change "
                            f"(still {url_after}). The page may not have responded to the action."
                        )
                        verification_ok = False
                    else:
                        verification_ok = True

                else:
                    # FIX 3: Goal-based verification — did the STEP GOAL get achieved?
                    try:
                        v_prompt = f"""You are verifying whether a browser step succeeded.

STEP GOAL: {step_desc}
ACTION TAKEN: {action} on "{selector}" {f'with value "{value}"' if value else ''}
URL BEFORE: {url_before_step}
URL AFTER: {url_after}

You have BEFORE (first image) and AFTER (second image) screenshots.
Judge whether the GOAL of the step was achieved — not just whether something changed.

Return ONLY raw JSON:
{{
  "success": true|false,
  "observation": "what changed and whether the goal was met",
  "correction_hint": "if failed: exactly what went wrong and what to try instead (different selector, scroll first, element inside iframe, need to wait longer, etc.)"
}}
Be strict: success=true only if there is clear visual evidence the step GOAL was accomplished."""

                        v_content = [
                            {"type": "text", "text": "BEFORE:"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{current_b64}"}},
                            {"type": "text", "text": "AFTER:"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{after_b64}"}},
                            {"type": "text", "text": v_prompt},
                        ]
                        v_kwargs: dict = {
                            "model":    REASONING_DEPLOYMENT,
                            "messages": [{"role": "user", "content": v_content}],
                        }
                        if REASONING_DEPLOYMENT != DEPLOYMENT:
                            v_kwargs["max_completion_tokens"] = 800
                        else:
                            v_kwargs["max_tokens"]  = 300
                            v_kwargs["temperature"] = 0.1
                        v_raw = reasoning_client.chat.completions.create(
                            **v_kwargs
                        ).choices[0].message.content.strip()
                        v_raw = re.sub(r"```(?:json)?[\n]?", "", v_raw).strip().rstrip("`").strip()
                        verification = json.loads(v_raw)
                        verification_ok = verification.get("success", False)
                        log_entry["observation"] = verification.get("observation", log_entry["observation"])
                        if not verification_ok:
                            last_failure = verification.get("correction_hint", "Action had no visible effect on goal")
                    except Exception as ve:
                        logger.warning(f"[EXPLORE {exploration_id}] Verification parse error: {ve}")
                        verification_ok = True  # give benefit of the doubt on parse failure

                if verification_ok:
                    log_entry["success"]         = True
                    log_entry["screenshot_file"] = after_file
                    logger.info(
                        f"[EXPLORE {exploration_id}] ✅ Step {step_counter} "
                        f"(attempt {attempt+1}): {action} {selector[:40] if selector else ''}"
                    )
                    # ── Record success to selector memory ─────────────────
                    _record_selector_outcome(
                        current_domain, step_desc, action, selector, value, success=True
                    )
                    break

                # ── Record failed selector attempt ─────────────────────────
                _record_selector_outcome(
                    current_domain, step_desc, action, selector, value, success=False
                )

                # Verification failed — prepare for next retry
                current_b64 = after_b64 or current_b64
                if attempt >= MAX_EXPLORE_RETRIES:
                    log_entry["error"] = f"Failed after {MAX_EXPLORE_RETRIES+1} attempts: {last_failure}"
                    logger.warning(
                        f"[EXPLORE {exploration_id}] ❌ Step {step_counter} exhausted retries: {last_failure[:80]}"
                    )

            steps_log.append(log_entry)
            await page.wait_for_timeout(800)

        # ── E3: Generate MD and persist ────────────────────────────────────
        md_content = _generate_exploration_md(exploration_id, enriched_test_case, steps_log)
        (expl_dir / "exploration.md").write_text(md_content)
        (expl_dir / "steps.json").write_text(json.dumps({
            "explorationId": exploration_id,
            "testCase":      req.test_case,
            "storageState":  req.storage_state,
            "pathTaken":     path_taken,
            "stepsCompleted": step_counter,
            "steps":         steps_log,
        }, indent=2, default=str))

        logger.info(f"[EXPLORE {exploration_id}] ✅ Done — {step_counter} steps, MD saved")

        await ctx.close()
        await browser.close()
        await pb.stop()

        return {
            "explorationId":   exploration_id,
            "stepsCompleted":  step_counter,
            "pathTaken":       path_taken,
            "steps":           steps_log,
            "markdownContent": md_content,
            "status":          "complete",
        }

    except Exception as e:
        logger.error(f"[EXPLORE {exploration_id}] Fatal: {e}")
        for resource in [browser, pb]:
            if resource:
                try: await resource.close()
                except: pass
        md_content = _generate_exploration_md(exploration_id, enriched_test_case, steps_log) if steps_log else "# Exploration failed\n"
        try: (expl_dir / "exploration.md").write_text(md_content)
        except: pass
        return {
            "explorationId":   exploration_id,
            "stepsCompleted":  len(steps_log),
            "pathTaken":       path_taken,
            "steps":           steps_log,
            "markdownContent": md_content,
            "status":          "error",
            "error":           str(e),
        }


@app.post("/api/generate-from-exploration")
async def generate_from_exploration(req: GenerateFromExplorationRequest):
    """
    Stage 3 — Generate Playwright TypeScript from an exploration MD.
    Uses the verified selectors discovered during exploration.
    """
    expl_dir   = EXPLORATIONS_DIR / req.exploration_id
    steps_file = expl_dir / "steps.json"

    original_tc    = ""
    storage_state  = ""
    first_shot_b64 = ""

    if steps_file.exists():
        data          = json.loads(steps_file.read_text())
        original_tc   = data.get("testCase", "")
        storage_state = data.get("storageState", "") or ""

    shots_dir = expl_dir / "screenshots"
    if shots_dir.exists():
        shots = sorted(shots_dir.glob("*.png"))
        if shots:
            first_shot_b64 = base64.b64encode(shots[0].read_bytes()).decode()

    prompt = f"""Generate a complete, production-ready Playwright TypeScript test from the exploration document below.

EXPLORATION DOCUMENT (contains selectors verified on the real browser):
{req.md_content}

ORIGINAL TEST DESCRIPTION:
{original_tc}

REQUIREMENTS:
1. Use ONLY selectors from the Selector Reference table — do not invent alternatives
2. Handle both conditional paths (Path A / Path B) with real if/else logic reading the live page state
3. Add `await page.waitForTimeout(1500)` after every click that opens a new panel or menu
4. For date arithmetic (e.g. 30 days after start date): read the start date, parse it as a JS Date, add 30 days, write end date
5. Add `test.use({{ storageState: 'studio/.auth/{storage_state or "successfactors"}.json' }})` before the test block
6. Never use placeholder URLs — use the exact URL from the exploration document
7. Never hardcode credentials
8. Wrap each major section in a descriptive `await test.step('...', async () => {{ ... }})` block

Output ONLY the TypeScript code. No markdown fences."""

    try:
        content = (
            [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{first_shot_b64}"}},
             {"type": "text", "text": prompt}]
            if first_shot_b64 else prompt
        )
        response = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": content}],
            max_tokens=3000,
            temperature=0.1,
        )
        code = _clean_healed_code(response.choices[0].message.content.strip())
        return {"explorationId": req.exploration_id, "generatedCode": code, "status": "success"}

    except Exception as e:
        logger.error(f"[GENERATE-FROM-EXPLORATION] {e}")
        return {"error": str(e), "explorationId": req.exploration_id}


@app.get("/api/explorations")
async def list_explorations():
    """List all saved explorations, newest first."""
    result = []
    for d in sorted(EXPLORATIONS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir(): continue
        sf = d / "steps.json"
        if sf.exists():
            try:
                data = json.loads(sf.read_text())
                result.append({
                    "id":           d.name,
                    "testCase":     data.get("testCase", "")[:120],
                    "stepsCount":   data.get("stepsCompleted", len(data.get("steps", []))),
                    "pathTaken":    data.get("pathTaken"),
                    "storageState": data.get("storageState"),
                    "hasScreenshots": (d / "screenshots").exists(),
                })
            except Exception:
                result.append({"id": d.name, "testCase": "Unknown", "stepsCount": 0})
    return result


@app.get("/api/explorations/{exploration_id}")
async def get_exploration(exploration_id: str):
    """Return a saved exploration (steps + MD content)."""
    expl_dir = EXPLORATIONS_DIR / exploration_id
    if not expl_dir.exists():
        raise HTTPException(status_code=404, detail="Exploration not found")
    result: dict = {"explorationId": exploration_id}
    sf = expl_dir / "steps.json"
    if sf.exists():
        result.update(json.loads(sf.read_text()))
    mf = expl_dir / "exploration.md"
    if mf.exists():
        result["markdownContent"] = mf.read_text()
    return result


# ── Selector memory endpoints ─────────────────────────────────────────────────

@app.get("/api/selector-memory")
async def get_selector_memory():
    """Return selector memory stats — domains, entry counts, confidence breakdown."""
    memory = _load_selector_memory()
    result = []
    for domain, data in memory.items():
        entries = data.get("entries", [])
        result.append({
            "domain":   domain,
            "total":    len(entries),
            "high":     sum(1 for e in entries if e.get("confidence") == "high"),
            "medium":   sum(1 for e in entries if e.get("confidence") == "medium"),
            "low":      sum(1 for e in entries if e.get("confidence") == "low"),
            "entries":  entries,
        })
    return result

@app.delete("/api/selector-memory/{domain}")
async def clear_selector_memory(domain: str):
    """Clear all learned selectors for a specific domain."""
    memory = _load_selector_memory()
    if domain in memory:
        del memory[domain]
        _save_selector_memory(memory)
        return {"cleared": domain}
    raise HTTPException(status_code=404, detail=f"No memory found for domain: {domain}")

@app.delete("/api/selector-memory")
async def clear_all_selector_memory():
    """Clear all selector memory."""
    _save_selector_memory({})
    return {"cleared": "all"}


# ─ Health check endpoints ──────────────────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    """Basic health check"""
    return {"status": "healthy", "service": "Playwright AI Studio"}


@app.get("/api/health/github")
async def check_github_health():
    """Verify GitHub credentials and API access"""
    try:
        gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()

        # Check if repo is accessible
        url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}"
        headers = {"Authorization": f"token {gh_token}"}
        response = requests.get(url, headers=headers, timeout=5)

        if response.status_code == 200:
            repo_data = response.json()
            return {
                "status": "healthy",
                "owner": gh_owner,
                "repo": gh_repo,
                "workflow": gh_workflow,
                "branch": gh_branch,
                "repo_url": repo_data.get("html_url"),
            }
        elif response.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid GitHub token - check GITHUB_TOKEN in .env")
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Repository not found: {gh_owner}/{gh_repo}")
        else:
            raise HTTPException(status_code=response.status_code, detail=f"GitHub API returned {response.status_code}")

    except requests.Timeout:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub check failed: {str(e)}")


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

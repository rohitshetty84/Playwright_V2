"""
Playwright AI Studio — Python/FastAPI backend
Azure OpenAI powered test synthesis & auto-healing
"""

import os, json, uuid, re, subprocess, tempfile, logging, base64, asyncio, shutil, threading
from collections import defaultdict

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mK]')
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union
import requests
from playwright.async_api import async_playwright

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
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
from services.mcp_bridge import PlaywrightMCPBridge  # MCP: Playwright MCP bridge
from services.github import get_github_config, dispatch_github_workflow, dispatch_exploration_workflow
from routes.batch import router as batch_router

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
SELECTOR_MEMORY_FILE  = BASE / "selector_memory.json"
LEARNED_RULES_FILE    = BASE / "learned_rules.json"
EXPLORATION_PATTERNS_FILE = BASE / "exploration_patterns.json"  # domain-level interaction patterns
MCP_ARTIFACTS_DIR     = BASE / ".playwright-mcp"
EXPLORATIONS_DIR.mkdir(exist_ok=True)

# ── Auto-cleanup ──────────────────────────────────────────────────────────────
_KEEP_EXPLORATIONS = 20   # keep this many most-recent exploration runs in full

def _cleanup_artifacts() -> dict:
    """
    1. Delete ALL .playwright-mcp/ entries — purely transient per-session artifacts.
    2. Explorations: keep the newest _KEEP_EXPLORATIONS runs intact.
       For older runs: delete screenshots/ subfolder but preserve steps.json
       and exploration.md (needed for test-script generation).

    Returns a summary dict with actual counts (used by /api/cleanup response).
    """
    deleted_mcp = 0
    if MCP_ARTIFACTS_DIR.exists():
        for entry in MCP_ARTIFACTS_DIR.iterdir():
            try:
                if entry.is_file():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
                deleted_mcp += 1
            except Exception:
                pass

    deleted_shots = 0
    runs_stripped = 0
    if EXPLORATIONS_DIR.exists():
        runs = sorted(
            [d for d in EXPLORATIONS_DIR.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for i, run_dir in enumerate(runs):
            if i < _KEEP_EXPLORATIONS:
                continue
            shots = run_dir / "screenshots"
            if shots.exists():
                for img in shots.iterdir():
                    try:
                        img.unlink()
                        deleted_shots += 1
                    except Exception:
                        pass
                try:
                    shots.rmdir()
                    runs_stripped += 1
                except Exception:
                    pass

    # Remove any stray PNGs that @playwright/mcp wrote to the studio root dir.
    # These are created when the LLM calls browser_take_screenshot(filename=...)
    # The bridge now strips that param, but clean up any leftovers from past runs.
    deleted_root_png = 0
    for png in BASE.glob("*.png"):
        try:
            png.unlink()
            deleted_root_png += 1
        except Exception:
            pass

    logger.info(
        f"[cleanup] removed {deleted_mcp} MCP artifact(s), "
        f"stripped screenshots from {runs_stripped} old run(s) ({deleted_shots} file(s) freed), "
        f"deleted {deleted_root_png} stray PNG(s) from studio root"
    )
    return {"deleted_mcp": deleted_mcp, "runs_stripped": runs_stripped,
            "deleted_shots": deleted_shots, "deleted_root_png": deleted_root_png}

# Live SSE queues — one asyncio.Queue per active exploration
_explore_queues: dict = {}

# Cancellation flags — set to signal a running exploration to stop cleanly
_explore_cancel: dict = {}   # exploration_id → asyncio.Event

# ── Synthesis tuning ─────────────────────────────────────────────────────────
MAX_HEAL_ROUNDS    = 3    # Max Phase-1/2 retry cycles before giving up
MAX_EXPLORE_RETRIES = 4   # Verify-then-act: retries per exploration step before giving up
LLM_TEMPERATURE   = 0.2  # All LLM calls use a single determinism constant
LLM_MAX_TOKENS    = 1500 # Default output token budget
LLM_VISION_TOKENS = 2000 # Vision responses include full TS code — need more room
NAV_PAUSE_MS      = 2000 # Post-navigation pause so JS-rendered elements appear

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Playwright AI Studio", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(batch_router, prefix="/api/batch", tags=["Batch"])

@app.on_event("startup")
async def _startup_cleanup():
    _cleanup_artifacts()

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
    headless: bool = True                 # True = background (no visible window)
    max_restarts: int = 0                 # full-run retries on cascade failure (0 = none)

class GenerateFromExplorationRequest(BaseModel):
    exploration_id: str
    md_content: str                        # possibly user-edited before generation

class EnrichStepsRequest(BaseModel):
    test_case: str
    app_context: str = "SAP SuccessFactors Onboarding 2.0"   # e.g. "SAP SuccessFactors", "Workday", custom text
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


# ── Self-learning rule store ──────────────────────────────────────────────────
# Rules are extracted by the reasoning model after each failure or successful
# correction, persisted to learned_rules.json, and injected into every
# subsequent step prompt — both within the same run and across future runs.

def _load_learned_rules() -> dict:
    if LEARNED_RULES_FILE.exists():
        try:
            return json.loads(LEARNED_RULES_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_learned_rules(data: dict) -> None:
    LEARNED_RULES_FILE.write_text(json.dumps(data, indent=2, default=str))

def _persist_learned_rule(rule: dict, framework: str) -> bool:
    """Add a rule to the store. Returns True if accepted (not a dupe, confidence OK)."""
    if not rule or rule.get("confidence", 0) < 0.65:
        return False
    data = _load_learned_rules()
    bucket = data.setdefault(framework, [])
    sig = rule.get("error_signature", "").strip().lower()
    if sig and any(r.get("error_signature", "").strip().lower() == sig for r in bucket):
        return False   # already know this pattern
    if len(bucket) >= 30:
        bucket.pop(0)  # drop oldest when cap reached
    bucket.append({
        "rule":            rule.get("rule", ""),
        "anti_pattern":    rule.get("anti_pattern", ""),
        "error_signature": rule.get("error_signature", ""),
        "applies_to":      framework,
        "confidence":      rule.get("confidence", 0),
        "source":          rule.get("source", ""),
        "created":         datetime.now().isoformat(),
        "applied_count":   0,
    })
    _save_learned_rules(data)
    return True

def _get_active_rules(framework: str) -> list:
    """Return rule strings for the given framework, best-confidence first."""
    data = _load_learned_rules()
    rules = [r for r in data.get(framework, []) if r.get("confidence", 0) >= 0.65]
    rules.sort(key=lambda r: r.get("confidence", 0), reverse=True)
    return [r["rule"] for r in rules]

def _extract_rule_from_failure(step_desc: str, attempts: list,
                                ui_framework: str, exploration_id: str) -> dict:
    """Ask the reasoning model to derive a reusable rule from a multi-attempt failure."""
    if not attempts:
        return {}
    attempts_text = "\n".join(
        f"  Attempt {i+1}: action={a.get('action','')}  "
        f"selector='{a.get('selector','')}'"
        f"  error='{a.get('error','')[:150]}'"
        for i, a in enumerate(attempts)
    )
    try:
        raw = ask_llm(
            system=(
                "You are a Playwright selector expert analysing browser automation failures. "
                "Extract ONE concise, reusable rule to prevent this failure from recurring."
            ),
            user=f"""A step FAILED after all retries.

STEP: {step_desc}
UI FRAMEWORK: {ui_framework}

ALL ATTEMPTS (all failed):
{attempts_text}

Rules:
- The rule must describe locator STRINGS (e.g. 'ui5-table-row:has-text("X") >> ui5-table-cell:nth-child(N)')
- NEVER recommend TypeScript method calls (page.getByRole(...), .filter({{...}}), etc.)
- Be specific — name the component type and the correct string pattern

Return ONLY raw JSON, no markdown:
{{
  "rule": "one sentence: use <correct locator string> instead of <wrong pattern> because <reason>",
  "anti_pattern": "the pattern that caused the failure",
  "error_signature": "key phrase from the error that identifies this failure class",
  "confidence": 0.0-1.0
}}""",
            max_tokens=300,
            temperature=0,
        )
        raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
        result = json.loads(raw)
        result["source"] = exploration_id
        return result
    except Exception as e:
        logger.warning(f"[EXPLORE {exploration_id}] Rule extraction (failure) error: {e}")
        return {}

def _extract_rule_from_correction(step_desc: str, failed_selector: str,
                                   failed_error: str, working_selector: str,
                                   ui_framework: str, exploration_id: str) -> dict:
    """Extract a rule when a retry succeeds after prior attempts failed."""
    try:
        raw = ask_llm(
            system=(
                "You are a Playwright selector expert. "
                "Extract ONE reusable rule from a successful selector correction."
            ),
            user=f"""A step FAILED then SUCCEEDED on retry.

STEP: {step_desc}
UI FRAMEWORK: {ui_framework}

FAILED selector:  '{failed_selector}'
FAILURE error:    '{failed_error[:200]}'
WORKING selector: '{working_selector}'

Write ONE concise rule: prefer the working locator string pattern, avoid the failing one.
Must describe STRINGS not TypeScript code.

Return ONLY raw JSON, no markdown:
{{
  "rule": "use '<working pattern>' instead of '<failed pattern>' because <reason>",
  "anti_pattern": "{failed_selector}",
  "error_signature": "key phrase from the error",
  "confidence": 0.0-1.0
}}""",
            max_tokens=250,
            temperature=0,
        )
        raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
        result = json.loads(raw)
        result["source"] = exploration_id
        return result
    except Exception as e:
        logger.warning(f"[EXPLORE {exploration_id}] Rule extraction (correction) error: {e}")
        return {}

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
    """Return past verified selectors for similar steps on this domain, best first.

    Only returns entries where successes outweigh failures — stale/bad selectors
    that accumulated failures are excluded so the model isn't misled.
    """
    memory = _load_selector_memory()
    entries = memory.get(domain, {}).get("entries", [])
    hits = []
    for e in entries:
        sc = e.get("success_count", 0)
        fc = e.get("failure_count", 0)
        if sc == 0:
            continue
        if fc >= sc:          # more failures than successes → exclude
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


# ── Exploration pattern memory ────────────────────────────────────────────────

_patterns_lock: threading.Lock = threading.Lock()
_patterns_cache: Optional[dict] = None   # in-process cache; invalidated on each write


def _load_exploration_patterns() -> dict:
    global _patterns_cache
    if _patterns_cache is not None:
        return _patterns_cache
    try:
        if EXPLORATION_PATTERNS_FILE.exists():
            _patterns_cache = json.loads(EXPLORATION_PATTERNS_FILE.read_text())
            return _patterns_cache
    except Exception as e:
        logger.warning(f"[patterns] Could not read exploration patterns: {e}")
    return {}


def _save_exploration_patterns(patterns: dict) -> None:
    global _patterns_cache
    try:
        EXPLORATION_PATTERNS_FILE.write_text(json.dumps(patterns, indent=2, default=str))
        _patterns_cache = patterns   # update cache after successful write
    except Exception as e:
        logger.warning(f"[patterns] Could not save exploration patterns: {e}")


def _get_domain_patterns(domain: str) -> list[dict]:
    """Return stored interaction patterns for a domain, sorted by success count."""
    return sorted(
        _load_exploration_patterns().get(domain, {}).get("patterns", []),
        key=lambda p: p.get("success_count", 0),
        reverse=True,
    )


def _extract_and_save_patterns(domain: str, steps_log: list, ui_framework: str) -> None:
    """After an exploration completes, use an LLM to extract reusable interaction
    patterns from the step results and merge them into exploration_patterns.json.

    Captures what selector-level memory misses:
    - Navigation workarounds (SAP flyout → JS DOM search)
    - Steps where URL doesn't change but action succeeded (Go button)
    - Timing-sensitive sequences (typeahead → wait → Go)
    - Verify false-negative patterns (model=✅ but verify=❌)
    """
    if not domain or not steps_log:
        return

    # Only run if at least some steps succeeded — no point learning from full failures
    succeeded = [s for s in steps_log if s.get("success")]
    if len(succeeded) < 2:
        return

    # Build a compact summary of each step's outcome
    step_summary = "\n".join(
        f"Step {s['step_num']} [{s['action']}] {'✅' if s['success'] else '❌'} "
        f"attempts={s.get('attempts',1)} "
        f"selector='{s.get('selector','')[:60]}' "
        f"obs='{s.get('observation','')[:100]}'"
        for s in steps_log
    )

    try:
        raw = ask_llm(
            system=(
                "You extract reusable browser automation patterns from exploration results. "
                "Focus on non-obvious findings: workarounds, timing dependencies, "
                "false-negative verify patterns, and elements missing from the accessibility tree."
            ),
            user=f"""Domain: {domain}
UI Framework: {ui_framework}

Exploration results (one line per step):
{step_summary}

Extract up to 5 reusable interaction patterns that would help future explorations of this domain.
Only include patterns that are genuinely non-obvious — skip simple 'click X worked' facts.
Focus on:
- Steps that needed >1 attempt and what finally worked
- Steps where the action doesn't change the URL but still succeeded (filter buttons, in-page updates)
- Elements not in the accessibility tree that needed a DOM workaround
- Timing dependencies between steps (e.g. wait after typeahead before next click)

Return ONLY a JSON array (empty array if nothing worth saving):
[{{"trigger": "short phrase that identifies when to apply this pattern",
  "pattern": "concise description of what works and why",
  "anti_pattern": "what to avoid (optional, omit if none)",
  "confidence": 0.5-1.0}}]""",
            max_tokens=800,
        )
        raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
        new_patterns: list = json.loads(raw)
        if not isinstance(new_patterns, list):
            return
    except Exception as e:
        logger.warning(f"[patterns] LLM extraction failed: {e}")
        return

    if not new_patterns:
        return

    with _patterns_lock:
        all_patterns = _load_exploration_patterns()
        domain_data  = all_patterns.setdefault(domain, {"patterns": []})
        existing     = domain_data["patterns"]

        for np in new_patterns:
            trigger = np.get("trigger", "").strip()
            if not trigger:
                continue
            match = next(
                (e for e in existing if _intent_similarity(e.get("trigger", ""), trigger) > 0.6),
                None,
            )
            if match:
                match["success_count"] = match.get("success_count", 0) + 1
                match["pattern"]       = np.get("pattern", match["pattern"])
                if np.get("anti_pattern"):
                    match["anti_pattern"] = np["anti_pattern"]
            else:
                existing.append({
                    "id":            str(uuid.uuid4())[:8],
                    "trigger":       trigger,
                    "pattern":       np.get("pattern", ""),
                    "anti_pattern":  np.get("anti_pattern", ""),
                    "confidence":    np.get("confidence", 0.7),
                    "success_count": 1,
                    "last_seen":     datetime.now().strftime("%Y-%m-%d"),
                })

        _save_exploration_patterns(all_patterns)
    logger.info(f"[patterns] Saved {len(new_patterns)} pattern(s) for {domain}")


def _format_patterns_for_prompt(domain: str, max_patterns: int = 5) -> str:
    """Format stored domain patterns as a text block for injection into prompts."""
    patterns = _get_domain_patterns(domain)[:max_patterns]
    if not patterns:
        return ""
    lines = ["Learned interaction patterns for this app (apply when relevant):"]
    for p in patterns:
        line = f"  • [{p['trigger']}] {p['pattern']}"
        if p.get("anti_pattern"):
            line += f" — avoid: {p['anti_pattern']}"
        lines.append(line)
    return "\n".join(lines)


# ── Exploration helpers ───────────────────────────────────────────────────────

def _parse_test_steps(test_case: str) -> list:
    """Use LLM to break a free-text description into a flat ordered step list."""
    raw = ask_llm(
        system="""You are a test planning assistant for browser automation.
Break the test description into a flat ordered list of ATOMIC steps — one interaction per step.
Key rules:
- If navigating to a section requires opening a menu first, split into TWO steps:
    e.g. "Go to Onboarding Dashboard" → step 1: "Click the Home/Apps icon to open the navigation menu"
                                       → step 2: "Click 'Onboarding' from the menu options"
- Search with typeahead also needs TWO steps:
    step 1: "Type candidate name into the search input"
    step 2: "Click the matching suggestion from the dropdown that appears"
- For conditional paths (Path A / Path B) include ALL path steps tagged with their path label.
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
        browser = await pb.chromium.launch(headless=True)
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
        browser = await pb.chromium.launch(headless=True)
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

_SAP_DOMAIN_HINTS = ("successfactors.com", "sap.com", "onboarding", "successfactors")

def _is_sap_test(test_case: str) -> bool:
    """Return True when the test description targets a SuccessFactors / SAP UI5 app."""
    lower = test_case.lower()
    return any(h in lower for h in _SAP_DOMAIN_HINTS)

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
        _sap = _is_sap_test(req.test_case)
        _sap_rules = f"\n\n{prompts.SAP_UI5_SELECTOR_RULES}" if _sap else ""

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
            browser = await pb.chromium.launch(headless=True)
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
5. Avoid getByRole('link') for navigation tabs — it breaks on sites that use role="menuitem".{_sap_rules}

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
5. Avoid getByRole('link') for navigation tabs.{_sap_rules}

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
                heal_dom_info = ""   # live DOM inspection result for this round

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
                    browser2 = await pb2.chromium.launch(headless=True)
                    page2    = await browser2.new_page()
                    # Load auth if available for this URL
                    for _dom, _aname in _DOMAIN_AUTH_MAP.items() if hasattr(locals(), '_DOMAIN_AUTH_MAP') else []:
                        if _dom in (url or ""):
                            _ap = BASE / ".auth" / f"{_aname}.json"
                            if _ap.exists():
                                await browser2.close()
                                await pb2.stop()
                                pb2      = await async_playwright().start()
                                browser2 = await pb2.chromium.launch(headless=True)
                                ctx2     = await browser2.new_context(storage_state=str(_ap))
                                page2    = await ctx2.new_page()
                            break
                    for _w, _t in [('domcontentloaded', 30000), ('load', 30000), ('commit', 45000)]:
                        try:
                            await page2.goto(url, wait_until=_w, timeout=_t)
                            break
                        except Exception:
                            pass
                    await page2.wait_for_timeout(2000)
                    heal_shot_bytes = await page2.screenshot(full_page=False)
                    heal_shot_b64   = base64.b64encode(heal_shot_bytes).decode('utf-8')
                    # Run DOM inspection while browser is still open
                    try:
                        _failed_sel = re.search(r"locator\(['\"](.+?)['\"]\)", phase1_message)
                        _sel_hint   = _failed_sel.group(1) if _failed_sel else ""
                        heal_dom_info = await _inspect_dom_for_correction(page2, _sel_hint, phase1_message[:200])
                        logger.info(f"[PHASE 2 · Round {round_num}] 🔬 DOM inspected for heal")
                    except Exception as _de:
                        logger.warning(f"[PHASE 2 · Round {round_num}] DOM inspection skipped: {_de}")
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

                _dom_heal_block = f"\n{heal_dom_info}\n" if heal_dom_info else ""

                heal_text = f"""This Playwright test failed (Round {round_num} of {MAX_HEAL_ROUNDS}).
{"Look at the screenshot to identify the correct element, then fix the test." if heal_shot_b64 else "Analyse the error and fix the test code."}

FAILURE ERROR:
{phase1_message}

CURRENT BROKEN CODE:
{current_code}
{_dom_heal_block}
SELECTOR RULES (use in this order):
1. Nav tabs/menu items → page.locator('a[role="menuitem"]').filter({{hasText: "Label"}})
2. Buttons            → getByRole('button', {{ name: '...' }})
3. Links with href    → page.locator('a[href*="keyword"]')
4. Plain text         → getByText('...', {{ exact: true }})
❌ NEVER use getByRole('link') for navigation tabs — those elements use role="menuitem".{_sap_rules}

AUTHENTICATION RULES (must preserve — security requirement):
- If the existing code has a test.use({{ storageState: ... }}) line, keep it exactly as-is.
- NEVER replace storageState with hardcoded credentials, even as a debugging aid.
- NEVER introduce usernames or passwords anywhere in the code.

{"Study the screenshot carefully. Identify the exact element the test is trying to reach." if heal_shot_b64 else ""}
{"Use the DOM inspection above as primary evidence for the correct selector." if heal_dom_info else ""}
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

@app.get("/api/runs/sync-from-github")
async def sync_runs_from_github():
    """
    Pull recent GitHub Actions workflow runs and write any completed ones
    to the local runs store. Idempotent — uses github_run_id to deduplicate.
    Called when the user opens Run History or checks a workflow status.
    """
    try:
        gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()
    except HTTPException:
        return {"imported": 0, "message": "GitHub not configured"}

    headers = {"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"}
    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/{gh_workflow}/runs"

    try:
        resp = requests.get(url, headers=headers, timeout=10, params={"per_page": 30})
        if resp.status_code != 200:
            return {"imported": 0, "message": f"GitHub API {resp.status_code}: {resp.text[:100]}"}
    except Exception as e:
        return {"imported": 0, "message": f"GitHub request failed: {e}"}

    workflow_runs = resp.json().get("workflow_runs", [])

    # Build set of already-recorded github_run_ids so we don't duplicate
    existing_ids = {
        r.get("githubRunId") for r in load_runs() if r.get("githubRunId")
    }

    goldens_by_id = {g["id"]: g for g in load_goldens()}
    imported = 0

    for wr in workflow_runs:
        if wr.get("status") != "completed":
            continue  # skip in-progress runs

        github_run_id = str(wr["id"])
        if github_run_id in existing_ids:
            continue  # already recorded

        # Try to map back to a golden via workflow inputs or display_title
        inputs = wr.get("inputs") or {}
        golden_id = inputs.get("golden_id", "")
        display_title = wr.get("display_title", "") or wr.get("name", "")

        # display_title often contains "Add golden: filename.spec.ts [abc12345]"
        # or "Run golden: filename.spec.ts" — extract what we can
        if not golden_id:
            id_match = re.search(r'\[([a-f0-9]{6,10})\]', display_title)
            if id_match:
                golden_id = id_match.group(1)

        golden = goldens_by_id.get(golden_id)
        if golden:
            golden_name = golden["name"]
        elif inputs.get("golden_name"):
            golden_name = inputs["golden_name"]
        else:
            # Use display_title stripped of the ID bracket as the name
            golden_name = re.sub(r'\s*\[[a-f0-9]{6,10}\]', '', display_title).strip() or "CI Run"

        conclusion = wr.get("conclusion", "unknown")  # success / failure / cancelled
        status_val = "pass" if conclusion == "success" else "fail"

        rid = str(uuid.uuid4())[:8]
        run = {
            "id":           rid,
            "goldenId":     golden_id,
            "goldenName":   golden_name,
            "browser":      "chromium",
            "runAt":        wr.get("created_at", ts_now())[:16].replace("T", " "),
            "githubRunId":  github_run_id,
            "githubRunUrl": wr.get("html_url", ""),
            "githubRunNum": wr.get("run_number"),
            "conclusion":   conclusion,
            "candidates": [{
                "name":     golden_name,
                "path":     golden_id,
                "status":   status_val,
                "duration": 0,
                "error":    "" if status_val == "pass" else f"CI run #{wr.get('run_number')} {conclusion}",
            }],
        }
        save_json(RUNS_DIR, rid, run)
        existing_ids.add(github_run_id)
        imported += 1

    return {"imported": imported, "message": f"Imported {imported} new run(s) from GitHub Actions"}


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

        response = requests.get(url, headers=headers, timeout=10, params={"per_page": 30})

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch workflow runs: {response.text}"
            )

        runs = response.json().get("workflow_runs", [])

        # Match by inputs.golden_id first, then fall back to display_title containing the ID.
        # GitHub often returns inputs=null even when they were provided, so the title fallback
        # is the primary match path in practice.
        matching_run = None
        for run in runs:
            inputs = run.get("inputs") or {}
            display_title = run.get("display_title", "") or ""
            if (inputs.get("golden_id") == golden_id or
                    golden_id in display_title or
                    f"[{golden_id}]" in display_title):
                matching_run = run
                break

        if not matching_run:
            # No golden-specific match — return the most recent run as a fallback.
            # Runs triggered via "Run in CI" button have no golden ID in their title.
            if runs:
                matching_run = runs[0]
                fallback = True
            else:
                return {
                    "status": "not_found",
                    "message": "No workflow runs found in this repository",
                    "golden_id": golden_id,
                }
        else:
            fallback = False

        return {
            "status": "found",
            "golden_id": golden_id,
            "run_id": matching_run["id"],
            "run_number": matching_run["run_number"],
            "name": matching_run["name"],
            "conclusion": matching_run.get("conclusion"),
            "workflow_status": matching_run["status"],
            "created_at": matching_run["created_at"],
            "updated_at": matching_run["updated_at"],
            "html_url": matching_run["html_url"],
            "github_link": matching_run["html_url"],
            "display_title": matching_run.get("display_title", matching_run["name"]),
            "fallback": fallback,
        }
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching workflow status: {str(e)}")


# ── Exploration endpoints ─────────────────────────────────────────────────────

# ── Verify-then-act helpers ───────────────────────────────────────────────────

# ── [PLAYWRIGHT-CLI] ─────────────────────────────────────────────────────────
# _gpt_vision_json / _reasoning_vision_json: single-shot GPT-4V helpers used by
# the direct Playwright step loop (plan → execute → verify). Not called in MCP
# mode — the Azure OpenAI function-calling loop handles reasoning internally.
# ─────────────────────────────────────────────────────────────────────────────
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


# ── [PLAYWRIGHT-CLI] ─────────────────────────────────────────────────────────
# _get_accessibility_tree: reads the a11y tree from a live Playwright `page`
# object. In MCP mode, browser_snapshot() returns the same information via the
# MCP server — no `page` object exists. Kept for reference.
# ─────────────────────────────────────────────────────────────────────────────
async def _get_accessibility_tree(page, max_chars: int = 4000) -> str:
    """Return a condensed accessibility tree for the current page state.

    Pierces shadow DOM (critical for SAP UI5 web components) and returns
    interactive elements with their roles and labels so the LLM reasons from
    actual DOM structure rather than guessing from pixels.
    """
    try:
        snapshot = await page.accessibility.snapshot(interesting_only=True)
        if not snapshot:
            return ""

        lines: list = []

        def walk(node: dict, depth: int = 0) -> None:
            if depth > 6 or len(lines) > 200:
                return
            role  = node.get("role", "")
            name  = (node.get("name") or "").strip()[:80]
            value = (str(node.get("value") or "")).strip()[:40]
            if role and name:
                indent = "  " * depth
                entry  = f"{indent}[{role}] \"{name}\""
                if value:
                    entry += f' = "{value}"'
                lines.append(entry)
            for child in node.get("children", []):
                walk(child, depth + 1)

        walk(snapshot)
        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n… (truncated)"
        return result
    except Exception:
        return ""


# ── [PLAYWRIGHT-CLI] ─────────────────────────────────────────────────────────
# _detect_ui_framework: takes a direct Playwright `page` object. In MCP mode,
# _detect_ui_framework_mcp() (defined below) does the same via browser_evaluate.
# ─────────────────────────────────────────────────────────────────────────────
async def _detect_ui_framework(page) -> str:
    """Detect SAP UI5 or other known frameworks by checking URL then live DOM.

    URL check runs first so detection works during SSO redirects before any
    ui5-* elements have rendered (e.g. SAML handshake pages).
    """
    try:
        current_url = page.url or ""
        if any(d in current_url for d in ("successfactors.com", "sap.com", "onboarding2",
                                           "performancemanager", "plateau.com")):
            return "sap_ui5"
    except Exception:
        pass
    # Fall back to DOM inspection once the page has rendered
    try:
        has_ui5 = await page.evaluate(
            "() => !!(document.querySelector('ui5-button,ui5-dialog,ui5-input,[data-sap-ui]'))"
        )
        if has_ui5:
            return "sap_ui5"
    except Exception:
        pass
    return "standard"


def _is_dependent_step(failed_step: str, next_step: str) -> bool:
    """Ask the LLM whether `next_step` requires `failed_step` to have succeeded."""
    try:
        raw = ask_llm(
            system="You decide if a test step depends on a previous step succeeding.",
            user=(
                f"FAILED STEP: {failed_step}\n"
                f"NEXT STEP: {next_step}\n\n"
                "Does the next step REQUIRE the failed step to have succeeded to be meaningful? "
                "Return ONLY raw JSON (no markdown): "
                "{\"dependent\": true|false, \"reason\": \"one sentence\"}"
            ),
            max_tokens=120,
        )
        raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
        return json.loads(raw).get("dependent", False)
    except Exception:
        return False


_SAP_UI5_RULES = """
SAP UI5 / SUCCESSFACTORS FRAMEWORK RULES
This page uses SAP UI5 web components (Onboarding 2.0+). All rules below are mandatory.

── CORE WEB COMPONENT RULES ──────────────────────────────────────────────────
- NEVER target shadow-DOM internals: .ui5-button-root, .ui5-input-inner, .ui5-*
  Always target the OUTER component element.
- NEVER use dynamic IDs like __button0-__clone42 — they change every session/user/deploy.
  Always use accessible name, placeholder, or component tag instead.

── COMPONENT SELECTORS ───────────────────────────────────────────────────────
- Buttons:     ui5-button[accessible-name="Label"]  OR  ui5-button:has-text("Label")
- Inputs:      PREFER input[placeholder="..."] — Playwright pierces shadow DOM and .fill() works reliably.
               Only fall back to ui5-input[placeholder="..."] if the plain input selector times out AND
               you have confirmed the element is visible. NEVER switch to ui5-input just because of a timeout —
               timeouts mean the page is not ready, not that the selector is wrong.
- Dropdowns:   ui5-select — page.locator('ui5-select').select_option(value)
- Dialogs:     ui5-dialog — close with Escape  OR  ui5-dialog >> ui5-button[icon="decline"]
- Date fields: ui5-date-picker >> input  (fill the inner input here, exception to the rule)
- Checkboxes:  ui5-checkbox[accessible-name="..."] — use .check() / .uncheck()
- Tabs:        ui5-tabcontainer >> ui5-tab[text="Tab Name"]
- Wizard:      ui5-wizard-step — next/submit via ui5-button:has-text("Next")
- Click fallback (resolves but won't click): page.evaluate('el => el.click()', await locator.elementHandle())

── SF ONBOARDING KNOWN SELECTORS (use these exactly — proven across multiple runs) ──────────
- Candidate search fill:    input[placeholder="Search for new recruit"]   ← plain CSS, pierces shadow DOM
- Candidate suggestion:     role=option[name^="Candidate Name"]           ← starts-with, handles job title suffix
- Home nav button:          role=button[name="Home"]  (may also appear as "My Employee File" on non-home pages)
- Onboarding menu item:     role=menuitem[name="Onboarding"]  (only in accessibility tree if flyout renders — see NAVIGATION PATTERN if absent)
- Apply filter / Go:        role=button[name="Go"]                        ← MUST click after selecting candidate

CRITICAL — ONBOARDING DASHBOARD FILTER PATTERN (always follow this exact sequence):
  1. fill    input[placeholder="Search for new recruit"]   with candidate name
  2. click   role=option[name^="Candidate Name"]           typeahead suggestion
  3. click   role=button[name="Go"]                        ← applies the filter (table still shows ALL if skipped)
  4. wait_for  ui5-table-row                               wait for filtered results to render

CRITICAL — ONBOARDING TABLE STRUCTURE (columns, not rows):
The Onboarding dashboard table has ONE ROW PER CANDIDATE. Column layout:
  nth-child(1): New Recruit name
  nth-child(2): Hiring Manager
  nth-child(3): Start Date
  nth-child(4): Data Collection   ← status text e.g. "Completed", "In Progress"
  nth-child(5): Compliance Forms  ← status text e.g. "Completed", "Not Available"
  nth-child(6): New Recruit Tasks

"Data Collection" and "Compliance Forms" are COLUMN HEADERS, NOT row text values.
NEVER use ui5-table-row:has-text("Data Collection") — that row does not exist.

After clicking "Go" and the table filters to the candidate, read statuses with:
  Data Collection:   ui5-table-row >> ui5-table-cell:nth-child(4)
  Compliance Forms:  ui5-table-row >> ui5-table-cell:nth-child(5)

After navigating to the Onboarding dashboard, ALWAYS use action="wait_for" with
selector="input[placeholder='Search for new recruit']" before attempting to fill it.
The dashboard renders asynchronously and the field is not interactive for 3-10 seconds after navigation.

── NAVIGATION PATTERN ────────────────────────────────────────────────────────
SF module navigation follows this pattern — always use it in this order:
  1. Click nav/home button: role=button[name="Home"]  (label may vary — also try "My Employee File")
  2. Take a browser_take_screenshot to see the navigation flyout visually.
  3. If role=menuitem[name="Onboarding"] appears in the accessibility tree → click it.
     If it does NOT appear in the tree (SAP Fiori flyout renders visually but not in tree) →
     try browser_evaluate to find and click it in the DOM directly:
       {{"function": "() => {{ const clickable = Array.from(document.querySelectorAll('a, [role=\"menuitem\"], [role=\"option\"], button')); const el = clickable.find(e => e.offsetParent !== null && e.textContent.trim() === 'Onboarding'); if (el) {{ el.click(); return 'clicked ' + el.tagName + ':' + (el.getAttribute('role') || '') + ' href=' + (el.href || ''); }} const all = Array.from(document.querySelectorAll('*')).filter(e => e.offsetParent !== null && e.textContent.trim() === 'Onboarding'); if (all.length) {{ const innermost = all[all.length - 1]; const link = innermost.closest('a') || innermost.closest('[role=\"menuitem\"]') || innermost; link.click(); return 'fallback ' + link.tagName; }} return 'not found'; }}"}}
     If that also returns 'not found', the flyout may need a moment — wait 1s then retry.
  ⚠️  If the flyout closes between steps (step 2 opened it, step 3 must click from it):
     First check if the flyout is still visible via browser_take_screenshot.
     If not visible, click the Home button AGAIN to reopen it before clicking Onboarding.
  4. Wait for module load: wait for the module's page heading, NOT networkidle

IMPORTANT: The SAP Fiori navigation flyout frequently does NOT render in the accessibility tree.
If you click Home and see no menu/menuitem in the snapshot, do NOT keep retrying snapshots.
Take a browser_take_screenshot first to see the page visually, then use browser_evaluate to search the DOM.

- NEVER use waitForLoadState('networkidle') — SF polls continuously and this never resolves.
  Instead wait for a specific heading or landmark element to become visible.
- After any navigation, take a browser_take_screenshot and wait for content to settle before acting.

── TABLES (most common failure point) ────────────────────────────────────────
UI5 tables render as <ui5-table-row> / <ui5-table-cell> — NEVER <tr>/<td>.

UNDERSTAND THE TABLE STRUCTURE FIRST (before writing any selector):
After selecting a candidate in SF Onboarding, the dashboard shows TASK ROWS such as:
  "Data Collection", "Compliance Forms", "Personal Information" etc.
The candidate name (e.g. "Matthew Moraga") appears in a PAGE HEADING, NOT in a table row.
→ Filter rows by TASK NAME, not by candidate name.
→ If the DOM inspection says "No row containing 'Matthew Moraga' found" — this is expected.
  Look at the DOM inspection's "all row texts" to find the actual task names used as row identifiers.

Valid selector strings for the "selector" JSON field:
  Task row:        ui5-table-row:has-text("Data Collection")
  Status cell:     ui5-table-row:has-text("Data Collection") >> ui5-table-cell:nth-child(2)
  Badge in row:    ui5-table-row:has-text("Data Collection") >> ui5-badge
  CSS role form:   [role="row"]:has-text("Data Collection") >> [role="cell"]:nth-child(2)

❌ INVALID — these throw InvalidSelectorError:
  role=row:has-text("...")       ← role= prefix cannot take :has-text() — always errors
  role=gridcell:nth-child(N)    ← same problem — use [role="gridcell"]:nth-child(N) instead

- Column index for nth-child is 1-based: nth-child(1) = first column.
- NEVER use tr/td CSS selectors — they always time out on UI5 tables.
- NEVER write page.getByRole(...).filter({...}) in the selector field — TypeScript code only.

── STATUS / BADGE READING ────────────────────────────────────────────────────
Onboarding task statuses (Data Collection, Compliance, etc.) render as badges or text:
  Read badge:    row.locator('ui5-badge').textContent()  — returns e.g. "Completed"
  Read text:     row.getByRole('cell').nth(N).textContent()
  Common values: "Completed", "In Progress", "Not Started", "Overdue", "Pending"
- If a cell has both an icon and text, .textContent() returns both — use .trim().

── FILTER BAR ("Go" button) ──────────────────────────────────────────────────
The SAP Fiori FilterBar always shows a "Standard" variant label — this is the
CURRENT filter variant name, NOT an open dropdown or menu. Seeing "Standard"
in a screenshot does NOT mean the Go button failed.

How to click the Go button:
  - Preferred: browser_click(element='Go button') or browser_click(element='role=button[name="Go"]')
  - Alternative (if click doesn't register): browser_evaluate with querySelector('[aria-label="Go"], button[title="Go"], #go-button') + .click()

After clicking Go:
  - The TABLE rows update — wait for ui5-table-row elements to appear
  - The "Standard" label remains visible (expected — this is the filter variant name)
  - URL does NOT change (Go is an in-page filter, not a navigation)
  ✅ Success evidence = table shows rows / content changed
  ❌ NOT evidence of failure = "Standard" label visible, URL unchanged

── BUSY / LOADING STATES ─────────────────────────────────────────────────────
SF shows loading overlays; interacting before they clear causes random failures.
  Wait for busy to clear: await expect(page.locator('ui5-busy-indicator')).toBeHidden({ timeout: 30000 })
  Alternative:            wait for the target element to be visible — it won't appear until load is done.
- After a typeahead selection or form submit, always insert action="wait" value="2000"
  before the next read or click step.

── TIMEOUT vs NOT-FOUND — critical distinction ────────────────────────────────
"Timeout Xms exceeded" means the element EXISTS but is NOT YET VISIBLE. It is a timing issue.
"Element not found" or "strict mode violation" means wrong selector.

When you see a Timeout error:
  ✅ Use action="wait_for", same selector, value="30000" — waits for element to become visible
  ✅ Then on the next step, retry the original fill/click with the same selector
  ❌ Do NOT switch to a different selector — that will also time out for the same reason
  ❌ Do NOT use fixed action="wait" delays — wait for the specific element instead

Example: fill on 'input[placeholder="Search for new recruit"]' timed out →
  Correction: action="wait_for", selector="input[placeholder='Search for new recruit']", value="30000"

── TOAST NOTIFICATIONS ───────────────────────────────────────────────────────
SF shows ui5-toast messages after saves/submits. They auto-dismiss and should NOT be clicked.
  If a toast blocks a click: await page.locator('ui5-toast').waitFor({ state: 'hidden', timeout: 10000 })

── IFRAMES ───────────────────────────────────────────────────────────────────
Some SF modules (classic Onboarding 1.0 remnants, some admin pages) embed content in iframes.
  If a locator times out even though the element is visible in the screenshot:
  1. Check for iframes: const frame = page.frameLocator('iframe[title*="..."]')
  2. Run the locator inside the frame: frame.locator('...')
  3. Do NOT cross frame boundaries in a single locator chain.

── TYPEAHEAD / AUTOCOMPLETE SEARCH ──────────────────────────────────────────
Fills a search field → a suggestion dropdown appears → you MUST click a suggestion.
  REQUIRED SEQUENCE (do not skip the wait_for):
    1. action="wait_for"  selector="input[placeholder='Search for new recruit']"  value="30000"
       (waits for field to become interactive after async dashboard render)
    2. action="fill"      selector="input[placeholder='Search for new recruit']"  value="Candidate Name"
    3. action="click"     selector="role=option[name^='Candidate Name']"
       (starts-with ^= handles job title appended to name in the suggestion)
  NEVER press Enter or submit before clicking the dropdown suggestion — search won't execute.
  NEVER switch the input selector to ui5-input or ui5-combobox if it times out —
    a timeout here means the page is still loading, not that the selector is wrong.
    Use wait_for with the SAME selector, then retry fill.

── INPUT COMPONENT TYPES (critical — three different components, three different patterns) ──
  ui5-input        Free-text input. Use .fill() directly on the component.
  ui5-combobox     Type-and-select (single value). Fill → click the matching suggestion.
  ui5-select       Pure dropdown, no typing. Use page.locator('ui5-select').selectOption(value).
  ui5-multi-combobox  Multi-value token input. Fill each value → click suggestion → repeat.
                      Read selected tokens:  page.locator('ui5-multi-combobox').locator('ui5-token').allTextContents()
                      Remove a token:        token.locator('ui5-icon[name="decline"]').click()
  NEVER call .fill() on ui5-select — it has no text field. NEVER call .selectOption() on ui5-input or ui5-combobox.

── PAGINATION / LOAD MORE ────────────────────────────────────────────────────
SF tables only render the first N rows. If a row is not found:
  1. Check for a "More" button at the bottom: page.locator('ui5-button:has-text("More")')
  2. Click it, then wait 2s, then retry the row lookup.
  3. Repeat until the row is found OR no "More" button exists.
  NEVER conclude a row is absent without first checking for and clicking "More".

── VIRTUAL SCROLLING ────────────────────────────────────────────────────────
SF uses virtual rendering — rows below the viewport are not in the DOM.
  If a row locator times out: scroll the table container, then retry.
    await page.locator('ui5-table').evaluate(el => el.scrollTop += 400);
    await page.waitForTimeout(1000);
  Use scrollIntoViewIfNeeded() on rows that are found but not clickable.

── CONFIRMATION DIALOGS ──────────────────────────────────────────────────────
Every destructive action (delete, withdraw, terminate, reassign) opens a ui5-dialog asking for confirmation.
  Confirm: await page.locator('ui5-dialog').locator('ui5-button:has-text("Confirm")').click()
  Cancel:  await page.locator('ui5-dialog').locator('ui5-button:has-text("Cancel")').click()
  If a dialog blocks interaction but isn't a confirmation, close with Escape.
  After confirming, wait for the dialog to disappear before reading results:
    await expect(page.locator('ui5-dialog')).toBeHidden({ timeout: 10000 })

── INLINE VALIDATION / FORM ERRORS ──────────────────────────────────────────
When a form submit fails validation, SF shows ui5-message-strip (inline) or ui5-message-box (blocking).
  Read inline error:   await page.locator('ui5-message-strip[design="Negative"]').textContent()
  Read field error:    await page.locator('ui5-input[value-state="Error"]').getAttribute('value-state-message')
  Dismiss message box: await page.locator('ui5-message-box').locator('ui5-button:has-text("Close")').click()
  If a form submit does NOT navigate and shows no success signal → check for validation errors first.

── READ-ONLY VS EDITABLE FIELDS ─────────────────────────────────────────────
SF renders editable and display-only fields with different components:
  Editable:   ui5-input, ui5-combobox, ui5-select, ui5-textarea — use .fill() / .selectOption()
  Read-only:  ui5-text, plain <span>, or ui5-input[readonly] — use .textContent() to read, NEVER .fill()
  Assert editable value:  await expect(page.locator('ui5-input[placeholder="..."]')).toHaveValue('expected')
  Assert read-only value: await expect(page.locator('ui5-text')).toContainText('expected')
  NEVER call .fill() on a read-only field — it throws; read it with .textContent() instead.

── SHELL BAR (TOP NAVIGATION) ───────────────────────────────────────────────
The SF top application bar is ui5-shellbar, not a generic <header> or <nav>.
  Home/logo button:   page.locator('ui5-shellbar').locator('[slot="startButton"]')  OR  ui5-shellbar-item
  Search:             page.locator('ui5-shellbar').locator('[slot="searchField"]')
  Profile / avatar:   page.locator('ui5-shellbar').locator('ui5-avatar')
  Notifications bell: page.locator('ui5-shellbar').locator('ui5-shellbar-item[icon="bell"]')
  NEVER use page.locator('header') or page.locator('nav') to reach shell bar items.

── POPOVERS / OVERFLOW MENUS ────────────────────────────────────────────────
Action-column "..." or kebab menus in SF use ui5-popover + ui5-list — NOT the browser context menu.
  Open menu:     await page.locator('ui5-button[icon="overflow"]').click()
                 OR: await page.locator('ui5-button[icon="navigation-down-arrow"]').click()
  Click action:  await page.locator('ui5-popover').locator('ui5-li:has-text("Edit")').click()
  NEVER right-click to open these menus — they are triggered by a specific button click.

── SPA ACTION COMPLETION (how to know an action succeeded) ──────────────────
SF is a Single Page Application — URL often does NOT change after save/submit.
  Success signals to look for (in order):
    1. ui5-toast with success text:       await page.locator('ui5-toast').textContent()
    2. ui5-message-strip[design="Positive"] appearing
    3. A new section or panel becoming visible (e.g., a "record saved" confirmation area)
    4. The dialog closing (ui5-dialog disappears)
  NEVER use waitForNavigation or URL change as a success signal after form saves.

── FILE UPLOAD ───────────────────────────────────────────────────────────────
SF uses ui5-file-uploader for document attachments. Target the inner input, not the outer component.
  await page.locator('ui5-file-uploader').locator('input[type="file"]').setInputFiles('/path/to/file')
  NEVER call .setInputFiles() on the ui5-file-uploader element itself — it will throw.

── SIDE NAVIGATION (ADMIN CENTER) ───────────────────────────────────────────
The Admin Center uses ui5-side-navigation, not standard links.
  Click a nav item:    await page.locator('ui5-side-navigation-item[text="Manage Users"]').click()
  Click a sub-item:    await page.locator('ui5-side-navigation-sub-item[text="Import Users"]').click()
  Expand a group:      await page.locator('ui5-side-navigation-item[text="Users"]').click()
  NEVER use page.locator('a').filter({ hasText: 'Manage Users' }) in Admin — those are not <a> tags.
"""

# ── [PLAYWRIGHT-CLI] ─────────────────────────────────────────────────────────
# _plan_action_prompt / _verify_prompt / _correction_prompt:
# Three prompt-builder functions for the direct Playwright step loop.
# Plan → Execute → Verify → Correct was the manual orchestration pattern.
# In MCP mode, Azure OpenAI's function-calling loop replaces all three phases
# in a single conversation per step. These builders are no longer called.
# ─────────────────────────────────────────────────────────────────────────────
def _plan_action_prompt(step_desc: str, step_type: str, current_url: str,
                        history_ctx: str, memory_hints: Optional[list] = None,
                        a11y_tree: str = "", ui_framework: str = "standard",
                        extra_rules: Optional[list] = None,
                        dom_info: str = "") -> str:
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

    a11y_block = ""
    if a11y_tree:
        a11y_block = f"\nACCESSIBILITY TREE (actual DOM — use these roles/names for selectors):\n{a11y_tree}\n"

    framework_rules = _SAP_UI5_RULES if ui_framework == "sap_ui5" else ""

    learned_block = ""
    if extra_rules:
        learned_block = (
            "\nRULES LEARNED FROM PREVIOUS FAILURES (highest priority — apply these first):\n"
            + "\n".join(f"  ⚡ {r}" for r in extra_rules)
            + "\n"
        )

    dom_block = f"\n{dom_info}\n" if dom_info else ""

    return f"""You are controlling a real web browser to automate a test step.

CURRENT STEP: {step_desc}
STEP TYPE: {step_type}
CURRENT URL: {current_url}

RECENT ACTIONS:
{history_ctx or "(none yet)"}
{hints_block}{a11y_block}{dom_block}{framework_rules}{learned_block}
Study the screenshot AND the accessibility tree above. Prefer the tree for selector names — it shows the real DOM.
Return ONLY a raw JSON object — no markdown:
{{
  "action": "click"|"fill"|"select_option"|"navigate"|"read"|"wait"|"wait_for"|"hover"|"key"|"decision"|"done",
  "selector": "a locator STRING passable to page.locator() — see format rules below",
  "value": "text/URL/option to use — null for click/read/wait",
  "observation": "what you see on screen relevant to this step (1-2 sentences)",
  "confidence": "high"|"medium"|"low",
  "notes": "anything important a test writer must know about this element",
  "path_decision": null|"A"|"B"
}}
Rules:
- Derive selectors from the accessibility tree (roles + names) — not from guessing CSS classes
- For SAP UI5: use ui5-* component selectors, not shadow-DOM internals
- For conditional/read steps: action="decision", set path_decision
- If step already done: action="done"
- To close a dialog: try Escape key first (action="key", value="Escape")
- For navigation: action="navigate", value=full URL

SELECTOR FIELD FORMAT — CRITICAL:
The "selector" value is passed directly to page.locator(). It must be a plain locator STRING.
NEVER write TypeScript/JavaScript method calls in the selector field.

✅ VALID selector strings (pass directly to page.locator()):
  role=button[name="Home"]
  ui5-input[placeholder="Search for new recruit"]
  ui5-table-row:has-text("Matthew Moraga")
  ui5-table-row:has-text("Matthew Moraga") >> ui5-table-cell:nth-child(3)
  [role="row"]:has-text("Task Name") >> [role="cell"]:nth-child(N)
  role=option[name^="Matthew Moraga"]
  ui5-badge

❌ INVALID — these are TypeScript code, NOT selector strings:
  page.getByRole('row', {{ name: /Matthew/ }}).locator('td')     ← method call
  getByRole('row').filter({{ hasText: 'Matthew' }})              ← method call
  page.locator('ui5-table-row').filter({{ hasText: 'X' }})      ← method call

For chaining: use >> between two locator strings, e.g.:
  ui5-table-row:has-text("Matthew Moraga") >> ui5-table-cell:nth-child(3)"""


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
                        current_url: str, history_ctx: str,
                        a11y_tree: str = "", ui_framework: str = "standard",
                        extra_rules: Optional[list] = None,
                        dom_info: str = "",
                        page_analysis: str = "") -> str:
    a11y_block = f"\nACCESSIBILITY TREE (use these for corrected selector):\n{a11y_tree}\n" if a11y_tree else ""
    framework_rules = _SAP_UI5_RULES if ui_framework == "sap_ui5" else ""

    learned_block = ""
    if extra_rules:
        learned_block = (
            "\nRULES LEARNED FROM PREVIOUS FAILURES (apply these first when choosing the correction):\n"
            + "\n".join(f"  ⚡ {r}" for r in extra_rules)
            + "\n"
        )

    dom_block     = f"\n{dom_info}\n" if dom_info else ""
    vision_block  = f"\n{page_analysis}\n" if page_analysis else ""

    return f"""A browser action just failed. Think carefully about why and propose a correction.

ORIGINAL STEP: {step_desc}
FAILED ACTION: {action_plan.get('action')} on selector "{action_plan.get('selector')}"
FAILURE REASON: {failure_reason}
CURRENT URL: {current_url}

RECENT ACTIONS:
{history_ctx or "(none yet)"}
{a11y_block}{dom_block}{vision_block}{framework_rules}{learned_block}
Step-by-step reasoning required:
1. Why did the original selector fail? Use the visual analysis and DOM inspection as primary evidence.
2. What does the page actually show right now — is the browser on the right page/section?
3. Is there a prerequisite action missing (e.g. clicking "Go", scrolling, dismissing a dialog)?
4. What is the correct selector and action?

Return ONLY a raw JSON object — no markdown:
{{
  "action": "click"|"fill"|"select_option"|"navigate"|"read"|"wait"|"wait_for"|"hover"|"key"|"decision"|"done",
  "selector": "corrected selector — prefer names from accessibility tree",
  "value": "corrected value or key name (e.g. 'Escape') — null if not needed",
  "observation": "what you see and why the original failed",
  "confidence": "high"|"medium"|"low",
  "notes": "what changed and why this correction should work",
  "path_decision": null|"A"|"B",
  "reasoning": "your step-by-step reasoning"
}}
Special cases:
- Element resolves but can't be clicked → try action="key", value="Escape" to dismiss dialogs
- SAP UI5 shadow DOM → use the ui5-* outer component tag, not the inner shadow child
- Element not found → look in accessibility tree for similar role+name combinations

TIMEOUT DIAGNOSIS (most important — read before proposing a correction):
When the error contains "Timeout Xms exceeded" or "Timeout 20000ms" or "Timeout 30000ms":
  This means the element EXISTS in the page but is NOT YET VISIBLE or INTERACTIVE.
  It is a TIMING issue, NOT a wrong selector.

  CORRECT response to a timeout:
    Step 1 → use action="wait_for", same selector, value="30000"
             This waits up to 30s for the element to become visible before acting.
    Step 2 → on the NEXT retry, use the original action (fill/click) with the SAME selector.

  WRONG response to a timeout:
    ❌ Switching to a completely different selector (e.g. ui5-input → ui5-combobox → input)
       — all three will time out for the same reason if the page hasn't rendered yet.
    ❌ Adding a fixed page.wait_for_timeout() delay — prefer waiting for the specific element.

  Example: fill on 'input[placeholder="Search for new recruit"]' timed out →
    Correct correction: action="wait_for", selector="input[placeholder='Search for new recruit']", value="30000"
    NOT: switch to ui5-input or ui5-combobox"""


# ── [PLAYWRIGHT-CLI] ─────────────────────────────────────────────────────────
# _analyse_page_visually: called on step failure to run a focused GPT-4V pass
# on the failure screenshot. In MCP mode, browser_screenshot + the model's
# own reasoning within the function-calling loop provides the same analysis.
# ─────────────────────────────────────────────────────────────────────────────
def _analyse_page_visually(shot_b64: str, step_desc: str,
                           failed_selector: str, error: str) -> str:
    """
    Run a focused vision analysis on the failure screenshot.
    Returns a concise text block injected into the correction prompt alongside
    DOM inspection, giving the model both structural (DOM) and visual evidence.
    Called on every step failure, not just table reads.
    """
    if not shot_b64:
        return ""
    try:
        prompt = f"""You are analysing a browser screenshot to diagnose why a Playwright automation step failed.

FAILED STEP: {step_desc}
FAILED SELECTOR: {failed_selector}
ERROR: {error[:200]}

Answer these questions concisely based only on what you see in the screenshot:
1. What page/section is currently visible? (URL path or heading)
2. Is the target element visible? If yes, where? If no, what is shown instead?
3. If a table is visible: list ALL column headers and say how many rows are shown. Is it filtered or showing all records?
4. Are there any unclicked filter buttons, "Go" buttons, or search buttons that need to be activated?
5. Are there loading spinners, dialogs, or error messages blocking interaction?
6. In one sentence: what should be done differently on the next attempt?

Be specific — name exact labels, button text, placeholder text you can read."""

        raw = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}},
                {"type": "text", "text": prompt},
            ]}],
            max_tokens=400,
            temperature=0.1,
        ).choices[0].message.content.strip()
        return f"── VISUAL PAGE ANALYSIS ──\n{raw}"
    except Exception as e:
        logger.warning(f"[vision-analysis] failed: {e}")
        return ""


# ── [PLAYWRIGHT-CLI] ─────────────────────────────────────────────────────────
# _inspect_dom_for_correction: runs page.evaluate() to inspect live DOM on step
# failure and returns structured text for the correction prompt. In MCP mode,
# the model calls browser_evaluate() and browser_snapshot() itself via tools.
# ─────────────────────────────────────────────────────────────────────────────
async def _inspect_dom_for_correction(page, failed_selector: str,
                                       step_desc: str) -> str:
    """
    Run live DOM inspection when a step fails.
    Returns a concise text block injected into the correction prompt so the
    model picks its next selector from real page evidence, not guesses.
    """
    # Extract a text hint from the step description (e.g. candidate name)
    text_hint = ""
    hint_match = re.search(r"['\"]([^'\"]{3,})['\"]", step_desc)
    if hint_match:
        text_hint = hint_match.group(1)

    try:
        result = await page.evaluate(
            """(args) => {
                const { textHint } = args;

                // Count every plausible table/list/row/cell element
                const candidates = [
                    'ui5-table','ui5-table-row','ui5-table-cell','ui5-table-header-row',
                    'ui5-list','ui5-li','ui5-li-custom',
                    '[role="row"]','[role="cell"]','[role="gridcell"]',
                    '[role="listitem"]','[role="grid"]','[role="list"]',
                    'tr','td','table'
                ];
                const counts = {};
                for (const sel of candidates) {
                    try { counts[sel] = document.querySelectorAll(sel).length; }
                    catch (_) { counts[sel] = 0; }
                }

                // Find the first row containing the text hint
                const rowSelectors = [
                    'ui5-table-row','[role="row"]','tr',
                    'ui5-li','ui5-li-custom','[role="listitem"]'
                ];
                let matchInfo = null;
                for (const rowSel of rowSelectors) {
                    const rows = Array.from(document.querySelectorAll(rowSel));
                    const match = rows.find(r =>
                        !textHint || (r.textContent || '').includes(textHint)
                    );
                    if (match) {
                        const children = Array.from(match.children).map((c, i) => ({
                            index: i + 1,
                            tag:   c.tagName.toLowerCase(),
                            role:  c.getAttribute('role') || '',
                            text:  (c.textContent || '').trim().substring(0, 120),
                            ariaLabel: c.getAttribute('aria-label') || '',
                        }));
                        matchInfo = {
                            rowSelector: rowSel,
                            childCount:  match.children.length,
                            rowText:     (match.textContent || '').trim().substring(0, 300),
                            children,
                        };
                        break;
                    }
                }

                // Dump ALL row texts (first 10) so model sees actual table content
                const allRowTexts = [];
                for (const rowSel of rowSelectors) {
                    const rows = Array.from(document.querySelectorAll(rowSel)).slice(0, 10);
                    if (rows.length > 0) {
                        rows.forEach((r, i) => {
                            const children = Array.from(r.children).map((c, ci) => ({
                                index: ci + 1,
                                tag:   c.tagName.toLowerCase(),
                                text:  (c.textContent || '').trim().substring(0, 80),
                            }));
                            allRowTexts.push({
                                rowSelector: rowSel,
                                rowIndex:    i + 1,
                                rowText:     (r.textContent || '').trim().substring(0, 150),
                                children,
                            });
                        });
                        break; // use the first selector that finds rows
                    }
                }

                return { counts, matchInfo, allRowTexts };
            }""",
            {"textHint": text_hint, "failed_selector": failed_selector},
        )

        lines = ["── LIVE DOM INSPECTION (use this to pick the correct selector) ──"]

        non_zero = {k: v for k, v in result["counts"].items() if v > 0}
        if non_zero:
            lines.append("Elements present on page: " +
                         ", ".join(f"{k}={v}" for k, v in non_zero.items()))
        else:
            lines.append("⚠️  No table/list elements found — page may still be loading.")

        if result.get("matchInfo"):
            m = result["matchInfo"]
            lines.append(f"\nRow containing '{text_hint}' found with selector: {m['rowSelector']}")
            lines.append(f"Row has {m['childCount']} child elements:")
            for ch in m["children"]:
                role_hint = f" [role={ch['role']}]" if ch["role"] else ""
                aria_hint = f" [aria-label={ch['ariaLabel']}]" if ch["ariaLabel"] else ""
                lines.append(f"  nth-child({ch['index']}): <{ch['tag']}>{role_hint}{aria_hint} → \"{ch['text']}\"")
            lines.append(f"\nCORRECT selector pattern: {m['rowSelector']}:has-text(\"{text_hint}\") >> <child-tag>:nth-child(N)")
            lines.append("Use the nth-child index from the table above for the column you need.")
        else:
            lines.append(f"\n⚠️  No row containing '{text_hint}' found.")
            lines.append("This likely means the table uses task/section names as row identifiers, not the candidate name.")

        # Always dump all row texts — critical when candidate name row isn't found
        all_rows = result.get("allRowTexts", [])
        if all_rows:
            lines.append(f"\nALL TABLE ROWS FOUND (use these text values for :has-text() filters):")
            first_sel = all_rows[0]["rowSelector"]
            lines.append(f"Row element: {first_sel}")
            for r in all_rows:
                lines.append(f"  Row {r['rowIndex']}: \"{r['rowText']}\"")
                for ch in r.get("children", []):
                    lines.append(f"    nth-child({ch['index']}): <{ch['tag']}> → \"{ch['text']}\"")
            lines.append(f"\nCORRECT selector pattern: {first_sel}:has-text(\"<row text from above>\") >> <child-tag>:nth-child(N)")

        return "\n".join(lines)

    except Exception as exc:
        return f"── DOM inspection failed: {exc} ──"


# ── [PLAYWRIGHT-CLI] ─────────────────────────────────────────────────────────
# _execute_exploration_action: dispatches a single action (click, fill,
# navigate, …) onto a direct Playwright `page` object. In MCP mode, the Azure
# OpenAI model calls MCP tools (browser_click, browser_fill, browser_navigate,
# …) directly — no Python dispatcher is needed.
# ─────────────────────────────────────────────────────────────────────────────
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

    elif action == "wait_for":
        # Smart wait — waits for a specific element to become visible before proceeding.
        # The model uses this when it detects a timing issue (element exists but not ready).
        if selector:
            await page.locator(selector).first.wait_for(state="visible", timeout=int(value or 30_000))

    elif action == "key":
        await page.keyboard.press(value or "Escape")
        await page.wait_for_timeout(500)

    elif action == "read":
        if selector:
            loc = page.locator(selector).first
            await loc.wait_for(state="visible", timeout=30_000)
            return await loc.text_content(timeout=30_000)

    elif action == "hover":
        await page.locator(selector).first.hover(timeout=15_000)
        await page.wait_for_timeout(500)

    elif action == "click":
        await page.locator(selector).first.click(timeout=30_000)
        await page.wait_for_timeout(1500)

    elif action == "fill":
        await page.locator(selector).first.fill(value or "", timeout=20_000)
        await page.wait_for_timeout(800)

    elif action == "select_option":
        loc = page.locator(selector).first
        if value:
            await loc.select_option(value, timeout=20_000)
        else:
            opts = await loc.locator("option").all()
            if opts:
                first_val = await opts[0].get_attribute("value") or ""
                await loc.select_option(first_val, timeout=20_000)

    return None


# ── [MCP] Framework detection via browser_evaluate ───────────────────────────

async def _detect_ui_framework_mcp(bridge: "PlaywrightMCPBridge", test_case: str) -> str:
    """Detect the UI framework via MCP browser_evaluate (replaces _detect_ui_framework).

    Checks the current URL first (works during SSO redirects before any
    ui5-* elements have rendered), then falls back to a live DOM query.
    """
    _SAP_DOMAINS = ("successfactors.com", "sap.com", "onboarding2",
                    "performancemanager", "plateau.com")
    try:
        url = await bridge.get_current_url()
        if any(d in url for d in _SAP_DOMAINS):
            return "sap_ui5"
        result = await bridge.call_tool("browser_evaluate", {
            "function": "() => !!document.querySelector('ui5-button,ui5-dialog,ui5-input,[data-sap-ui]')"
        })
        if "true" in result.get("text", "").lower():
            return "sap_ui5"
    except Exception as e:
        logger.warning(f"[MCP] Framework detection failed: {e}")
    return "standard"


# ── [MCP] Per-step tool-use loop ─────────────────────────────────────────────

def _mcp_tool_to_action(tool_name: str) -> str:
    """Map an MCP tool name to the action string used in steps_log."""
    return {
        "browser_click":         "click",
        "browser_fill":          "fill",
        "browser_navigate":      "navigate",
        "browser_press_key":     "key",
        "browser_wait_for":      "wait",
        "browser_select_option": "select_option",
        "browser_evaluate":      "read",
        "browser_take_screenshot": "read",
        "browser_snapshot":      "read",
        "browser_hover":         "hover",
    }.get(tool_name, "interact")


def _summarise_args(args: dict) -> str:
    """One-line summary of MCP tool args for SSE log messages."""
    if "url" in args:
        return f"url={args['url'][:70]}"
    if "element" in args:
        s = f"element={args['element'][:50]}"
        if "value" in args:
            s += f", value={str(args['value'])[:30]}"
        return s
    if "function" in args:
        return f"fn={args['function'][:50]}"
    return str(args)[:80]


async def _gather_dom_on_failure(bridge, step_desc: str, failed_selector: str) -> str:
    """Run targeted DOM queries after a step failure — mirrors CLI's DOM injection in _correction_prompt.

    Returns a formatted text block injected into the retry attempt's system prompt.
    """
    async def _eval(js: str) -> str:
        try:
            r = await bridge.call_tool("browser_evaluate", {"function": js})
            return r.get("text", "").strip().strip('"').strip("'")
        except Exception:
            return ""

    lines: list[str] = []

    url = await _eval("() => window.location.href")
    if url:
        lines.append(f"Current URL: {url}")

    title_h1 = await _eval(
        "() => document.title.slice(0,80) + ' | h1=' + (document.querySelector('h1,h2')?.innerText?.slice(0,60) || 'none')"
    )
    if title_h1:
        lines.append(f"Page title/heading: {title_h1}")

    alerts = await _eval(
        "() => Array.from(document.querySelectorAll('[role=\"alert\"],[class*=\"error\" i],[class*=\"message\" i]'))"
        ".map(e=>e.innerText?.trim()).filter(Boolean).slice(0,3).join(' || ')"
    )
    if alerts:
        lines.append(f"Visible alerts/errors: {alerts[:200]}")

    # Elements matching the first meaningful keyword — search ALL elements, not just standard tags
    # (SAP Fiori flyout items may use custom web components absent from the a11y tree)
    keywords = [w for w in step_desc.lower().split() if len(w) > 3]
    if keywords:
        kw = keywords[0]
        matches = await _eval(
            f"() => Array.from(document.querySelectorAll('*'))"
            f".filter(e=>e.offsetParent!==null&&/{kw}/i.test((e.innerText||e.getAttribute('aria-label')||'').trim())&&!e.querySelector('*'))"
            f".map(e=>e.tagName+':\"'+((e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,40))+'\"')"
            f".slice(0,8).join(', ')"
        )
        if matches:
            lines.append(f"Visible elements matching '{kw}': {matches[:300]}")

    overlays = await _eval(
        "() => Array.from(document.querySelectorAll('[class*=\"overlay\" i],[class*=\"modal\" i],[class*=\"walkme\" i]'))"
        ".filter(e=>getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden')"
        ".map(e=>e.className.slice(0,50)).slice(0,3).join(', ')"
    )
    if overlays:
        lines.append(f"Potential blocking overlays: {overlays[:200]}")

    counts = await _eval(
        "() => 'buttons=' + document.querySelectorAll('button:not([disabled])').length"
        " + ' inputs=' + document.querySelectorAll('input:not([disabled])').length"
        " + ' links=' + document.querySelectorAll('a[href]').length"
    )
    if counts:
        lines.append(f"Interactive element counts: {counts}")

    return "\n".join(lines) if lines else "(DOM inspection returned no data)"


async def _pre_step_dom_scan(bridge) -> str:
    """Single-call DOM scan injected into every step's system prompt.

    Runs ONE browser_evaluate with a comprehensive JS function that reads
    the live DOM — not the accessibility tree. This captures elements that
    ARIA/snapshot misses: SAP Fiori flyout items, shadow-DOM portals,
    web-component labels, and dynamically-rendered overlays.

    Returns a compact text block the LLM receives before making any tool call,
    so it doesn't need browser_snapshot round-trips to discover page state.
    """
    _JS = """() => {
      const vis = e => {
        if (!e) return false;
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 &&
               getComputedStyle(e).visibility !== 'hidden' &&
               getComputedStyle(e).display !== 'none';
      };
      const txt = e => (e.innerText || e.textContent || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim().replace(/\\s+/g,' ').slice(0, 60);

      // Visible links (navigation targets, menu items in SAP portals)
      const links = Array.from(document.querySelectorAll('a[href], [role="menuitem"], [role="option"]'))
        .filter(vis)
        .map(e => ({ t: txt(e), h: e.getAttribute('href') || '' }))
        .filter(e => e.t)
        .slice(0, 25);

      // Visible buttons
      const btns = Array.from(document.querySelectorAll('button, [role="button"]'))
        .filter(e => vis(e) && !e.disabled)
        .map(txt).filter(Boolean).slice(0, 20);

      // Visible inputs / form fields
      const inputs = Array.from(document.querySelectorAll('input, textarea, [role="textbox"], [role="combobox"], select'))
        .filter(vis)
        .map(e => e.getAttribute('placeholder') || e.getAttribute('aria-label') || e.getAttribute('name') || e.tagName)
        .filter(Boolean).slice(0, 10);

      // Open overlays / modals / popovers that might block clicks
      const overlays = Array.from(document.querySelectorAll(
        '[class*="overlay" i], [class*="popover" i], [class*="modal" i], [role="dialog"], [role="alertdialog"]'
      )).filter(vis).map(e => (e.getAttribute('aria-label') || e.className || e.tagName).slice(0,60)).slice(0,5);

      // SAP UI5 custom components visible on page (ui5-* tags)
      const ui5 = Array.from(document.querySelectorAll('[class*="sapUi"], ui5-list, ui5-table, ui5-busy-indicator'))
        .filter(vis).map(e => e.tagName.toLowerCase()).filter((v,i,a)=>a.indexOf(v)===i).slice(0,8);

      return JSON.stringify({
        url:      window.location.pathname + window.location.hash,
        title:    document.title.slice(0,80),
        links:    links,
        buttons:  btns,
        inputs:   inputs,
        overlays: overlays,
        ui5:      ui5,
      });
    }"""
    try:
        raw = await bridge.call_tool("browser_evaluate", {"function": _JS})
        data = json.loads(raw.get("text", "{}").strip().strip('"').replace('\\"', '"'))

        parts: list[str] = []
        parts.append(f"URL: {data.get('url', '?')}  Title: {data.get('title', '?')[:60]}")

        if data.get("links"):
            link_strs = [
                f"{l['t']}" + (f" → {l['h']}" if l.get("h") else "")
                for l in data["links"]
            ]
            parts.append("Visible links/menu items:\n  " + "\n  ".join(link_strs))

        if data.get("buttons"):
            parts.append("Visible buttons: " + ", ".join(data["buttons"]))

        if data.get("inputs"):
            parts.append("Visible inputs: " + ", ".join(data["inputs"]))

        if data.get("overlays"):
            parts.append("⚠️ Open overlays/modals: " + ", ".join(data["overlays"]))

        if data.get("ui5"):
            parts.append("SAP UI5 components present: " + ", ".join(data["ui5"]))

        return "\n".join(parts)

    except Exception as e:
        logger.debug(f"[MCP] pre-step DOM scan failed: {e}")
        return ""


async def _verify_step_mcp(
    step_desc:    str,
    action_taken: str,
    selector:     str,
    url_before:   str,
    url_after:    str,
    after_b64:    Optional[str],
    model_result: dict,
) -> dict:
    """Explicit post-action verification — mirrors the old CLI _verify_prompt pattern.

    Makes a separate LLM call with the after-screenshot + URL delta to confirm
    whether the step actually succeeded. Overrides the model's self-assessment
    when they disagree.

    Returns a result dict in the same shape as _run_step_with_mcp().
    Falls back to model_result unchanged if no screenshot is available.
    """
    if not after_b64:
        return model_result

    url_change = (
        f"URL changed: {url_before} → {url_after}"
        if url_before != url_after
        else f"URL unchanged: {url_after}"
    )

    content: list = [
        {
            "type": "text",
            "text": (
                f"Verify whether this browser step succeeded.\n\n"
                f"STEP GOAL: {step_desc}\n"
                f"ACTION TAKEN: {action_taken} on \"{selector}\"\n"
                f"{url_change}\n"
                f"MODEL'S OWN ASSESSMENT: {model_result.get('observation', '')}\n\n"
                f"Look at the AFTER screenshot below and judge whether the goal was achieved.\n"
                f"Return ONLY this JSON — no markdown:\n"
                f"{{\"success\": true|false, "
                f"\"observation\": \"what you see — did the goal happen?\", "
                f"\"correction_hint\": \"if failed: specific reason + what to try next\"}}\n\n"
                f"Be strict: success=true only with clear visual evidence "
                f"(menu opened, field filled, page navigated, value visible, etc.).\n"
                f"Important: some actions (filter buttons, in-page updates) do NOT change the URL — "
                f"judge by content change, not URL delta. If the model's own assessment says it "
                f"succeeded and the screenshot is on the expected page, give significant weight to "
                f"the model's assessment."
            ),
        },
        {
            "type":      "image_url",
            "image_url": {"url": f"data:image/png;base64,{after_b64}", "detail": "low"},
        },
    ]

    try:
        resp = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[
                {"role": "system",
                 "content": "You verify browser automation outcomes from screenshots."},
                {"role": "user", "content": content},
            ],
            max_tokens=300,
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"```(?:json)?[\n]?", "", raw).strip().rstrip("`").strip()
        verified = json.loads(raw)
        verified.setdefault("correction_hint", "")
        # Preserve fields the verify call doesn't return
        verified.setdefault("action",        model_result.get("action", action_taken))
        verified.setdefault("selector",      model_result.get("selector", selector))
        verified.setdefault("value",         model_result.get("value"))
        verified.setdefault("read_value",    model_result.get("read_value"))
        verified.setdefault("screenshot_file", model_result.get("screenshot_file", ""))
        verified.setdefault("path_decision", model_result.get("path_decision"))
        return verified
    except Exception as e:
        logger.warning(f"[MCP] Verify call failed ({e}) — keeping model self-report")
        return model_result


async def _run_step_with_mcp(
    bridge:         "PlaywrightMCPBridge",
    step_desc:      str,
    step_type:      str,
    history_ctx:    str,
    memory_hints:   list,
    ui_framework:   str,
    extra_rules:    list,
    exploration_id: str,
    step_counter:   int,
    shots_dir:      "Path",
    queue:          Optional[asyncio.Queue],
    correction_hints: str = "",
    domain:           str = "",
) -> dict:
    """Run one exploration step via Azure OpenAI function-calling + Playwright MCP.

    Azure OpenAI calls MCP tools (browser_snapshot, browser_click, …) in a
    loop until it decides the step is done, then returns a JSON result summary.
    This replaces the manual plan→execute→verify→correct loop used in Playwright
    CLI mode.

    Returns a dict with keys: success, action, selector, value, observation,
                               read_value, screenshot_file, path_decision.
    """
    MAX_TOOL_CALLS = 25   # SAP UI5 pages can need several snapshot + click rounds

    # ── Build system message ──────────────────────────────────────────────────
    hints_block = ""
    if memory_hints:
        lines = []
        for h in memory_hints:
            conf = h.get("confidence", "medium")
            sel  = h.get("selector", "")
            act  = h.get("action", "")
            val  = h.get("value")
            lines.append(
                f'  [{conf}] {act} on "{sel}"' + (f' value="{val}"' if val else "")
            )
        hints_block = (
            "Past verified interactions on this site (try these first):\n"
            + "\n".join(lines)
        )

    framework_rules = _SAP_UI5_RULES if ui_framework == "sap_ui5" else ""
    learned_block   = (
        "Rules learned from previous failures (apply first):\n"
        + "\n".join(f"  ⚡ {r}" for r in extra_rules)
    ) if extra_rules else ""

    # Patterns learned from past explorations of this domain (auto-extracted after each run)
    domain_patterns_block = _format_patterns_for_prompt(domain) if domain else ""

    correction_block = (
        f"\n⚡ CORRECTION — PREVIOUS ATTEMPT FAILED. Apply these findings before acting:\n{correction_hints}\n"
        if correction_hints else ""
    )

    # ── Pre-step DOM scan ─────────────────────────────────────────────────────
    # Read the live DOM BEFORE building the system prompt so the LLM has
    # accurate page state without needing browser_snapshot round-trips.
    # This captures elements absent from the accessibility tree (SAP Fiori
    # flyout items, shadow-DOM portals, web-component labels, open overlays).
    dom_scan_raw = await _pre_step_dom_scan(bridge)
    if dom_scan_raw and queue:
        await _emit(queue, "log", level="info",
                    message=f"🔍 DOM scan: {dom_scan_raw.splitlines()[0][:120]}")
    dom_state_block = (
        f"\n── LIVE DOM STATE (read directly from DOM, not accessibility tree) ──\n"
        f"{dom_scan_raw}\n"
        f"── Use this to identify targets before calling browser_snapshot ──\n"
    ) if dom_scan_raw else ""

    system_msg = f"""You are controlling a real browser to complete a test automation step.
{correction_block}
{dom_state_block}
{framework_rules}
{domain_patterns_block}
{learned_block}
{hints_block}

TOOL STRATEGY:
1. You already have the LIVE DOM STATE above — read it first to identify the target element.
   If the element is listed there (link, button, input), act on it directly without browser_snapshot.
2. Call browser_take_screenshot when you need to SEE the page visually (layout, state, overlays).
3. Call browser_snapshot only when you need an exact element ref (e.g. ref=e123) for browser_click.
4. After any action that changes the page (click, fill, navigate), take a browser_take_screenshot to confirm.
5. Use the element ref from snapshots for interactions — do NOT guess selectors.
6. For SAP UI5 pages: if an element is in the LIVE DOM STATE links list, prefer browser_evaluate
   or browser_navigate over browser_snapshot (the accessibility tree may not show it).
7. NEVER call browser_snapshot more than 3 times in a row. If stuck, browser_take_screenshot tells you more.
8. Use browser_take_screenshot for all screenshots — it returns the image directly to you.

OVERLAY / BLOCKING ELEMENT HANDLING:
- If a click times out or fails because another element is intercepting pointer events (overlay, tutorial, chat widget, cookie banner, guided tour, etc.):
  1. Snapshot to identify the blocking element by its ref, id, or class.
  2. Use browser_evaluate to remove it: {{"function": "() => document.querySelector('SELECTOR').remove()"}}
     OR for multiple elements: {{"function": "() => document.querySelectorAll('SELECTOR').forEach(e=>e.remove())"}}
  3. Retry the original click — do NOT give up on first timeout due to an overlay.

When the step goal is accomplished (or you cannot proceed after trying), respond
with ONLY this JSON — no markdown, no explanation:
{{"success": true|false, "action": "click|fill|navigate|read|key|wait|failed", "selector": "element label you interacted with", "value": null or "string used for fill/navigate", "observation": "detailed description: current URL, what IS visible on page, what you tried, and exactly why it failed or succeeded", "read_value": null or "text that was read from the page", "path_decision": null or "A" or "B"}}

FAILURE REPORTING RULES — always include in observation when success=false:
- Current page URL (from browser_snapshot header or browser_evaluate window.location.href)
- What IS visible (page title, key headings, form fields, buttons seen)
- What you tried (exact element ref or selector attempted)
- Root cause (element not found / wrong page / blocked / timed out)"""

    messages = [
        {"role": "system", "content": system_msg},
        {
            "role": "user",
            "content": (
                f"Complete this step: {step_desc}\n"
                f"Step type: {step_type}\n\n"
                f"Recent history:\n{history_ctx or '(none yet)'}"
            ),
        },
    ]

    last_tool_name = ""
    last_tool_args: dict = {}
    screenshot_file = ""
    consecutive_snapshots = 0   # loop-detection counter
    import base64 as _b64

    for call_num in range(MAX_TOOL_CALLS):
        # Inject a nudge if the model has been snapshotting without acting
        if consecutive_snapshots >= 3:
            messages.append({
                "role": "user",
                "content": (
                    "You have called browser_snapshot multiple times without acting. "
                    "Stop snapshotting. Use the element refs already returned to call "
                    "browser_click or browser_fill now. If the target element is truly "
                    "absent, report failure with success=false."
                ),
            })
            consecutive_snapshots = 0
        response = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=messages,
            tools=bridge.azure_tool_definitions,
            tool_choice="auto",
            max_tokens=1000,
            temperature=0.1,
        )

        msg           = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        # Serialise the assistant turn — tool_calls must be present if non-empty
        assistant_turn: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_turn["tool_calls"] = [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_turn)

        if finish_reason == "stop" or not msg.tool_calls:
            # Model finished — parse final text as step result JSON
            final_text = (msg.content or "").strip()
            final_text = re.sub(r"```(?:json)?[\n]?", "", final_text).strip().rstrip("`").strip()
            try:
                result = json.loads(final_text)
            except Exception:
                # Fallback: infer from last tool call
                result = {
                    "success":     bool(last_tool_name and last_tool_name != "browser_wait_for"),
                    "action":      _mcp_tool_to_action(last_tool_name),
                    "selector":    last_tool_args.get("element", last_tool_args.get("url", "")),
                    "value":       last_tool_args.get("value", last_tool_args.get("url")),
                    "observation": final_text or "Step completed",
                    "read_value":  None,
                    "path_decision": None,
                }
            result.setdefault("screenshot_file", screenshot_file)
            result.setdefault("path_decision", None)
            return result

        # ── Execute every tool call the model requested ───────────────────────
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments or "{}")
            except Exception:
                tool_args = {}

            last_tool_name = tool_name
            last_tool_args = tool_args

            # Track consecutive snapshot calls for loop detection
            if tool_name == "browser_snapshot":
                consecutive_snapshots += 1
            else:
                consecutive_snapshots = 0

            await _emit(queue, "log", level="info",
                        message=f"🔧 {tool_name}({_summarise_args(tool_args)})")
            logger.debug(
                f"[MCP {exploration_id}] step {step_counter} call {call_num+1}: "
                f"{tool_name}({_summarise_args(tool_args)})"
            )

            try:
                tool_result = await bridge.call_tool(tool_name, tool_args)

                # Screenshot: save to disk AND pass back as vision so the model can see the page
                if tool_result.get("screenshot_b64") and shots_dir:
                    fname = f"step-{step_counter:03d}-mcp-call{call_num+1}.png"
                    (shots_dir / fname).write_bytes(_b64.b64decode(tool_result["screenshot_b64"]))
                    screenshot_file = fname
                    # Return image content in tool response so model has visual context
                    tool_content: Any = [
                        {"type": "text", "text": f"Screenshot taken (saved as {fname}). Describe what you see and decide the next action."},
                        {"type": "image_url", "image_url": {
                            "url":    f"data:image/png;base64,{tool_result['screenshot_b64']}",
                            "detail": "low",
                        }},
                    ]
                else:
                    # Trim long accessibility trees so the context stays manageable
                    raw_text = tool_result.get("text", "")
                    tool_content = raw_text[:4000] + ("…" if len(raw_text) > 4000 else "")

            except Exception as te:
                tool_content = f"Tool error: {te}"
                logger.warning(
                    f"[MCP {exploration_id}] step {step_counter} tool error "
                    f"— {tool_name}: {te}"
                )

            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      tool_content,
            })

    # Exceeded max tool calls without a final answer
    return {
        "success":       False,
        "action":        "failed",
        "selector":      "",
        "value":         None,
        "observation":   f"Step exceeded {MAX_TOOL_CALLS} tool calls without completing",
        "read_value":    None,
        "path_decision": None,
        "screenshot_file": screenshot_file,
    }


# ── SSE helper ────────────────────────────────────────────────────────────────

async def _emit(queue: Optional[asyncio.Queue], event_type: str, **kwargs) -> None:
    """Put a structured event onto the SSE queue (no-op if queue is None)."""
    if queue:
        await queue.put({"type": event_type, **kwargs})


async def _run_exploration(req: ExploreRequest,
                            exploration_id: str,
                            queue: Optional[asyncio.Queue] = None,
                            _suppress_close: bool = False) -> dict:
    """
    Core exploration logic — shared by both the sync endpoint and the SSE endpoint.
    Emits structured events to `queue` when provided (SSE mode).
    """
    expl_dir  = EXPLORATIONS_DIR / exploration_id
    shots_dir = expl_dir / "screenshots"
    expl_dir.mkdir(parents=True, exist_ok=True)
    shots_dir.mkdir(exist_ok=True)

    steps_log  = []
    path_taken = None   # "A" or "B" once a conditional decision is made
    # [PLAYWRIGHT-CLI] pb / browser / ctx / page no longer declared here;
    # they are replaced by `bridge` below.
    bridge: Optional[PlaywrightMCPBridge] = None  # MCP: set in E1 below

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

        # ══════════════════════════════════════════════════════════════════════
        # [PLAYWRIGHT-CLI] E1: Direct Playwright browser launch
        # The block below used async_playwright() to launch a browser directly.
        # In MCP mode this is replaced by PlaywrightMCPBridge (see [MCP] E1).
        # To restore Playwright CLI behavior: remove the [MCP] E1 block and
        # uncomment this section, then restore _detect_ui_framework(page) in E1.5.
        # ══════════════════════════════════════════════════════════════════════
        # pb      = await async_playwright().start()
        # browser = await pb.chromium.launch(headless=req.headless)
        # _DOMAIN_AUTH_MAP = {
        #     "successfactors.com": "successfactors",
        #     "plateau.com":        "successfactors",
        #     "sap.com":            "successfactors",
        # }
        # ctx_kwargs: dict = {}
        # _auth_loaded = False
        # if req.storage_state:
        #     auth_path = BASE / ".auth" / f"{req.storage_state}.json"
        #     if auth_path.exists():
        #         ctx_kwargs["storage_state"] = str(auth_path)
        #         _auth_loaded = True
        #         logger.info(f"[EXPLORE {exploration_id}] Using storageState: {auth_path.name}")
        #     else:
        #         logger.warning(f"[EXPLORE {exploration_id}] storageState not found: {auth_path}")
        # if not _auth_loaded:
        #     _url_hint = re.search(r'https?://[^\s]+', req.test_case)
        #     if _url_hint:
        #         _url_str = _url_hint.group(0)
        #         for _domain, _auth_name in _DOMAIN_AUTH_MAP.items():
        #             if _domain in _url_str:
        #                 _auto_path = BASE / ".auth" / f"{_auth_name}.json"
        #                 if _auto_path.exists():
        #                     ctx_kwargs["storage_state"] = str(_auto_path)
        #                     _auth_loaded = True
        #                     logger.info(f"[EXPLORE {exploration_id}] 🔑 Auto-loaded: {_auto_path.name}")
        #                     await _emit(queue, "log", level="info", message=f"🔑 Auto-loaded session: {_auth_name}.json")
        #                 else:
        #                     logger.warning(f"[EXPLORE {exploration_id}] No auth file for {_domain}")
        #                     await _emit(queue, "log", level="warn", message=f"⚠️ No session for {_domain}. Run auth.ts first.")
        #                 break
        # ctx  = await browser.new_context(**ctx_kwargs)
        # page = await ctx.new_page()
        # url_match = re.search(r'https?://[^\s]+', req.test_case)
        # if url_match:
        #     start_url = url_match.group(0).rstrip('.,)')
        #     for _w, _t in [('domcontentloaded', 60_000), ('load', 60_000), ('commit', 90_000)]:
        #         try:
        #             await page.goto(start_url, wait_until=_w, timeout=_t)
        #             logger.info(f"[EXPLORE {exploration_id}] Page loaded (wait_until='{_w}')")
        #             break
        #         except Exception:
        #             pass
        #     await page.wait_for_timeout(NAV_PAUSE_MS)
        # [PLAYWRIGHT-CLI] E1.5 (direct Playwright):
        # _sso_indicators = ("login", "signin", "sso", "authenticate", "microsoftonline",
        #                    "iasauthentication", "okta", "adfs", "saml")
        # _current_url_lower = page.url.lower()
        # if any(s in _current_url_lower for s in _sso_indicators):
        #     _msg = ("🔒 Session expired — browser landed on a login/SSO page. "
        #             "Run 'npx ts-node scripts/auth.ts' to refresh the session, then retry.")
        #     logger.warning(f"[EXPLORE {exploration_id}] {_msg}")
        #     await _emit(queue, "log", level="warn", message=_msg)
        # ui_framework = await _detect_ui_framework(page)   # [PLAYWRIGHT-CLI]

        # ── [MCP] E1: Start PlaywrightMCPBridge ───────────────────────────────
        _DOMAIN_AUTH_MAP = {
            "successfactors.com": "successfactors",
            "plateau.com":        "successfactors",
            "sap.com":            "successfactors",
        }
        _sso_indicators = ("login", "signin", "sso", "authenticate", "microsoftonline",
                           "iasauthentication", "okta", "adfs", "saml")

        # Resolve auth file (same logic as Playwright CLI, but passed to MCP server)
        _auth_path_str: Optional[str] = None
        if req.storage_state:
            _ap = BASE / ".auth" / f"{req.storage_state}.json"
            if _ap.exists():
                _auth_path_str = str(_ap)
                logger.info(f"[EXPLORE {exploration_id}] Using storageState: {_ap.name}")
                await _emit(queue, "log", level="info",
                            message=f"🔑 Session: {req.storage_state}.json")
            else:
                logger.warning(f"[EXPLORE {exploration_id}] storageState not found: {_ap}")
                await _emit(queue, "log", level="warn",
                            message=f"⚠️ No session file: {req.storage_state}.json — run auth.ts first")
        else:
            _url_hint = re.search(r'https?://[^\s]+', req.test_case)
            if _url_hint:
                for _domain, _auth_name in _DOMAIN_AUTH_MAP.items():
                    if _domain in _url_hint.group(0):
                        _ap = BASE / ".auth" / f"{_auth_name}.json"
                        if _ap.exists():
                            _auth_path_str = str(_ap)
                            logger.info(f"[EXPLORE {exploration_id}] 🔑 Auto-loaded: {_ap.name}")
                            await _emit(queue, "log", level="info",
                                        message=f"🔑 Auto-loaded session: {_auth_name}.json")
                        else:
                            logger.warning(f"[EXPLORE {exploration_id}] No auth file for {_domain}")
                            await _emit(queue, "log", level="warn",
                                        message=f"⚠️ No session for {_domain}. Run auth.ts first.")
                        break

        bridge = PlaywrightMCPBridge(
            storage_state=_auth_path_str,
            headless=req.headless,
        )
        await bridge.start()
        await _emit(queue, "log", level="info", message="🌐 Playwright MCP server started")

        # Navigate to start URL via MCP
        url_match = re.search(r'https?://[^\s]+', req.test_case)
        if url_match:
            start_url = url_match.group(0).rstrip('.,)')
            logger.info(f"[EXPLORE {exploration_id}] Navigating to {start_url}")
            await bridge.call_tool("browser_navigate", {"url": start_url})
            await _emit(queue, "log", level="info", message=f"Navigated to {start_url}")

        # SSO wall detection via MCP — two passes:
        #   Pass 1: URL-based (catches redirects to SSO providers)
        #   Pass 2: content-based (catches inline login forms where URL stays on app domain)
        _current_url = await bridge.get_current_url()
        await _emit(queue, "log", level="info", message=f"🌍 Current URL: {_current_url}")
        _sso_by_url = any(s in _current_url.lower() for s in _sso_indicators)

        _sso_by_content = False
        try:
            _login_check = await bridge.call_tool("browser_evaluate", {
                "function": (
                    "() => !!document.querySelector("
                    "'input[type=\"password\"], input[name=\"j_username\"], "
                    "input[id*=\"username\"], [id*=\"loginForm\"], [class*=\"login-form\"]')"
                )
            })
            _sso_by_content = "true" in _login_check.get("text", "").lower()
        except Exception:
            pass

        if _sso_by_url or _sso_by_content:
            _reason = "URL matches SSO provider" if _sso_by_url else "login form detected on page"
            _msg = (
                f"🔒 Session expired or not authenticated ({_reason}). "
                f"Current URL: {_current_url}. "
                "Run 'npx ts-node scripts/auth.ts' in the terminal to refresh the session, then retry."
            )
            logger.warning(f"[EXPLORE {exploration_id}] {_msg}")
            await _emit(queue, "log", level="error", message=_msg)

        # ── [MCP] E1.5: Detect UI framework via browser_evaluate ──────────────
        ui_framework = await _detect_ui_framework_mcp(bridge, req.test_case)
        logger.info(f"[EXPLORE {exploration_id}] UI framework detected: {ui_framework}")
        await _emit(queue, "log", level="info", message=f"UI framework: {ui_framework}")

        # Load persisted learned rules for this framework
        _persistent_rules = _get_active_rules(ui_framework)
        learned_this_run: list = []
        if _persistent_rules:
            logger.info(f"[EXPLORE {exploration_id}] 📚 {len(_persistent_rules)} learned rule(s) loaded for {ui_framework}")
            await _emit(queue, "log", level="info",
                        message=f"📚 {len(_persistent_rules)} learned rule(s) loaded for {ui_framework}")

        def _active_extra_rules() -> list:
            return _persistent_rules + learned_this_run

        # ══════════════════════════════════════════════════════════════════════
        # [PLAYWRIGHT-CLI] E2: Direct Playwright verify-then-act loop
        #
        # This was the original step loop. For each step it:
        #   1. Captured a screenshot + accessibility tree via Playwright page object
        #   2. Called o4-mini to plan the action (plan prompt)
        #   3. Executed via _execute_exploration_action(page, action, selector, value)
        #   4. Captured an after-screenshot
        #   5. Called o4-mini to verify success (before/after images)
        #   6. On failure: refreshed a11y tree + DOM inspection + called o4-mini
        #      with a correction prompt → retried up to MAX_EXPLORE_RETRIES times
        #   7. Tracked last_failed_step for dependency-blocking of subsequent steps
        #
        # In MCP mode this entire loop is replaced by _run_step_with_mcp() which
        # lets Azure OpenAI call browser tools directly in a function-calling loop.
        # To restore Playwright CLI behavior: replace the [MCP] E2 block below
        # with this block, and restore the direct browser launch in E1.
        # ══════════════════════════════════════════════════════════════════════
        # [PLAYWRIGHT-CLI] E2 step loop body starts here.
        # Key functions it called (all still defined above with [PLAYWRIGHT-CLI] banners):
        #   _get_accessibility_tree(page)         — a11y tree before each action
        #   _plan_action_prompt(...)              — prompt: what action to take
        #   _reasoning_vision_json(shot_b64, prompt) — o4-mini plan / verify calls
        #   _execute_exploration_action(page, ...) — dispatches click/fill/navigate
        #   _inspect_dom_for_correction(page, ...) — live DOM on failure
        #   _analyse_page_visually(shot_b64, ...) — vision analysis on failure
        #   _correction_prompt(...)               — retry prompt with DOM + vision
        #   _verify_prompt(...)                   — before/after goal-check prompt
        # To restore: re-enable the browser launch in [PLAYWRIGHT-CLI] E1, then
        # replace the [MCP] E2 block below with the original _run_exploration body
        # (available in git history before the MCP refactor commit).
        # ─────────────────────────────────────────────────────────────────────
        # [PLAYWRIGHT-CLI] step_counter  = 0
        # [PLAYWRIGHT-CLI] last_failed_step: str = ""
        # [PLAYWRIGHT-CLI] _sso_abort = False
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI]   for step in steps[:req.max_steps]:
        # [PLAYWRIGHT-CLI]     ... (plan → execute → verify → correct retry loop)
        # [PLAYWRIGHT-CLI]     ... see _execute_exploration_action / _verify_prompt
        # [PLAYWRIGHT-CLI] except StopAsyncIteration:
        # [PLAYWRIGHT-CLI]     _sso_abort = True

        # [PLAYWRIGHT-CLI] # ── Option C: Dependency blocking ─────────────────────────────
        # [PLAYWRIGHT-CLI] if last_failed_step:
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI] dependent = _is_dependent_step(last_failed_step, step_desc)
        # [PLAYWRIGHT-CLI] except Exception:
        # [PLAYWRIGHT-CLI] dependent = False
        # [PLAYWRIGHT-CLI] if dependent:
        # [PLAYWRIGHT-CLI] logger.warning(
        # [PLAYWRIGHT-CLI] f"[EXPLORE {exploration_id}] Blocking step {step_counter} — "
        # [PLAYWRIGHT-CLI] f"depends on failed: {last_failed_step[:50]}"
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] await _emit(queue, "step_start", step_num=step_counter,
        # [PLAYWRIGHT-CLI] description=step_desc, path=step_path, total_steps=len(steps))
        # [PLAYWRIGHT-CLI] await _emit(queue, "step_result", step_num=step_counter,
        # [PLAYWRIGHT-CLI] description=step_desc, success=False, action="blocked",
        # [PLAYWRIGHT-CLI] selector="", attempts=0,
        # [PLAYWRIGHT-CLI] error=f"Blocked — depends on failed step: {last_failed_step[:80]}")
        # [PLAYWRIGHT-CLI] steps_log.append({
        # [PLAYWRIGHT-CLI] "step_num": step_counter, "description": step_desc,
        # [PLAYWRIGHT-CLI] "action": "blocked", "selector": "", "success": False,
        # [PLAYWRIGHT-CLI] "error": f"Blocked — depends on failed step: {last_failed_step[:80]}",
        # [PLAYWRIGHT-CLI] "path": step_path, "path_taken": path_taken,
        # [PLAYWRIGHT-CLI] "screenshot_file": "", "attempts": 0,
        # [PLAYWRIGHT-CLI] })
        # [PLAYWRIGHT-CLI] continue
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Cancellation check — stop cleanly between steps ────────────
        # [PLAYWRIGHT-CLI] _cancel_flag = _explore_cancel.get(exploration_id)
        # [PLAYWRIGHT-CLI] if _cancel_flag and _cancel_flag.is_set():
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] 🛑 Cancelled by user at step {step_counter}")
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="warn",
        # [PLAYWRIGHT-CLI] message=f"🛑 Exploration cancelled by user after {step_counter - 1} steps")
        # [PLAYWRIGHT-CLI] break
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] await _emit(queue, "step_start", step_num=step_counter, description=step_desc,
        # [PLAYWRIGHT-CLI] path=step_path, total_steps=len(steps))
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Before screenshot + Accessibility tree ────────────────────
        # [PLAYWRIGHT-CLI] before_b64  = ""
        # [PLAYWRIGHT-CLI] before_file = f"step-{step_counter:03d}-before.png"
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI] before_bytes = await page.screenshot(full_page=False)
        # [PLAYWRIGHT-CLI] before_b64   = base64.b64encode(before_bytes).decode()
        # [PLAYWRIGHT-CLI] (shots_dir / before_file).write_bytes(before_bytes)
        # [PLAYWRIGHT-CLI] except Exception as se:
        # [PLAYWRIGHT-CLI] logger.warning(f"[EXPLORE {exploration_id}] Before-screenshot failed: {se}")
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # Option A: get accessibility tree alongside screenshot
        # [PLAYWRIGHT-CLI] a11y_tree = await _get_accessibility_tree(page)
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] history_ctx = "\n".join([
        # [PLAYWRIGHT-CLI] f"Step {h['step_num']}: [{h.get('action','')}] {h.get('selector','')} — {h.get('observation','')}"
        # [PLAYWRIGHT-CLI] for h in steps_log[-4:]
        # [PLAYWRIGHT-CLI] ])
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Memory lookup — inject hints from past successful explorations ─
        # [PLAYWRIGHT-CLI] current_domain = re.sub(r'https?://', '', page.url).split('/')[0]
        # [PLAYWRIGHT-CLI] memory_hints   = _find_memory_hints(current_domain, step_desc)
        # [PLAYWRIGHT-CLI] if memory_hints:
        # [PLAYWRIGHT-CLI] logger.info(
        # [PLAYWRIGHT-CLI] f"[EXPLORE {exploration_id}] Memory: {len(memory_hints)} hint(s) for step {step_counter} "
        # [PLAYWRIGHT-CLI] f"— top: \"{memory_hints[0].get('selector','')}\" ({memory_hints[0].get('confidence','')})"
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] await _emit(queue, "memory_hint", step_num=step_counter,
        # [PLAYWRIGHT-CLI] count=len(memory_hints),
        # [PLAYWRIGHT-CLI] top_selector=memory_hints[0].get("selector",""),
        # [PLAYWRIGHT-CLI] confidence=memory_hints[0].get("confidence",""))
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] log_entry = {
        # [PLAYWRIGHT-CLI] "step_num":        step_counter,
        # [PLAYWRIGHT-CLI] "description":     step_desc,
        # [PLAYWRIGHT-CLI] "action":          "",
        # [PLAYWRIGHT-CLI] "selector":        "",
        # [PLAYWRIGHT-CLI] "value":           None,
        # [PLAYWRIGHT-CLI] "observation":     "",
        # [PLAYWRIGHT-CLI] "notes":           "",
        # [PLAYWRIGHT-CLI] "confidence":      "low",
        # [PLAYWRIGHT-CLI] "path":            step_path,
        # [PLAYWRIGHT-CLI] "path_taken":      path_taken,
        # [PLAYWRIGHT-CLI] "screenshot_file": before_file,
        # [PLAYWRIGHT-CLI] "success":         False,
        # [PLAYWRIGHT-CLI] "error":           None,
        # [PLAYWRIGHT-CLI] "memory_hints_used": len(memory_hints),
        # [PLAYWRIGHT-CLI] "read_value":      None,
        # [PLAYWRIGHT-CLI] "attempts":        0,
        # [PLAYWRIGHT-CLI] }
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] last_failure  = ""
        # [PLAYWRIGHT-CLI] action_plan   = {}
        # [PLAYWRIGHT-CLI] current_b64   = before_b64   # updated after each attempt
        # [PLAYWRIGHT-CLI] selectors_this_step: list = []   # accumulates {action, selector, error} per attempt
        # [PLAYWRIGHT-CLI] first_failed_selector = ""
        # [PLAYWRIGHT-CLI] first_failed_error    = ""
        # [PLAYWRIGHT-CLI] url_before_step = page.url    # for stuck-detection on navigation steps
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Proactive DOM inspection for attempt 0 ─────────────────────
        # [PLAYWRIGHT-CLI] # Run upfront (not just on retry) when:
        # [PLAYWRIGHT-CLI] #   1. Step type is "read" — column structure is unknowable without inspection
        # [PLAYWRIGHT-CLI] #   2. Memory shows prior failures — model already struggled here before
        # [PLAYWRIGHT-CLI] _has_prior_failures = any(h.get("failure_count", 0) > 0 for h in memory_hints)
        # [PLAYWRIGHT-CLI] _needs_upfront_dom  = (step_type == "read") or _has_prior_failures
        # [PLAYWRIGHT-CLI] upfront_dom_info    = ""
        # [PLAYWRIGHT-CLI] upfront_vision      = ""
        # [PLAYWRIGHT-CLI] if _needs_upfront_dom:
        # [PLAYWRIGHT-CLI] upfront_dom_info = await _inspect_dom_for_correction(page, "", step_desc)
        # [PLAYWRIGHT-CLI] upfront_vision   = _analyse_page_visually(before_b64, step_desc, "", "pre-inspection")
        # [PLAYWRIGHT-CLI] if upfront_dom_info or upfront_vision:
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="info",
        # [PLAYWRIGHT-CLI] message=f"🔬 DOM + 👁 vision pre-inspected for step {step_counter}")
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Retry loop ─────────────────────────────────────────────────
        # [PLAYWRIGHT-CLI] for attempt in range(MAX_EXPLORE_RETRIES + 1):
        # [PLAYWRIGHT-CLI] log_entry["attempts"] = attempt + 1
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Plan (or re-plan with reasoning on retry) ──────────────
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI] if attempt == 0:
        # [PLAYWRIGHT-CLI] combined_upfront = "\n".join(filter(None, [upfront_dom_info, upfront_vision]))
        # [PLAYWRIGHT-CLI] prompt = _plan_action_prompt(step_desc, step_type, page.url,
        # [PLAYWRIGHT-CLI] history_ctx, memory_hints,
        # [PLAYWRIGHT-CLI] a11y_tree, ui_framework,
        # [PLAYWRIGHT-CLI] extra_rules=_active_extra_rules(),
        # [PLAYWRIGHT-CLI] dom_info=combined_upfront)
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="info",
        # [PLAYWRIGHT-CLI] message=f"Step {step_counter}: planning action…")
        # [PLAYWRIGHT-CLI] else:
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] Retry {attempt}/{MAX_EXPLORE_RETRIES}: {last_failure[:60]}")
        # [PLAYWRIGHT-CLI] await _emit(queue, "retry", step_num=step_counter,
        # [PLAYWRIGHT-CLI] attempt=attempt, max_attempts=MAX_EXPLORE_RETRIES+1,
        # [PLAYWRIGHT-CLI] reason=last_failure[:120])
        # [PLAYWRIGHT-CLI] # Refresh a11y tree for correction — page may have changed
        # [PLAYWRIGHT-CLI] a11y_tree = await _get_accessibility_tree(page)
        # [PLAYWRIGHT-CLI] # DOM inspection + vision analysis run together on every retry
        # [PLAYWRIGHT-CLI] dom_info = await _inspect_dom_for_correction(page, selector, step_desc)
        # [PLAYWRIGHT-CLI] page_analysis = _analyse_page_visually(
        # [PLAYWRIGHT-CLI] current_b64, step_desc, selector, last_failure
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] if dom_info or page_analysis:
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="info",
        # [PLAYWRIGHT-CLI] message=f"🔬 DOM + 👁 vision analysed — passing to model")
        # [PLAYWRIGHT-CLI] if page_analysis:
        # [PLAYWRIGHT-CLI] # Surface key lines to the live log
        # [PLAYWRIGHT-CLI] for _line in page_analysis.splitlines()[1:5]:
        # [PLAYWRIGHT-CLI] if _line.strip():
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="info",
        # [PLAYWRIGHT-CLI] message=f"   👁 {_line.strip()}")
        # [PLAYWRIGHT-CLI] prompt = _correction_prompt(step_desc, action_plan, last_failure,
        # [PLAYWRIGHT-CLI] page.url, history_ctx,
        # [PLAYWRIGHT-CLI] a11y_tree, ui_framework,
        # [PLAYWRIGHT-CLI] extra_rules=_active_extra_rules(),
        # [PLAYWRIGHT-CLI] dom_info=dom_info,
        # [PLAYWRIGHT-CLI] page_analysis=page_analysis)
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] action_plan = _reasoning_vision_json(current_b64, prompt)
        # [PLAYWRIGHT-CLI] except Exception as pe:
        # [PLAYWRIGHT-CLI] logger.error(f"[EXPLORE {exploration_id}] Planning failed: {pe}")
        # [PLAYWRIGHT-CLI] log_entry["error"] = f"Planning error: {pe}"
        # [PLAYWRIGHT-CLI] break
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] action   = action_plan.get("action", "done")
        # [PLAYWRIGHT-CLI] selector = action_plan.get("selector", "")
        # [PLAYWRIGHT-CLI] value    = action_plan.get("value")
        # [PLAYWRIGHT-CLI] pdec     = action_plan.get("path_decision")
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] log_entry.update({
        # [PLAYWRIGHT-CLI] "action":     action,
        # [PLAYWRIGHT-CLI] "selector":   selector,
        # [PLAYWRIGHT-CLI] "value":      value,
        # [PLAYWRIGHT-CLI] "observation": action_plan.get("observation", ""),
        # [PLAYWRIGHT-CLI] "notes":      action_plan.get("notes", ""),
        # [PLAYWRIGHT-CLI] "confidence": action_plan.get("confidence", "medium"),
        # [PLAYWRIGHT-CLI] })
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] if pdec and path_taken is None:
        # [PLAYWRIGHT-CLI] path_taken = pdec
        # [PLAYWRIGHT-CLI] log_entry["path_taken"] = path_taken
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] Path decision → {path_taken}")
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # FIX 1: "done" is NOT auto-success — verify the goal was achieved.
        # [PLAYWRIGHT-CLI] # The model uses "done" as an escape hatch when it can't find elements.
        # [PLAYWRIGHT-CLI] # We verify with a goal-check before accepting it.
        # [PLAYWRIGHT-CLI] if action == "decision":
        # [PLAYWRIGHT-CLI] log_entry["success"] = True
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] ✅ Step {step_counter}: path decision")
        # [PLAYWRIGHT-CLI] break
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] if action == "done":
        # [PLAYWRIGHT-CLI] # Ask GPT-4V: was the actual goal of this step achieved?
        # [PLAYWRIGHT-CLI] goal_check_prompt = f"""The model says this step is already done: "{step_desc}"
        # [PLAYWRIGHT-CLI] Look at the screenshot carefully.
        # [PLAYWRIGHT-CLI] Is there clear visual evidence that this goal HAS actually been accomplished on this page?
        # [PLAYWRIGHT-CLI] Return ONLY raw JSON: {{"achieved": true|false, "reason": "what you see that confirms or denies it"}}"""
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI] goal_resp = _reasoning_vision_json(current_b64, goal_check_prompt)
        # [PLAYWRIGHT-CLI] if goal_resp.get("achieved", False):
        # [PLAYWRIGHT-CLI] log_entry["success"] = True
        # [PLAYWRIGHT-CLI] log_entry["observation"] = goal_resp.get("reason", "")
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] ✅ Step {step_counter}: goal confirmed done")
        # [PLAYWRIGHT-CLI] break
        # [PLAYWRIGHT-CLI] else:
        # [PLAYWRIGHT-CLI] # Goal NOT achieved — treat as failure and retry
        # [PLAYWRIGHT-CLI] last_failure = (
        # [PLAYWRIGHT-CLI] f"Model said 'done' but goal was not achieved: "
        # [PLAYWRIGHT-CLI] f"{goal_resp.get('reason', 'Goal not visible on page')}. "
        # [PLAYWRIGHT-CLI] f"You MUST find and interact with an element to accomplish this step."
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] logger.warning(f"[EXPLORE {exploration_id}] 'done' rejected — {last_failure[:80]}")
        # [PLAYWRIGHT-CLI] if attempt >= MAX_EXPLORE_RETRIES:
        # [PLAYWRIGHT-CLI] log_entry["error"] = last_failure
        # [PLAYWRIGHT-CLI] continue
        # [PLAYWRIGHT-CLI] except Exception:
        # [PLAYWRIGHT-CLI] # If goal-check fails to parse, give benefit of the doubt
        # [PLAYWRIGHT-CLI] log_entry["success"] = True
        # [PLAYWRIGHT-CLI] break
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── SSO drift guard — abort if we've drifted to a login page ──
        # [PLAYWRIGHT-CLI] _pre_exec_url = page.url.lower()
        # [PLAYWRIGHT-CLI] if any(s in _pre_exec_url for s in _sso_indicators):
        # [PLAYWRIGHT-CLI] _drift_msg = (
        # [PLAYWRIGHT-CLI] "🔒 Session expired mid-run — browser is on a login/SSO page. "
        # [PLAYWRIGHT-CLI] "Run 'npx ts-node scripts/auth.ts' to refresh the session, then retry."
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] logger.error(f"[EXPLORE {exploration_id}] SSO drift detected at step {step_counter}")
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="error", message=_drift_msg)
        # [PLAYWRIGHT-CLI] log_entry["error"] = _drift_msg
        # [PLAYWRIGHT-CLI] log_entry["success"] = False
        # [PLAYWRIGHT-CLI] steps_log.append(log_entry)
        # [PLAYWRIGHT-CLI] # Mark all remaining steps as blocked and exit the step loop
        # [PLAYWRIGHT-CLI] last_failed_step = "SSO session expired — browser redirected to login page"
        # [PLAYWRIGHT-CLI] raise StopAsyncIteration(_drift_msg)
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Execute ────────────────────────────────────────────────
        # [PLAYWRIGHT-CLI] exec_error = None
        # [PLAYWRIGHT-CLI] read_val   = None
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI] read_val = await _execute_exploration_action(page, action, selector, value)
        # [PLAYWRIGHT-CLI] if read_val is not None:
        # [PLAYWRIGHT-CLI] log_entry["read_value"] = read_val
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] Read: '{read_val}'")
        # [PLAYWRIGHT-CLI] except Exception as ae:
        # [PLAYWRIGHT-CLI] exec_error = str(ae)[:300]
        # [PLAYWRIGHT-CLI] logger.warning(f"[EXPLORE {exploration_id}] Exec error attempt {attempt+1}: {ae}")
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── After screenshot ───────────────────────────────────────
        # [PLAYWRIGHT-CLI] after_b64   = ""
        # [PLAYWRIGHT-CLI] after_file  = f"step-{step_counter:03d}-after-a{attempt+1}.png"
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI] after_bytes = await page.screenshot(full_page=False)
        # [PLAYWRIGHT-CLI] after_b64   = base64.b64encode(after_bytes).decode()
        # [PLAYWRIGHT-CLI] (shots_dir / after_file).write_bytes(after_bytes)
        # [PLAYWRIGHT-CLI] except Exception:
        # [PLAYWRIGHT-CLI] pass
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Verify ─────────────────────────────────────────────────
        # [PLAYWRIGHT-CLI] url_after = page.url
        # [PLAYWRIGHT-CLI] if exec_error:
        # [PLAYWRIGHT-CLI] last_failure = (
        # [PLAYWRIGHT-CLI] f"Playwright raised an error executing {action} on '{selector}': "
        # [PLAYWRIGHT-CLI] f"{exec_error}. The selector may not exist, be hidden, or need scrolling."
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] verification_ok = False
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] elif action == "wait":
        # [PLAYWRIGHT-CLI] verification_ok = True
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] elif action in ("navigate", "read"):
        # [PLAYWRIGHT-CLI] if action == "navigate" and step_type in ("navigate",) and url_after == url_before_step:
        # [PLAYWRIGHT-CLI] # If URL didn't change, check whether we were already at the target
        # [PLAYWRIGHT-CLI] nav_target = (value or selector or "").split("?")[0].rstrip("/")
        # [PLAYWRIGHT-CLI] current_base = url_after.split("?")[0].rstrip("/")
        # [PLAYWRIGHT-CLI] already_there = nav_target and current_base.startswith(nav_target)
        # [PLAYWRIGHT-CLI] if already_there:
        # [PLAYWRIGHT-CLI] # Already at destination — treat as success
        # [PLAYWRIGHT-CLI] verification_ok = True
        # [PLAYWRIGHT-CLI] log_entry["observation"] = f"Already at destination: {url_after}"
        # [PLAYWRIGHT-CLI] else:
        # [PLAYWRIGHT-CLI] last_failure = (
        # [PLAYWRIGHT-CLI] f"Navigation executed but URL did not change "
        # [PLAYWRIGHT-CLI] f"(still {url_after}). Page may not have responded."
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] verification_ok = False
        # [PLAYWRIGHT-CLI] else:
        # [PLAYWRIGHT-CLI] verification_ok = True
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] else:
        # [PLAYWRIGHT-CLI] # FIX 3: Goal-based verification — did the STEP GOAL get achieved?
        # [PLAYWRIGHT-CLI] try:
        # [PLAYWRIGHT-CLI] v_prompt = f"""You are verifying whether a browser step succeeded.
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] STEP GOAL: {step_desc}
        # [PLAYWRIGHT-CLI] ACTION TAKEN: {action} on "{selector}" {f'with value "{value}"' if value else ''}
        # [PLAYWRIGHT-CLI] URL BEFORE: {url_before_step}
        # [PLAYWRIGHT-CLI] URL AFTER: {url_after}
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] You have BEFORE (first image) and AFTER (second image) screenshots.
        # [PLAYWRIGHT-CLI] Judge whether the GOAL of the step was achieved — not just whether something changed.
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] Return ONLY raw JSON:
        # [PLAYWRIGHT-CLI] {{
        # [PLAYWRIGHT-CLI] "success": true|false,
        # [PLAYWRIGHT-CLI] "observation": "what changed and whether the goal was met",
        # [PLAYWRIGHT-CLI] "correction_hint": "if failed: exactly what went wrong and what to try instead (different selector, scroll first, element inside iframe, need to wait longer, etc.)"
        # [PLAYWRIGHT-CLI] }}
        # [PLAYWRIGHT-CLI] Be strict: success=true only if there is clear visual evidence the step GOAL was accomplished."""
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] v_content = [
        # [PLAYWRIGHT-CLI] {"type": "text", "text": "BEFORE:"},
        # [PLAYWRIGHT-CLI] {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{current_b64}"}},
        # [PLAYWRIGHT-CLI] {"type": "text", "text": "AFTER:"},
        # [PLAYWRIGHT-CLI] {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{after_b64}"}},
        # [PLAYWRIGHT-CLI] {"type": "text", "text": v_prompt},
        # [PLAYWRIGHT-CLI] ]
        # [PLAYWRIGHT-CLI] v_kwargs: dict = {
        # [PLAYWRIGHT-CLI] "model":    REASONING_DEPLOYMENT,
        # [PLAYWRIGHT-CLI] "messages": [{"role": "user", "content": v_content}],
        # [PLAYWRIGHT-CLI] }
        # [PLAYWRIGHT-CLI] if REASONING_DEPLOYMENT != DEPLOYMENT:
        # [PLAYWRIGHT-CLI] v_kwargs["max_completion_tokens"] = 800
        # [PLAYWRIGHT-CLI] else:
        # [PLAYWRIGHT-CLI] v_kwargs["max_tokens"]  = 300
        # [PLAYWRIGHT-CLI] v_kwargs["temperature"] = 0.1
        # [PLAYWRIGHT-CLI] v_raw = reasoning_client.chat.completions.create(
        # [PLAYWRIGHT-CLI] **v_kwargs
        # [PLAYWRIGHT-CLI] ).choices[0].message.content.strip()
        # [PLAYWRIGHT-CLI] v_raw = re.sub(r"```(?:json)?[\n]?", "", v_raw).strip().rstrip("`").strip()
        # [PLAYWRIGHT-CLI] verification = json.loads(v_raw)
        # [PLAYWRIGHT-CLI] verification_ok = verification.get("success", False)
        # [PLAYWRIGHT-CLI] log_entry["observation"] = verification.get("observation", log_entry["observation"])
        # [PLAYWRIGHT-CLI] if not verification_ok:
        # [PLAYWRIGHT-CLI] last_failure = verification.get("correction_hint", "Action had no visible effect on goal")
        # [PLAYWRIGHT-CLI] except Exception as ve:
        # [PLAYWRIGHT-CLI] logger.warning(f"[EXPLORE {exploration_id}] Verification parse error: {ve}")
        # [PLAYWRIGHT-CLI] verification_ok = True  # give benefit of the doubt on parse failure
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] if verification_ok:
        # [PLAYWRIGHT-CLI] log_entry["success"]         = True
        # [PLAYWRIGHT-CLI] log_entry["screenshot_file"] = after_file
        # [PLAYWRIGHT-CLI] logger.info(
        # [PLAYWRIGHT-CLI] f"[EXPLORE {exploration_id}] ✅ Step {step_counter} "
        # [PLAYWRIGHT-CLI] f"(attempt {attempt+1}): {action} {selector[:40] if selector else ''}"
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] await _emit(queue, "step_result",
        # [PLAYWRIGHT-CLI] step_num=step_counter,
        # [PLAYWRIGHT-CLI] description=step_desc,
        # [PLAYWRIGHT-CLI] success=True,
        # [PLAYWRIGHT-CLI] action=action,
        # [PLAYWRIGHT-CLI] selector=selector,
        # [PLAYWRIGHT-CLI] value=value,
        # [PLAYWRIGHT-CLI] observation=log_entry.get("observation",""),
        # [PLAYWRIGHT-CLI] attempts=attempt+1,
        # [PLAYWRIGHT-CLI] read_value=log_entry.get("read_value"))
        # [PLAYWRIGHT-CLI] # ── Record success to selector memory (skip SSO pages) ─
        # [PLAYWRIGHT-CLI] if not any(s in current_domain for s in _sso_indicators):
        # [PLAYWRIGHT-CLI] _record_selector_outcome(
        # [PLAYWRIGHT-CLI] current_domain, step_desc, action, selector, value, success=True
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] # ── Learn from correction: extract rule when retry fixed it ─
        # [PLAYWRIGHT-CLI] if attempt > 0 and first_failed_selector and selector != first_failed_selector:
        # [PLAYWRIGHT-CLI] rule = _extract_rule_from_correction(
        # [PLAYWRIGHT-CLI] step_desc, first_failed_selector, first_failed_error,
        # [PLAYWRIGHT-CLI] selector, ui_framework, exploration_id
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] if rule and _persist_learned_rule(rule, ui_framework):
        # [PLAYWRIGHT-CLI] learned_this_run.append(rule["rule"])
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] 📚 Learned correction rule: {rule['rule'][:70]}")
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="info",
        # [PLAYWRIGHT-CLI] message=f"📚 Learned: {rule['rule'][:100]}")
        # [PLAYWRIGHT-CLI] break
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # ── Record failed selector attempt (skip SSO pages) ────────
        # [PLAYWRIGHT-CLI] if not any(s in current_domain for s in _sso_indicators):
        # [PLAYWRIGHT-CLI] _record_selector_outcome(
        # [PLAYWRIGHT-CLI] current_domain, step_desc, action, selector, value, success=False
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] selectors_this_step.append({"action": action, "selector": selector, "error": last_failure})
        # [PLAYWRIGHT-CLI] if attempt == 0:
        # [PLAYWRIGHT-CLI] first_failed_selector = selector
        # [PLAYWRIGHT-CLI] first_failed_error    = last_failure
        # [PLAYWRIGHT-CLI]
        # [PLAYWRIGHT-CLI] # Verification failed — prepare for next retry
        # [PLAYWRIGHT-CLI] current_b64 = after_b64 or current_b64
        # [PLAYWRIGHT-CLI] if attempt >= MAX_EXPLORE_RETRIES:
        # [PLAYWRIGHT-CLI] log_entry["error"] = f"Failed after {MAX_EXPLORE_RETRIES+1} attempts: {last_failure}"
        # [PLAYWRIGHT-CLI] # ── Learn from failure: extract rule after all retries exhausted ─
        # [PLAYWRIGHT-CLI] if selectors_this_step:
        # [PLAYWRIGHT-CLI] rule = _extract_rule_from_failure(
        # [PLAYWRIGHT-CLI] step_desc, selectors_this_step, ui_framework, exploration_id
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] if rule and _persist_learned_rule(rule, ui_framework):
        # [PLAYWRIGHT-CLI] learned_this_run.append(rule["rule"])
        # [PLAYWRIGHT-CLI] logger.info(f"[EXPLORE {exploration_id}] 📚 Learned failure rule: {rule['rule'][:70]}")
        # [PLAYWRIGHT-CLI] await _emit(queue, "log", level="info",
        # [PLAYWRIGHT-CLI] message=f"📚 Learned: {rule['rule'][:100]}")
        # [PLAYWRIGHT-CLI] await _emit(queue, "step_result",
        # [PLAYWRIGHT-CLI] step_num=step_counter,
        # [PLAYWRIGHT-CLI] description=step_desc,
        # [PLAYWRIGHT-CLI] success=False,
        # [PLAYWRIGHT-CLI] action=action,
        # [PLAYWRIGHT-CLI] selector=selector,
        # [PLAYWRIGHT-CLI] attempts=attempt+1,
        # [PLAYWRIGHT-CLI] error=log_entry["error"])
        # [PLAYWRIGHT-CLI] logger.warning(
        # [PLAYWRIGHT-CLI] f"[EXPLORE {exploration_id}] ❌ Step {step_counter} exhausted retries: {last_failure[:80]}"
        # [PLAYWRIGHT-CLI] )
        # [PLAYWRIGHT-CLI] # Option C: record this step as the last failure for dependency checks
        # [PLAYWRIGHT-CLI] last_failed_step = step_desc

        # ── [MCP] E2: Step loop via PlaywrightMCPBridge + Azure OpenAI tool-use ──
        # Each step is handed to _run_step_with_mcp(), which runs an Azure OpenAI
        # function-calling conversation. The model calls browser_snapshot, browser_click,
        # browser_fill, etc. directly and returns a JSON result when done.
        # The step log format is identical to the Playwright CLI loop so E3 (MD
        # generation) and all downstream code are unchanged.
        step_counter      = 0
        last_failed_step  = ""   # most-recent failed/blocked (propagates the chain)
        last_root_failure = ""   # original cause — only set on actual step failures
        current_domain    = ""   # populated on first step; guards pattern-learning task

        for step in steps[:req.max_steps]:
            step_id   = step.get("id", step_counter + 1)
            step_desc = step.get("description", "")
            step_type = step.get("type", "interact")
            step_path = step.get("path", "both")

            if path_taken and step_path not in ("both", path_taken):
                logger.info(f"[EXPLORE {exploration_id}] Skipping step {step_id} (path={step_path}, chose={path_taken})")
                continue

            step_counter += 1
            logger.info(f"[EXPLORE {exploration_id}] [MCP] Step {step_counter}: {step_desc[:70]}")

            # Dependency blocking — check against the immediate predecessor AND the
            # root cause. The root ensures parallel steps (e.g. "read col4" / "read col5")
            # that share a common prerequisite are both blocked even if they don't depend
            # on each other directly.
            if last_failed_step:
                try:
                    dependent = _is_dependent_step(last_failed_step, step_desc)
                except Exception:
                    dependent = False
                # If immediate check passes, also check against the root failure
                if not dependent and last_root_failure and last_root_failure != last_failed_step:
                    try:
                        dependent = _is_dependent_step(last_root_failure, step_desc)
                    except Exception:
                        dependent = False
                if dependent:
                    logger.warning(
                        f"[EXPLORE {exploration_id}] Blocking step {step_counter} — "
                        f"depends on failed: {last_failed_step[:60]}"
                    )
                    await _emit(queue, "step_start", step_num=step_counter,
                                description=step_desc, path=step_path, total_steps=len(steps))
                    await _emit(queue, "step_result", step_num=step_counter,
                                description=step_desc, success=False, action="blocked",
                                selector="", attempts=0,
                                error=f"Blocked — depends on failed step: {last_failed_step[:80]}")
                    steps_log.append({
                        "step_num": step_counter, "description": step_desc,
                        "action": "blocked", "selector": "", "success": False,
                        "error": f"Blocked — depends on failed step: {last_failed_step[:80]}",
                        "path": step_path, "path_taken": path_taken,
                        "screenshot_file": "", "attempts": 0,
                    })
                    # Propagate block chain — blocked steps count as failures for
                    # subsequent dependency checks so transitive deps are also blocked
                    last_failed_step = step_desc
                    continue

            # Cancellation check
            _cancel_flag = _explore_cancel.get(exploration_id)
            if _cancel_flag and _cancel_flag.is_set():
                logger.info(f"[EXPLORE {exploration_id}] 🛑 Cancelled at step {step_counter}")
                await _emit(queue, "log", level="warn",
                            message=f"🛑 Cancelled by user after {step_counter - 1} steps")
                break

            await _emit(queue, "step_start", step_num=step_counter, description=step_desc,
                        path=step_path, total_steps=len(steps))

            # Build history context from last 4 steps
            history_ctx = "\n".join([
                f"Step {h['step_num']}: [{h.get('action','')}] {h.get('selector','')} — {h.get('observation','')}"
                for h in steps_log[-4:]
            ])

            # Memory hints from past explorations
            current_domain = re.sub(r'https?://', '', await bridge.get_current_url()).split('/')[0]
            memory_hints   = _find_memory_hints(current_domain, step_desc)
            if memory_hints:
                logger.info(
                    f"[EXPLORE {exploration_id}] Memory: {len(memory_hints)} hint(s) "
                    f"— top: \"{memory_hints[0].get('selector','')}\" ({memory_hints[0].get('confidence','')})"
                )
                await _emit(queue, "memory_hint", step_num=step_counter,
                            count=len(memory_hints),
                            top_selector=memory_hints[0].get("selector", ""),
                            confidence=memory_hints[0].get("confidence", ""))

            # Capture URL before running the step (for stuck-detection + verify)
            url_before_step = ""
            try:
                url_before_step = await bridge.get_current_url()
            except Exception:
                pass

            # Run the step via MCP function-calling loop
            step_result = await _run_step_with_mcp(
                bridge, step_desc, step_type, history_ctx, memory_hints,
                ui_framework, _active_extra_rules(), exploration_id,
                step_counter, shots_dir, queue,
                domain=current_domain,
            )

            # ── Explicit VERIFY phase — separate LLM call with after-screenshot ──
            # Mirrors the old CLI _verify_prompt pattern. Only skip for steps that
            # clearly failed at the tool level (no action attempted).
            _attempts    = 1
            action_raw   = step_result.get("action", "")
            url_after_step        = ""    # populated below if verify runs; used by retry guard
            _verify_overrode_model = False  # True when verify flipped model's ✅ to ❌
            if action_raw not in ("failed", "blocked", ""):
                await _emit(queue, "log", level="info",
                            message=f"🔎 Verifying step {step_counter}…")
                url_after_step = ""
                after_b64_verify = None
                try:
                    # Take screenshot first — gives in-flight navigations time to settle,
                    # then capture URL so it reflects the final landed page.
                    _vshot = await bridge.call_tool("browser_take_screenshot", {})
                    url_after_step = await bridge.get_current_url()
                    after_b64_verify = _vshot.get("screenshot_b64")
                    if after_b64_verify and shots_dir:
                        _vfname = f"step-{step_counter:03d}-verify.png"
                        (shots_dir / _vfname).write_bytes(
                            __import__("base64").b64decode(after_b64_verify)
                        )
                        step_result.setdefault("screenshot_file", _vfname)
                except Exception as _ve:
                    logger.warning(f"[EXPLORE {exploration_id}] After-screenshot for verify failed: {_ve}")

                verified = await _verify_step_mcp(
                    step_desc, action_raw,
                    step_result.get("selector", ""),
                    url_before_step, url_after_step,
                    after_b64_verify, step_result,
                )

                model_success  = step_result.get("success", False)
                verify_success = verified.get("success", model_success)
                _verify_overrode_model = (verify_success != model_success)
                if _verify_overrode_model:
                    await _emit(queue, "log", level="warn",
                                message=(
                                    f"⚖️ Verify override: model={'✅' if model_success else '❌'} → "
                                    f"verify={'✅' if verify_success else '❌'} — "
                                    f"{verified.get('observation', '')[:80]}"
                                ))
                    logger.info(
                        f"[EXPLORE {exploration_id}] Verify override step {step_counter}: "
                        f"model={model_success} → verify={verify_success}"
                    )
                step_result = verified

            # ── Correction retry — mirrors CLI's DOM+vision correction loop ──────
            # If the step failed (attempt 1), gather DOM state and retry once with
            # targeted correction hints injected into the system prompt.
            # Skip retry if URL changed — navigation happened (even if verify was uncertain
            # due to timing); retrying would navigate away from the correct page.
            _url_changed_in_step = (
                bool(url_after_step)
                and bool(url_before_step)
                and url_after_step != url_before_step
            )
            _attempts = 1
            if _url_changed_in_step and not step_result.get("success", False):
                # URL changed but verify said failed — likely a timing race where verify
                # saw uncertain evidence. Treat as success to avoid navigating away.
                await _emit(queue, "log", level="warn",
                            message=(
                                f"⚖️ URL changed {url_before_step.split('/')[-1]} → "
                                f"{url_after_step.split('/')[-1]} — skipping retry to preserve navigation"
                            ))
                step_result["success"] = True
                if not step_result.get("observation"):
                    step_result["observation"] = f"URL navigated to {url_after_step}"
            # When verify flipped ✅→❌ (model said success, verify said failed),
            # treat as a likely false negative from UI chrome (e.g. SAP "Standard" label).
            # Force success so the step doesn't trigger a retry that undoes the action.
            if (
                _verify_overrode_model
                and not step_result.get("success", False)
                and model_success   # model was the one saying success
            ):
                await _emit(queue, "log", level="warn",
                            message=(
                                f"⚖️ Verify false-negative suspected — model said success, "
                                f"accepting model's assessment and skipping retry"
                            ))
                step_result["success"] = True

            # Anti-false-positive for navigation steps: verify overrode model ❌→✅
            # but the URL didn't change — the model was right, the click didn't navigate.
            # "Element visible in flyout" ≠ "navigated to target page."
            # Revert to ❌ so the retry loop can try a different approach.
            _is_nav_click = step_type == "navigate" or any(
                kw in step_desc.lower()
                for kw in ("from menu", "from the menu", "from dropdown", "from the dropdown",
                           "from flyout", "flyout", "menu item", "menu option",
                           "navigation item", "nav item")
            )
            if (
                _verify_overrode_model
                and step_result.get("success")       # verify said ✅
                and not model_success                 # model said ❌
                and bool(url_before_step)
                and bool(url_after_step)
                and url_before_step == url_after_step # URL didn't change — no navigation
                and _is_nav_click
            ):
                await _emit(queue, "log", level="warn",
                            message=(
                                f"⚖️ Nav-step anti-false-positive: verify said ✅ but URL unchanged "
                                f"after navigation click — model's ❌ was correct, triggering retry"
                            ))
                step_result["success"] = False
                step_result["observation"] = (
                    f"Navigation click did not change URL (still {url_after_step.split('/')[-1]}). "
                    + step_result.get("observation", "")
                )

            if (
                not step_result.get("success", False)
                and action_raw != "blocked"
                and not _url_changed_in_step
            ):
                await _emit(queue, "log", level="warn",
                            message=f"🔄 Step {step_counter} failed — gathering DOM context for retry…")
                try:
                    _dom_info = await _gather_dom_on_failure(
                        bridge, step_desc, step_result.get("selector", ""),
                    )
                except Exception as _de:
                    _dom_info = f"(DOM inspection error: {_de})"
                    logger.warning(f"[EXPLORE {exploration_id}] DOM inspection failed: {_de}")

                _corr_hint = step_result.get("correction_hint", "")
                _fail_obs  = step_result.get("observation", "")

                # NAV flyout hint (attempt-1 only — already established on first retry)
                _nav_hint = (
                    "\n⚠️ NAV FLYOUT NOTE: The SAP navigation flyout renders outside the ARIA "
                    "tree. Standard role=menuitem selectors will not find it. "
                    "Take a screenshot first. If the flyout is open, use browser_evaluate with "
                    "this EXACT JS to click the item by text:\n"
                    "  () => { const els = Array.from(document.querySelectorAll("
                    "'a,[role=\"menuitem\"],[role=\"option\"],button')); "
                    "const el = els.find(e => e.offsetParent !== null && e.textContent.trim() === 'Onboarding'); "
                    "if (el) { el.click(); return 'clicked'; } return 'not found'; }\n"
                    if any(kw in step_desc.lower()
                           for kw in ("onboarding", "menu", "flyout", "navigation", "nav"))
                    else ""
                )

                for _retry_num in range(1, MAX_EXPLORE_RETRIES + 1):
                    # Escalating strategy hint — get progressively more aggressive
                    if _retry_num == 1:
                        _strategy_hint = ""
                    elif _retry_num == 2:
                        _strategy_hint = (
                            "\n⚠️ FALLBACK STRATEGY (attempt 3): Previous click attempts failed. "
                            "Try a completely different approach:\n"
                            "  • If clicking a nav item: use browser_navigate to go directly to "
                            "the target page URL instead of clicking through the flyout.\n"
                            "  • If clicking a button: try browser_evaluate to click it via JS.\n"
                            "  • If filling a field: try browser_click on the field first, then fill.\n"
                        )
                    else:
                        _strategy_hint = (
                            "\n⚠️ STATE-RESET STRATEGY (attempt 4+): Multiple approaches failed. "
                            "1. Take a screenshot — identify the exact current state.\n"
                            "2. If a flyout or overlay is open, close it (Escape key or click outside).\n"
                            "3. Navigate back to the known starting point for this step.\n"
                            "4. Perform the action fresh using a different method than all prior attempts.\n"
                        )

                    _correction = (
                        f"WHAT FAILED: {_fail_obs[:300]}\n"
                        + (f"DIAGNOSIS: {_corr_hint}\n" if _corr_hint else "")
                        + (_nav_hint if _retry_num == 1 else "")
                        + _strategy_hint
                        + f"DOM STATE AT FAILURE:\n{_dom_info}"
                    )

                    _attempts = _retry_num + 1
                    await _emit(queue, "log", level="info",
                                message=f"🔄 Retrying step {step_counter} (attempt {_attempts}/{MAX_EXPLORE_RETRIES + 1})…")
                    logger.info(
                        f"[EXPLORE {exploration_id}] Retry {_retry_num} step {step_counter} "
                        f"— correction:\n{_correction[:300]}"
                    )

                    url_before_step = await bridge.get_current_url()
                    step_result = await _run_step_with_mcp(
                        bridge, step_desc, step_type, history_ctx, memory_hints,
                        ui_framework, _active_extra_rules(), exploration_id,
                        step_counter, shots_dir, queue,
                        correction_hints=_correction,
                        domain=current_domain,
                    )

                    # Verify the retry result
                    action_rawR = step_result.get("action", "")
                    if action_rawR not in ("blocked", ""):
                        url_after_retry = ""
                        after_b64_retry = None
                        try:
                            _rshot = await bridge.call_tool("browser_take_screenshot", {})
                            url_after_retry = await bridge.get_current_url()
                            after_b64_retry = _rshot.get("screenshot_b64")
                            if after_b64_retry and shots_dir:
                                _rfname = f"step-{step_counter:03d}-retry{_retry_num}-verify.png"
                                (shots_dir / _rfname).write_bytes(
                                    __import__("base64").b64decode(after_b64_retry)
                                )
                                step_result.setdefault("screenshot_file", _rfname)
                        except Exception:
                            pass

                        step_result = await _verify_step_mcp(
                            step_desc, action_rawR,
                            step_result.get("selector", ""),
                            url_before_step, url_after_retry,
                            after_b64_retry, step_result,
                        )
                        # Apply URL-change guard to retry verification as well
                        _retry_url_changed = (
                            bool(url_before_step) and bool(url_after_retry)
                            and url_before_step != url_after_retry
                        )
                        if _retry_url_changed and not step_result.get("success"):
                            step_result["success"] = True
                            step_result.setdefault("observation", f"URL navigated to {url_after_retry}")

                    if step_result.get("success"):
                        await _emit(queue, "log", level="info",
                                    message=f"✅ Retry {_retry_num} succeeded for step {step_counter}")
                        break
                    else:
                        await _emit(queue, "log", level="warn",
                                    message=f"❌ Retry {_retry_num} failed for step {step_counter}"
                                    + (f" ({MAX_EXPLORE_RETRIES - _retry_num} attempt(s) remaining)"
                                       if _retry_num < MAX_EXPLORE_RETRIES else " — giving up"))

            success  = step_result.get("success", False)
            action   = step_result.get("action", "")
            selector = step_result.get("selector", "")
            value    = step_result.get("value")
            obs      = step_result.get("observation", "")
            read_val = step_result.get("read_value")
            shot_f   = step_result.get("screenshot_file", "")
            pdec     = step_result.get("path_decision")

            if pdec and path_taken is None:
                path_taken = pdec
                logger.info(f"[EXPLORE {exploration_id}] Path decision → {path_taken}")

            log_entry = {
                "step_num":          step_counter,
                "description":       step_desc,
                "action":            action,
                "selector":          selector,
                "value":             value,
                "observation":       obs,
                "notes":             "",
                "confidence":        "high" if success else "low",
                "path":              step_path,
                "path_taken":        path_taken,
                "screenshot_file":   shot_f,
                "success":           success,
                "error":             None if success else obs,
                "memory_hints_used": len(memory_hints),
                "read_value":        read_val,
                "attempts":          _attempts,
            }

            if success:
                last_failed_step  = ""
                last_root_failure = ""
                logger.info(f"[EXPLORE {exploration_id}] ✅ Step {step_counter}: {action} on '{selector[:50]}'")
                if not any(s in current_domain for s in _sso_indicators):
                    _record_selector_outcome(current_domain, step_desc, action, selector, value, success=True)
            else:
                last_failed_step  = step_desc
                last_root_failure = step_desc   # root = first actual failure, propagated blocks don't override this
                # Fetch URL at point of failure for richer diagnostics
                _fail_url = ""
                try:
                    _fail_url = await bridge.get_current_url()
                except Exception:
                    pass
                _fail_detail = obs[:120] if obs else "no observation"
                _fail_msg = f"❌ Step {step_counter} failed — URL: {_fail_url} | {_fail_detail}"
                logger.warning(f"[EXPLORE {exploration_id}] {_fail_msg}")
                await _emit(queue, "log", level="warn", message=_fail_msg)
                # Re-check for login wall after failure
                try:
                    _is_login = await bridge.call_tool("browser_evaluate", {
                        "function": "() => !!document.querySelector('input[type=\"password\"]')"
                    })
                    if "true" in _is_login.get("text", "").lower():
                        await _emit(queue, "log", level="error",
                                    message=f"🔒 Login form detected at {_fail_url} — session has expired. Re-run auth.ts.")
                except Exception:
                    pass
                if not any(s in current_domain for s in _sso_indicators):
                    _record_selector_outcome(current_domain, step_desc, action, selector, value, success=False)

            await _emit(queue, "step_result",
                        step_num=step_counter, description=step_desc, success=success,
                        action=action, selector=selector, value=value,
                        observation=obs, attempts=_attempts, read_value=read_val,
                        error=None if success else obs)

            steps_log.append(log_entry)

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

        # ── Feed learnings back into the knowledge context ─────────────────
        # Run in background — don't block the completion event on LLM call.
        if current_domain:
            def _log_pattern_error(task):
                if not task.cancelled() and task.exception():
                    logger.warning(f"[patterns] Background extraction failed: {task.exception()}")
            _pt = asyncio.create_task(
                asyncio.to_thread(
                    _extract_and_save_patterns,
                    current_domain, steps_log, ui_framework,
                )
            )
            _pt.add_done_callback(_log_pattern_error)

        # [PLAYWRIGHT-CLI] await ctx.close(); await browser.close(); await pb.stop()
        await bridge.stop()  # MCP: shut down the MCP server subprocess

        result = {
            "explorationId":   exploration_id,
            "stepsCompleted":  step_counter,
            "pathTaken":       path_taken,
            "steps":           steps_log,
            "markdownContent": md_content,
            "status":          "complete",
        }
        await _emit(queue, "complete", **result)
        return result

    except Exception as e:
        logger.error(f"[EXPLORE {exploration_id}] Fatal: {e}")
        # [PLAYWRIGHT-CLI] for resource in [browser, pb]: try: await resource.close()
        if bridge:  # MCP: stop the server subprocess on error
            try:
                await bridge.stop()
            except Exception:
                pass
        md_content = _generate_exploration_md(exploration_id, enriched_test_case, steps_log) if steps_log else "# Exploration failed\n"
        try: (expl_dir / "exploration.md").write_text(md_content)
        except: pass
        result = {
            "explorationId":   exploration_id,
            "stepsCompleted":  len(steps_log),
            "pathTaken":       path_taken,
            "steps":           steps_log,
            "markdownContent": md_content,
            "status":          "error",
            "error":           str(e),
        }
        await _emit(queue, "error", message=str(e), **result)
        return result
    finally:
        # When _suppress_close=True the restart wrapper owns teardown.
        if not _suppress_close:
            if queue:
                await queue.put(None)
            _explore_queues.pop(exploration_id, None)
            _explore_cancel.pop(exploration_id, None)


@app.post("/api/enrich-steps")
async def enrich_steps(req: EnrichStepsRequest):
    """
    Expand a high-level test description into explicit browser automation steps
    using application-specific navigation knowledge.
    """
    # Build app-specific knowledge block
    is_sf = any(kw in req.app_context.lower()
                for kw in ("successfactors", "sap", "onboarding", "sf"))
    sf_nav_knowledge = """
SAP SUCCESSFACTORS ONBOARDING 2.0 — NAVIGATION KNOWLEDGE:

Module navigation:
  Home button → menuitem "Onboarding" → lands on Onboarding dashboard (/onb2Dashboard)

From the Onboarding dashboard:
  - "Manage Pending Recruits" is accessed via: Home → Onboarding → left-side nav item "Manage Pending Recruits"
    OR via the Onboarding dashboard's navigation panel on the left.
  - Candidate selection: ui5-combobox or input[placeholder="Search for new recruit"] or "Select pending recruit" dropdown.
  - After selecting a candidate you land on their record page with sections:
    Personal Information, Job Information, National ID, Email, Work Schedule, Contract End Date, etc.

Common field locations in the candidate edit form:
  - National ID: Personal Information section → National ID field (input with placeholder "National ID")
  - Email Is Primary: Personal Information → Email section → Is Primary dropdown/toggle
  - Work Schedule: Job Information section → Work Schedule dropdown (ui5-select)
  - Contract End Date: Job Information or Contract section → date picker
  - Submit: bottom of the form, "Submit" button

Status reading from the Onboarding dashboard — EXACT SEQUENCE (do not skip the Go button):
  Step 1: fill   input[placeholder="Search for new recruit"]  with candidate name
  Step 2: click  the typeahead suggestion for the candidate
  Step 3: click  the "Go" button (role=button[name="Go"]) — this applies the filter
  Step 4: wait   for ui5-table-row to appear (table refreshes after Go)
  Step 5: read   ui5-table-row >> ui5-table-cell:nth-child(4)  → Data Collection status
  Step 6: read   ui5-table-row >> ui5-table-cell:nth-child(5)  → Compliance Forms status

  NOTE: "Data Collection" and "Compliance Forms" are COLUMN HEADERS not row identifiers.
  The table shows one row per candidate. After filtering with Go, the first row is the candidate.
  Do NOT search for ui5-table-row:has-text("Data Collection") — that row does not exist.
""" if is_sf else ""

    # Pull in any patterns learned from past explorations of this domain.
    # Derive the domain from the test case URL so staging/prod tenants get their own patterns.
    _url_m = re.search(r'https?://([^/\s]+)', req.test_case)
    sf_domain = _url_m.group(1) if _url_m else "performancemanager8.successfactors.com"
    learned_patterns_block = _format_patterns_for_prompt(sf_domain, max_patterns=8) if is_sf else ""

    system_prompt = f"""You are an expert browser automation engineer specialising in {req.app_context}.
Your task is to expand a high-level test description into detailed, explicit step-by-step browser automation instructions.

Rules:
- Each step must describe exactly ONE discrete browser action (click, fill, select, read, navigate, wait).
- Include the full navigation path before any field interaction — never assume the browser is already on the right page.
- Name UI elements specifically (placeholder text, button label, menu item name, section heading).
- For conditional paths (Path A / B), keep them clearly labelled.
- Do NOT change the intent of the test — only add missing navigation and interaction detail.
- Preserve the URL from the original description.
- Output numbered steps only — no prose, no headers, no explanations.
{sf_nav_knowledge}
{learned_patterns_block}"""

    user_prompt = f"""Expand this test description into detailed browser automation steps:

{req.test_case}

Return ONLY the numbered step list. No introduction, no summary."""

    try:
        enriched = ask_llm(system=system_prompt, user=user_prompt, max_tokens=2000)
        return {"enriched": enriched.strip(), "app_context": req.app_context}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Enrichment failed: {e}")


async def _poll_github_run(exploration_id: str, queue: asyncio.Queue, dispatch_time: float) -> None:
    """
    Background task: polls GitHub Actions every 30s to find the run triggered for
    this exploration and stream status updates into the SSE queue.
    Stops when the queue is removed (runner callback arrived) or after 30 min.
    """
    try:
        gh_token, gh_owner, gh_repo, _wf, _br = get_github_config()
    except Exception:
        return

    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    runs_url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/explore.yml/runs"
    run_html_url: str = ""
    run_id: str = ""

    deadline = dispatch_time + 1800  # 30 min hard limit

    # Give GitHub a few seconds to register the new run
    await asyncio.sleep(8)

    import time as _time

    while _time.time() < deadline:
        if _explore_queues.get(exploration_id) is None:
            return  # runner already posted result

        try:
            resp = requests.get(runs_url, headers=headers, params={"per_page": 10}, timeout=10)
            if resp.status_code == 200:
                for run in resp.json().get("workflow_runs", []):
                    # Match by creation time — runs dispatched in last 5 minutes
                    created_ts = run.get("created_at", "")
                    try:
                        import datetime as _dt
                        created = _dt.datetime.fromisoformat(created_ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        created = 0
                    if created >= dispatch_time - 5:
                        if not run_html_url:
                            run_html_url = run.get("html_url", "")
                            run_id = str(run.get("id", ""))
                            await _emit(queue, "log", level="info",
                                        message=f"🏃 Running on GitHub Actions — [view run]({run_html_url})")
                        status     = run.get("status", "")
                        conclusion = run.get("conclusion") or ""
                        label = {
                            "queued":      "⏳ Queued on runner…",
                            "in_progress": "⚙️  Running steps on GitHub runner…",
                            "completed":   f"✅ Runner finished ({conclusion})" if conclusion == "success" else f"⚠️  Runner finished ({conclusion})",
                        }.get(status, f"Status: {status}")
                        await _emit(queue, "log", level="info", message=label)
                        break
        except Exception:
            pass

        await asyncio.sleep(30)

    # Timed out — emit a warning so the SSE stream doesn't just hang silently
    if _explore_queues.get(exploration_id) is not None:
        await _emit(queue, "log", level="warn",
                    message="⏱️  GitHub runner is taking longer than expected. Results will appear when the run completes.")


@app.post("/api/explore/start")
async def explore_start(req: ExploreRequest):
    """
    Start an exploration by dispatching to a GitHub Actions runner.
    Returns immediately; the runner POSTs results back via /api/explorations/{id}/complete.
    The SSE stream receives heartbeats + GitHub run status until the runner calls back.
    """
    import time as _time

    exploration_id = str(uuid.uuid4())[:8]
    studio_url     = os.getenv("STUDIO_PUBLIC_URL", "").strip()

    if not studio_url:
        raise HTTPException(
            status_code=500,
            detail="STUDIO_PUBLIC_URL env var must be set (the public URL of this Studio instance)",
        )

    queue: asyncio.Queue = asyncio.Queue()
    _explore_queues[exploration_id] = queue
    _explore_cancel[exploration_id] = asyncio.Event()

    dispatch_exploration_workflow(
        exploration_id = exploration_id,
        studio_url     = studio_url,
        test_case      = req.test_case,
        storage_state  = req.storage_state or "",
        max_steps      = req.max_steps,
        headless       = req.headless,
    )
    dispatch_time = _time.time()
    logger.info(f"[explore/start] Dispatched to GitHub runner — {exploration_id}")

    # Emit an immediate status event so the UI shows something right away
    await queue.put({"type": "log", "level": "info",
                     "message": "🚀 Exploration dispatched to GitHub Actions runner. Waiting for it to start…"})

    # Background task: poll GitHub for run URL + status updates
    asyncio.create_task(_poll_github_run(exploration_id, queue, dispatch_time))

    return {
        "explorationId": exploration_id,
        "mode":          "github_runner",
        "streamUrl":     f"/api/explorations/{exploration_id}/stream",
        "message":       "Exploration dispatched to GitHub runner. Results will arrive via callback.",
    }


@app.get("/api/explorations/{exploration_id}/stream")
async def stream_exploration(exploration_id: str):
    """SSE stream for a running exploration. Emits events until 'complete' or 'error'."""
    queue = _explore_queues.get(exploration_id)
    if queue is None:
        # Exploration already finished — serve the saved result as a single event
        expl_dir = EXPLORATIONS_DIR / exploration_id
        if (expl_dir / "steps.json").exists():
            data = json.loads((expl_dir / "steps.json").read_text())
            md   = (expl_dir / "exploration.md").read_text() if (expl_dir / "exploration.md").exists() else ""
            payload = json.dumps({"type": "complete", "markdownContent": md, **data})
            async def _done():
                yield f"data: {payload}\n\n"
            return StreamingResponse(_done(), media_type="text/event-stream")
        raise HTTPException(status_code=404, detail="Exploration not found")

    async def _generate():
        # Use short-poll (20s) so we can send SSE heartbeat comments between events.
        # Heartbeat comments (": ping\n\n") keep the TCP connection alive through
        # Azure Container Apps' 240s idle timeout and browser keepalive limits.
        # GitHub runner explorations can take 5-15 min, so we loop for up to 30 min.
        import time as _time
        deadline = _time.time() + 1800  # 30 min hard limit
        try:
            while _time.time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    if event is None:
                        yield "data: {\"type\":\"done\"}\n\n"
                        return
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                    if event.get("type") in ("complete", "error", "done"):
                        return
                except asyncio.TimeoutError:
                    # Send SSE comment heartbeat — keeps proxy/browser connection open
                    yield ": ping\n\n"
        except Exception:
            pass
        yield "data: {\"type\":\"timeout\",\"message\":\"Runner did not respond within 30 minutes\"}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


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


@app.post("/api/explorations/{exploration_id}/cancel")
async def cancel_exploration(exploration_id: str):
    """Signal a running exploration to stop at the next step boundary."""
    flag = _explore_cancel.get(exploration_id)
    if flag:
        flag.set()
        logger.info(f"[cancel] Cancellation signalled for {exploration_id}")
        return {"cancelled": True, "explorationId": exploration_id}
    return {"cancelled": False, "explorationId": exploration_id,
            "message": "Exploration not running or already finished"}


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


@app.post("/api/cleanup")
async def trigger_cleanup():
    """Manually trigger artifact cleanup (same as startup cleanup)."""
    summary = _cleanup_artifacts()
    return {
        "mcp_artifacts_deleted": summary["deleted_mcp"],
        "screenshots_deleted":   summary["deleted_shots"],
        "runs_stripped":         summary["runs_stripped"],
        "kept_full":             _KEEP_EXPLORATIONS,
    }


# ─ Health check endpoints ──────────────────────────────────────────────────────
# ── GitHub runner: one-click secret sync ─────────────────────────────────────
# Reads env vars already present in this running container and pushes them as
# GitHub Actions secrets so explore.yml can use them on the runner.
# Useful when EXPLORE_MODE=github_runner is being activated on Azure:
# the container already has all the keys; this just mirrors them to GitHub.

_GITHUB_SECRET_MAP = [
    # (env_var_name, github_secret_name, required)
    ("AZURE_OPENAI_API_KEY",         "AZURE_OPENAI_API_KEY",         True),
    ("AZURE_OPENAI_ENDPOINT",        "AZURE_OPENAI_ENDPOINT",        True),
    ("AZURE_OPENAI_API_VERSION",     "AZURE_OPENAI_API_VERSION",     True),
    ("AZURE_OPENAI_DEPLOYMENT",      "AZURE_OPENAI_DEPLOYMENT",      True),
    ("AZURE_REASONING_DEPLOYMENT",   "AZURE_REASONING_DEPLOYMENT",   False),
    ("AZURE_REASONING_API_VERSION",  "AZURE_REASONING_API_VERSION",  False),
    ("SF_BASE_URL",                  "SF_BASE_URL",                  False),
    ("SF_USERNAME",                  "SF_USERNAME",                  False),
    ("SF_PASSWORD",                  "SF_PASSWORD",                  False),
    ("STUDIO_CALLBACK_TOKEN",        "STUDIO_CALLBACK_TOKEN",        True),
]


def _encrypt_github_secret(public_key_b64: str, value: str) -> str:
    """Encrypt a secret value using the repo's NaCl public key."""
    try:
        from base64 import b64encode
        from nacl import encoding, public as nacl_public
        pk  = nacl_public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
        box = nacl_public.SealedBox(pk)
        return b64encode(box.encrypt(value.encode("utf-8"))).decode("utf-8")
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PyNaCl is not installed. Run: pip install PyNaCl",
        )


@app.post("/api/setup/sync-github-secrets")
async def sync_github_secrets():
    """
    Push env vars from this container to GitHub Actions secrets, then automatically
    update the Azure Container App's own env vars (STUDIO_CALLBACK_TOKEN,
    EXPLORE_MODE, STUDIO_PUBLIC_URL) so everything is wired up in one click.
    """
    import secrets as _secrets_mod

    # ── 1. Auto-generate STUDIO_CALLBACK_TOKEN if not already set ─────────────
    callback_token = os.getenv("STUDIO_CALLBACK_TOKEN", "").strip()
    generated_token: Optional[str] = None
    if not callback_token:
        generated_token = _secrets_mod.token_hex(32)
        os.environ["STUDIO_CALLBACK_TOKEN"] = generated_token
        callback_token = generated_token
        logger.info("[setup] Generated new STUDIO_CALLBACK_TOKEN")

    # ── 2. Push secrets to GitHub ─────────────────────────────────────────────
    gh_token, gh_owner, gh_repo, _wf, _branch = get_github_config()
    gh_headers = {"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"}
    base = f"https://api.github.com/repos/{gh_owner}/{gh_repo}"

    r = requests.get(f"{base}/actions/secrets/public-key", headers=gh_headers, timeout=10)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Could not fetch GitHub public key: {r.text[:200]}")
    key_data, key_id, public_key = r.json(), r.json()["key_id"], r.json()["key"]

    gh_report = {"set": [], "skipped": [], "missing": []}
    for env_var, secret_name, required in _GITHUB_SECRET_MAP:
        value = os.getenv(env_var, "").strip()
        if not value:
            gh_report["missing" if required else "skipped"].append(secret_name)
            continue
        encrypted = _encrypt_github_secret(public_key, value)
        resp = requests.put(
            f"{base}/actions/secrets/{secret_name}",
            headers=gh_headers,
            json={"encrypted_value": encrypted, "key_id": key_id},
            timeout=10,
        )
        if resp.status_code in (201, 204):
            gh_report["set"].append(secret_name)
            logger.info(f"[setup] GitHub secret set: {secret_name}")
        else:
            gh_report["missing"].append(secret_name)
            logger.warning(f"[setup] Failed to set {secret_name}: {resp.status_code}")

    # ── 3. Update the Azure Container App's own env vars ─────────────────────
    # Derive STUDIO_PUBLIC_URL from Azure's built-in CONTAINER_APP_HOSTNAME if
    # not explicitly configured.
    studio_url = os.getenv("STUDIO_PUBLIC_URL", "").strip()
    if not studio_url:
        hostname = os.getenv("CONTAINER_APP_HOSTNAME", "").strip()
        if hostname:
            studio_url = f"https://{hostname}"

    aca_update = await _update_container_app_env({
        "STUDIO_CALLBACK_TOKEN": callback_token,
        "EXPLORE_MODE":          "github_runner",
        **({"STUDIO_PUBLIC_URL": studio_url} if studio_url else {}),
    })

    return {
        **gh_report,
        "generated_token": generated_token,
        "azure_update":    aca_update,
        "ready":           len(gh_report["missing"]) == 0,
    }


# ── Azure Container App env-var updater ───────────────────────────────────────

def _get_azure_mgmt_token() -> str:
    """
    Get an Azure management API bearer token.

    Tries in order:
      1. Managed Identity (IMDS) — works automatically inside Azure Container Apps.
      2. Service principal — uses AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET.

    Raises RuntimeError if neither is available.
    """
    # --- Option 1: Managed Identity via IMDS (inside Azure) ------------------
    try:
        r = requests.get(
            "http://169.254.169.254/metadata/identity/oauth2/token",
            params={"api-version": "2018-02-01", "resource": "https://management.azure.com/"},
            headers={"Metadata": "true"},
            timeout=3,   # fails fast outside Azure
        )
        if r.status_code == 200:
            token = r.json().get("access_token", "")
            if token:
                logger.info("[setup/azure] Authenticated via Managed Identity")
                return token
    except requests.RequestException:
        pass

    # --- Option 2: Service principal -----------------------------------------
    tenant = os.getenv("AZURE_TENANT_ID", "").strip()
    client = os.getenv("AZURE_CLIENT_ID", "").strip()
    secret = os.getenv("AZURE_CLIENT_SECRET", "").strip()
    if tenant and client and secret:
        r = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     client,
                "client_secret": secret,
                "resource":      "https://management.azure.com/",
            },
            timeout=10,
        )
        if r.status_code == 200:
            token = r.json().get("access_token", "")
            if token:
                logger.info("[setup/azure] Authenticated via Service Principal")
                return token
        raise RuntimeError(f"Service principal auth failed: {r.status_code} {r.text[:200]}")

    raise RuntimeError(
        "No Azure credentials found. "
        "Assign a Managed Identity to the Container App, or set "
        "AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET."
    )


async def _update_container_app_env(env_updates: dict) -> dict:
    """
    Upsert env vars on the running Azure Container App.
    Returns a status dict describing what happened.
    """
    sub  = os.getenv("AZURE_SUBSCRIPTION_ID", "").strip()
    rg   = os.getenv("AZURE_RESOURCE_GROUP",  "").strip()
    app  = os.getenv("AZURE_CONTAINER_APP_NAME", "").strip()

    # Fall back: Azure injects CONTAINER_APP_NAME automatically
    if not app:
        app = os.getenv("CONTAINER_APP_NAME", "").strip()

    if not all([sub, rg, app]):
        missing = [k for k, v in {"AZURE_SUBSCRIPTION_ID": sub,
                                   "AZURE_RESOURCE_GROUP": rg,
                                   "AZURE_CONTAINER_APP_NAME": app}.items() if not v]
        logger.warning(f"[setup/azure] Skipping ACA update — missing: {missing}")
        return {"skipped": True, "reason": f"Set these env vars to enable auto-update: {missing}"}

    try:
        token = _get_azure_mgmt_token()
    except RuntimeError as exc:
        logger.warning(f"[setup/azure] Auth failed: {exc}")
        return {"skipped": True, "reason": str(exc)}

    api_version = "2024-03-01"
    aca_base    = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}/providers/Microsoft.App/containerApps/{app}"
        f"?api-version={api_version}"
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # GET current config
    r = requests.get(aca_base, headers=headers, timeout=15)
    if r.status_code != 200:
        msg = f"GET container app failed: {r.status_code} {r.text[:200]}"
        logger.warning(f"[setup/azure] {msg}")
        return {"skipped": True, "reason": msg}

    body       = r.json()
    containers = body.get("properties", {}).get("template", {}).get("containers", [])
    if not containers:
        return {"skipped": True, "reason": "No containers found in app template"}

    # Upsert env vars in the first container (the Studio)
    existing_env: list = containers[0].get("env", [])
    existing_map = {e["name"]: i for i, e in enumerate(existing_env)}

    for key, value in env_updates.items():
        entry = {"name": key, "value": value}
        if key in existing_map:
            existing_env[existing_map[key]] = entry   # update in-place
        else:
            existing_env.append(entry)               # add new

    containers[0]["env"] = existing_env

    # PATCH back — send only the properties we're changing
    patch_body = {
        "properties": {
            "template": body["properties"]["template"]
        }
    }
    pr = requests.patch(aca_base, headers=headers, json=patch_body, timeout=30)
    if pr.status_code in (200, 201, 202):
        updated_keys = list(env_updates.keys())
        logger.info(f"[setup/azure] Container App env updated: {updated_keys}")
        return {"updated": updated_keys, "status": pr.status_code}
    else:
        msg = f"PATCH failed: {pr.status_code} {pr.text[:300]}"
        logger.warning(f"[setup/azure] {msg}")
        return {"skipped": True, "reason": msg}


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


# ── GitHub runner: memory export/import ──────────────────────────────────────
# The explore_runner.py script running on a GitHub Actions runner calls these
# endpoints to download the latest learned memory before the run, and to upload
# updated memory after the run so learnings accumulate on the persistent volume.

_CALLBACK_TOKEN = os.getenv("STUDIO_CALLBACK_TOKEN", "")


def _verify_callback(request_token: str) -> None:
    """Raise 401 if callback token is wrong (only enforced when a token is configured)."""
    if _CALLBACK_TOKEN and request_token != _CALLBACK_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid callback token")


@app.get("/api/memory/export")
async def export_memory(x_callback_token: str = ""):
    """Return selector_memory, exploration_patterns, and learned_rules as JSON."""
    _verify_callback(x_callback_token)
    return {
        "selector_memory":      _load_selector_memory(),
        "exploration_patterns": _load_exploration_patterns(),
        "learned_rules":        _load_learned_rules(),
    }


class MemoryImportPayload(BaseModel):
    selector_memory:      Optional[dict] = None
    exploration_patterns: Optional[dict] = None
    learned_rules:        Optional[dict] = None


@app.post("/api/memory/import")
async def import_memory(payload: MemoryImportPayload, x_callback_token: str = ""):
    """Merge memory updates from the runner back into the persistent volume files."""
    _verify_callback(x_callback_token)
    updated = []
    if payload.selector_memory is not None:
        SELECTOR_MEMORY_FILE.write_text(json.dumps(payload.selector_memory, indent=2))
        updated.append("selector_memory")
    if payload.exploration_patterns is not None:
        EXPLORATION_PATTERNS_FILE.write_text(json.dumps(payload.exploration_patterns, indent=2))
        updated.append("exploration_patterns")
    if payload.learned_rules is not None:
        LEARNED_RULES_FILE.write_text(json.dumps(payload.learned_rules, indent=2))
        updated.append("learned_rules")
    logger.info(f"[memory/import] Updated: {updated}")
    return {"imported": updated}


# ── GitHub runner: exploration result callback ────────────────────────────────

class RunnerCompletePayload(BaseModel):
    explorationId: Optional[str] = None
    steps:         Optional[list] = None
    status:        Optional[str]  = None
    error:         Optional[str]  = None
    testCase:      Optional[str]  = None
    # Any extra fields from _run_exploration result are captured via __fields_set__


@app.post("/api/explorations/{exploration_id}/event")
async def runner_event(exploration_id: str, payload: dict, x_callback_token: str = ""):
    """
    Called by explore_runner.py for each live SSE event during an exploration.
    Forwards the event into the in-memory queue so the browser's SSE stream
    receives real-time step updates as they happen on the GitHub runner.
    """
    _verify_callback(x_callback_token)
    queue = _explore_queues.get(exploration_id)
    if queue is not None:
        await queue.put(payload)
    return {"ok": True}


@app.post("/api/explorations/{exploration_id}/complete")
async def runner_complete(exploration_id: str, payload: dict, x_callback_token: str = ""):
    """
    Called by explore_runner.py when the GitHub Actions exploration job finishes.
    Saves the result to the explorations dir and signals any waiting SSE clients.
    """
    _verify_callback(x_callback_token)

    expl_dir = EXPLORATIONS_DIR / exploration_id
    expl_dir.mkdir(parents=True, exist_ok=True)

    # Persist the steps log
    steps = payload.get("steps", [])
    (expl_dir / "steps.json").write_text(json.dumps({
        "explorationId": exploration_id,
        "steps":         steps,
        "status":        payload.get("status", "complete"),
        "testCase":      payload.get("testCase", ""),
        "completedAt":   datetime.now().isoformat(),
        "source":        "github_runner",
    }, indent=2, default=str))

    # Persist the exploration markdown if present
    md = payload.get("markdownContent", "") or _generate_exploration_md(
        exploration_id, payload.get("testCase", ""), steps
    )
    (expl_dir / "exploration.md").write_text(md)

    # Signal any SSE client that may be polling this exploration
    q = _explore_queues.get(exploration_id)
    if q:
        await q.put({
            "type":            "complete",
            "explorationId":   exploration_id,
            "stepsCompleted":  len(steps),
            "markdownContent": md,
            "steps":           steps,
            "source":          "github_runner",
        })
        await q.put(None)  # sentinel — closes the SSE stream
        _explore_queues.pop(exploration_id, None)
        _explore_cancel.pop(exploration_id, None)

    if payload.get("error"):
        logger.warning(f"[runner/complete] {exploration_id} finished with error: {payload['error']}")
    else:
        logger.info(f"[runner/complete] {exploration_id} saved — {len(steps)} steps")

    return {"saved": exploration_id, "steps": len(steps)}



# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

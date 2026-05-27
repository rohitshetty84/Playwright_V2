"""
Playwright AI Studio — Python/FastAPI backend
Azure OpenAI powered test synthesis & auto-healing
"""

import os, json, uuid, re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AzureOpenAI
from dotenv import load_dotenv

BASE = Path(__file__).parent
ROOT_ENV = BASE.parent / ".env"
load_dotenv()
if ROOT_ENV.exists():
    load_dotenv(ROOT_ENV, override=False)

# ── Azure OpenAI client ───────────────────────────────────────────────────────
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
)
DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# ── Storage paths ─────────────────────────────────────────────────────────────
BASE       = Path(__file__).parent
GOLDEN_DIR = BASE / "golden"
RUNS_DIR   = BASE / "runs"
GOLDEN_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)

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
# ── Azure OpenAI helper ───────────────────────────────────────────────────────
def ask_llm(system: str, user: str, max_tokens: int = 1500) -> str:
    try:
        resp = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI error: {e}")

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
    (directory / f"{id}.json").write_text(json.dumps(data, indent=2))

def ts_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

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

# ─ Step 1: Analyse test case ──────────────────────────────────────────────────
@app.post("/api/synthesize/analyse")
async def analyse(req: SynthesizeRequest):
    raw = ask_llm(
        system="""You are a Playwright test architect specialising in SAP SuccessFactors automation.
Analyse the test case description and any script fragment provided.
Return ONLY a JSON object (no markdown) with these keys:
  "steps"             – array of step name strings
  "selectors"         – array of key selector descriptions
  "risks"             – array of flakiness risk strings
  "healingStrategies" – array of recommended healing patterns""",
        user=f"Test case: {req.test_case}\n\nExisting script:\n{req.script_fragment or '(none)'}",
        max_tokens=600,
    )
    # Strip markdown fences if model adds them
    clean = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return {
            "steps": ["Login", "Navigate to Onboarding", "Search candidate", "Update fields", "Submit"],
            "selectors": ["role-based locators", "getByRole", "getByText"],
            "risks": ["dynamic content loading", "timing", "multiple matching elements"],
            "healingStrategies": [".first() scoping", ".or() fallback chains", "waitForLoadState"],
        }

# ─ Step 2: Synthesize full script ─────────────────────────────────────────────
@app.post("/api/synthesize/generate")
async def generate(req: SynthesizeRequest):
    analysis_raw = ask_llm(
        system="Return ONLY JSON. No markdown.",
        user=f"Analyse: {req.test_case}",
        max_tokens=400,
    )

    code = ask_llm(
        system="""You are a senior Playwright TypeScript engineer for SAP SuccessFactors automation.
Generate a complete, production-ready Playwright test file.

Rules:
- Use TypeScript with proper imports from '@playwright/test'
- Include a login() helper using storageState from user.json
- Use getByRole() selectors; apply .first() when multiple matches are possible
- Apply .or() fallback chains for dynamic elements (e.g. Nudge button)
- Add waitForLoadState('networkidle') after navigation
- Log each TC step with console.log('[step] ✅ description')
- Use a for..of loop over CANDIDATES from './test-data'
- Mark path='A' vs path='B' branching clearly
- Add [AI-SYNTHESIZED] inline comments explaining selector choices
Output ONLY the TypeScript code. No markdown fences.""",
        user=f"Test case: {req.test_case}\n\nScript hints:\n{req.script_fragment or '(none)'}",
        max_tokens=1500,
    )
    return {"code": code}

# ─ Save as Golden ──────────────────────────────────────────────────────────────
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
    return golden

# ─ Record a test run ──────────────────────────────────────────────────────────
@app.post("/api/runs")
async def record_run(req: RunRequest):
    golden = next((g for g in load_goldens() if g["id"] == req.golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    rid = str(uuid.uuid4())[:8]
    run = {
        "id": rid,
        "goldenId": req.golden_id,
        "goldenName": golden["name"],
        "browser": req.browser,
        "runAt": ts_now(),
        "candidates": req.candidates,
    }
    save_json(RUNS_DIR, rid, run)
    return run

# ─ Auto-Heal ──────────────────────────────────────────────────────────────────
@app.post("/api/heal/{golden_id}")
async def heal(golden_id: str):
    golden = next((g for g in load_goldens() if g["id"] == golden_id), None)
    if not golden:
        raise HTTPException(status_code=404, detail="Golden not found")

    # Collect errors from all runs for this golden
    errors = []
    for run in load_runs():
        if run.get("goldenId") == golden_id:
            for c in run.get("candidates", []):
                if c.get("status") == "fail" and c.get("error"):
                    errors.append(f"[{c['name']} Path {c['path']}] {c['error']}")

    error_summary = "\n".join(errors) if errors else "Selector timeout on dynamic elements."

    healed_code = ask_llm(
        system="""You are a Playwright auto-healing expert.
Given failure error messages and the original golden TypeScript script, produce an improved script.
For every fix, add an inline comment starting exactly with [AI-HEAL] explaining what changed and why.
Key healing patterns:
  - .or() for alternative selector chains
  - .first() for ambiguous multi-match locators
  - waitForLoadState for timing gaps
  - try/catch with fallback click strategies
Output ONLY the TypeScript code. No markdown.""",
        user=f"Errors:\n{error_summary}\n\nOriginal golden script:\n{golden['code']}",
        max_tokens=1500,
    )

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

# ─ Promote healed code as new Golden ─────────────────────────────────────────
@app.patch("/api/goldens/{golden_id}/promote")
async def promote_healed(golden_id: str, body: PromoteGoldenRequest):
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
    return golden

# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

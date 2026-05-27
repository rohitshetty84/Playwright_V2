"""
Playwright AI Studio — Python/FastAPI backend
Azure OpenAI powered test synthesis & auto-healing
"""

import os, json, uuid, re, subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional
import requests

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

class TriggerCIRequest(BaseModel):
    golden_ids: str  # comma-separated list of golden IDs
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

    # Log for debugging
    status = "✓ recorded" if golden else "⚠ recorded (golden not found, using ID as name)"
    print(f"[api/runs] {status} — ID={rid}, golden={req.golden_id}, candidates={len(req.candidates)}")

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

    # ── Auto-trigger GitHub Actions to test the healed golden ──────────────────
    # This ensures the healed code is tested with the updated golden file
    workflow_result = {"status": "skipped", "message": "GitHub workflow not configured"}
    try:
        print(f"[promote] Auto-triggering workflow for healed golden: {golden_id}")
        workflow_result = dispatch_github_workflow({"golden_ids": golden_id})
        print(f"[promote] Workflow triggered successfully: {workflow_result.get('message')}")
    except HTTPException as e:
        # Workflow dispatch failed but golden was saved successfully
        print(f"[promote] Warning: Could not trigger workflow: {e.detail}")
        workflow_result = {"status": "failed", "message": str(e.detail)}
    except Exception as e:
        print(f"[promote] Unexpected error triggering workflow: {e}")
        workflow_result = {"status": "failed", "message": str(e)}

    return {
        "golden": golden,
        "workflowTriggered": workflow_result.get("status") == "success",
        "workflowMessage": workflow_result.get("message", "Unknown"),
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

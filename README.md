# Playwright AI Studio

AI-powered Playwright test synthesis and auto-healing platform.
Backed by **Azure OpenAI** (GPT-4o) · Python **FastAPI** · single-file HTML frontend.

> Runs **locally** for authoring and healing tests, and on **GitHub Actions**
> for headless execution against every push / pull request.

---

## Prerequisites

| Tool | Version | Used by |
|------|---------|---------|
| Python | 3.11+ | FastAPI backend, CI helper scripts |
| Node.js | 20+ | Playwright test runner |
| Azure OpenAI resource | GPT-4o deployed | Synthesize + heal (local dev) |
| GitHub account | repo + Actions enabled | CI execution |

---

## Local quickstart

```bash
# 1. Clone / copy this folder into your project
cd playwright-ai-studio

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install backend dependencies
pip install -r requirements.txt

# 4. Configure Azure OpenAI
cp .env.example .env
# Edit .env — fill in your endpoint, key, and deployment name

# 5. (Optional) Pre-populate with your existing onboarding data
python seed_data.py

# 6. Start the server
python server.py
```

Then open **http://localhost:8000** in your browser.

### Local Playwright run (optional)

To exercise the goldens locally exactly the way CI will:

```bash
npm install
npx playwright install --with-deps msedge
python ci/export_goldens.py --from golden --to tests
npx playwright test
```

---

## Running on GitHub Actions

The workflow definition lives in `.github/workflows/playwright.yml`. It has three jobs:

| Job | What it does |
|-----|--------------|
| `prepare-goldens` | Reads `golden/*.json` and writes each `code` field out as `tests/<name>.spec.ts`. Uploads `tests/` as an artifact. |
| `playwright-test` | `npm ci` + `npx playwright install --with-deps msedge` + `npx playwright test`. Emits HTML report, JUnit, and `results.json` as artifacts. |
| `report-runs` | POSTs the run summary back to a deployed Studio at `$PLAYWRIGHT_AI_STUDIO_URL/api/runs`. Skipped automatically if that **variable** is unset. |

Triggers (in the workflow `on:` block): pushes to `main`, pull requests against `main`, a nightly schedule (`0 2 * * *` UTC), and manual `workflow_dispatch` from the Actions tab.

### 1. Enable Actions on the repo

In GitHub: **Settings → Actions → General** → ensure "Allow all actions" (or at minimum "Allow actions created by GitHub" plus the specific marketplace action `mikepenz/action-junit-report` if you're locking down). The workflow runs on GitHub-hosted `ubuntu-latest` runners by default — no self-hosted runner needed.

If you'd rather run on a **self-hosted runner**, change `runs-on: ubuntu-latest` in each job to `runs-on: [self-hosted, linux]` (or your runner label). Make sure the host has Python 3.11+ and Node 20+ installed.

### 2. Add repository secrets and variables

GitHub: **Settings → Secrets and variables → Actions**. Two tabs — **Secrets** (encrypted, never echoed in logs) and **Variables** (plaintext, fine for non-sensitive flags).

| Name | Type | Required? | Notes |
|------|------|-----------|-------|
| `PLAYWRIGHT_AI_STUDIO_URL` | **Variable** | optional | e.g. `https://studio.example.com`. Acts as the gate — the `report-runs` job only runs when this variable is set. |
| `PLAYWRIGHT_AI_STUDIO_TOKEN` | **Secret** | optional | Sent as `Authorization: Bearer …` to `/api/runs` if your deployment requires auth. |
| `GOLDEN_ID` | **Variable** | optional | Defaults to `seed-g1`. Override per-environment if you maintain multiple goldens. |
| `BROWSER` | **Variable** | optional | Defaults to `msedge`. |
| `AZURE_OPENAI_ENDPOINT` | **Variable** | only for auto-heal jobs | Not used by default CI run. Add if you wire up a nightly heal job. |
| `AZURE_OPENAI_API_KEY` | **Secret** | only for auto-heal jobs | Always keep as a secret, never a variable. |
| `AZURE_OPENAI_API_VERSION` | **Variable** | only for auto-heal jobs | e.g. `2024-02-01`. |
| `AZURE_OPENAI_DEPLOYMENT` | **Variable** | only for auto-heal jobs | e.g. `gpt-4o`. |

Rule of thumb: **Secrets** for anything you'd be unhappy to see in a job log; **Variables** for URLs, flags, and switches.

### 3. Push and watch

On the next push the workflow runs automatically. Open the **Actions** tab → click the run → scroll to **Artifacts** at the bottom:

- `playwright-report` — HTML report, traces, videos, screenshots, `results.json`, and `junit.xml`
- JUnit results are also rendered as a check summary on the commit / PR via the `action-junit-report` step
- `tests` — the materialized `.spec.ts` files (useful for debugging selector drift)

### 4. (Optional) Schedule and manual triggers

The workflow already includes both:

- **Schedule**: nightly at `0 2 * * *` UTC (edit the cron in the `on.schedule` block).
- **Manual**: Actions tab → "Playwright AI Studio" workflow → **Run workflow** button (uses the `workflow_dispatch` trigger).

---

## .env values (local dev)

```env
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_API_VERSION=2024-02-01
AZURE_OPENAI_DEPLOYMENT=gpt-4o      # match your Azure deployment name
```

`.env` is for **local dev only** — never commit it. In CI, the same names come
from GitHub repository secrets and variables (see above), so `server.py` picks
them up without changes.

---

## Workflow

### 1 — Synthesize
Describe your test case in plain English (e.g. "Path A — update National ID, set email Is Primary = No, submit").
Paste any existing script fragments as hints.
AI analyses selector risks, applies healing strategies, and generates a complete TypeScript Playwright file.
Save it as a **Golden** file.

### 2 — Golden Files
Immutable reference scripts. Each golden tracks its heal count and last-healed date.
Golden files are **never silently modified**. In CI they're exported to `tests/` per pipeline run, never edited in place.

### 3 — Run History
After each Playwright run, results are POSTed to `/api/runs`. In CI this is done automatically by `ci/report_run.py`. Locally you can also use `seed_data.py` to load historical data.
Pass/fail per candidate is displayed with full error messages.

### 4 — Auto-Heal
Select a golden with recent failures.
AI reads the failure errors, generates a healed script with `[AI-HEAL]` inline comments, shows you the changes, and only promotes after your explicit approval.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/goldens` | List all golden files |
| POST | `/api/goldens` | Save a new golden |
| PATCH | `/api/goldens/{id}/promote` | Promote healed code as golden |
| GET | `/api/runs` | List all test runs |
| POST | `/api/runs` | Record a new test run |
| POST | `/api/synthesize/analyse` | Analyse test case (returns JSON) |
| POST | `/api/synthesize/generate` | Generate TypeScript script |
| POST | `/api/heal/{golden_id}` | Run auto-heal for a golden |

---

## Recording a run from Playwright (manual)

The CI pipeline does this for you via `ci/report_run.py`. To do it by hand:

```python
import requests

requests.post("http://localhost:8000/api/runs", json={
    "golden_id": "seed-g1",
    "browser": "msedge",
    "candidates": [
        {"name": "Rosa Philp",  "path": "A", "status": "pass", "duration": "48s"},
        {"name": "Test Onb123", "path": "B", "status": "fail", "duration": "12s",
         "error": "TimeoutError: Nudge button not found after 15000ms"},
    ]
})
```

---

## File structure

```
playwright-ai-studio/
├── server.py            # FastAPI backend
├── seed_data.py         # Pre-populate with existing project data
├── requirements.txt     # Python deps (FastAPI, OpenAI, etc.)
├── package.json         # Node deps (Playwright)
├── playwright.config.ts # Playwright config — CI-friendly defaults
├── .env.example         # Local-dev template (CI uses GitHub secrets/variables)
├── .github/
│   └── workflows/
│       └── playwright.yml   # GitHub Actions: prepare → test → report
├── ci/
│   ├── export_goldens.py  # golden/*.json  →  tests/*.spec.ts
│   └── report_run.py      # results.json   →  POST /api/runs
├── static/
│   └── index.html       # Full UI — no build step
├── golden/              # Auto-created — stores golden JSON files
├── runs/                # Auto-created — stores run result JSON files
└── tests/               # Auto-generated in CI from golden/ (gitignored)
```

---

## Troubleshooting (CI)

**`no goldens exported — nothing to test`**
The `golden/` directory is empty (or only has malformed JSON). Commit at least one valid golden, or run `python seed_data.py` and commit `golden/seed-g1.json`.

**`Executable doesn't exist at ...ms-edge`**
Edge channel needs a one-time install. The workflow already runs `npx playwright install --with-deps msedge` — make sure you didn't remove it.

**`report-runs` job never runs**
That job is gated by the `PLAYWRIGHT_AI_STUDIO_URL` repository **variable** (not secret). Add it under Settings → Secrets and variables → Actions → Variables tab. Variables added on a fork won't be available until the workflow runs from the base repo.

**`Resource not accessible by integration` on the JUnit summary step**
The `mikepenz/action-junit-report` action needs `checks: write` permission. If you've tightened the default `GITHUB_TOKEN` permissions, add this to the workflow:
```yaml
permissions:
  contents: read
  checks: write
```

**HTTP 502 from the FastAPI server in CI**
Only happens if you've enabled the optional Azure-OpenAI-in-CI path. Confirm the four `AZURE_OPENAI_*` secrets/variables are set correctly.

# Playwright AI Studio

AI-powered Playwright test synthesis and auto-healing platform.  
Backed by **Azure OpenAI** (GPT-4o) · Python **FastAPI** · single-file HTML frontend.

---

## Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.11+ |
| Azure OpenAI resource | GPT-4o deployed |

---

## Quickstart

```bash
# 1. Clone / copy this folder into your project
cd playwright-ai-studio

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
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

---

## .env values

```env
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_API_VERSION=2024-02-01
AZURE_OPENAI_DEPLOYMENT=gpt-4o      # match your Azure deployment name
```

---

## Workflow

### 1 — Synthesize
Describe your test case in plain English (e.g. "Path A — update National ID, set email Is Primary = No, submit").  
Paste any existing script fragments as hints.  
AI analyses selector risks, applies healing strategies, and generates a complete TypeScript Playwright file.  
Save it as a **Golden** file.

### 2 — Golden Files
Immutable reference scripts. Each golden tracks its heal count and last-healed date.  
Golden files are **never silently modified**.

### 3 — Run History
After each Playwright run, POST your results to `/api/runs` (or use `seed_data.py` to seed historical data).  
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

## Recording a run from Playwright

After your test suite finishes, post results like this:

```python
import requests, json

requests.post("http://localhost:8000/api/runs", json={
    "golden_id": "seed-g1",
    "browser": "msedge",
    "candidates": [
        {"name": "Rosa Philp",      "path": "A", "status": "pass", "duration": "48s"},
        {"name": "Test Onb123",     "path": "B", "status": "fail", "duration": "12s",
         "error": "TimeoutError: Nudge button not found after 15000ms"},
    ]
})
```

Or use the included `excel-reporter.ts` as a reference to also POST to this API.

---

## File structure

```
playwright-ai-studio/
├── server.py          # FastAPI backend
├── seed_data.py       # Pre-populate with existing project data
├── requirements.txt
├── .env.example
├── static/
│   └── index.html     # Full UI — no build step
├── golden/            # Auto-created — stores golden JSON files
└── runs/              # Auto-created — stores run result JSON files
```

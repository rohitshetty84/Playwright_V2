# TestMind

AI-powered browser automation tool. Describe a test in plain English, watch it drive a real browser step-by-step, then promote the verified result as a golden test file that runs in CI.

---

## Quick Start

```bash
cd studio
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env        # fill in your Azure OpenAI credentials
python3 -m uvicorn server:app --host 0.0.0.0 --port 7860
```

Open **http://localhost:7860** — you land on the Explore tab.

---

## The Four Tabs

### 🔍 Explore — main workflow

The browser explorer is the primary entry point. It takes a plain-English test description, drives a real Chromium browser through every step, verifies each action, and produces a structured Markdown report of what happened.

**How to use it:**

1. **Paste your test case** in the text area. Include the full starting URL and describe every step, including conditional branches (Path A / Path B). Example:
   ```
   1. Navigate to https://my-app.example.com/dashboard
   2. Click the "New Request" button
   3. Fill in the "Employee Name" field with "Jane Smith"
   4. Submit the form
   5. Verify the confirmation banner appears
   ```

2. **Select an Application Context** from the dropdown (SAP SuccessFactors, Workday, ServiceNow, etc.) or choose "Custom" and type your own. This tells the AI which navigation patterns and UI framework to expect.

3. **(Optional) Click ✨ Enrich Steps.** The AI expands your high-level description into granular steps — explicit clicks, waits, menu openings — using knowledge of the selected app. A preview appears for you to review and edit before continuing. Click "✓ Use this" to apply.

4. **Set Auth Session** — the filename stem of your saved auth state under `studio/.auth/` (e.g. `successfactors` → loads `studio/.auth/successfactors.json`). This lets the browser skip the login screen.

5. **Adjust Max Steps** if your flow is long (default: 25).

6. **Headless** — checked by default; the browser runs silently in the background. Uncheck to watch it happen on screen.

7. **Click 🔍 Start Exploration.** A live log streams below the button showing each step as it executes. Steps show ✅ / ❌ with the action taken, selector used, and number of attempts.

8. **When complete**, the Exploration Result card appears with a Markdown summary. From here you can:
   - **Generate Golden** — promotes the exploration into a TypeScript Playwright test file saved as a Golden
   - **Copy Markdown** — copy the step-by-step report for documentation or debugging

---

### 📄 Golden Files

Goldens are the source-of-truth test scripts. They are committed to the repository and executed by CI on every push.

**What you can do here:**

- **View all goldens** — each card shows the test name, description, last heal date, and heal count
- **Copy code** — grab the raw TypeScript for any golden
- **⬆️ Sync to GitHub** — commits and pushes any unsaved goldens so the CI pipeline can find them
- **▶ Dispatch GitHub workflow** — run specific goldens in CI right now without waiting for a push. Enter the golden IDs separated by commas (or click "Fill all IDs") then click "Run selected IDs"

Golden files are never silently modified. The only way a golden changes is if you explicitly promote a healed version in the Auto-Heal tab.

---

### 📥 Batch Regression

Upload an Excel workbook with rows of test cases and golden targets, then dispatch the batch to GitHub for execution. Use the batch upload list to monitor batches, download result spreadsheets, and re-dispatch failed batch runs.

---

### ▶ Run History

- Results sync automatically when you open the tab
- Click **↺ Refresh from GitHub** to pull the latest
- Failed runs show the exact error message and the step that failed — use this to decide whether to trigger Auto-Heal

---

### 🔧 Auto-Heal

When CI starts failing, Auto-Heal analyses the failures, generates a fixed script, and shows you the diff. You decide whether to promote it.

**How to use it:**

1. Switch to the Auto-Heal tab — a red badge on the tab icon shows how many goldens have recent failures
2. **Select a Golden** from the dropdown (failures are flagged with ❌)
3. **Click 🔧 Run Auto-Heal** — the AI reads the failure errors and generates a healed TypeScript file with `[AI-HEAL]` inline comments explaining each change
4. **Review the Changes Made** section — a summary of what was fixed and why
5. **Review the Healed Script** — the full TypeScript with changes highlighted
6. **Promote or discard:**
   - **✓ Promote as New Golden** — replaces the current golden with the healed version (heal count increments)
   - **Discard** — throws away the suggestion; the current golden is unchanged

The current golden is **never overwritten without your approval**.

---

## Setting Up Auth (First Time)

The browser explorer needs a saved login session to skip the auth wall on each run.

```bash
# Record your session once
npx ts-node scripts/auth.ts
# Follow the browser prompt to log in — saves studio/.auth/<app>.json
```

The auth file is gitignored and stays local. Re-record it when it expires (typically 30 days for SSO-based apps).

---

## Running Tests Locally

To run the goldens on your machine exactly as CI does:

```bash
npm install
npx playwright install --with-deps msedge
python ci/export_goldens.py --from studio/golden --to tests
npx playwright test
```

---

## CI / GitHub Actions

The workflow in `.github/workflows/playwright.yml` runs automatically on push, pull request, and nightly. It:

1. Exports `studio/golden/*.json` → `tests/*.spec.ts`
2. Runs `npx playwright test` on `ubuntu-latest`
3. Posts results back to Studio (if `PLAYWRIGHT_AI_STUDIO_URL` is set as a repo variable)

**Required GitHub Secrets / Variables** (Settings → Secrets and variables → Actions):

| Name | Type | Purpose |
|------|------|---------|
| `AZURE_OPENAI_ENDPOINT` | Variable | Azure OpenAI resource URL |
| `AZURE_OPENAI_API_KEY` | **Secret** | API key — always a secret |
| `AZURE_OPENAI_API_VERSION` | Variable | e.g. `2024-02-01` |
| `AZURE_OPENAI_DEPLOYMENT` | Variable | e.g. `gpt-4o` |
| `PLAYWRIGHT_AI_STUDIO_URL` | Variable | Optional — enables run reporting back to Studio |

---

## .env (local dev only)

```env
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_API_VERSION=2024-02-01
AZURE_OPENAI_DEPLOYMENT=gpt-4o
```

Never commit `.env`. In CI the same values come from GitHub secrets/variables.

---

## File Structure

```
.
├── studio/
│   ├── server.py                  # FastAPI backend
│   ├── static/index.html          # Full UI — no build step
│   ├── golden/                    # Source-of-truth test scripts (committed)
│   ├── explorations/              # Exploration run reports (gitignored)
│   ├── .auth/                     # Saved browser sessions (gitignored)
│   ├── selector_memory.json       # Learned selectors across runs (committed)
│   ├── exploration_patterns.json  # Learned interaction patterns (committed)
│   └── learned_rules.json         # Learned healing rules (committed)
├── scripts/
│   ├── auth.ts                    # One-time auth session recorder
│   └── smoke_explore.py           # CLI smoke test for the explorer pipeline
├── ci/
│   ├── export_goldens.py          # golden/*.json → tests/*.spec.ts
│   └── report_run.py              # Playwright results → POST /api/runs
├── .github/workflows/playwright.yml
└── playwright.config.ts
```

---

## Troubleshooting

**Server not starting** — make sure you're using `python3 -m uvicorn server:app` from the `studio/` directory with the venv active.

**Auth session expired** — re-run `npx ts-node scripts/auth.ts` to record a fresh session.

**Step keeps failing at navigation** — use ✨ Enrich Steps first. SAP Fiori and similar apps require explicit menu-opening steps before clicking a navigation item; the enrichment adds these automatically.

**Exploration produces 0 steps** — check that the server can reach the app URL and the auth session is valid.

**CI: `no goldens exported`** — the `golden/` directory is empty or contains malformed JSON. Save at least one golden from the Explore tab and sync it to GitHub.

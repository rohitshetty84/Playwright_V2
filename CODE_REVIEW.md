# Code Review — Playwright AI Studio

**Reviewer:** Claude (Cowork)
**Date:** 2026-06-02
**Scope:** `studio/server.py` (1,931 LOC) · `studio/healing_engine.py` · `studio/static/index.html` (1,291 LOC) · `validate-test.js` · `scripts/auth.ts` · `.github/workflows/playwright.yml` · `ci/export_goldens.py` · `ci/report_run.py` · 6 goldens · 19-attempt healing history sampled.

The goal of this review is to identify product issues blocking efficient browser test automation and to recommend changes that raise output quality, reduce flakiness, and lower the cost of running this system at scale.

---

## Executive summary

The product idea is strong: synthesize tests from natural language, validate locally before committing, heal selectors with vision context, push goldens to GitHub for canonical runs. The implementation works — six goldens have been created, healing history shows the loop is exercising itself, and the CI pipeline lands the artifacts where they belong.

The biggest risks are not feature gaps. They are:

1. **Quality drift in goldens.** Healing prompts encourage `.or()` chains and over-broad selectors (`getByRole('heading').first()`). Goldens are getting *less* specific over time, not more.
2. **One real bug** in `record_run` and **one parse-fragility bug** in `validate_test_locally` that together are responsible for most of the "ERROR" entries in healing history.
3. **Architectural debt** — `server.py` at 1,931 lines, `index.html` at 1,291 lines, the same 30-line vision-heal block copy-pasted into three endpoints. This is slowing iteration and will keep slowing it.
4. **Security exposure** — every API endpoint is unauthenticated, CORS is `*`, and a stored GitHub PAT is required to push goldens. Fine for laptop use, dangerous if this ever runs on a shared host.
5. **Healing engine is too narrow.** Four hardcoded regex patterns; most real failures fall into "UNKNOWN" and get a generic prompt.

Findings below are tagged P0 (fix now), P1 (fix this sprint), P2 (next quarter), P3 (nice to have).

---

## P0 — Real bugs to fix today

### P0-1 — `record_run` references undefined `history` on the happy path

`studio/server.py:1317-1328`. The `history` variable is only assigned inside `if has_failures:`. When all candidates pass, the `else:` branch reads `if history:` — `NameError`. In practice this means **every successful healing run also raises an exception** that's silently swallowed because FastAPI returns 200 before this code runs (it's after the response is computed, but inside the same handler — actually it runs and crashes the request).

```python
if has_failures:
    ...
    history = load_healing_history(req.golden_id)   # defined here
    if history: ...
else:
    # All tests passed! Healing succeeded!
    if history:        # ← UnboundLocalError
        save_healing_attempt(...)
```

Fix: hoist `history = load_healing_history(req.golden_id)` above the `if has_failures` branch.

### P0-2 — `validate-test.js` JSON parse swallows the failure screenshot path

`validate-test.js:113-121` searches stdout for a line starting with `{`. But Playwright's JSON reporter emits a massive single-line JSON blob *and* the `[validate]` log lines are also written to stdout. The parser finds the first `{`-prefixed line, but that's often the Playwright report itself, not the structured result line that gets `console.log(JSON.stringify(result))` at the bottom. The healing history shows this concretely:

```
"newError": "Failed to parse validation result: [validate] Copied...
            [validate] Running with LOCAL_VALIDATION=true
            [validate] Cleaned up...
            {\"status\":\"FAIL\",\"error\":\"locator.waitFor: ...
```

Fix: scan from the *end* of stdout and pick the first JSON-shaped line, not the first. Or write the result to a temp file and have the Python side read it back. Or use a sentinel marker:

```js
console.log('---RESULT---');
console.log(JSON.stringify(result));
```

Then in Python: split on the marker and parse what's after it.

### P0-3 — Concurrent writes to a golden / healing-history clobber each other

`save_json` does a blind overwrite. `load_healing_history` returns a list, the caller appends, then re-saves. If two heal requests for the same golden land within the same second (very possible from the UI), one will overwrite the other's append. **No bug observed yet** but the pattern is unsafe and the surface area will only grow.

Fix: per-golden `asyncio.Lock` keyed by `golden_id`, held across read-modify-write. Or move to SQLite — it gives you transactions, indexes, and concurrency safety for the same effort as the JSON-file-per-record approach.

---

## P1 — Architectural debt blocking iteration speed

### P1-1 — `server.py` is doing eight jobs

1,931 lines, ~20 endpoints, vision client + healing engine + git automation + GitHub Actions dispatch + screenshot capture + LLM prompting + log file management. This file should be a router that delegates to:

```
studio/
├── server.py                 # FastAPI app + route registration (≤150 LOC)
├── api/
│   ├── goldens.py            # /api/goldens/*
│   ├── runs.py               # /api/runs/*
│   ├── heal.py               # /api/heal/*
│   ├── synthesize.py         # /api/synthesize/*
│   ├── github.py             # /api/trigger-ci, /api/workflow-status
│   └── health.py             # /api/health/*
├── services/
│   ├── llm.py                # ask_llm, vision call wrappers, retries
│   ├── playwright_runner.py  # validate_test_locally, screenshot helpers
│   ├── git_sync.py           # git_sync_goldens
│   └── prompts.py            # All system/user prompts as constants
├── healing/
│   ├── engine.py             # current healing_engine.py
│   └── history.py            # load/save healing attempts with locking
└── storage/
    └── repository.py         # load_goldens, load_runs, save_json (one place)
```

This is not aesthetic. The synthesis-with-validation handler is 600+ lines because everything it touches is in the same file — there's nowhere to extract to. As long as that's true, every new feature lands *inside* that handler.

### P1-2 — Three identical vision-heal blocks

Search `studio/server.py` for `data:image/png;base64,` — you'll find the same ~30-line pattern in `/api/heal`, `/api/heal-and-validate`, and `/api/synthesize/with-validation`. They differ only in their system prompt and `max_tokens`. Extract:

```python
async def vision_heal(
    *, image_b64: str | None, system_prompt: str, user_prompt: str,
    max_tokens: int = LLM_VISION_TOKENS,
) -> str:
    if image_b64:
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            {"type": "text", "text": f"{system_prompt}\n\n{user_prompt}"},
        ]
    else:
        content = f"{system_prompt}\n\n{user_prompt}"
    resp = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
        temperature=LLM_TEMPERATURE,
    )
    return _clean_healed_code(resp.choices[0].message.content.strip())
```

Same story for the four copies of the navigation-strategy loop (`for _w, _t in [('domcontentloaded', 30000), ...`).

### P1-3 — `index.html` at 1,291 lines is at the file-size limit of "no build step"

It works today. It will not survive another feature. Either (a) commit to a build step (Vite + a couple of components is enough — you don't need React), or (b) split into `index.html` + `app.css` + `app.js` + per-tab JS modules. Lazy load goldens/runs/heal panes so the initial bundle stays small.

### P1-4 — Synthesis workflow is one mega-function instead of a state machine

The `synthesize_with_validation` endpoint is a 600-line linear script with PHASE 0 → PHASE 1.R → PHASE 2.R → PHASE TUNE → PHASE 3 inlined. Each phase has its own error handling, its own log_json_result call, its own success/fail accumulator. Extract phases as classes with a shared `Context` object:

```python
class SynthesisContext:
    test_case: str
    url: str | None
    code: str
    rounds: list[RoundResult]
    decision: Decision | None

class Phase0_Synthesize:    async def run(self, ctx): ...
class Phase1_Validate:      async def run(self, ctx): ...
class Phase2_VisionHeal:    async def run(self, ctx): ...
class PhaseTune_Selectors:  async def run(self, ctx): ...
class Phase3_Validate:      async def run(self, ctx): ...

PIPELINE = [Phase0Synthesize, Phase1Validate, Phase2VisionHeal, PhaseTuneSelectors, Phase3Validate]
```

Now adding a "PhaseScreenshot diff" or "PhaseAccessibility check" is a class, not a 100-line patch.

---

## P1 — Test quality issues that compound over time

### P1-5 — Generated selectors are too generic, and healing makes them worse

Sampled golden `2ec86545.json`:

```ts
const result = page.getByRole('heading').first();
await expect(result).toBeVisible();
```

Every Google results page has dozens of headings. `.first()` is whichever happens to be first in DOM order — usually the "About" or "More options" link, not the result the test claims to be checking. The assertion is meaningless: the test passes whether or not search worked.

The healing prompts reinforce this:

```
For Google search box specifically:
   - Primary: input[name="q"]
   - Fallback 1: input[aria-label="Search"]
   - Fallback 2: .gLFyf
```

Hardcoded CSS classes (`.gLFyf`) defeat the whole point of a healing engine — they'll be invalidated next Google deploy.

Recommendations:

- Bias prompts toward **semantic role + accessible name** (`getByRole('searchbox', { name: 'Search' })`) — these survive UI refactors.
- Tell the model **not** to use `.first()` unless the test explicitly states "any heading" — require a disambiguating name or text instead.
- Penalize CSS class selectors in the tune phase (Phase TUNE) — auto-replace them with role/name pairs.
- Add an **assertion-strength check** to Phase 3: if the test passes but the assertion is `toBeVisible()` on an unnamed locator, mark it `WEAK` and warn.

### P1-6 — `.or()` fallback chains are an anti-pattern at scale

The healing engine promotes `.or()` chains for "robustness." In practice they make debugging nearly impossible (which branch matched?), they hide selector drift (a test keeps passing while pointing at the wrong element), and they pile up across heal rounds. Real Playwright guidance is: pick *one* good selector and let it fail loudly.

Replace the `.or()` pattern in the healing prompts with: "If the selector is ambiguous, change the test to use a more specific accessible name. If no accessible name exists, that is a bug in the page, not the test."

### P1-7 — Tests rely on `waitForLoadState('networkidle')` which is documented as flaky

Playwright explicitly says `networkidle` is discouraged because modern apps poll continuously. Prefer waiting for a specific element to be `visible` — that's both faster and more meaningful. Healing prompts should phase this out.

### P1-8 — No regression detection on healing

The `healCount` field counts heals but doesn't track *quality*. A golden could heal 12 times, pass each time, and silently be testing nothing. Add a hash of the assertions on every promote, and compare:

- `assertions_hash` — sha256 of every `expect(...).to*()` call's text representation
- If a heal changes selectors only, hash is unchanged → safe heal
- If a heal also weakens or removes an assertion, flag it: `assertion_drift: true`

This catches the failure mode where the healer "fixes" a test by deleting the assertion that was failing.

---

## P1 — Healing engine is too narrow

### P1-9 — Four patterns, mostly UNKNOWN diagnoses

`healing_engine.py:FRAME_ERROR_PATTERNS` has four entries. Looking at real-world Playwright failure categories the engine doesn't model:

| Failure mode | Frequency | Current handling |
|---|---|---|
| Selector timeout | High | ✅ `selector_timeout` |
| Frame context mismatch | Low-Med | ✅ `login_corruption` / `selector_mixing` |
| Network timeout / page didn't load | High | ❌ UNKNOWN |
| Assertion mismatch (`expect(x).toHaveText(y)` failed) | High | ❌ UNKNOWN |
| Element occluded / not clickable | Medium | ❌ UNKNOWN |
| Iframe traversal needed | Medium | ❌ UNKNOWN |
| Modal / overlay intercepted click | High | ❌ UNKNOWN |
| Cookie consent banner blocking | Very high (EU sites) | ❌ UNKNOWN |
| New tab / popup not awaited | Medium | ❌ UNKNOWN |
| File upload / download dialog | Low | ❌ UNKNOWN |
| Element re-rendered between locate and act | Medium | ❌ UNKNOWN |

Each one has a recognizable error signature. Add them with targeted fix prompts. The healing engine should diagnose at ≥ 80% of failures, not the current ~30%.

### P1-10 — Regex patterns are too loose

```python
"timing_race": { "pattern": r"waitFor|timing|race|async", ... }
```

That regex matches the word "async" — present in every Playwright test. The `timing_race` bucket will swallow every UNKNOWN error. Use specific error strings ("page.goto: Timeout 30000ms exceeded", "expect.toBeVisible failed: locator resolved to 2 elements", etc.).

### P1-11 — Diagnoses are not fed back into the prompt

Look at the heal prompt — it gets `latest_error or error_summary` and the original code. The diagnosis (`root_cause`, `confidence`, `evidence`) is logged but only weakly used (just to pick which `fix_prompt` to attach). The model is reading the error from scratch every time. Instead:

```
ERROR ANALYSIS:
- Root cause: selector_timeout (confidence 95%)
- Evidence: "Element timeout — selector: input[name='q']"
- Recommended strategy: increase timeout to 30000ms + add networkidle wait

YOUR TASK:
Apply the strategy above to fix the test. Do NOT diagnose — diagnosis is done.
```

This shaves tokens and forces a more targeted edit.

### P1-12 — No learning across goldens

Healing history is per-golden. But if 5 different goldens hit the same `Cookie banner intercepted click` error, the engine learns it 5 times. Add a **global pattern store**: when an error+fix pair succeeds three times across goldens, promote it to the catalog.

---

## P2 — Security & operational hygiene

### P2-1 — Every API endpoint is unauthenticated

`server.py` mounts FastAPI with `CORSMiddleware(allow_origins=["*"])` and no auth dependency. Anyone reaching the host can synthesize tests, heal goldens, push to GitHub, or trigger workflows.

For a single-user laptop tool this is fine. The moment you put it on a corporate VM or share the URL, it's a serious problem. Add a single bearer token check:

```python
async def require_token(authorization: str = Header(None)):
    expected = os.getenv("STUDIO_BEARER_TOKEN")
    if not expected:
        return  # auth disabled
    if authorization != f"Bearer {expected}":
        raise HTTPException(401)

app.include_router(router, dependencies=[Depends(require_token)])
```

Disabled by default (don't break the laptop case), enabled with one env var.

### P2-2 — `git_sync_goldens` runs `git push` from the server with no isolation

If the goldens directory has uncommitted changes from another source, `git add studio/golden/` is targeted but the `git push` will send everything that's already staged. Add `git stash --keep-index` before push, or use a porcelain script that only allows pushing if HEAD matches a known state.

Also: `git_sync_goldens` swallows push failures into a warning log. If GitHub rate-limits or the credential expires, every "Save as Golden" succeeds locally but silently fails to sync. Surface this in the UI — return `gitSynced: false` and show a banner.

### P2-3 — Vision API takes screenshots of any URL in the test description

If a tester pastes "go to https://internal-finance-app.example/payroll and verify…", the server will navigate to it, screenshot it, and send the screenshot to Azure OpenAI. For some compliance regimes this is a data exfiltration event.

Add an allowlist of host patterns, configurable via `.env`:

```env
VISION_ALLOWED_HOSTS=*.public-test.com,wikipedia.org,github.com
```

Block vision (fall back to text-only) for anything else, with a clear log message.

### P2-4 — Azure OpenAI errors leak the full exception to the client

`ask_llm` returns the exception string in the HTTP detail. If Azure returns a 401 with a key fingerprint in the body, that fingerprint ends up in the browser network tab. Sanitize:

```python
except Exception as e:
    logger.exception("LLM error")
    raise HTTPException(502, "LLM request failed — see server logs")
```

### P2-5 — Default `.env` location loads root `.env` after the studio one with `override=False`

`server.py:107-110`:

```python
load_dotenv()          # current working dir → may not find anything
if ROOT_ENV.exists():
    load_dotenv(ROOT_ENV, override=False)
```

If the user runs `python studio/server.py` from the repo root vs from `studio/`, behavior differs. Be explicit:

```python
load_dotenv(BASE / ".env", override=False)             # studio/.env
load_dotenv(BASE.parent / ".env", override=False)      # repo root .env
```

---

## P2 — CI pipeline polish

### P2-6 — No matrix strategy across goldens

The `playwright-test` job runs every exported spec serially in one VM. With six goldens that's already 90s+ of wall time. Use a matrix:

```yaml
strategy:
  fail-fast: false
  matrix:
    golden: ${{ fromJson(needs.prepare-goldens.outputs.golden_list) }}
```

Have `prepare-goldens` emit a `golden_list` output, then each matrix entry runs one spec. Linear speedup, plus failure isolation (one broken golden doesn't fail the rest).

### P2-7 — Browser cache key is too coarse

```yaml
key: playwright-${{ runner.os }}-${{ hashFiles('package-lock.json', 'package.json') }}
```

`package.json` doesn't change when Playwright minor version changes. Include the resolved version explicitly:

```bash
PLAYWRIGHT_VERSION=$(npx playwright --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
echo "PLAYWRIGHT_VERSION=$PLAYWRIGHT_VERSION" >> $GITHUB_ENV
```

Then key on `$PLAYWRIGHT_VERSION`. Saves 60-90s per run after the first cache miss.

### P2-8 — `report-runs` only runs once at the end

Currently it POSTs the entire run report. If you matrix the test job, each matrix entry should POST its slice. The Studio's `/api/runs` endpoint already accepts per-golden runs — wire one POST per matrix entry. Saves you from needing to aggregate across jobs.

### P2-9 — Workflow file references `node-version: "20"` but Node 22 is current LTS

Not urgent — Node 20 is still supported. Worth bumping when you next touch the file.

### P2-10 — No PR status check enforcement

Nothing in the workflow gates the `report-runs` step on `playwright-test` outcome. If you want goldens to be quality-gated, add a "passing-rate" check that fails the workflow if < N% of candidates pass.

---

## P3 — Performance & DX

### P3-1 — Vision screenshots are repeated each heal round even when the page hasn't changed

`Phase 2.R` takes a fresh screenshot every round. If round N and round N+1 fail at the same step on the same URL, the screenshot is identical. Cache by `(url, step_at_failure)` and reuse — saves a browser launch (~3-5s) per round.

### P3-2 — `validate_test_locally` spawns a fresh `npx playwright test` per call

`npx`-ing is slow (~2-4s of node + playwright startup). For tight heal loops this dominates wall time. Two options:

- **Quick win**: invoke `node node_modules/@playwright/test/cli.js test ...` directly — skips the npx resolution.
- **Better**: keep a long-running Playwright worker process; the server pipes a JSON command in and reads results back. ~10x faster for the heal loop.

### P3-3 — `LOCAL_VALIDATION=true` flips headless mode invisibly

A test that passes headed often fails headless and vice versa. The current setup runs heal-loop validation headed (visible browser) but CI runs headless. Healed goldens may pass locally and fail in CI for this reason. Either:

- Always run validation headless (matches CI), or
- Add a UI toggle and surface "this validation ran headed" in the result panel so users know.

### P3-4 — UI offers no way to delete a golden

`/api/goldens` has GET and POST, no DELETE. Users accumulate experimental goldens that pollute the list. Add `DELETE /api/goldens/{id}` (with confirmation) and a trash-can icon in the list.

### P3-5 — No "golden lineage" view

`healCount` grows but there's no UI to see what a golden looked like before round N. Save each promoted version (cheap — just keep the code field with a timestamp) so a user can diff and revert.

### P3-6 — Synthesis logs are written to local files only

`studio/logs/synthesis-*.log` and `synthesis-results.jsonl` are great for development. For any deployment they need to also go to stdout (so Docker / k8s can collect them). Add a `RotatingFileHandler` for files and keep `StreamHandler` on `INFO`.

### P3-7 — `requirements.txt` is duplicated (`./requirements.txt`, `studio/requirements.txt`, `ci/requirements.txt`)

If they diverge, the bug is invisible until CI runs. Pick one source of truth, have the others reference it via `-r ../requirements.txt`.

### P3-8 — `TEST_CASE_DESCRIPTIONS.md` is excellent reference content but isn't surfaced in the UI

It's a goldmine of curated test ideas. Add a "Suggested test cases" dropdown in the Synthesize tab that pulls from this file (or from an inline `studio/test-suggestions.json`). One click → prefilled test case → first golden.

---

## Recommended sequence (the smallest set of changes for the biggest gain)

If you do nothing else, do these six things in order. Each unblocks the next.

1. **Fix P0-1** (`history` UnboundLocalError) — 5 minutes. Stops silently failing.
2. **Fix P0-2** (`validate-test.js` parse) — 30 minutes. Restores feedback in the heal loop.
3. **Extract `services/prompts.py` and `services/llm.py`** (P1-2) — half a day. The same prompt strings are scattered in 6 places; consolidating them lets you A/B test prompt changes without grep-and-replace.
4. **Add assertion-strength check to Phase 3** (P1-5) — half a day. Stops the slow degradation of golden quality.
5. **Expand the healing engine catalog** to cover cookie banners, modal interception, and assertion mismatch (P1-9). One day. Triples the diagnosis hit rate.
6. **Matrix the workflow** (P2-6) — two hours. Linear speedup on CI; failure isolation.

Everything else is genuine but cheaper once these six are done.

---

## Things that are good and should be preserved

Worth calling out explicitly so they're not lost in the next refactor:

- The **storageState pattern in `scripts/auth.ts`** is the right design — credentials never enter test code, login is run once locally and the session is reused. Keep this exactly as is.
- The **synthesis → validate → tune → re-validate** loop is the right shape, even if the implementation is overgrown. The phase boundaries are clear and the JSONL audit trail is gold.
- The **failure-screenshot-from-the-moment-of-failure** mechanism in `validate-test.js` is a real innovation over typical "screenshot the homepage and ask the model to figure it out." Don't lose this when refactoring.
- The **vision-with-text-only-fallback** in `Phase 0` is robust — if Azure vision is down or the URL doesn't return a screenshot, the workflow keeps moving. Pattern is reusable elsewhere.
- The `[AI-HEAL]` and `[AI-SYNTHESIZED]` inline comment convention is excellent for human review. Preserve it through all refactors.

---

*End of review. Happy to drill into any specific finding, prototype a fix, or scope the recommended refactor.*

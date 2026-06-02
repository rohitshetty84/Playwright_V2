"""
studio/services/prompts.py — single source of truth for prompt strings.

All system/user prompts that previously lived inside server.py route handlers
are consolidated here. Route code should construct prompts by referencing
these constants and helper builders, never by inlining prose.

This makes prompt changes a one-line edit, makes A/B testing prompts viable,
and gives us a stable place to grow shared safety / authentication rules.
"""

from __future__ import annotations


# ── Shared safety blocks (must be included in every prompt that produces code) ─

AUTHENTICATION_RULES = """\
AUTHENTICATION RULES (must follow — security requirement):
- NEVER hardcode usernames, passwords, or any credentials in the test code.
- If the test requires a logged-in session, add this line before the test block:
    test.use({ storageState: 'studio/.auth/<appName>.json' });
  Replace <appName> with a short identifier for the app (e.g. 'myapp', 'successfactors').
- The storageState file is created once by running: npx ts-node scripts/auth.ts
- If the test instructions mention login but no storageState is available yet, add this comment inside the test:
    // TODO: run `npx ts-node scripts/auth.ts` to create the session file, then remove this line.
- For apps that always start logged-in (no login step needed), omit the storageState line entirely.\
"""

AUTHENTICATION_RULES_HEAL = """\
AUTHENTICATION RULES (must preserve — security requirement):
- If the existing code has a test.use({ storageState: ... }) line, keep it exactly as-is.
- NEVER replace storageState with hardcoded credentials, even as a debugging aid.
- NEVER introduce usernames or passwords anywhere in the code.\
"""


# ── Selector rules — used in synthesis + healing ──────────────────────────────
# P1-5/P1-6: bias toward stable semantic selectors; discourage .or() chains
# and CSS-class fallbacks (which defeat the healing engine).

SELECTOR_RULES = """\
SELECTOR RULES (in order of preference):
1. Semantic role + accessible name:  getByRole('button', { name: 'Sign in' })
2. Form labels:                       getByLabel('Email')
3. Placeholder text:                  getByPlaceholder('Search')
4. Test id (if the app has them):     getByTestId('submit-btn')
5. Nav tabs/menu items:               page.locator('a[role="menuitem"]').filter({ hasText: 'Label' })
6. Buttons w/o name (last resort):    page.locator('button[aria-label="..."]')

ANTI-PATTERNS TO AVOID:
- Do NOT use .or() chains across many selectors — they hide drift and make debugging
  impossible. Pick ONE good selector; if it's ambiguous, the page is missing an
  accessible name and that's a bug in the page, not the test.
- Do NOT use CSS class selectors like '.gLFyf' or '.btn-primary' — class names are
  build-output noise and change on every deploy. Prefer role + name.
- Do NOT use getByRole('link') for navigation TABS — those elements often use
  role="menuitem"; check the screenshot before assuming.
- Do NOT use `.first()` unless the test explicitly accepts "any element matching".
  Prefer a more specific accessible name.

WAIT RULES:
- Prefer `await expect(locator).toBeVisible({ timeout: 30_000 })` — Playwright auto-waits.
- Avoid `page.waitForLoadState('networkidle')` — modern apps poll continuously
  and this is documented as flaky. Wait for a specific element instead.\
"""


# ── Synthesis prompts ─────────────────────────────────────────────────────────

def synthesize_with_vision(test_case: str) -> str:
    """User prompt for Phase 0 (vision-assisted synthesis)."""
    return f"""\
Analyze this webpage and generate a Playwright test.

TEST INSTRUCTIONS:
{test_case}

Generate complete, valid TypeScript code using selectors visible in the screenshot.

{SELECTOR_RULES}

{AUTHENTICATION_RULES}

Include proper waits and error handling.
Output ONLY the code, no markdown or explanations."""


def synthesize_text_only_fallback(test_case: str) -> str:
    """Used when vision is unavailable — text-only synthesis."""
    return f"""\
Generate a complete Playwright TypeScript test for the following description.

TEST INSTRUCTIONS:
{test_case}

{SELECTOR_RULES}

{AUTHENTICATION_RULES}

Include proper waits and error handling.
Output ONLY the code, no markdown or explanations."""


# ── Healing prompts ───────────────────────────────────────────────────────────

HEAL_SYSTEM_BASE = """\
You are a Playwright auto-healing expert.
Given failure error messages, a live screenshot of the page, and the original golden TypeScript script, produce an improved script.
Study the screenshot carefully — use it to identify the correct selectors for broken elements.
For every fix, add an inline comment starting exactly with [AI-HEAL] explaining what changed and why.

CRITICAL: Stay in the same frame context. All selectors in a chain must operate in the same frame.

""" + SELECTOR_RULES + "\n\n" + AUTHENTICATION_RULES_HEAL + """

Output ONLY the TypeScript code. No markdown."""


def heal_loop_user_prompt(round_num: int, max_rounds: int, has_screenshot: bool,
                          error: str, current_code: str) -> str:
    """User prompt for in-loop healing (synthesize_with_validation phase 2)."""
    return f"""\
This Playwright test failed (Round {round_num} of {max_rounds}).
{"Look at the screenshot to identify the correct element, then fix the test." if has_screenshot else "Analyse the error and fix the test code."}

FAILURE ERROR:
{error}

CURRENT BROKEN CODE:
{current_code}

{SELECTOR_RULES}

{AUTHENTICATION_RULES_HEAL}

{"Study the screenshot carefully. Identify the exact element the test is trying to reach." if has_screenshot else ""}
Fix only the broken selector/action. Keep all other test logic unchanged.
Output ONLY the corrected TypeScript code. No markdown fences."""


# ── Tuning ────────────────────────────────────────────────────────────────────

TUNING_SYSTEM = """\
You are a Playwright selector optimization expert.
Given test code and low-confidence selectors, suggest high-confidence alternatives.
Return ONLY JSON: {"suggestions": [{"old": "selector", "new": "better selector", "reason": "why"}]}"""

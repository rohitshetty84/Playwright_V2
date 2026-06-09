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


# ── SAP UI5 / SuccessFactors selector rules — appended to SELECTOR_RULES for SF tests ──
# Included whenever the target app is identified as SAP UI5 / SuccessFactors.

SAP_UI5_SELECTOR_RULES = """\
SAP UI5 / SUCCESSFACTORS ADDITIONAL RULES (this test targets an SAP UI5 app):

COMPONENT SELECTORS — always target the outer ui5-* element, never shadow-DOM internals:
  Buttons:     page.locator('ui5-button[accessible-name="Label"]')
               page.locator('ui5-button:has-text("Label")')
  Inputs:      page.locator('input[placeholder="..."]')  — plain CSS pierces shadow DOM, .fill() works reliably
               Only use page.locator('ui5-input[placeholder="..."]') if plain input selector is confirmed absent
  Dropdowns:   page.locator('ui5-select').selectOption(value)
  Checkboxes:  page.locator('ui5-checkbox[accessible-name="..."]').check()
  Dialogs:     page.locator('ui5-dialog')  — close with page.keyboard.press('Escape')
  Date fields: page.locator('ui5-date-picker').locator('input')  — fill the inner input
  Tabs:        page.locator('ui5-tabcontainer').locator('ui5-tab[text="Tab Name"]')

TABLES — UI5 tables are NOT native HTML (no <tr>/<td>):
  const row = page.getByRole('row').filter({ hasText: candidateName });
  const cellText = await row.getByRole('cell').nth(columnIndex).textContent();
  await expect(row).toContainText('Completed');
  // NEVER: page.locator('tr').locator('td:nth-child(4)') — always times out on UI5 tables

STATUS / BADGES:
  const status = await row.locator('ui5-badge').textContent();
  // Common values: "Completed" | "In Progress" | "Not Started" | "Overdue" | "Pending"
  // Trim result — badge text content may include surrounding whitespace

NAVIGATION:
  // Module nav: open home menu → click module menuitem
  await page.getByRole('button', { name: 'Home' }).click();   // label may vary
  await page.getByRole('menuitem', { name: 'Onboarding' }).click();
  // After navigation: wait for a module-specific heading, NOT networkidle
  await expect(page.getByRole('heading', { name: 'Onboarding' })).toBeVisible({ timeout: 30_000 });

TYPEAHEAD SEARCH:
  await page.locator('ui5-input[placeholder="Search for new recruit"]').fill(candidateName);
  await page.getByRole('option', { name: new RegExp('^' + candidateName) }).click();
  // The option label includes the job title — use regex starts-with match

TIMING — SAP UI5 renders asynchronously:
  // After typeahead selection or navigation, let JS settle before next action
  await page.waitForTimeout(2000);
  // Before reading a table row, wait for it to be visible
  await expect(row).toBeVisible({ timeout: 30_000 });
  // If a busy overlay blocks interaction:
  await expect(page.locator('ui5-busy-indicator')).toBeHidden({ timeout: 30_000 });

IFRAMES — some SF modules embed content in iframes:
  const frame = page.frameLocator('iframe[title*="SuccessFactors"]');
  await frame.locator('ui5-button:has-text("Submit")').click();

INPUT COMPONENT TYPES — three different components, three different patterns:
  // ui5-input        → free text, use .fill()
  // ui5-combobox     → type-and-select single value, fill then click suggestion
  // ui5-select       → pure dropdown, use .selectOption(value) — NO typing
  // ui5-multi-combobox → multi-value tokens, fill each value then click suggestion
  const tokens = await page.locator('ui5-multi-combobox').locator('ui5-token').allTextContents();
  // NEVER: page.locator('ui5-select').fill(...)  — ui5-select has no text input

PAGINATION — SF tables only render the first N rows:
  const moreBtn = page.locator('ui5-button:has-text("More")');
  while (await moreBtn.isVisible()) {
    await moreBtn.click();
    await page.waitForTimeout(1500);
  }
  // Always check for "More" before concluding a row is absent

VIRTUAL SCROLLING — rows below viewport are not in DOM:
  await page.locator('ui5-table').evaluate(el => el.scrollTop += 400);
  await page.waitForTimeout(1000);
  // Then retry the row locator

CONFIRMATION DIALOGS — required after every destructive action:
  await page.locator('ui5-dialog').locator('ui5-button:has-text("Confirm")').click();
  await expect(page.locator('ui5-dialog')).toBeHidden({ timeout: 10_000 });

INLINE VALIDATION / FORM ERRORS:
  const errMsg = await page.locator('ui5-message-strip[design="Negative"]').textContent();
  // If submit doesn't navigate → check for validation errors before retrying
  // Field-level error: ui5-input[value-state="Error"]

READ-ONLY VS EDITABLE FIELDS:
  // Editable (ui5-input, ui5-combobox, ui5-textarea): use .fill() or .selectOption()
  await expect(page.locator('ui5-input[placeholder="..."]')).toHaveValue('expected');
  // Read-only (ui5-text, span, ui5-input[readonly]): use .textContent()
  await expect(page.locator('ui5-text')).toContainText('expected');
  // NEVER .fill() a read-only field — it throws

SHELL BAR — the SF top nav is ui5-shellbar, not <header>/<nav>:
  await page.locator('ui5-shellbar').locator('ui5-avatar').click();   // profile
  await page.locator('ui5-shellbar-item[icon="bell"]').click();       // notifications

POPOVERS / OVERFLOW MENUS — triggered by button, not right-click:
  await page.locator('ui5-button[icon="overflow"]').click();
  await page.locator('ui5-popover').locator('ui5-li:has-text("Edit")').click();

SPA ACTION COMPLETION — URL does NOT change after SF saves, check these instead:
  await expect(page.locator('ui5-toast')).toContainText('saved');    // success toast
  // OR: ui5-message-strip[design="Positive"] appears
  // OR: ui5-dialog disappears after confirmation

FILE UPLOAD:
  await page.locator('ui5-file-uploader').locator('input[type="file"]').setInputFiles('/path/to/file');
  // NEVER setInputFiles on the ui5-file-uploader element itself

SIDE NAVIGATION (Admin Center):
  await page.locator('ui5-side-navigation-item[text="Manage Users"]').click();
  await page.locator('ui5-side-navigation-sub-item[text="Import Users"]').click();
  // NEVER page.locator('a').filter({ hasText: '...' }) in Admin — not <a> tags

ANTI-PATTERNS FOR SAP UI5:
  ❌ page.locator('tr').locator('td')                 — native HTML, not UI5
  ❌ page.locator('[id*="__button0"]')                — dynamic IDs, change every session
  ❌ page.locator('.ui5-button-root')                 — shadow-DOM internal
  ❌ page.waitForLoadState('networkidle')             — never resolves on SF
  ❌ page.locator('role=row[has-text="..."]')         — invalid ARIA attribute syntax
  ❌ page.locator('[aria-colindex="4"]')              — use .nth(N) instead
  ❌ page.locator('ui5-select').fill(...)             — ui5-select has no text input
  ❌ page.locator('nav') or page.locator('header')    — shell bar is ui5-shellbar
  ❌ page.waitForNavigation()                         — SPA, navigation rarely fires after saves\
"""


def _sf_extra_rules(is_sap: bool) -> str:
    """Returns the SAP UI5 block when the target is a SuccessFactors app; empty string otherwise."""
    return f"\n\n{SAP_UI5_SELECTOR_RULES}" if is_sap else ""


# ── Synthesis prompts ─────────────────────────────────────────────────────────

def synthesize_with_vision(test_case: str, is_sap: bool = False) -> str:
    """User prompt for Phase 0 (vision-assisted synthesis)."""
    return f"""\
Analyze this webpage and generate a Playwright test.

TEST INSTRUCTIONS:
{test_case}

Generate complete, valid TypeScript code using selectors visible in the screenshot.

{SELECTOR_RULES}{_sf_extra_rules(is_sap)}

{AUTHENTICATION_RULES}

Include proper waits and error handling.
Output ONLY the code, no markdown or explanations."""


def synthesize_text_only_fallback(test_case: str, is_sap: bool = False) -> str:
    """Used when vision is unavailable — text-only synthesis."""
    return f"""\
Generate a complete Playwright TypeScript test for the following description.

TEST INSTRUCTIONS:
{test_case}

{SELECTOR_RULES}{_sf_extra_rules(is_sap)}

{AUTHENTICATION_RULES}

Include proper waits and error handling.
Output ONLY the code, no markdown or explanations."""


# ── Healing prompts ───────────────────────────────────────────────────────────

def build_heal_system(is_sap: bool = False) -> str:
    """System prompt for the auto-healing expert, optionally including SAP UI5 rules."""
    return (
        "You are a Playwright auto-healing expert.\n"
        "Given failure error messages, a live screenshot of the page, and the original golden TypeScript script, produce an improved script.\n"
        "Study the screenshot carefully — use it to identify the correct selectors for broken elements.\n"
        "For every fix, add an inline comment starting exactly with [AI-HEAL] explaining what changed and why.\n\n"
        "CRITICAL: Stay in the same frame context. All selectors in a chain must operate in the same frame.\n\n"
        + SELECTOR_RULES
        + _sf_extra_rules(is_sap)
        + "\n\n"
        + AUTHENTICATION_RULES_HEAL
        + "\n\nOutput ONLY the TypeScript code. No markdown."
    )


HEAL_SYSTEM_BASE = build_heal_system(is_sap=False)


def heal_loop_user_prompt(round_num: int, max_rounds: int, has_screenshot: bool,
                          error: str, current_code: str, is_sap: bool = False) -> str:
    """User prompt for in-loop healing (synthesize_with_validation phase 2)."""
    return f"""\
This Playwright test failed (Round {round_num} of {max_rounds}).
{"Look at the screenshot to identify the correct element, then fix the test." if has_screenshot else "Analyse the error and fix the test code."}

FAILURE ERROR:
{error}

CURRENT BROKEN CODE:
{current_code}

{SELECTOR_RULES}{_sf_extra_rules(is_sap)}

{AUTHENTICATION_RULES_HEAL}

{"Study the screenshot carefully. Identify the exact element the test is trying to reach." if has_screenshot else ""}
Fix only the broken selector/action. Keep all other test logic unchanged.
Output ONLY the corrected TypeScript code. No markdown fences."""


# ── Tuning ────────────────────────────────────────────────────────────────────

TUNING_SYSTEM = """\
You are a Playwright selector optimization expert.
Given test code and low-confidence selectors, suggest high-confidence alternatives.
Return ONLY JSON: {"suggestions": [{"old": "selector", "new": "better selector", "reason": "why"}]}"""

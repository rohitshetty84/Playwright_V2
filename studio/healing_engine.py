"""
Advanced Healing Engine with Root Cause Analysis
Improved auto-healing with error signature recognition and targeted fixes
"""

import re
from typing import Optional, Dict, List, Tuple

class ErrorSignature:
    """Maps error patterns to root causes and targeted fixes"""

    FRAME_ERROR_PATTERNS = {
        "selector_timeout": {
            "pattern": r"waitForSelector.*Timeout|page\.locator.*Timeout",
            "description": "Selector element not found within timeout period",
            "indicators": [
                "Timeout",
                "waiting for locator",
                "waitForSelector",
                "exceeded"
            ],
            "fix_strategy": "INCREASE_TIMEOUTS_AND_FALLBACKS",
            "fix_prompt": """The error is caused by a selector timeout - the element isn't visible within 15000ms.

FIX STRATEGY - Increase timeouts AND add fallback selectors:
1. Increase waitForSelector timeout from 15000ms to 30000-45000ms
2. Add page.waitForLoadState('networkidle') before looking for elements
3. Use fallback selectors with .or() chains for robust element location
4. For Google search box specifically:
   - Primary: input[name="q"]
   - Fallback 1: input[aria-label="Search"]
   - Fallback 2: .gLFyf
   - Fallback 3: input[data-target="entry"]

IMPLEMENTATION PATTERN:
  await page.goto(url);
  await page.waitForLoadState('networkidle');

  // For Google search with fallbacks:
  const searchInput = page.locator('input[name="q"]')
    .or(page.locator('input[aria-label="Search"]'))
    .or(page.locator('.gLFyf'));

  await searchInput.waitFor({timeout: 30000, state: 'visible'});
  await searchInput.fill('search term');

ADVANCED - If still timing out:
1. Add explicit wait with page.waitForFunction():
   await page.waitForFunction(() => {
     const input = document.querySelector('input[name="q"]');
     return input && getComputedStyle(input).display !== 'none';
   }, {timeout: 30000});

2. Try scrolling into view before interaction:
   await searchInput.scrollIntoViewIfNeeded();
   await searchInput.fill('search term');

3. Use page.waitForTimeout for dynamic loading:
   await page.waitForTimeout(2000); // Wait for JS to render
   await searchInput.fill('search term');"""
        },
        "login_corruption": {
            "pattern": r"addCookies\(\)|page\.context\(\).*before.*goto",
            "description": "Page context setup before navigation corrupting frame",
            "indicators": [
                "login()",
                "page.context().addCookies()",
                "context.addCookies()",
                "loginPage(",
                "authenticat"
            ],
            "fix_strategy": "REMOVE_LOGIN_SETUP",
            "fix_prompt": """The error is caused by page context being manipulated BEFORE navigation.
This creates a frame context mismatch.

FIX:
1. Find and remove any login() function calls at the start of the test
2. Remove any page.context().addCookies() or similar context setup BEFORE page.goto()
3. Start with page.goto() directly without prior context manipulation
4. If authentication is needed, do it AFTER page.goto() has established the main frame

The original code likely has:
  const login = async () => { page.context().addCookies(...) };
  await login(); // ← WRONG PLACE
  await page.goto(url);

Should be:
  await page.goto(url); // ← Establish main frame FIRST
  // Then do auth if needed"""
        },
        "selector_mixing": {
            "pattern": r"\.or\(.*getByRole.*locator|getByRole.*\.or.*locator",
            "description": "Mixing different selector types in .or() chains",
            "indicators": [
                "getByRole",
                ".or()",
                "getByText",
                "page.locator",
                "mixing"
            ],
            "fix_strategy": "NORMALIZE_SELECTORS",
            "fix_prompt": """The error is caused by mixing different selector types in .or() chains.

FIX:
1. Use ONLY page.locator() chains, not getByRole/getByText
2. For fallbacks, chain locator().or(() => locator())
3. Use .first() to disambiguate, not .or() for different types
4. All locators in a chain must be page.locator() consistently

Example:
❌ WRONG:
  const el = page.getByRole('button', {name: 'Save'})
    .or(() => page.locator('button[data-action="save"]'));

✅ CORRECT:
  const el = page.locator('button[aria-label="Save"]')
    .or(() => page.locator('button[data-action="save"]'))
    .first();"""
        },
        "timing_race": {
            # P1-10: tightened — old regex matched "async" which is in every Playwright test.
            "pattern": r"(net::ERR_|page\.goto.*Timeout|navigation timeout|page closed)",
            "description": "Page didn't load — navigation timeout or network error",
            "indicators": [
                "page.goto: Timeout",
                "navigation timeout",
                "net::ERR_",
                "page closed",
            ],
            "fix_strategy": "INCREASE_NAVIGATION_TIMEOUT",
            "fix_prompt": """The error is caused by the page failing to load within the timeout.

FIX:
1. Increase navigation timeout: page.goto(url, { timeout: 60_000 })
2. Use a more lenient wait condition: { waitUntil: 'domcontentloaded' }
3. After goto, wait for a SPECIFIC element instead of networkidle (which is flaky).
4. If the URL is occasionally slow, add a retry block around page.goto.

Pattern:
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  // Wait for something real — not networkidle:
  await expect(page.getByRole('heading')).toBeVisible({ timeout: 30_000 });"""
        },

        # ── P1-9: cookie / consent banner intercepts ───────────────────────────
        "cookie_banner_intercept": {
            "pattern": r"(intercepted|element receives pointer-events|<button[^>]*(Accept|Consent|Cookie))",
            "description": "Cookie / GDPR / consent banner is intercepting clicks",
            "indicators": [
                "intercepts pointer events",
                "intercepted",
                "Accept all",
                "consent",
                "cookie",
            ],
            "fix_strategy": "DISMISS_BANNER_FIRST",
            "fix_prompt": """The error is caused by a cookie / consent banner sitting on top of your target element.

FIX:
1. After page.goto, dismiss the banner BEFORE interacting with the page:
   await page.getByRole('button', { name: /accept|agree|got it/i })
     .click({ timeout: 5_000 })
     .catch(() => { /* no banner — that's fine */ });
2. Make the dismiss tolerant — many CI runs won't see a banner at all.
3. If the banner is in an iframe, use page.frameLocator('iframe[id*="consent"]')."""
        },

        # ── P1-9: modal / overlay intercepts click ─────────────────────────────
        "modal_overlay_intercept": {
            "pattern": r"(intercepted|<div[^>]*modal|<div[^>]*overlay)",
            "description": "A modal dialog or overlay is blocking interaction",
            "indicators": [
                "intercepts pointer events",
                "modal",
                "overlay",
                "backdrop",
                "dialog",
            ],
            "fix_strategy": "DISMISS_MODAL_FIRST",
            "fix_prompt": """A modal or overlay is between the test and the target element.

FIX:
1. Identify the modal close button — usually getByRole('button', { name: /close|dismiss/i }).
2. Click it before continuing, with a tolerant timeout.
3. If the modal appears mid-flow, the test should expect it explicitly:
     await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click();
4. Never use .force() — that masks the issue instead of fixing it."""
        },

        # ── P1-9: assertion mismatch (test ran, expectation failed) ────────────
        "assertion_mismatch": {
            "pattern": r"(expect\([^)]*\)\..*Expected|Received string:|to have text|toHaveText|toContainText)",
            "description": "Test ran successfully but an assertion didn't match",
            "indicators": [
                "Expected:",
                "Received:",
                "toHaveText",
                "toContainText",
                "Expected substring",
            ],
            "fix_strategy": "SOFTEN_OR_NARROW_ASSERTION",
            "fix_prompt": """The selector worked, but the assertion's expected value didn't match the page.

FIX (one of these, depending on intent):
1. If the wording on the page changed: update the expected string.
2. If the assertion was too strict: use a regex with toMatch():
     await expect(locator).toHaveText(/playwright/i);
3. If the test just needs to confirm presence: use toBeVisible() instead of text equality.
4. If the page is i18n and the test is locale-dependent: pin the locale via test.use({ locale: 'en-US' }).

DO NOT delete the assertion. A test that asserts nothing passes for the wrong reason."""
        },

        # ── P1-9: iframe traversal needed ──────────────────────────────────────
        "iframe_traversal": {
            "pattern": r"(<iframe|inside an iframe|frameLocator|cross-origin)",
            "description": "Target element lives inside an iframe",
            "indicators": [
                "iframe",
                "frame-locator",
                "cross-origin frame",
                "frameLocator",
            ],
            "fix_strategy": "USE_FRAME_LOCATOR",
            "fix_prompt": """The element lives inside an iframe. page.locator() doesn't traverse iframes.

FIX:
1. Identify the iframe by attribute: src, name, id, or title.
2. Use frameLocator to traverse:
     const frame = page.frameLocator('iframe[title="Payment"]');
     await frame.getByLabel('Card number').fill('4242 4242 4242 4242');
3. If the frame is cross-origin, you can still interact with its DOM via frameLocator —
   but you cannot read its cookies / localStorage."""
        },

        # ── P1-9: element re-rendered (locator detached) ───────────────────────
        "stale_locator": {
            "pattern": r"(detached|stale|element is not attached|context destroyed)",
            "description": "Locator points to an element that re-rendered between locate and act",
            "indicators": [
                "element is not attached",
                "detached from DOM",
                "Execution context was destroyed",
            ],
            "fix_strategy": "RELOCATE_BEFORE_EACH_ACTION",
            "fix_prompt": """The element was re-rendered between when you located it and when you tried to act on it.

FIX:
1. Don't reuse a locator across re-renders. Re-locate immediately before each action:
     await page.getByRole('button', { name: 'Save' }).click();
     // After the click, the form re-renders. To check the new state:
     await expect(page.getByText(/saved/i)).toBeVisible();
2. If you need a stable reference, wait for the re-render to settle:
     await expect(page.getByRole('button', { name: 'Save' })).toBeVisible();
3. Avoid storing locator results in variables across awaits — they go stale."""
        },
    }

    @classmethod
    def diagnose(cls, error_msg: str, test_code: str) -> Dict:
        """Analyze error message and code to identify root cause"""

        if not error_msg or not isinstance(error_msg, str):
            return {"root_cause": "UNKNOWN", "confidence": 0.0}

        error_lower = error_msg.lower()

        # ═══ PRIORITY 1: Check for SELECTOR TIMEOUT errors ═══
        # These are most common and need specific timeout + fallback strategies
        if ("timeout" in error_lower and ("waiting for" in error_lower or "waitfor" in error_lower)) or \
           ("waitforselector" in error_lower and "timeout" in error_lower):

            # Extract selector from error message for better context
            selector_match = re.search(r"locator\('([^']+)'\)", error_msg)
            selector_info = f" - selector: {selector_match.group(1)}" if selector_match else ""

            return {
                "root_cause": "selector_timeout",
                "confidence": 0.95,
                "pattern": cls.FRAME_ERROR_PATTERNS.get("selector_timeout", {}),
                "evidence": f"Element selector timeout detected{selector_info}",
                "selector": selector_match.group(1) if selector_match else None
            }

        # ═══ PRIORITY 2: Check for frame-related errors ═══
        if "locators must belong to the same frame" in error_lower or \
           "frame context" in error_lower or \
           "same frame" in error_lower:

            # Check for login corruption
            if cls._has_login_corruption(test_code):
                return {
                    "root_cause": "login_corruption",
                    "confidence": 0.95,
                    "pattern": cls.FRAME_ERROR_PATTERNS["login_corruption"],
                    "evidence": "Found login() setup in code"
                }

            # Check for selector mixing
            if cls._has_selector_mixing(test_code):
                return {
                    "root_cause": "selector_mixing",
                    "confidence": 0.85,
                    "pattern": cls.FRAME_ERROR_PATTERNS["selector_mixing"],
                    "evidence": "Found mixed selector types in .or() chains"
                }

            # Default to timing/wait issues
            return {
                "root_cause": "timing_race",
                "confidence": 0.60,
                "pattern": cls.FRAME_ERROR_PATTERNS["timing_race"],
                "evidence": "Generic frame error - likely timing issue"
            }

        # ═══ PRIORITY 3: P1-9 new specific patterns ═══

        # Cookie / consent banner intercept
        if ("intercept" in error_lower and ("cookie" in error_lower or "consent" in error_lower
                                            or "accept" in error_lower)):
            return {
                "root_cause": "cookie_banner_intercept",
                "confidence": 0.90,
                "pattern": cls.FRAME_ERROR_PATTERNS["cookie_banner_intercept"],
                "evidence": "Pointer-events intercepted by consent/cookie banner",
            }

        # Modal / overlay intercept (general)
        if "intercept" in error_lower and ("modal" in error_lower or "overlay" in error_lower
                                           or "dialog" in error_lower or "backdrop" in error_lower):
            return {
                "root_cause": "modal_overlay_intercept",
                "confidence": 0.85,
                "pattern": cls.FRAME_ERROR_PATTERNS["modal_overlay_intercept"],
                "evidence": "Pointer-events intercepted by modal/overlay",
            }

        # Assertion mismatch (test ran, expectation failed)
        if any(s in error_lower for s in ("expected:", "received:", "tohavetext",
                                          "tocontaintext", "expected substring")):
            return {
                "root_cause": "assertion_mismatch",
                "confidence": 0.90,
                "pattern": cls.FRAME_ERROR_PATTERNS["assertion_mismatch"],
                "evidence": "Expect() assertion's expected value didn't match the page",
            }

        # Iframe required
        if "iframe" in error_lower or "frame-locator" in error_lower or "cross-origin" in error_lower:
            return {
                "root_cause": "iframe_traversal",
                "confidence": 0.85,
                "pattern": cls.FRAME_ERROR_PATTERNS["iframe_traversal"],
                "evidence": "Target element lives inside an iframe",
            }

        # Stale locator
        if "detached" in error_lower or "context was destroyed" in error_lower \
                or "not attached" in error_lower:
            return {
                "root_cause": "stale_locator",
                "confidence": 0.90,
                "pattern": cls.FRAME_ERROR_PATTERNS["stale_locator"],
                "evidence": "Element re-rendered between locate and act",
            }

        # Navigation timeout (page didn't load)
        if "page.goto" in error_lower and "timeout" in error_lower:
            return {
                "root_cause": "timing_race",  # reusing the now-narrowed "navigation" bucket
                "confidence": 0.90,
                "pattern": cls.FRAME_ERROR_PATTERNS["timing_race"],
                "evidence": "page.goto() timed out — page didn't load in time",
            }

        # ═══ PRIORITY 4: Generic timeout errors (broadest fallback) ═══
        if "timeout" in error_lower or "waiting for" in error_lower:
            return {
                "root_cause": "selector_timeout",
                "confidence": 0.70,
                "pattern": cls.FRAME_ERROR_PATTERNS.get("selector_timeout", {}),
                "evidence": "Generic timeout detected - likely selector timeout"
            }

        return {"root_cause": "UNKNOWN", "confidence": 0.0}

    @staticmethod
    def _has_login_corruption(code: str) -> bool:
        """Check if code has login setup that corrupts frame"""
        if not code:
            return False

        has_login = "login(" in code or "context().addCookies" in code
        has_goto_after = "page.goto" in code

        # Check if login appears before goto in the code
        if has_login:
            login_pos = code.find("login(") if "login(" in code else code.find("context().addCookies")
            goto_pos = code.find("page.goto")
            if login_pos >= 0 and goto_pos >= 0 and login_pos < goto_pos:
                return True

        return False

    @staticmethod
    def _has_selector_mixing(code: str) -> bool:
        """Check if code mixes different selector types"""
        if not code:
            return False

        # Look for .or() chains with mixed types
        or_pattern = r"\.or\s*\(\s*\(\)\s*=>\s*"
        or_matches = re.findall(or_pattern, code)

        if or_matches:
            # Check if it's mixing getByRole with locator
            if "getByRole" in code and "page.locator" in code:
                # Look for them in the same statement or block
                lines = code.split('\n')
                for i, line in enumerate(lines):
                    if ".or(" in line:
                        # Check context around this line
                        context = '\n'.join(lines[max(0, i-3):min(len(lines), i+4)])
                        if "getByRole" in context and "page.locator" in context:
                            return True

        return False


def generate_targeted_healing_prompt(
    error_msg: str,
    test_code: str,
    diagnosis: Dict,
    learning_context: str = ""
) -> Tuple[str, str]:
    """Generate targeted healing prompts based on diagnosis"""

    root_cause = diagnosis.get("root_cause", "UNKNOWN")
    pattern = diagnosis.get("pattern", {})
    confidence = diagnosis.get("confidence", 0.0)
    selector = diagnosis.get("selector")

    base_system = """You are a Playwright auto-healing expert specializing in timeout and element visibility issues.
Your task: Fix the test code to resolve the specific root cause identified.

For every fix, add an inline comment starting with [AI-HEAL] explaining what changed and why.

CRITICAL RULES:
1. Maintain all existing test logic and assertions
2. Only fix the identified root cause
3. Do NOT introduce new selector patterns that might fail
4. Use proven healing patterns only
5. Always prioritize stability over speed"""

    if root_cause == "selector_timeout":
        system = base_system + """

ROOT CAUSE: Selector element timeout - element not visible within default 15000ms
STRATEGY: Increase timeouts, add wait strategies, and implement fallback selectors

FIX RULES - MANDATORY:
1. ✅ INCREASE TIMEOUT: Change waitForSelector timeout from 15000ms to 30000-45000ms
2. ✅ ADD NETWORK WAIT: After page.goto(), call await page.waitForLoadState('networkidle')
3. ✅ USE FALLBACK SELECTORS: Chain multiple selectors with .or() for robustness
4. ✅ CHECK VISIBILITY: Use .waitFor({state: 'visible'}) before interaction
5. ✅ SCROLL INTO VIEW: Use .scrollIntoViewIfNeeded() before fill/click actions

IMPLEMENTATION EXAMPLES:

For simple selectors with fallbacks:
  const element = page.locator('selector1')
    .or(page.locator('selector2'))
    .or(page.locator('selector3'));
  await element.waitFor({timeout: 30000, state: 'visible'});
  await element.fill('value');

For Google search box specifically (COMMON PATTERN):
  const searchInput = page.locator('input[name="q"]')
    .or(page.locator('input[aria-label="Search"]'))
    .or(page.locator('.gLFyf'));
  await searchInput.waitFor({timeout: 45000, state: 'visible'});
  await searchInput.scrollIntoViewIfNeeded();
  await searchInput.fill('search term');

For elements on dynamically-loaded pages:
  await page.waitForLoadState('networkidle');
  await page.waitForFunction(() =>
    document.querySelector('target-selector') !== null
  , {timeout: 30000});
  const elem = page.locator('target-selector');
  await elem.fill('value');

ADVANCED - If element is still timing out:
1. Check for shadow DOM: page.locator('pierce=selector')
2. Add explicit wait: await page.waitForTimeout(2000)
3. Use keyboard input: await elem.focus(); await page.keyboard.type('text')
4. Chain waits: await page.waitForLoadState('load'); await page.waitForLoadState('networkidle')"""

    elif root_cause == "login_corruption":
        system = base_system + """

ROOT CAUSE: Page context corrupted by login() setup before navigation.
STRATEGY: Remove the login setup that happens before page.goto()

FIX RULES:
1. Find and REMOVE any login() function definition and calls
2. Remove page.context().addCookies() that happens BEFORE page.goto()
3. Ensure page.goto() is called FIRST to establish the main frame
4. If authentication is needed, it must come AFTER page.goto()
5. The test should start with: await page.goto(url);"""

    elif root_cause == "selector_mixing":
        system = base_system + """

ROOT CAUSE: Mixed selector types (getByRole + locator) in .or() chains
STRATEGY: Normalize all selectors to use only page.locator() consistently

FIX RULES:
1. Replace all getByRole(), getByText(), getByLabel() with page.locator()
2. For .or() chains, use: locator().or(() => locator()).first()
3. Use .first() to disambiguate, not .or() for type switching
4. Keep all locators in the same frame context"""

    elif root_cause == "timing_race":
        # Now means "navigation timeout / page didn't load" after P1-10 narrowing.
        system = base_system + "\n\n" + pattern.get("fix_prompt", "")

    elif root_cause == "cookie_banner_intercept":
        system = base_system + "\n\n" + pattern.get("fix_prompt", "")

    elif root_cause == "modal_overlay_intercept":
        system = base_system + "\n\n" + pattern.get("fix_prompt", "")

    elif root_cause == "assertion_mismatch":
        system = base_system + "\n\n" + pattern.get("fix_prompt", "")

    elif root_cause == "iframe_traversal":
        system = base_system + "\n\n" + pattern.get("fix_prompt", "")

    elif root_cause == "stale_locator":
        system = base_system + "\n\n" + pattern.get("fix_prompt", "")

    else:
        system = base_system

    user_prompt = f"""
Error: {error_msg}

Root cause identified: {root_cause} (confidence: {confidence:.0%})
Reason: {diagnosis.get('evidence', 'Analysis complete')}

Original code:
```typescript
{test_code}
```
{learning_context}

Generate the FIXED code with [AI-HEAL] comments explaining each change."""

    return system, user_prompt


def analyze_healing_history(history: List[Dict]) -> Dict:
    """Analyze healing history to detect patterns"""

    if not history:
        return {"patterns": [], "recommendations": []}

    error_counts = {}
    root_cause_counts = {}

    for attempt in history:
        error = attempt.get("error", "UNKNOWN")
        if error:
            error_counts[error] = error_counts.get(error, 0) + 1

        root_cause = attempt.get("rootCause", "UNKNOWN")
        if root_cause:
            root_cause_counts[root_cause] = root_cause_counts.get(root_cause, 0) + 1

    recommendations = []

    # If same error persists
    max_error = max(error_counts.items(), key=lambda x: x[1]) if error_counts else None
    if max_error and max_error[1] >= 2:
        recommendations.append(f"⚠️ Same error repeated {max_error[1]} times: {max_error[0]}")

    # If root cause keeps being the same but fix doesn't work
    if len(history) >= 3:
        recent = [h.get("rootCause") for h in history[-3:]]
        if len(set(recent)) == 1 and recent[0] != "UNKNOWN":
            recommendations.append(f"⚠️ Repeated attempts with same root cause ({recent[0]}) - consider escalating")

    return {
        "error_distribution": error_counts,
        "root_cause_distribution": root_cause_counts,
        "recommendations": recommendations,
        "needs_manual_review": len(history) >= 4 and any("repeated" in r.lower() for r in recommendations)
    }

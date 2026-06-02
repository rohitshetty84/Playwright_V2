"""
studio/services/assertions.py — P1-5: detect weak assertions before promote.

A passing test that asserts on the wrong thing is worse than a failing one,
because it gives confidence where there should be none. This module scans
test code and surfaces patterns that historically correlate with weak coverage.

Used by the synthesis pipeline's Phase 3 result to warn the user before they
promote a golden.

The output is intentionally a *warning*, never a *block* — false positives are
inevitable and forcing a re-run would frustrate users. The UI should display
the warnings prominently and let the user override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import List


@dataclass
class AssertionWarning:
    severity: str        # "high" | "medium" | "low"
    line: int            # 1-indexed line number in the test code
    rule: str            # short identifier, e.g. "weak.toBeVisible.firstHeading"
    message: str         # human-readable explanation
    snippet: str         # the offending line, trimmed


@dataclass
class AssertionReport:
    score: int                                 # 0-100; 100 = no concerns
    warnings: List[AssertionWarning] = field(default_factory=list)
    assertion_count: int = 0                   # total `expect(...)` calls found
    weak_count: int = 0                        # count of warnings (any severity)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "assertionCount": self.assertion_count,
            "weakCount": self.weak_count,
            "warnings": [asdict(w) for w in self.warnings],
        }


# ── Rule definitions ──────────────────────────────────────────────────────────
# Each rule is: (severity, regex, rule_id, message).
# Regexes are deliberately conservative — false positives are worse than misses
# here, because they erode user trust in the warnings.

_RULES = [
    # toBeVisible() on an unnamed locator — almost always a meaningless check.
    ("high",
     re.compile(r"expect\(\s*page\.getByRole\(['\"]\w+['\"]\)\s*\.first\(\)\s*\)\s*\.toBeVisible"),
     "weak.toBeVisible.unnamedFirst",
     "toBeVisible() on getByRole(...).first() with no accessible name — "
     "this passes whenever ANY element of that role exists on the page. "
     "Add a `name` to disambiguate."),

    ("high",
     re.compile(r"expect\(\s*page\.locator\(['\"][^'\"]+['\"]\)\s*\.first\(\)\s*\)\s*\.toBeVisible"),
     "weak.toBeVisible.firstLocator",
     "toBeVisible() on locator(...).first() — `.first()` resolves to whatever "
     "happens to be first in DOM order, often not the element you mean. "
     "Tighten the selector instead of using .first()."),

    # .or() chains — discourage in goldens (see P1-6 in review).
    ("medium",
     re.compile(r"\.or\(\s*\(\)\s*=>\s*page\."),
     "antipattern.orChain",
     ".or() fallback chain detected — these mask selector drift "
     "(the test keeps passing while pointing at the wrong element). "
     "Prefer one specific selector; if ambiguous, the page is missing an accessible name."),

    # Hardcoded CSS class selectors (e.g. .gLFyf) — high churn, ephemeral.
    ("medium",
     re.compile(r"page\.locator\(['\"]\.[A-Za-z][A-Za-z0-9_-]{3,}['\"]\)"),
     "antipattern.cssClassSelector",
     "Hardcoded CSS class selector — class names are build-output noise and "
     "change every deploy. Use role + accessible name instead."),

    # waitForLoadState('networkidle') — Playwright explicitly discourages this.
    ("low",
     re.compile(r"waitForLoadState\(\s*['\"]networkidle['\"]\s*\)"),
     "antipattern.networkidle",
     "waitForLoadState('networkidle') is documented as flaky on modern apps "
     "that poll continuously. Wait for a specific element to be visible instead."),

    # Bare URL match instead of asserting page content.
    ("medium",
     re.compile(r"toHaveURL\(\s*['\"](?:https?:)?//"),
     "weak.toHaveURL",
     "Asserting on the URL alone tells you navigation happened, not that the "
     "page loaded correctly. Pair with a content assertion (toHaveText, toBeVisible)."),

    # toBeTruthy() / toBeDefined() on a locator — almost meaningless.
    ("high",
     re.compile(r"expect\(\s*page\.[a-zA-Z]+\([^)]*\)\s*\)\s*\.(toBeTruthy|toBeDefined)\("),
     "weak.toBeTruthy",
     "expect(locator).toBeTruthy() passes for any non-null value, including "
     "locators that resolve to zero elements. Use toBeVisible / toHaveCount instead."),

    # console.log used as an "assertion" — not an assertion at all.
    ("low",
     re.compile(r"console\.log\(\s*['\"]\[step\][^'\"]*['\"]"),
     "info.consoleLogStep",
     "console.log() doesn't fail the test — it just emits output. "
     "Make sure each step has a real expect() following it."),
]


_EXPECT_RE = re.compile(r"\bexpect\s*\(")


def evaluate(code: str) -> AssertionReport:
    """Return an AssertionReport for the given TypeScript test code."""
    if not code or not code.strip():
        return AssertionReport(score=0, warnings=[], assertion_count=0, weak_count=0)

    warnings: List[AssertionWarning] = []
    lines = code.split("\n")

    for idx, line in enumerate(lines, start=1):
        for severity, pattern, rule_id, message in _RULES:
            if pattern.search(line):
                warnings.append(AssertionWarning(
                    severity=severity,
                    line=idx,
                    rule=rule_id,
                    message=message,
                    snippet=line.strip()[:200],
                ))

    assertion_count = len(_EXPECT_RE.findall(code))

    # Score: start at 100, dock points by severity.
    # A test with no expect() calls at all is already in trouble — cap score at 40.
    score = 100
    if assertion_count == 0:
        score = min(score, 40)
        warnings.insert(0, AssertionWarning(
            severity="high",
            line=0,
            rule="weak.noAssertions",
            message="No expect() assertions found at all. This test cannot fail.",
            snippet="",
        ))

    for w in warnings:
        score -= {"high": 20, "medium": 8, "low": 3}.get(w.severity, 0)
    score = max(0, min(100, score))

    return AssertionReport(
        score=score,
        warnings=warnings,
        assertion_count=assertion_count,
        weak_count=len(warnings),
    )

"""
studio/services/vision_policy.py — P2-3: gate which URLs the vision pipeline
is allowed to screenshot and send to Azure OpenAI.

Default behaviour: empty `VISION_ALLOWED_HOSTS` env var = allow all (matches
the pre-policy behaviour, backward compatible).

To restrict, set a comma-separated list of glob patterns:

    VISION_ALLOWED_HOSTS=wikipedia.org,*.github.com,github.io,mdn.io

To turn vision OFF entirely (text-only synthesis everywhere):

    VISION_ALLOWED_HOSTS=none

Matching is done against the hostname only (port stripped), case-insensitive,
using fnmatch — so `*.example.com` matches `app.example.com` but not
`example.com` itself. List both if you want both.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("playwright_ai_studio")


@dataclass
class VisionDecision:
    allowed: bool
    host: Optional[str]
    reason: str          # human-readable so we can log it
    matched_pattern: Optional[str] = None


def _parse_patterns(raw: str) -> Optional[list[str]]:
    """Return None for allow-all, [] for block-all, or a list of patterns."""
    if not raw or not raw.strip():
        return None                # empty → allow-all
    if raw.strip().lower() in ("none", "off", "disabled", "[]"):
        return []                  # explicit block-all
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def decide(url: str) -> VisionDecision:
    """Decide whether vision is permitted for this URL."""
    raw = os.getenv("VISION_ALLOWED_HOSTS", "")
    patterns = _parse_patterns(raw)

    # Parse the URL — if we can't, fail closed (block).
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except Exception:
        host = ""

    if not host:
        return VisionDecision(
            allowed=False, host=None,
            reason="URL has no parseable hostname — blocking vision call",
        )

    # Empty allowlist = allow all (backward compatible)
    if patterns is None:
        return VisionDecision(
            allowed=True, host=host,
            reason="VISION_ALLOWED_HOSTS unset — allow-all (default)",
        )

    # Explicit block-all
    if not patterns:
        return VisionDecision(
            allowed=False, host=host,
            reason="VISION_ALLOWED_HOSTS=none — vision disabled",
        )

    # Check each glob pattern against the hostname
    for pat in patterns:
        if fnmatch(host, pat):
            return VisionDecision(
                allowed=True, host=host, matched_pattern=pat,
                reason=f"host '{host}' matched pattern '{pat}'",
            )

    return VisionDecision(
        allowed=False, host=host,
        reason=f"host '{host}' not in VISION_ALLOWED_HOSTS — falling back to text-only",
    )


def log_decision(decision: VisionDecision, *, context: str = "vision") -> None:
    """Standard log line so every call site emits consistent telemetry."""
    if decision.allowed:
        logger.info(f"[{context}] ✅ vision allowed for '{decision.host}' ({decision.reason})")
    else:
        logger.warning(f"[{context}] ⚠️  vision BLOCKED for '{decision.host}' — {decision.reason}")

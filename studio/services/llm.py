"""
studio/services/llm.py — Azure OpenAI client wrappers used by route handlers.

Consolidates:
- the bare ask_llm helper (text-only chat completion)
- the vision_heal helper (image + text content block)
- a small retry-on-transient-error wrapper

P2-4: errors here are converted to HTTP-safe messages by the call site.
Callers log the full exception; clients see only "LLM request failed".
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from openai import AzureOpenAI

logger = logging.getLogger("playwright_ai_studio")


class LLMService:
    def __init__(self, client: AzureOpenAI, deployment: str,
                 default_temperature: float = 0.2,
                 default_max_tokens: int = 1500,
                 vision_max_tokens: int = 2000):
        self.client = client
        self.deployment = deployment
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        self.vision_max_tokens = vision_max_tokens

    # ── text-only chat completion ─────────────────────────────────────────────
    def ask(self, system: str, user: str, max_tokens: Optional[int] = None,
            temperature: Optional[float] = None, retries: int = 2) -> str:
        """Plain text-in, text-out. Retries once on 429/5xx."""
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.deployment,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=max_tokens or self.default_max_tokens,
                    temperature=temperature if temperature is not None else self.default_temperature,
                )
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < retries and _is_transient(e):
                    backoff = 1.5 ** attempt
                    logger.warning(f"[llm.ask] transient error (attempt {attempt+1}/{retries+1}) — backing off {backoff:.1f}s: {e}")
                    time.sleep(backoff)
                    continue
                break
        # Re-raise so the caller can decide how to surface it to the HTTP client.
        raise last_exc if last_exc else RuntimeError("LLM call failed without exception")

    # ── vision-assisted heal / synthesis ──────────────────────────────────────
    def vision_heal(self, *, system_prompt: str, user_prompt: str,
                    image_b64: Optional[str] = None,
                    max_tokens: Optional[int] = None,
                    temperature: Optional[float] = None,
                    retries: int = 2) -> str:
        """
        Image + text in, text out.

        If `image_b64` is None, falls through to a text-only call with the
        same system/user prompts — so route handlers can use one code path
        regardless of whether vision was available.
        """
        if image_b64 is None:
            return self.ask(
                system=system_prompt,
                user=user_prompt,
                max_tokens=max_tokens or self.vision_max_tokens,
                temperature=temperature,
                retries=retries,
            )

        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.deployment,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                            {"type": "text",
                             "text": f"{system_prompt}\n\n{user_prompt}"},
                        ],
                    }],
                    max_tokens=max_tokens or self.vision_max_tokens,
                    temperature=temperature if temperature is not None else self.default_temperature,
                )
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < retries and _is_transient(e):
                    backoff = 1.5 ** attempt
                    logger.warning(f"[llm.vision_heal] transient error (attempt {attempt+1}/{retries+1}) — backing off {backoff:.1f}s: {e}")
                    time.sleep(backoff)
                    continue
                break
        raise last_exc if last_exc else RuntimeError("LLM vision call failed without exception")


def _is_transient(exc: Exception) -> bool:
    """Conservative retry classifier — 429 / 5xx / connection errors."""
    msg = str(exc).lower()
    return any(s in msg for s in (
        "429", "rate limit", "timeout", "connection", "temporarily",
        "503", "502", "504", "internal server error",
    ))

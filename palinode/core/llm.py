"""Typed LLM seam — Protocol + adapters for text generation.

Mirrors the EmbedderProtocol pattern from ``embedder.py``: callers depend on
the ``LLMProvider`` protocol; concrete adapters (``OllamaProvider`` for
production, ``FakeProvider`` for tests) satisfy it.

The tools-over-pipeline approach keeps model swaps as adapter changes instead
of codebase-wide rewrites across every module that touches Ollama.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

import httpx

from palinode.core.config import config

__all__ = [
    "LLMProvider",
    "OllamaProvider",
    "FakeProvider",
    "LLMError",
    "LLMTimeout",
    "LLMUnreachable",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base error for LLM operations."""


class LLMTimeout(LLMError):
    """The LLM endpoint did not respond in time."""


class LLMUnreachable(LLMError):
    """The LLM endpoint could not be contacted or returned a bad response."""


# ---------------------------------------------------------------------------
# Protocol — the named seam for dependency injection / test fakes
# ---------------------------------------------------------------------------


class LLMProvider(Protocol):
    """Generate text from a prompt.

    Implementations must be safe to pass user-supplied content as the prompt
    body — defang at the *caller* layer if needed (e.g., wrap in
    ``<user_content>`` tags).  Raises subclasses of ``LLMError`` on failure.
    """

    def generate(
        self,
        prompt: str,
        *,
        max_chars: int | None = None,
        timeout: float = 30.0,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Real adapter — Ollama /api/generate
# ---------------------------------------------------------------------------


class OllamaProvider:
    """Calls Ollama's ``/api/generate`` endpoint via httpx.

    Defaults to ``config.auto_summary.ollama_url`` /
    ``config.auto_summary.model`` so existing callers that construct with no
    arguments get identical behaviour to the pre-refactor inline ``httpx.post``.
    """

    def __init__(self, url: str | None = None, model: str | None = None) -> None:
        self._url = url
        self._model = model

    @property
    def url(self) -> str:
        return (
            self._url
            or config.auto_summary.ollama_url
            or config.embeddings.primary.url
        )

    @property
    def model(self) -> str:
        return self._model or config.auto_summary.model

    def generate(
        self,
        prompt: str,
        *,
        max_chars: int | None = None,
        timeout: float = 30.0,
    ) -> str:
        """Send a prompt to Ollama and return the response text.

        Raises ``LLMTimeout`` on deadline exceeded, ``LLMUnreachable`` on
        network / HTTP / parse errors.  Never swallows exceptions silently.
        """
        try:
            resp = httpx.post(
                f"{self.url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip().strip("\"'").strip()
        except httpx.TimeoutException as exc:
            raise LLMTimeout(str(exc)) from exc
        except (httpx.HTTPError, OSError, json.JSONDecodeError, ValueError) as exc:
            raise LLMUnreachable(str(exc)) from exc

        if max_chars is not None and len(raw) > max_chars:
            raw = raw[:max_chars]
        return raw


# ---------------------------------------------------------------------------
# Test adapter — deterministic, no network
# ---------------------------------------------------------------------------


class FakeProvider:
    """Returns deterministic responses keyed by prompt prefix.

    ``responses`` is a ``{prefix: reply}`` dict.  On ``generate()``, the
    *longest* matching prefix wins.  If nothing matches, ``default`` is
    returned.  Useful for unit tests that need predictable LLM output.
    """

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        default: str = "",
    ) -> None:
        self._responses = responses or {}
        self._default = default
        self.calls: list[str] = []  # audit trail for assertions

    def generate(
        self,
        prompt: str,
        *,
        max_chars: int | None = None,
        timeout: float = 30.0,
    ) -> str:
        self.calls.append(prompt)

        # Longest-prefix match
        best_match = ""
        best_reply = self._default
        for prefix, reply in self._responses.items():
            if prompt.startswith(prefix) and len(prefix) > len(best_match):
                best_match = prefix
                best_reply = reply

        if max_chars is not None and len(best_reply) > max_chars:
            best_reply = best_reply[:max_chars]
        return best_reply

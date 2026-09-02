"""Minimal OpenAI-compatible chat client (stdlib only).

Two roles, each configured independently so the answerer and the judge can be
different vendors — the methodological point:

    LME_ANSWER_BASE_URL / LME_ANSWER_MODEL / LME_ANSWER_API_KEY / LME_ANSWER_EXTRA_JSON
    LME_JUDGE_BASE_URL  / LME_JUDGE_MODEL  / LME_JUDGE_API_KEY  / LME_JUDGE_EXTRA_JSON

``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` are the fallbacks for both roles.

A base URL of ``codex://`` routes the role through the local Codex CLI
(``codex exec``, ChatGPT-subscription OAuth) instead of HTTP — for a frontier
*answerer* row without an API key. Not for the judge: comparability needs the
upstream ``gpt-4o-2024-08-06`` judge, which only the metered API serves.
``*_EXTRA_JSON`` is a JSON object merged into every request body — e.g.
``{"reasoning_effort": "none"}`` so a thinking model (Gemini 2.5) answers the
judge prompt inside upstream's ``max_tokens=10`` instead of spending it thinking.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    model: str
    api_key: str = ""
    extra: tuple[tuple[str, object], ...] = ()   # merged into the request body
    timeout_s: float = 120.0                      # LME_<ROLE>_TIMEOUT_S; 40k-token prompts on a local box need more

    @classmethod
    def from_env(cls, role: str) -> "Endpoint":
        p = f"LME_{role.upper()}_"
        base = os.environ.get(p + "BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get(p + "MODEL")
        if not model:
            raise SystemExit(f"{p}MODEL is not set")
        key = os.environ.get(p + "API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        extra = json.loads(os.environ.get(p + "EXTRA_JSON") or "{}")
        if not isinstance(extra, dict):
            raise SystemExit(f"{p}EXTRA_JSON must be a JSON object")
        timeout_s = float(os.environ.get(p + "TIMEOUT_S") or 120.0)
        base = base if base.startswith(CODEX_SCHEME) else base.rstrip("/")
        return cls(base, model, key, tuple(sorted(extra.items())), timeout_s)

    def describe(self) -> str:
        s = f"{self.model} @ {self.base_url} timeout={self.timeout_s:.0f}s"
        return f"{s} {dict(self.extra)}" if self.extra else s


@dataclass
class Completion:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_s: float


CODEX_SCHEME = "codex://"
# 429 backoff — total ≈ 2.5 min, well under the supervisor's stall threshold. Row D2
# (2026-08-29) lost 89/500 answers to OpenAI rate limits with a single 2 s retry.
RATE_LIMIT_BACKOFF_S = (20.0, 40.0, 80.0)
_CODEX_TOKENS_RE = re.compile(r"tokens used\s*\n\s*([\d,]+)")


def codex_exec(ep: Endpoint, messages: list[dict[str, str]], *, timeout: float,
               run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Completion:
    """Answer via ``codex exec`` — read-only sandbox, ephemeral, no user config or
    rules, empty cwd, prompt on stdin, reply via ``--output-last-message``.
    ``tokens used`` from stdout is the *total* Codex reports (its own system
    prompt included, ~9k on an empty prompt), recorded as prompt_tokens."""
    prompt = "\n\n".join(m["content"] for m in messages)
    with tempfile.TemporaryDirectory(prefix="lme-codex-") as cwd:
        out_path = os.path.join(cwd, "last.txt")
        cmd = ["codex", "exec", "-m", ep.model, "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
               "--ignore-user-config", "--ignore-rules", "--color", "never", "-o", out_path, "-"]
        t0 = time.perf_counter()
        proc = run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        latency = time.perf_counter() - t0
        if proc.returncode != 0:
            raise RuntimeError(f"codex exec failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
        text = open(out_path, encoding="utf-8").read().strip() if os.path.exists(out_path) else ""
    if not text:
        raise RuntimeError("codex exec returned no last message")
    m = _CODEX_TOKENS_RE.search((proc.stderr or "") + "\n" + (proc.stdout or ""))   # codex prints it on stderr
    used = int(m.group(1).replace(",", "")) if m else None
    return Completion(text=text, prompt_tokens=used, completion_tokens=None, latency_s=latency)


def chat(ep: Endpoint, messages: list[dict[str, str]], *, temperature: float = 0.0,
         max_tokens: int = 512, retries: int = 1, timeout: float | None = None) -> Completion:
    """One retry by default: worst case ≈ 2 × timeout, which must stay under the
    supervisor's stall threshold or a hung backend call gets the whole process
    killed and restarted instead of just this question recorded as an error.
    (Row A, 2026-08-27: 4 retries × 300 s outran the 12-min watchdog nine times.)"""
    if timeout is None:
        timeout = ep.timeout_s
    if ep.base_url.startswith(CODEX_SCHEME):
        return codex_exec(ep, messages, timeout=timeout)
    payload = {
        "model": ep.model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
        **dict(ep.extra),
    }
    if "max_completion_tokens" in payload:
        # OpenAI reasoning-line models (gpt-5.x) reject max_tokens outright; an
        # endpoint that declares max_completion_tokens via *_EXTRA_JSON owns the
        # budget, and temperature is likewise unsupported there.
        payload.pop("max_tokens", None)
        payload.pop("temperature", None)
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    req = urllib.request.Request(ep.base_url + "/chat/completions", data=body, headers=headers)
    delay = 2.0
    rate_limit_waits = list(RATE_LIMIT_BACKOFF_S)   # 429s get their own budget: a quota window, not a hang
    attempt = -1
    while True:
        attempt += 1
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                data = json.load(resp)
            usage = data.get("usage") or {}
            choice = (data.get("choices") or [{}])[0]
            if "message" not in choice:
                # Gemini returns a choice with only finish_reason (e.g.
                # "content_filter: PROHIBITED_CONTENT") when it refuses the
                # input. Deterministic per input — surface it as such, not as a
                # KeyError the caller mistakes for a transient outage.
                raise RuntimeError(
                    f"blocked by {ep.model}: finish_reason={choice.get('finish_reason')!r} (no message in choice; usage={usage})"
                )
            msg = choice["message"]
            text = (msg.get("content") or "").strip()
            if not text:
                # Thinking models return null content when reasoning eats max_tokens.
                # Surface it as an error, never as an empty (auto-wrong) answer.
                raise RuntimeError(
                    f"empty content from {ep.model} (finish_reason="
                    f"{data['choices'][0].get('finish_reason')}, usage={usage}) — "
                    "raise max_tokens or disable thinking via *_EXTRA_JSON"
                )
            return Completion(
                text=text,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                latency_s=time.perf_counter() - t0,
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError) as e:
            detail = str(e)
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail += " " + e.read()[:300].decode("utf-8", "replace")   # the server's reason, e.g. context length
                except Exception:  # noqa: BLE001
                    pass
                if e.code == 429:
                    if "insufficient_quota" in detail or "credit_balance_exhausted" in detail:
                        # Not a window to wait out — the account is empty. Fail fast so the
                        # row lands as an error and the run finishes instead of stalling 2.5 min per question.
                        raise RuntimeError(f"chat failed against {ep.base_url} ({ep.model}): OUT OF CREDITS — {detail}") from e
                    if not rate_limit_waits:
                        raise RuntimeError(f"chat failed against {ep.base_url} ({ep.model}): {detail}") from e
                    wait = rate_limit_waits.pop(0)
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    if retry_after and retry_after.strip().isdigit():
                        wait = max(wait, float(retry_after))
                    time.sleep(wait)
                    attempt -= 1            # rate-limit waits don't consume the transient-retry budget
                    continue
                if 400 <= e.code < 500:
                    raise RuntimeError(f"chat failed against {ep.base_url} ({ep.model}): {detail}") from e
            if attempt >= retries:
                raise RuntimeError(f"chat failed against {ep.base_url} ({ep.model}): {detail}") from e
            time.sleep(delay)
            delay *= 2

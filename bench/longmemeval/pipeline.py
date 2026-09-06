"""Row E: the production write path, per question.

Rows A–D index raw transcripts. This module simulates what an agent does at
the end of every session in production, in chronological order:

1. **extract** — an LLM plays the agent at session end and produces the
   ``session_end`` payload (summary + dated facts about the user + preferences).
   This *is* a chat-LLM call at ingest; ``run.py`` reports it as such.
2. **write** — the payload goes through Palinode's real session-end function
   (``palinode.api.routers.session.session_end_api``, called in-process): a
   dated ``daily/YYYY-MM-DD.md`` entry tagged ``project/user`` and an indexed
   ProjectSnapshot file. The clock the router stamps the note with is patched
   to the haystack session's date — the one thing production has that a
   replayed 2023 transcript does not.
3. **profile** — the extracted facts are appended to ``projects/user.md`` (the
   seeded user profile) and tagged with the real ``fact_ids`` bootstrap so the
   consolidation executor can address them. Session-end itself appends only a
   400-char one-liner to ``projects/<p>-status.md`` — an index, not a fact
   store — so the profile append is the harness's, not session-end's, and the
   status file is deliberately not seeded so consolidation targets ``user.md``.
4. **consolidate** (E1) — the real ``run_consolidation`` with an injected
   ``llm_fn`` updates ``projects/user.md``; compacted daily notes move to
   ``archive/<year>/`` (still indexed).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterator

from bench.longmemeval import llm
from bench.longmemeval.adapter import _date_of, _turn_md

PROJECT = "user"
PROFILE_REL = os.path.join("projects", f"{PROJECT}.md")
PIPELINES = ("raw", "session-end", "session-end+consolidate")

# ── Extraction ────────────────────────────────────────────────────────────────

EXTRACT_PROMPTS = {
    "v1": (
        "You are an AI assistant finishing a conversation with a user. Write the session-end "
        "memory note that a future assistant will rely on to answer questions about this user "
        "weeks or months from now.\n\n"
        "Return ONLY a JSON object with these keys:\n"
        '  "summary": 2-5 sentences covering what the user discussed, asked for, and what was '
        "decided or recommended. Keep concrete details (names, numbers, places, products, dates).\n"
        '  "facts": a list of short, specific, self-contained statements about the user — their '
        "life, possessions, relationships, work, plans, events, numbers, and anything they said "
        "changed. Each item is a string. Preserve exact values and quantities. When the user gives a "
        "relative time (\"last week\", \"next Friday\", \"two years ago\"), resolve it against the "
        "session date and state the absolute date in the fact. Include facts the assistant stated "
        "to the user that the user accepted (recommendations, computed results).\n"
        '  "preferences": a list of strings: the user\'s stated preferences, tastes, constraints, '
        "and how they like to be helped.\n\n"
        "Write nothing the transcript does not support. Empty lists are fine. No markdown fences."
    ),
    # v2: the E1-v1 subset (2026-08-30) lost multi-session and assistant questions to
    # extraction, not retrieval — a countable event dropped (a $500 workshop in a $720 total,
    # a third dinner party, purchase and arrival dates) or one item kept from a list the
    # assistant recommended. Same shape as v1; "facts" becomes an exhaustive ledger.
    "v2": (
        "You are an AI assistant finishing a conversation with a user. Write the session-end "
        "memory note that a future assistant will rely on to answer questions about this user "
        "weeks or months from now — including questions that count or total things across many "
        "conversations (\"how many X did I … this month\", \"how much did I spend on …\").\n\n"
        "Return ONLY a JSON object with these keys:\n"
        '  "summary": 2-5 sentences covering what the user discussed, asked for, and what was '
        "decided or recommended. Keep concrete details (names, numbers, places, products, dates).\n"
        '  "facts": an EXHAUSTIVE ledger, one string per item. One entry for every distinct event, '
        "purchase, appointment, trip, meal, meeting, workout, service, subscription, gift, or task "
        "the user mentions — never summarise several as \"a few\" or \"several\"; list each with its "
        "own quantity, amount, and absolute date. Also one entry for each fact about the user's life, "
        "possessions, relationships, work, plans, and numbers, and for anything they said changed. "
        "When the user gives a relative time (\"last week\", \"next Friday\", \"two years ago\"), "
        "resolve it against the session date and state the absolute date. When the assistant "
        "recommended or listed things (places, products, websites, books, options) and the user "
        "engaged with the list, record EVERY item by name in one entry, not just one of them. "
        "Preserve exact values.\n"
        '  "preferences": a list of strings: the user\'s stated preferences, tastes, constraints, '
        "and how they like to be helped.\n\n"
        "Write nothing the transcript does not support. Empty lists are fine. No markdown fences."
    ),
    # v1t: v1's content, terse output — for a local extractor where output tokens are the
    # whole cost (37 tok/s single-stream on the local extraction host vs ~2 s per call on Gemini). Facts only,
    # no prose summary (the summary is the first fact), preferences folded into facts.
    "v1t": (
        "You are an AI assistant finishing a conversation with a user. Write the session-end "
        "memory note a future assistant will rely on to answer questions about this user.\n\n"
        "Return ONLY a JSON object: {\"summary\": one sentence, \"facts\": [...], \"preferences\": [...]}.\n"
        "facts: short, specific statements about the user (life, possessions, relationships, work, "
        "plans, events, numbers, changes) and anything the assistant told them that they accepted. "
        "Preserve exact values; resolve relative times against the session date to absolute dates. "
        "Telegraphic style, no filler, at most 12 facts and 3 preferences. "
        "Write nothing the transcript does not support. No markdown fences."
    ),
}
#: v1 stays the default, deliberately — the ledger prompt's gain is
#: reader-dependent, not general. v2 beat v1 by +12 on the fully local subset row
#: (0.860 vs 0.740) and LOST under the Gemini-family reader the published rows use
#: (0.780 vs E1noarch's 0.820), at 3,803 prompt tokens per answer against 2,731.
#: For a strong reader whose v1 extractions were already good, an exhaustive ledger
#: adds more noise than signal. A default that makes the published rows worse is the
#: wrong default, and selecting one automatically per reader would make two runs
#: incomparable without reading their meta — so the choice stays explicit. Set
#: LME_EXTRACT_PROMPT_VERSION to pick, and state which prompt each row used in any
#: cross-row comparison. Numbers: docs/BENCHMARKS.md row E.
DEFAULT_EXTRACT_PROMPT_VERSION = "v1"


def extract_prompt_version() -> str:
    v = os.environ.get("LME_EXTRACT_PROMPT_VERSION", DEFAULT_EXTRACT_PROMPT_VERSION)
    if v not in EXTRACT_PROMPTS:
        raise SystemExit(f"LME_EXTRACT_PROMPT_VERSION={v!r}; known: {sorted(EXTRACT_PROMPTS)}")
    return v


def extract_messages(session_md: str, ts: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": EXTRACT_PROMPTS[extract_prompt_version()]},
        {"role": "user", "content": f"Session date: {ts}\n\nTranscript:\n\n{session_md}"},
    ]


@dataclass
class SessionEndPayload:
    session_id: str
    ts: str                      # LongMemEval timestamp, e.g. ``2023/05/20 (Sat) 02:21``
    date: str                    # YYYY-MM-DD
    summary: str
    facts: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_s: float = 0.0
    parse_ok: bool = True
    refused: bool = False        # the extraction model refused the transcript (content filter)


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def _strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                t = x.get("text") or x.get("fact") or x.get("content")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
        return out
    return []


def parse_extraction(text: str) -> tuple[dict[str, Any], bool]:
    """``(payload_dict, parse_ok)``. A model that answers in prose instead of
    JSON still yields a usable note: the whole reply becomes the summary."""
    raw = text.strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    if not raw.startswith("{"):
        i = raw.find("{")
        if i >= 0:
            raw = raw[i:]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json  # type: ignore[import-not-found]

            obj = json.loads(repair_json(raw))
        except Exception:  # noqa: BLE001 - no JSON at all: fall back to prose
            obj = None
    if not isinstance(obj, dict):
        return {"summary": text.strip(), "facts": [], "preferences": []}, False
    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = " ".join(_strings(obj.get("facts"))) or text.strip()
    return {"summary": " ".join(summary.split()), "facts": _strings(obj.get("facts")),
            "preferences": _strings(obj.get("preferences"))}, True


REFUSED_SUMMARY = ("The assistant's session-end extraction was refused by the model's content filter; "
                   "nothing was recorded for this session.")


def extract_session(ep: llm.Endpoint, sid: str, ts: str, turns: list[dict[str, Any]], *,
                    max_tokens: int = 1500) -> SessionEndPayload:
    body = "\n\n".join(_turn_md(t) for t in turns)
    try:
        comp = llm.chat(ep, extract_messages(body, ts), max_tokens=max_tokens)
    except RuntimeError as e:
        if "blocked by" not in str(e) and "content_filter" not in str(e):
            raise
        # Deterministic refusal (Gemini: finish_reason "content_filter: PROHIBITED_CONTENT"
        # on some ShareGPT sessions). Production would simply have no note for the
        # session; write the stub so the run proceeds and the loss is counted.
        return SessionEndPayload(session_id=str(sid), ts=ts, date=_date_of(ts), summary=REFUSED_SUMMARY,
                                 parse_ok=False, refused=True)
    parsed, ok = parse_extraction(comp.text)
    return SessionEndPayload(session_id=str(sid), ts=ts, date=_date_of(ts), summary=parsed["summary"],
                             facts=parsed["facts"], preferences=parsed["preferences"],
                             prompt_tokens=comp.prompt_tokens, completion_tokens=comp.completion_tokens,
                             latency_s=comp.latency_s, parse_ok=ok)


ExtractFn = Callable[[str, str, list[dict[str, Any]]], SessionEndPayload]


def extract_all(item: dict[str, Any], extract_fn: ExtractFn, *, workers: int = 8) -> list[SessionEndPayload]:
    """Extract every haystack session, *workers*-way parallel, returned in
    chronological order (the order they are then written in). One failed
    extraction raises — the caller's backoff owns the retry."""
    sessions = list(zip(item["haystack_session_ids"], item["haystack_dates"], item["haystack_sessions"], strict=True))
    if workers <= 1:
        out = [extract_fn(str(sid), ts, turns) for sid, ts, turns in sessions]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            out = list(pool.map(lambda s: extract_fn(str(s[0]), s[1], s[2]), sessions))
    # Stable: LongMemEval haystacks are already chronological; a stable sort keeps
    # same-day sessions in transcript order.
    return sorted(out, key=lambda p: p.date)


# ── Session-end write path ───────────────────────────────────────────────────

def seed_profile(palinode_dir: str) -> str:
    """Create the empty user profile consolidation will compact into.

    Plain project document, no ``update_policy: replace``: a living document
    refuses SUPERSEDE/ARCHIVE/RETRACT by design (ADR-015), and SUPERSEDE on
    knowledge updates is exactly what row E measures.
    """
    path = os.path.join(palinode_dir, PROFILE_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\nname: {PROJECT}\ntype: ProjectSnapshot\nentities:\n  - project/{PROJECT}\n---\n"
                f"# User profile\n\nWhat is known about the user, gathered at the end of each conversation. "
                f"One dated bullet per fact, one section per session.\n")
    # The previous preamble ("Facts about the user, one dated bullet each, appended at every
    # session end, one section per session.") is a deterministic bge-m3 NaN input:
    # index_file aborted the whole profile and it was FTS-only in every smoke. Verified 2026-08-30.
    return path


_clock_lock = threading.Lock()


@contextlib.contextmanager
def _clock_at(date: str) -> Iterator[None]:
    """Make the session-end router and the save capability stamp *date*.

    Both read a module-level ``_utc_now``; patching it is the seam that lets a
    2023 session land as a 2023 daily note instead of today's.
    """
    from palinode.api.routers import session as session_mod
    from palinode.core import save as save_mod

    fixed = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, tzinfo=UTC)
    with _clock_lock:
        orig_s, orig_v = session_mod._utc_now, save_mod._utc_now
        session_mod._utc_now = lambda: fixed  # type: ignore[assignment]
        save_mod._utc_now = lambda: fixed  # type: ignore[assignment]
        try:
            yield
        finally:
            session_mod._utc_now, save_mod._utc_now = orig_s, orig_v  # type: ignore[assignment]


def write_session_end(palinode_dir: str, payload: SessionEndPayload) -> dict[str, Any]:
    """Run the real session-end function for one extracted session, then append
    its facts to the profile. Returns the router's response plus ``facts_written``."""
    from palinode.api.routers.session import SessionEndRequest, session_end_api

    req = SessionEndRequest(summary=payload.summary, decisions=payload.preferences or None,
                            blockers=None, project=PROJECT, source="lme-extract", push=False,
                            harness="longmemeval", session_id=payload.session_id)
    with _clock_at(payload.date):
        resp = session_end_api(req)
    n = _append_facts(palinode_dir, payload)
    resp["facts_written"] = n
    return resp


def _append_facts(palinode_dir: str, payload: SessionEndPayload) -> int:
    path = os.path.join(palinode_dir, PROFILE_REL)
    if not os.path.exists(path):
        seed_profile(palinode_dir)
    lines = [f"- [{payload.date}] {' '.join(f.split())}" for f in payload.facts]
    if not lines:
        return 0
    # One ``##`` section per session: the indexer chunks on ``##``, so the profile
    # is retrievable session by session (a single 300-bullet section embeds to
    # mush and BM25 length-normalises it away — smoke 2026-08-30: 0/3 profile hits),
    # and a profile hit stays traceable to its session for evidence recall.
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## [{payload.date}] session {payload.session_id}\n" + "\n".join(lines) + "\n")
    return len(lines)


def tag_profile_facts(palinode_dir: str) -> int:
    """``<!-- fact:id -->`` every profile bullet — the real bootstrap, so the
    executor can address facts by the same ids production would mint."""
    from palinode.consolidation.fact_ids import add_fact_ids_to_file

    path = os.path.join(palinode_dir, PROFILE_REL)
    return add_fact_ids_to_file(path) if os.path.exists(path) else 0


@dataclass
class WriteStats:
    sessions: int = 0
    facts: int = 0
    preferences: int = 0
    parse_failures: int = 0
    refused: int = 0             # sessions the extraction model refused (content filter) — stub note only
    deduplicated: int = 0        # individual indexed file suppressed by session-end's semantic dedup
    prompt_tokens: int = 0
    completion_tokens: int = 0
    extract_wall_s: float = 0.0
    write_wall_s: float = 0.0


def ingest_session_end(palinode_dir: str, item: dict[str, Any], extract_fn: ExtractFn, *,
                       workers: int = 8) -> WriteStats:
    """Extract every session (parallel), then replay session-end in order."""
    st = WriteStats()
    t0 = time.perf_counter()
    payloads = extract_all(item, extract_fn, workers=workers)
    st.extract_wall_s = time.perf_counter() - t0
    seed_profile(palinode_dir)
    t1 = time.perf_counter()
    for p in payloads:
        resp = write_session_end(palinode_dir, p)
        st.sessions += 1
        st.facts += resp["facts_written"]
        st.preferences += len(p.preferences)
        st.parse_failures += 0 if p.parse_ok or p.refused else 1
        st.refused += 1 if p.refused else 0
        st.deduplicated += 1 if resp.get("deduplicated_against") else 0
        st.prompt_tokens += p.prompt_tokens or 0
        st.completion_tokens += p.completion_tokens or 0
    tag_profile_facts(palinode_dir)
    st.write_wall_s = time.perf_counter() - t1
    return st


# ── Consolidation ────────────────────────────────────────────────────────────

def _repo_prompts_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "specs", "prompts")


def install_prompts(palinode_dir: str) -> None:
    """``run_consolidation`` reads ``<store>/specs/prompts/compaction.md``."""
    dst = os.path.join(palinode_dir, "specs", "prompts")
    os.makedirs(dst, exist_ok=True)
    shutil.copy(os.path.join(_repo_prompts_dir(), "compaction.md"), os.path.join(dst, "compaction.md"))


def consolidation_llm_fn(ep: llm.Endpoint, *, max_tokens: int | None = None, usage: dict[str, int] | None = None):
    """The runner's propose seam, backed by an ``LME_CONSOLIDATE_*`` endpoint.
    Ops JSON for a 40-session profile does not fit ``config.consolidation.llm_max_tokens``
    (2000); the seam sets its own budget — ``LME_CONSOLIDATE_MAX_TOKENS`` (default 16000).
    Local vLLM validates prompt+max_tokens against max_model_len, so a 24k-ctx server
    needs ~12000 here."""
    if max_tokens is None:
        max_tokens = int(os.environ.get("LME_CONSOLIDATE_MAX_TOKENS", "16000"))
    def fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        comp = llm.chat(ep, [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": user_prompt}], max_tokens=max_tokens)
        if usage is not None:
            usage["calls"] = usage.get("calls", 0) + 1
            usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + (comp.prompt_tokens or 0)
            usage["completion_tokens"] = usage.get("completion_tokens", 0) + (comp.completion_tokens or 0)
        return comp.text, ep.model
    return fn


def allowed_ops_from_env() -> list[str] | None:
    """``LME_CONSOLIDATE_ALLOWED_OPS=KEEP,UPDATE,MERGE,SUPERSEDE`` narrows the ops the weekly
    pass may apply — the production knob ``config.consolidation.allowed_ops``. ``None`` keeps
    the config default (all six). The E1 subset (2026-08-30) lost seven questions to ARCHIVE
    alone: the compaction prompt archives "stale" facts, and a whole 2023 haystack compacted in
    one pass looks stale."""
    raw = os.environ.get("LME_CONSOLIDATE_ALLOWED_OPS")
    if not raw:
        return None
    ops = [o.strip().upper() for o in raw.split(",") if o.strip()]
    known = {"KEEP", "UPDATE", "MERGE", "SUPERSEDE", "ARCHIVE", "RETRACT", "PROPOSE_CONTRADICTS"}
    bad = sorted(set(ops) - known)
    if bad:
        raise SystemExit(f"LME_CONSOLIDATE_ALLOWED_OPS: unknown op(s) {bad}; known: {sorted(known)}")
    return ops


def consolidate(palinode_dir: str, item: dict[str, Any], llm_fn, *, allowed_ops: list[str] | None = None) -> dict[str, Any]:
    """The real weekly pass over this question's daily notes → ``projects/user.md``.

    Lookback reaches back to the earliest haystack date (2023 is a long time ago
    for a 7-day default). ``sources=("daily",)`` — the store has no insights.
    Compacted notes are archived by the runner; the moved paths' stale chunks are
    GC'd here so the re-index that follows sees one copy of each.
    """
    from palinode.consolidation.runner import run_consolidation
    from palinode.core import store
    from palinode.core.config import config

    install_prompts(palinode_dir)
    earliest = min(_date_of(ts) for ts in item["haystack_dates"])
    days = (datetime.now(UTC).date() - datetime.strptime(earliest, "%Y-%m-%d").date()).days + 2
    # The runner groups notes by ``project/<id>`` refs in the note body, else by
    # ``config.consolidation.keyword_map`` — the production knob for daily notes
    # that never spell out the ref. Every session-end entry carries its heading.
    orig_map = config.consolidation.keyword_map
    orig_ops = config.consolidation.allowed_ops
    config.consolidation.keyword_map = {f"project/{PROJECT}": ["Session End"]}
    if allowed_ops is not None:
        config.consolidation.allowed_ops = list(allowed_ops)
    t0 = time.perf_counter()
    try:
        result = run_consolidation(lookback_days=days, llm_fn=llm_fn, sources=("daily",))
    finally:
        config.consolidation.keyword_map = orig_map
        config.consolidation.allowed_ops = orig_ops
    result["allowed_ops"] = list(allowed_ops) if allowed_ops is not None else list(orig_ops)
    result["wall_s"] = round(time.perf_counter() - t0, 3)
    from bench import harness

    valid = {os.path.abspath(p) for p in harness._glob_md(palinode_dir)}
    removed_paths, removed_chunks = store.gc_orphaned_chunks(valid)
    result["gc_paths"] = removed_paths
    result["gc_chunks"] = removed_chunks
    return result


OPS_KEYS = ("kept", "updated", "merged", "superseded", "archived", "retracted", "unmatched", "protected_rejected")


def ops_histogram(result: dict[str, Any] | None) -> dict[str, int]:
    return {k: int(result.get(k, 0)) for k in OPS_KEYS} if result else {}

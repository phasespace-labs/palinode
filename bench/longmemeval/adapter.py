"""Palinode adapter: haystack sessions → daily notes → index → recall.

Each LongMemEval question ships its own haystack, so each question gets a
*fresh* store. Sessions are written as dated ``daily/YYYY-MM-DD.md`` notes —
the same shape a real ``session_end`` produces — so the optional consolidation
pass (``--consolidate``) sees exactly what production would see.

Nothing is mocked: the real ``index_file`` pipeline, the real SQLite-vec +
FTS5 index, the real ``search_hybrid`` / ``search_fts``.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from typing import Any

from bench import harness


def _date_of(ts: str) -> str:
    """LongMemEval timestamps look like ``2023/05/20 (Sat) 02:21``."""
    return ts.split()[0].replace("/", "-")


def _turn_md(turn: dict[str, Any]) -> str:
    who = "User" if turn.get("role") == "user" else "Assistant"
    return f"**{who}:** {turn.get('content', '').strip()}"


_UNSAFE_RE = re.compile(r"[^\w.-]+")
_SID_FROM_PATH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)\.md$")


def session_rel_path(date: str, sid: str) -> str:
    return os.path.join("daily", f"{date}-{_UNSAFE_RE.sub('_', sid)}.md")


def session_id_of(file_path: str) -> str | None:
    m = _SID_FROM_PATH_RE.match(os.path.basename(file_path))
    return m.group(1) if m else None


def write_sessions(palinode_dir: str, item: dict[str, Any]) -> dict[str, str]:
    """Write every haystack session as its own dated daily note.

    One file per session, no ``##`` headings inside, so the parser yields
    exactly one chunk per session at any length — session-level retrieval,
    the granularity LongMemEval's own baselines use. The ``YYYY-MM-DD-`` prefix
    is what the consolidation runner keys daily notes on.

    Returns ``{session_id: relative_file_path}``.
    """
    where: dict[str, str] = {}
    os.makedirs(os.path.join(palinode_dir, "daily"), exist_ok=True)
    for sid, ts, turns in zip(item["haystack_session_ids"], item["haystack_dates"], item["haystack_sessions"], strict=True):
        sid, date = str(sid), _date_of(ts)
        rel = session_rel_path(date, sid)
        body = "\n\n".join(_turn_md(t) for t in turns)
        with open(os.path.join(palinode_dir, rel), "w", encoding="utf-8") as f:
            f.write(f"---\ndate: {date}\ntype: DailyNote\nsession_id: {sid}\n---\n# Session {sid} — {ts}\n\n{body}\n")
        where[sid] = rel
    return where


def reset_backends() -> None:
    """Drop the process-wide Ollama client so the next call opens fresh sockets.

    Observed 2026-08-26: after a "connection reset by peer" the pooled httpx
    connection kept returning 500s for the rest of a 7-minute backoff while a
    fresh process embedded fine against the same host.
    """
    from palinode.core import ollama_client

    with ollama_client._singleton_lock:
        old = ollama_client._singleton
        ollama_client._singleton = None
    if old is not None:
        try:
            old.close()
        except Exception:  # noqa: BLE001 - best effort on a broken client
            pass


def fresh_store(palinode_dir: str) -> None:
    """Wipe and re-create a scratch store at *palinode_dir*."""
    if os.path.isdir(palinode_dir):
        shutil.rmtree(palinode_dir)
    harness.point_config_at(palinode_dir)
    harness.init_store()


@dataclass
class Ingest:
    """``harness.IngestResult`` plus how many files had to be indexed keyword-only."""
    result: Any
    fts_only_files: int


def index_with_fallback(palinode_dir: str) -> Ingest:
    """Index the store; any file whose embed failed is re-indexed FTS-only.

    ``index_file`` aborts a file (no chunks at all, not even keyword) when the
    embedder errors on it. Ollama's bge-m3 returns NaN vectors → HTTP 500 for
    some inputs (observed 2026-08-26), so without this a session would vanish
    from the haystack and depress evidence recall silently. The cold-embed
    gate is flipped for the retry so reconcile takes its FTS-only path.
    """
    from palinode.indexer import reconcile as reconcile_mod
    from palinode.indexer.index_file import index_file

    result = harness.index_all(palinode_dir)
    fts_only = 0
    # Always diff paths: ``chunks < files`` missed a dropped file whenever other
    # files carried several sections each (row E's profile, 2026-08-30).
    indexed = _indexed_paths()
    missing = [fp for fp in harness._glob_md(palinode_dir) if os.path.abspath(fp) not in indexed]
    if missing:
        orig = reconcile_mod._embeds_deferred
        reconcile_mod._embeds_deferred = lambda client: True  # type: ignore[assignment]
        try:
            for fp in missing:
                index_file(fp)
                fts_only += 1
        finally:
            reconcile_mod._embeds_deferred = orig  # type: ignore[assignment]
    return Ingest(result=result, fts_only_files=fts_only)


def _indexed_paths() -> set[str]:
    from palinode.core import store

    db = store.get_db()
    try:
        return {os.path.abspath(r[0]) for r in db.execute("SELECT DISTINCT file_path FROM chunks")}
    finally:
        db.close()


@dataclass
class Retrieval:
    hits: list[dict[str, Any]]
    mode: str                  # "hybrid" | "hybrid-reworded" | "keyword" | "keyword-fallback"
    session_ids: list[str]     # session ids seen in hits, in rank order
    context_chars: int
    embed_error: str | None = None
    dup_hits: int = 0          # exact-duplicate chunks dropped (session-end dual-writes daily + indexed file)
    profile_hit: bool = False  # ``projects/user.md`` (the consolidated profile) was among the hits


_SESSION_ID_IN_ENTRY_RE = re.compile(r"\*\*Session ID:\*\*\s*(\S+)|^##\s+\[\d{4}-\d{2}-\d{2}\] session (\S+)", re.M)


def hit_session_id(h: dict[str, Any]) -> str | None:
    """Which haystack session a hit came from: the raw-transcript filename
    (rows A–D), the ``Session ID`` line session-end stamps into its entry
    (row E's daily notes and their indexed twins), or the per-session heading
    of a profile section."""
    sid = session_id_of(h.get("file_path", ""))
    if sid:
        return sid
    m = _SESSION_ID_IN_ENTRY_RE.search(h.get("content", ""))
    return (m.group(1) or m.group(2)) if m else None


def is_profile_hit(h: dict[str, Any]) -> bool:
    return os.path.basename(h.get("file_path", "")) == "user.md" and os.path.basename(os.path.dirname(h.get("file_path", ""))) == "projects"


_AUTO_FOOTER_RE = re.compile(r"\n## See also\s*\n<!-- palinode-auto-footer -->[\s\S]*$")


def dedupe_hits(hits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop hits whose text duplicates an earlier hit. Session-end writes the
    same entry to ``daily/`` and to an indexed twin, which ``save_memory``
    suffixes with an auto ``## See also`` footer — ignored for the comparison."""
    seen: set[str] = set()
    out = []
    for h in hits:
        key = " ".join(_AUTO_FOOTER_RE.sub("", h.get("content", "")).split())
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out, len(hits) - len(out)


_NONWORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def keyword_query(question: str) -> str:
    """Strip punctuation FTS5 chokes on (``?`` ``:`` ``(`` …) that
    ``sanitize_fts_query`` leaves in place. Natural-language questions almost
    always end in ``?``; without this the BM25 arm errors and hybrid silently
    degrades to vector-only."""
    return " ".join(_NONWORD_RE.sub(" ", question).split())


_STOPWORDS = frozenset("""
a an the and or but if then of in on at to for from by with about as into like through after over
between out against during without before under around among is are was were be been being am do
does did doing have has had having i me my mine we our ours you your yours he him his she her hers it
its they them their theirs this that these those what which who whom whose how much many when where
why will would shall should can could may might must not no nor so than too very just also there here
s t d ll m re ve
""".split())


def content_words(question: str) -> str:
    """Stopword-stripped form — a second embedding attempt when the full string
    trips a per-input embedder bug (bge-m3 NaN). Observed 2026-08-26:
    the 17-word question failed, its five content words embedded fine."""
    return " ".join(w for w in keyword_query(question).split() if w.lower() not in _STOPWORDS)


def retrieve(question: str, *, top_k: int, threshold: float, hybrid: bool) -> Retrieval:
    from palinode.core import embedder, store

    kw = keyword_query(question)
    embed_error: str | None = None
    mode = "hybrid"
    vec: list[float] | None = None
    if hybrid:
        for attempt, text in (("hybrid", question), ("hybrid-reworded", content_words(question))):
            if not text:
                continue
            try:
                vec = embedder.embed(text)
                mode = attempt
                break
            except Exception as e:  # noqa: BLE001 - per-input embed failure (e.g. NaN 500): degrade, don't abort
                embed_error = str(e)[:200]
    # Over-fetch so the reader still sees top_k *distinct* excerpts after the
    # session-end daily/indexed-twin duplicates are dropped (row E); with no
    # duplicates (rows A–D) the first top_k of 2×top_k are exactly the old top_k.
    if vec:
        hits = store.search_hybrid(kw, vec, top_k=2 * top_k, threshold=threshold,
                                   include_daily=True, record_access=False)
    else:
        hits = store.search_fts(kw, top_k=2 * top_k)
        mode = "keyword-fallback" if embed_error else "keyword"
    hits, dups = dedupe_hits(hits)
    hits = hits[:top_k]
    sids: list[str] = []
    for h in hits:
        sid = hit_session_id(h)
        if sid and sid not in sids:
            sids.append(sid)
    return Retrieval(hits=hits, mode=mode, session_ids=sids,
                     context_chars=sum(len(h.get("content", "")) for h in hits),
                     embed_error=embed_error, dup_hits=dups,
                     profile_hit=any(is_profile_hit(h) for h in hits))


_DATE_IN_STEM_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def context_label(file_path: str) -> str:
    """The date-ish label the reader sees on an excerpt: the file stem for a raw
    transcript (``2023-05-20-<sid>``), the date alone for a session-end daily
    note's indexed twin (``session-end-2023-05-20-user-<hash>``), the stem
    (``2023-05-20`` / ``user``) otherwise."""
    stem = os.path.splitext(os.path.basename(file_path))[0]
    if stem.startswith("session-end-"):
        m = _DATE_IN_STEM_RE.search(stem)
        return m.group(0) if m else stem
    return stem


def format_context(hits: list[dict[str, Any]]) -> str:
    out = []
    for i, h in enumerate(hits, 1):
        out.append(f"[{i}] ({context_label(h.get('file_path', ''))})\n{h.get('content', '').strip()}")
    return "\n\n".join(out)


ANSWER_PROMPTS = {
    # v1: rows A–C (2026-08-27/29). gpt-4o-2024-08-06 over-abstains under it — 152 of 183
    # wrong answers were "not available in memory" with the evidence session in the prompt.
    "v1": (
        "You answer questions about a user using only the memory excerpts provided. "
        "The excerpts are dated. If the excerpts do not contain the information needed, "
        "say clearly that the information is not available in memory — do not guess. "
        "Be concise and give the direct answer first."
    ),
    # v2: same contract, phrased so the reader reads before declining.
    "v2": (
        "You answer questions about a user from the memory excerpts below, which are dated "
        "transcripts of the user's earlier conversations. Read all of them carefully; the answer "
        "is usually stated somewhere in them. Give the direct answer first, briefly. Only if none "
        "of the excerpts contain relevant information, say so."
    ),
}
DEFAULT_PROMPT_VERSION = "v2"


def prompt_version() -> str:
    v = os.environ.get("LME_PROMPT_VERSION", DEFAULT_PROMPT_VERSION)
    if v not in ANSWER_PROMPTS:
        raise SystemExit(f"LME_PROMPT_VERSION={v!r}; known: {sorted(ANSWER_PROMPTS)}")
    return v


def answer_messages(question: str, question_date: str, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": ANSWER_PROMPTS[prompt_version()]},
        {"role": "user", "content": (
            f"Today's date: {question_date}\n\nMemory excerpts:\n\n{context}\n\n"
            f"Question: {question}"
        )},
    ]

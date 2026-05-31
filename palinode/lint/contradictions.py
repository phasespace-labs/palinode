"""
Deep semantic contradiction detection for `palinode lint --deep-contradictions`.

Algorithm:
1. Collect all type:Decision memories from PALINODE_DIR.
2. For each pair (A, B) that shares at least one entity:
   - Compute cosine similarity between their embeddings.
   - If similarity > threshold (default 0.75): candidate contradiction.
3. Cap candidates at top-K by similarity (default 50) to bound LLM calls.
4. For each candidate pair, call the configured LLM via the consolidation
   endpoint and ask it to classify as CONTRADICTION / AGREEMENT / UNRELATED.
5. Emit a finding for each pair the LLM classifies as CONTRADICTION.

Only activated by the --deep-contradictions flag on `palinode lint`.
Default lint (no flag) never reaches this module.
"""
from __future__ import annotations

import glob
import logging
import math
import os
from typing import Any

import frontmatter as _frontmatter

from palinode.core.config import config
from palinode.core import parser
from palinode.core.ollama_client import OllamaError, OllamaRole, get_ollama_client

logger = logging.getLogger("palinode.lint.contradictions")

# Default similarity threshold above which a pair becomes a candidate.
DEFAULT_SIMILARITY_THRESHOLD: float = 0.75

# Default max LLM calls per run.
DEFAULT_MAX_LLM_CALLS: int = 50

# Characters of body text sent to the LLM per memory.
_BODY_SNIPPET_CHARS: int = 1500

_CONTRADICTION_SYSTEM_PROMPT = (
    "You are a fact-consistency auditor for an AI memory system. "
    "You will be shown two Decision memories. "
    "Reply with exactly one of: CONTRADICTION, AGREEMENT, or UNRELATED on the "
    "first line, then a single sentence explanation."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length vectors.

    Returns 0.0 if either vector is empty or zero-length.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _collect_decision_memories(memory_dir: str) -> list[dict[str, Any]]:
    """Collect all type:Decision memories from *memory_dir*.

    Returns a list of dicts with keys:
        path (str): absolute file path
        rel_path (str): path relative to memory_dir
        entities (list[str])
        body (str): markdown body (frontmatter stripped)
        embedding (list[float]): may be empty if embedding unavailable
    """
    skip_dirs = {"archive", "logs", ".obsidian"}
    pattern = os.path.join(memory_dir, "**", "*.md")
    memories: list[dict[str, Any]] = []

    for filepath in glob.glob(pattern, recursive=True):
        rel = os.path.relpath(filepath, memory_dir)
        parts = rel.split(os.sep)
        if parts[0] in skip_dirs:
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                content = fh.read()
            metadata, _ = parser.parse_markdown(content)
            mem_type = metadata.get("type", "")
            if mem_type != "Decision":
                continue
            try:
                post = _frontmatter.loads(content)
                body = post.content
            except Exception:
                body = content

            memories.append(
                {
                    "path": filepath,
                    "rel_path": rel,
                    "entities": metadata.get("entities", []),
                    "body": body,
                    "embedding": [],  # filled lazily below
                }
            )
        except Exception:
            pass

    return memories


def _embed_memories(memories: list[dict[str, Any]]) -> None:
    """Fill the ``embedding`` field for each memory in-place.

    Uses the palinode embedder. If embedding fails for a memory the field
    remains an empty list (that memory will be skipped during pair evaluation).
    """
    from palinode.core import embedder as _embedder

    for mem in memories:
        try:
            vec = _embedder.embed(mem["body"][:_BODY_SNIPPET_CHARS])
            mem["embedding"] = vec if vec else []
        except Exception as exc:
            logger.debug("Embedding failed for %s: %s", mem["rel_path"], exc)
            mem["embedding"] = []


def _candidate_pairs(
    memories: list[dict[str, Any]],
    threshold: float,
    max_candidates: int,
) -> list[tuple[dict, dict, float]]:
    """Return top-K candidate pairs ordered by similarity (descending).

    Only pairs that share at least one entity AND whose similarity exceeds
    *threshold* are included. Capped at *max_candidates*.
    """
    pairs: list[tuple[dict, dict, float]] = []

    n = len(memories)
    for i in range(n):
        a = memories[i]
        if not a["embedding"]:
            continue
        entities_a = set(a["entities"])
        for j in range(i + 1, n):
            b = memories[j]
            if not b["embedding"]:
                continue
            # Only evaluate pairs that share at least one entity.
            if not entities_a.intersection(b["entities"]):
                continue
            sim = _cosine_similarity(a["embedding"], b["embedding"])
            if sim >= threshold:
                pairs.append((a, b, sim))

    # Sort descending by similarity; take the top-K.
    pairs.sort(key=lambda t: t[2], reverse=True)
    return pairs[:max_candidates]


def _call_llm_for_contradiction(
    body_a: str, body_b: str, llm_url: str, llm_model: str, temperature: float = 0.0
) -> str:
    """Call the LLM to classify a pair as CONTRADICTION / AGREEMENT / UNRELATED.

    Returns the raw first-line verdict string, or '' on failure.
    """
    user_prompt = (
        f"Decision A says:\n{body_a[:_BODY_SNIPPET_CHARS]}\n\n"
        f"Decision B says:\n{body_b[:_BODY_SNIPPET_CHARS]}\n\n"
        "Do A and B contradict each other? "
        'Reply "CONTRADICTION", "AGREEMENT", or "UNRELATED" on the first line, '
        "then a one-sentence explanation."
    )
    try:
        # #338 Phase 4: route through the centralized client (CONSOLIDATION role,
        # OpenAI-compatible /v1/chat/completions). retries=0 — a contradiction
        # check is best-effort; a failure just yields UNKNOWN.
        content = get_ollama_client().chat_completions(
            [
                {"role": "system", "content": _CONTRADICTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=llm_model,
            base_url=llm_url,
            temperature=temperature,
            max_tokens=150,
            timeout=60.0,
            retries=0,
            role=OllamaRole.CONSOLIDATION,
        )
        return content.strip()
    except OllamaError as exc:
        logger.warning("LLM call for contradiction check failed: %s", exc)
        return ""


def _parse_llm_verdict(raw: str) -> tuple[str, str]:
    """Parse raw LLM output into (verdict, explanation).

    verdict is normalised to one of: CONTRADICTION, AGREEMENT, UNRELATED, UNKNOWN.
    """
    if not raw:
        return "UNKNOWN", ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    first = lines[0].upper() if lines else ""
    explanation = " ".join(lines[1:]) if len(lines) > 1 else ""
    if "CONTRADICTION" in first:
        return "CONTRADICTION", explanation
    if "AGREEMENT" in first:
        return "AGREEMENT", explanation
    if "UNRELATED" in first:
        return "UNRELATED", explanation
    # First line might combine verdict with explanation; try to extract.
    for keyword in ("CONTRADICTION", "AGREEMENT", "UNRELATED"):
        if keyword in raw.upper():
            idx = raw.upper().index(keyword)
            return keyword, raw[idx + len(keyword):].strip().lstrip(":").strip()
    return "UNKNOWN", raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_deep_contradiction_check(
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
    memory_dir: str | None = None,
    # Injected for testing:
    _llm_caller: Any = None,
) -> dict[str, Any]:
    """Run the deep semantic contradiction check.

    Args:
        similarity_threshold: Cosine similarity floor for candidate pairs (0–1).
        max_llm_calls: Hard cap on LLM calls for this run.
        memory_dir: Override for palinode memory directory (default: config).
        _llm_caller: Optional callable(body_a, body_b, llm_url, llm_model) → str.
            Injected in tests to replace the real HTTP call.

    Returns:
        dict with keys:
            decisions_found (int)
            candidate_pairs (int)
            llm_calls (int)
            llm_budget (int)
            contradictions (list of finding dicts)
    """
    base_dir = memory_dir or getattr(config, "memory_dir", config.palinode_dir)
    llm_url = config.consolidation.llm_url
    llm_model = config.consolidation.llm_model
    llm_caller = _llm_caller if _llm_caller is not None else _call_llm_for_contradiction

    memories = _collect_decision_memories(base_dir)
    logger.info("Deep contradiction check: found %d Decision memories", len(memories))

    _embed_memories(memories)

    embedded = [m for m in memories if m["embedding"]]
    candidates = _candidate_pairs(embedded, similarity_threshold, max_llm_calls)

    contradictions: list[dict[str, Any]] = []
    llm_calls = 0

    for mem_a, mem_b, sim in candidates:
        if llm_calls >= max_llm_calls:
            break
        raw = llm_caller(mem_a["body"], mem_b["body"], llm_url, llm_model)
        llm_calls += 1
        verdict, explanation = _parse_llm_verdict(raw)
        if verdict == "CONTRADICTION":
            contradictions.append(
                {
                    "file_a": mem_a["rel_path"],
                    "file_b": mem_b["rel_path"],
                    "similarity": round(sim, 4),
                    "llm_explanation": explanation,
                }
            )

    return {
        "decisions_found": len(memories),
        "candidate_pairs": len(candidates),
        "llm_calls": llm_calls,
        "llm_budget": max_llm_calls,
        "contradictions": contradictions,
    }

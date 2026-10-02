"""Whether a delivery holds a confident answer, read from the pre-fusion arms.

A delivered ``score`` cannot answer this. It is a normalized RRF rank: the
top hit carries 1.0 whether it is the answer or the least bad of a weak
field, so a caller conditioning on it learns nothing (see
:mod:`palinode.core.scoring`). The floors the ranker applies cannot answer it
either — they are admission floors, and a result that cleared one is only
*eligible*, not *right*.

What can answer it is each arm's own score, on its own scale, before fusion
flattened them: real cosine similarity for the vector arm, normalized BM25
for the keyword arm — this query's BM25 as a fraction of what this query could
score here at all (:func:`palinode.core.store.bm25_query_scale`), where 1.0 is
a chunk holding every query term once. Both ride the delivered rows already
(``raw_score`` and ``keyword_score``), so the verdict costs no extra lookup
and describes exactly the slate the caller was handed.

Three verdicts, and the middle one is why there are three:

``confident``
    An arm's best delivered score reaches a mark where, in the measurements
    below, wrong answers do not live — and, where both arms ran, the other
    arm at least recognises the query (see *Corroboration*).
``weak``
    Something matched, but nothing reaches that mark. Ordinary for a broad
    question; the results are still worth reading.
``none``
    Nothing delivered reaches even the low mark. The store most likely does
    not hold this. Results are **not** withheld for it by default: an empty
    slate would trade a wrong answer for a lie about the corpus, and the
    caller can only tell the difference if it sees both the verdict and the
    rows.

**Where the marks come from.** Neither pair is a round number chosen for
looking tidy.

*Vector arm.* The 54-pair threshold study recorded in
:class:`palinode.core.config.SearchConfig`: for the TRUE match, 98% clear
0.5 and 74% clear 0.6; for the same query's hardest WRONG answer, 0% clear
0.6. So 0.60 is the mark above which no measured distractor appeared, and
0.50 is the mark below which measured true matches essentially do not appear.
Independently, an earlier measurement behind ``describe_match`` — 32 searches
whose answer was absent from the corpus — put absent-answer cosine in
0.402–0.459, entirely below 0.50.

*Corroboration, and why cosine alone does not decide.* Those marks were
carried from a study whose distractors were *the same query's* wrong answers.
Run against a whole corpus, the vector arm behaves differently: on the
``bench.relevance`` fixture with real bge-m3, three questions with **no**
answer in the corpus produced a best cosine of 0.607–0.635 — above the 0.60
mark, and above four answerable questions' best cosine. No cosine mark
separates them without cost: the lowest mark that admits none of the three is
0.64, and it also demotes six correct confident answers. What does separate
them is the other arm. All three are queries the keyword arm barely registers
(best normalized BM25 0.000, 0.000, 0.199 — all below its weak mark, on the
old scale and on this one), while every answerable question in the same band
clears it. So the rule is
structural rather than a higher number: **a vector-only enthusiasm about a
query the keyword arm cannot find is not confidence.** Requiring
corroboration leaves 46 of 48 answerable questions confident — every one of
them with a relevant record in the slate — against 0 of 14 no-answer ones,
which no cosine mark achieves. On the rescaled keyword arm it now costs
nothing on this fixture: it demotes those three and no answerable question,
where against ``|bm25| / 25`` it also took one held-out paraphrase (cosine
0.681) whose keyword score, priced per query rather than against a constant,
turns out to clear the weak mark. A raised solo-cosine tier would have kept
that one too, at the price of a constant fitted to three points with 0.005 of
headroom — the same numerology this module refuses on the keyword side.

*Keyword arm.* Re-calibrated on the ``bench.relevance`` fixture (48 answerable
questions, 14 with no answer in the corpus, held-out paraphrase and identifier
variants included) after the arm was rescaled per query. Both marks moved,
because the scale under them did: they used to be 0.22 and 0.13 on
``|bm25| / 25``.

``0.60`` is the round value in the middle of a flat stretch. The highest score
any no-answer question produced is 0.557; the answerable count is 33 at every
mark from 0.56 to 0.62 and every one of those 33 slates held a relevant
record, so the number is chosen from a plateau rather than fitted to the
0.557. Against the old scale's 25 (26 on this host), the eight exact-identifier
questions are the difference — the measured defect this rescale exists to fix.

``0.22`` is the last value that calls no answerable question ``none``. It puts
8 of the 14 no-answer questions there, against 7 at 0.20 and 6 under the old
marks; the first answerable question it would demote sits at 0.227, and the
first one whose slate actually held a relevant record at 0.244. Since
corroboration spends this mark (below), the hybrid cost of raising it is
measurable and real: 0.24 costs one confident answer, 0.27 costs two.

That ``0.60`` matches the vector arm's confident mark is a coincidence of two
calibrations, not a shared scale. The two arms are still incomparable numbers.

**What the marks now mean, and where they still bend.** A keyword score is
coverage: 1.0 is a chunk carrying every term of the query once, and the scale
no longer moves with the store's size — an exact identifier scores ~1.0 on a
three-record store and on a two-hundred-record one (measured: 1.016 / 1.022 /
1.023). What it cannot distinguish is a query with nothing rare in it: ask a
single common word and every chunk containing it covers the query completely
and scores ~1.0, which is true and unhelpful. A query whose every term is in
half the store or more scores 0.0 by construction (the store's scale returns
zero rather than divide two zeros); between those lies a band where the arm
will call a common-word query confident. No measured question on the fixture
reaches the mark that way, and the verdict stays advisory — nothing is
withheld for it unless an operator asks.

Corroboration used to inherit the drift: because a hybrid ``confident``
depends on the keyword arm clearing its weak mark, a store of a handful of
records could not produce one however well it matched. That is the cost the
rescale removes — an exact identifier hit reaches the confident mark on a
three-record store now, so it can also vouch for the vector arm there.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

CONFIDENT = "confident"
WEAK = "weak"
NONE = "none"

#: Strongest first — the order a surface should present them in.
VERDICTS: tuple[str, ...] = (CONFIDENT, WEAK, NONE)

_RANK: dict[str, int] = {NONE: 0, WEAK: 1, CONFIDENT: 2}

#: Vector-arm marks, on real cosine similarity. See the module docstring.
VECTOR_CONFIDENT = 0.60
VECTOR_WEAK = 0.50

#: Keyword-arm marks, on per-query normalized BM25 (1.0 = a chunk holding
#: every query term once; see :func:`palinode.core.store.bm25_query_scale`).
KEYWORD_CONFIDENT = 0.60
KEYWORD_WEAK = 0.22

#: Which arm each delivered row reports its own score under.
_ARM_KEYS: tuple[tuple[str, str, float, float], ...] = (
    ("vector", "raw_score", VECTOR_CONFIDENT, VECTOR_WEAK),
    ("keyword", "keyword_score", KEYWORD_CONFIDENT, KEYWORD_WEAK),
)

#: Modes that ranked something against a query. ``recency`` did not — it
#: returns the newest rows whatever they say — so it gets no verdict rather
#: than a made-up one.
RANKED_MODES: frozenset[str] = frozenset(
    {"hybrid", "vector", "lexical", "keyword-fallback"}
)

#: Modes in which BOTH arms ran, so one can be asked to corroborate the other.
#: Only ``hybrid`` qualifies: ``vector`` skipped the keyword arm on the
#: caller's own ``hybrid=false``, and ``lexical`` / ``keyword-fallback`` never
#: built a query vector. Asking an arm that did not run for corroboration
#: would read its silence as dissent.
CORROBORATING_MODES: frozenset[str] = frozenset({"hybrid"})


def _arm_verdict(best: float | None, confident_at: float, weak_at: float) -> str:
    """One arm's verdict from its best delivered score (``None`` ⇒ ``none``)."""
    if best is None:
        return NONE
    if best >= confident_at:
        return CONFIDENT
    if best >= weak_at:
        return WEAK
    return NONE


def _best(results: Sequence[Mapping[str, Any]], key: str) -> float | None:
    """The best score any delivered row carries for one arm, or ``None``.

    ``None`` means this delivery has no evidence from that arm: either the arm
    did not run (lexical retrieval has no vector arm) or none of its
    candidates survived to the slate. Both are the absence of a vouch, which
    is what the verdict needs; neither is a zero.
    """
    values = [
        float(row[key]) for row in results
        if isinstance(row, Mapping) and row.get(key) is not None
    ]
    return max(values) if values else None


def assess(
    results: Sequence[Mapping[str, Any]], *, active_mode: str
) -> dict[str, Any] | None:
    """The confidence verdict for one delivery, with the arm evidence behind it.

    ``results`` are the rows exactly as the delivery is about to return them.
    Returns ``{"confidence": <verdict>, "arms": {...}}`` — the block a surface
    merges into its retrieval diagnostics — or ``None`` when the mode ranked
    nothing against a query and no verdict applies.

    The overall verdict is the strongest arm verdict: one arm vouching is
    enough, matching the ranker, where a candidate needs to clear only its own
    arm's floor to be admitted. An arm with no evidence in this delivery is
    reported with ``best: null`` rather than dropped, so a reader can tell
    "the arm said nothing" from "the arm was never asked".

    **With one exception, and it is the measured one.** When both arms ran
    (``hybrid``), the vector arm may not claim ``confident`` over a query the
    keyword arm does not reach its own weak mark on; such a delivery is
    reported ``weak`` and the block carries ``corroboration: "missing"``. See
    the module docstring for why that is a rule about arms rather than a
    higher cosine mark. The demotion is one-directional: the keyword arm
    needed no corroboration in the measurement, and requiring it would make
    ``confident`` unreachable in lexical mode, where there is no vector arm to
    ask.
    """
    if active_mode not in RANKED_MODES:
        return None
    arms: dict[str, Any] = {}
    for name, key, confident_at, weak_at in _ARM_KEYS:
        best = _best(results, key)
        arms[name] = {
            "best": best,
            "verdict": _arm_verdict(best, confident_at, weak_at),
            "confident_at": confident_at,
            "weak_at": weak_at,
        }

    vector, keyword = arms["vector"]["verdict"], arms["keyword"]["verdict"]
    block: dict[str, Any] = {}
    if (
        active_mode in CORROBORATING_MODES
        and vector == CONFIDENT
        and keyword == NONE
    ):
        vector = WEAK
        block["corroboration"] = "missing"
    verdict = vector if _RANK[vector] >= _RANK[keyword] else keyword
    return {"confidence": verdict, "arms": arms, **block}


def delivered_verdict(receipt: Mapping[str, Any] | None) -> str | None:
    """The verdict a delivery receipt carries, or ``None`` when it carries none.

    Tolerant on purpose: an older API server sends a receipt without a
    ``retrieval`` block, and a caller must not have to know that.
    """
    if not isinstance(receipt, Mapping):
        return None
    retrieval = receipt.get("retrieval")
    if not isinstance(retrieval, Mapping):
        return None
    verdict = retrieval.get("confidence")
    return verdict if verdict in VERDICTS else None


__all__ = [
    "CONFIDENT",
    "KEYWORD_CONFIDENT",
    "KEYWORD_WEAK",
    "NONE",
    "RANKED_MODES",
    "VECTOR_CONFIDENT",
    "VECTOR_WEAK",
    "VERDICTS",
    "WEAK",
    "assess",
    "delivered_verdict",
]

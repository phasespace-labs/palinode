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

*Co-occurrence.* The keyword arm used to need no second opinion, and on short
operations requests that was the gap. Its score is coverage of the query's IDF,
so a chunk repeating one rare-ish term ("write" inside "dual-write") carries a
three-word command past the confident mark by itself; and on a large store of
one project's work notes even a chunk holding several terms proves little:
"delete", "fallback" and "file" are each in hundreds of its chunks, so some
chunk holds them together whatever the question was. Neither arm's best score
separates such commands from real coding requests there — on a 12,800-chunk
store of development notes, short commands and answerable requests alike
scored 0.52–0.68 cosine and 0.45–1.7 keyword. So the keyword arm also has to
show that the row's combination of query terms is rarer than chance in this
index (:func:`palinode.core.store.keyword_chance`): the expected number of
chunks that would hold those terms together if terms fell into chunks
independently. At one or more, any index of this size and vocabulary has such
a row, and the keyword arm does not vouch for it: the delivery reports
``weak`` with ``co_occurrence: "chance"``. A single matched term is always at
least one (its own document count), so one term can no longer make a
multi-term query confident. Two kinds of row are not judged and keep the
score's own verdict: one answering a one-term query, which has no
co-occurrence to measure (the exact-identifier case), and one whose every
matched term is in half the index or more, the few-records regime where the
score counts terms rather than weighing them and every combination is chance.
One is the mark because it is where chance stops supplying the row, not
because a measurement put it there. On ``bench.relevance`` (real bge-m3) it
changes exactly one verdict in every arm: the prompt-shaped false confident on
"write the release notes for this". No question in the published set and no
answerable prompt moves. The rule governs the keyword arm's own claim only.
Its corroboration of the vector arm is unchanged: requiring the figure there
too demoted a sixth of the published answerable questions in a simulation over
the same rows, because on a small store most corroborating rows are ordinary
combinations. A vector-led delivery on a large homogeneous store was therefore
still reachable by an everyday command, which is what the next two rules close
for automatic recall.

*Subject coverage (prompt mode only).* Neither arm's score tells a command
from a question. On a 14,000-chunk store of development notes, "bump the
version and tag it" scored keyword 1.20 against release-prep notes holding
all three words, rarer than chance under independence because those words
travel together. How rare a word is in the store does not separate them
either: "what threshold do new triggers default to" matched only words more
common there than "bump". What separates them is what the prompt is about.
:func:`palinode.core.prompt_query.parse_prompt` splits it, deterministically,
into subject terms and working-state terms (the working copy, the session,
words pointing back into the conversation), and the keyword arm may vouch for
a prompt only through a row that matched a subject term. ``subject_units`` is
that set; a delivery the rule holds below ``confident`` says
``subject_coverage: "no_subject"`` (the prompt names nothing) or
``"working_state_only"`` (its keyword match missed the subject). On the
dogfood store this took prompt-shaped no-answers from 17 of 40 to 6, held-out
commands from 11 of 14 to 1, and moved no answerable question, together with
the vector mark below.

*The vector mark over the store's background (prompt mode only).* The 0.60
cosine mark was calibrated on a store where a contentless prompt ("continue")
scores 0.471; on that development store it scores 0.601, so any prompt
clears 0.60 there. ``vector_confident_at`` lets the caller raise the mark to
``background + 0.129`` (:mod:`palinode.core.vector_baseline`), the calibrated
mark's height above its own background. It never lowers the mark. On a large
homogeneous store the vector arm then almost never vouches alone; meaning-only
questions scored 0.51–0.61 there, inside the background, so it could not tell
them from noise in any case.
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

#: The keyword arm vouches only for a row whose combination of query terms is
#: rarer than chance in this index: fewer than this many documents would hold
#: it by accident (:func:`palinode.core.store.keyword_chance`). One is where
#: "the row is about these terms" starts to beat "any index this size has a
#: row like this"; it is not fitted. See *Co-occurrence* in the module docstring.
KEYWORD_CHANCE_BELOW = 1.0

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


def _keyword_rows(results: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Rows at the keyword confident mark whose terms are together more rarely
    than chance would put them (see *Co-occurrence*).

    A row without the figure (``None``: a one-term query, or a row from a
    caller that does not compute it) is judged on its score alone, as before.
    """
    rows = []
    for row in results:
        if not isinstance(row, Mapping):
            continue
        score = row.get("keyword_score")
        if score is None or float(score) < KEYWORD_CONFIDENT:
            continue
        chance = row.get("keyword_chance")
        if chance is None or float(chance) < KEYWORD_CHANCE_BELOW:
            rows.append(row)
    return rows


def _covers_subject(row: Mapping[str, Any], subject_units: frozenset[str]) -> bool:
    """Whether a keyword row matched at least one of the prompt's subject units.

    A row that does not say which units it holds (``keyword_units`` absent: a
    caller that does not compute it) is not judged.
    """
    units = row.get("keyword_units")
    if units is None:
        return True
    return any(str(unit).lower() in subject_units for unit in units)


def _keyword_vouches(results: Sequence[Mapping[str, Any]]) -> bool:
    """Whether some row at the keyword confident mark is rarer than chance."""
    return bool(_keyword_rows(results))


def assess(
    results: Sequence[Mapping[str, Any]],
    *,
    active_mode: str,
    subject_units: frozenset[str] | None = None,
    vector_confident_at: float | None = None,
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

    **With two exceptions, both measured.** When both arms ran (``hybrid``),
    the vector arm may not claim ``confident`` over a query the keyword arm
    does not reach its own weak mark on; such a delivery is reported ``weak``
    and the block carries ``corroboration: "missing"``. See the module
    docstring for why that is a rule about arms rather than a higher cosine
    mark. The keyword arm is not asked for a second arm's word — that would
    make ``confident`` unreachable in lexical mode, where there is no vector
    arm to ask — but in every mode it may claim ``confident`` only on a row
    whose combination of query terms is rarer than chance in this index; a
    delivery whose confident keyword rows are all chance-level is reported
    ``weak`` with ``co_occurrence: "chance"`` (*Co-occurrence*, above). As
    with corroboration, the arm still reports its own verdict.

    ``subject_units`` (prompt-mode recall) are the prompt's subject terms as
    match units; when given, a keyword row vouches only if its
    ``keyword_units`` include one (*Subject coverage*). ``vector_confident_at``
    raises the vector arm's confident mark, never lowers it.
    """
    if active_mode not in RANKED_MODES:
        return None
    arms: dict[str, Any] = {}
    for name, key, confident_at, weak_at in _ARM_KEYS:
        if name == "vector" and vector_confident_at is not None:
            confident_at = max(confident_at, vector_confident_at)
        best = _best(results, key)
        arms[name] = {
            "best": best,
            "verdict": _arm_verdict(best, confident_at, weak_at),
            "confident_at": confident_at,
            "weak_at": weak_at,
        }

    vector, keyword = arms["vector"]["verdict"], arms["keyword"]["verdict"]
    block: dict[str, Any] = {}
    vouching = _keyword_rows(results) if keyword == CONFIDENT else []
    chance_level = keyword == CONFIDENT and not vouching
    uncovered: str | None = None
    if keyword == CONFIDENT and vouching and subject_units is not None:
        if not subject_units:
            uncovered = "no_subject"
        elif not any(_covers_subject(row, subject_units) for row in vouching):
            uncovered = "working_state_only"
    if chance_level or uncovered:
        keyword = WEAK
    if (
        active_mode in CORROBORATING_MODES
        and vector == CONFIDENT
        and keyword == NONE
    ):
        vector = WEAK
        block["corroboration"] = "missing"
    verdict = vector if _RANK[vector] >= _RANK[keyword] else keyword
    if chance_level and verdict != CONFIDENT:
        # Named only where it is why the delivery is below the arm's own
        # verdict; a vector-led confident delivery needs no excuse.
        block["co_occurrence"] = "chance"
    if uncovered and verdict != CONFIDENT:
        block["subject_coverage"] = uncovered
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
    "KEYWORD_CHANCE_BELOW",
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

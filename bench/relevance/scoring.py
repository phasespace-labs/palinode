"""Scoring for the relevance/abstention evaluation.

Pure functions over already-delivered results, so every number here is
reproducible from a results file without a store, an embedder or a network —
and unit-testable on a hand-built slate.

**What each metric counts, and against what denominator.** The denominators
matter more than the rates: a "footer-only hit rate" over questions and one
over results are different numbers with the same name.

``recall_at_k``
    Per question, the fraction of the question's expected relevant refs that
    appear anywhere in the delivered slate. Aggregated micro (sum of found over
    sum of expected), so a question with two expected refs weighs twice a
    question with one. Undefined — and excluded — for no-answer questions.

``top1_relevant``
    Did the first delivered result answer the question. Denominator: answerable
    questions that returned at least one result.

``irrelevant_injections``
    Delivered results that are neither a relevant ref nor a tolerated one.
    Counted per result; the rate's denominator is delivered results. Every
    result of a no-answer question is an injection by construction.

``footer_only_hits``
    Delivered results that matched through the auto-generated ``See also``
    footer and nowhere else (:func:`is_footer_only`). Counted per result.

``redundant_results``
    Delivered results that restate an earlier result in the same slate — the
    second and later members of a ``dup_group``. Counted per result.

``same_file_repeats``
    Delivered results after the first from the same source file. A different
    shape of redundancy from ``dup_group``: not two records saying the same
    thing, but one record spending several slots of a small slate. Counted per
    result.

``isolation_violations``
    Delivered results belonging to a project other than the question's, and not
    an expected ref. Counted per result over **answerable** questions only: on a
    no-answer question about a project with no records, every result is already
    an injection, and counting it again as an isolation violation would double
    the same failure under two names.

``abstained`` / ``confident_match``
    No-answer questions only. ``abstained`` is an empty slate. A
    ``confident_match`` is a non-empty slate whose top score reaches the
    *confidence band*: the median top score across answerable questions in the
    same mode that got their top-1 right. That band is what a caller would have
    to treat as "this looks like a real answer", so a no-answer question
    reaching it is the inappropriate-confident-match case rather than a thin
    result list. ``band_distinct_values`` is reported alongside it: when the
    delivered score is a function of rank rather than of relevance, every
    correct top-1 carries the same score, the band is degenerate, and
    ``confident_match`` collapses onto ``returned_any``. That is a finding
    about the score, not a measurement artefact to hide.

``confidence_verdict``
    The delivery-level verdict (``confident`` / ``weak`` / ``none``) crossed
    with whether the question was answerable — the confusion matrix for the
    signal itself, independent of the ``confidence_band`` above it. The band
    is derived from the delivered score and collapses when that score is a
    rank; the verdict is derived from the pre-fusion arm scores and does not.

``recency_trap_top1``
    The question declares refs that must not rank first — a retired value, or a
    newer but undecided proposal. Denominator: questions declaring a trap that
    returned at least one result.
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from palinode.core.embedding_preprocess import (
    AUTO_FOOTER_MARKER,
    strip_auto_footer,
    strip_wikilinks,
)

from bench.relevance.corpus import CLASSES, Question, Record

#: Function words dropped before deciding whether a query term appears in a
#: result. Not a general stoplist — just enough that "what is the current
#: release" does not match a footer on the strength of "the".
_STOPWORDS: frozenset[str] = frozenset("""
a an and any are as at be been before but by can did do does for from had has
have how if in into is it its no not now of on or our right so still than that
the their them then there these they this to up was we were what when where
which who whose why will with would you your ever about after also
""".split())

#: FTS5's default tokenizer splits on non-alphanumerics; matching that is what
#: makes "did this result match through the footer" mean the same thing here as
#: it does in the keyword arm.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def content_tokens(text: str) -> set[str]:
    """FTS-style content tokens: alphanumeric runs, minus function words.

    Bare numbers of one or two digits are dropped as well. They carry no
    topicality — every prose record contains a "2" somewhere — and keeping them
    would let an identifier query like ``HL-4.2.0`` match anything.
    """
    tokens = set()
    for token in _TOKEN_RE.findall(text.lower()):
        if token in _STOPWORDS:
            continue
        if token.isdigit() and len(token) <= 2:
            continue
        tokens.add(token)
    return tokens


def is_footer_only(content: str, query: str) -> bool:
    """True when *content* matches *query* only through its auto-footer.

    The chunk carries the auto-footer marker, no query term survives in the
    body once the footer is stripped, and at least one query term is present in
    the footer itself. That is the shape a footer-only hit has: the record was
    retrieved for links appended by the wiki contract rather than for anything
    it says.
    """
    if AUTO_FOOTER_MARKER not in content:
        return False
    terms = content_tokens(query)
    if not terms:
        return False
    marker_at = content.find(AUTO_FOOTER_MARKER)
    body_terms = content_tokens(strip_auto_footer(content))
    footer_terms = content_tokens(strip_wikilinks(content[marker_at:]))
    return not (terms & body_terms) and bool(terms & footer_terms)


@dataclass(frozen=True)
class Hit:
    """One delivered result, resolved back to the fixture record it came from.

    ``score`` is the fused rank the caller sees. ``cosine`` and ``keyword``
    are the two pre-fusion arm scores the row carries (``raw_score`` and
    ``keyword_score``), recorded because the fused value is a rank and cannot
    be calibrated against — they are what the delivery's confidence verdict is
    computed from, so a results file has to hold them for the verdict to be
    re-derivable without a store.
    """

    rank: int
    record_id: str | None
    section_id: str
    content: str
    score: float | None
    file_path: str = ""
    cosine: float | None = None
    keyword: float | None = None


@dataclass
class QuestionScore:
    """Every counted outcome for one question in one mode."""

    question_id: str
    cls: str
    variant: str
    project: str
    answerable: bool
    returned: int
    relevant_expected: int
    relevant_found: int
    recall_at_k: float | None
    top1_relevant: bool | None
    first_relevant_rank: int | None
    irrelevant_injections: int
    footer_only_hits: int
    redundant_results: int
    same_file_repeats: int
    isolation_violations: int | None
    unresolved_hits: int
    abstained: bool | None
    recency_trap_top1: bool | None
    trap_hits: int
    top_score: float | None
    payload_tokens: int
    useful_tokens: int
    #: The delivery's confidence verdict, from the pre-fusion arm scores.
    verdict: str | None = None
    #: The best score each arm contributed to this slate.
    best_cosine: float | None = None
    best_keyword: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_question(
    question: Question,
    hits: Sequence[Hit],
    records: dict[str, Record],
    *,
    payload_tokens: int,
    useful_tokens: int,
    verdict: str | None = None,
) -> QuestionScore:
    """Score one delivered slate against one question's labels."""
    relevant = set(question.relevant)
    tolerated = set(question.tolerated)
    traps = set(question.must_not_be_top)

    found = {hit.record_id for hit in hits if hit.record_id in relevant}
    first_relevant_rank = next(
        (hit.rank for hit in hits if hit.record_id in relevant), None
    )

    injections = sum(
        1 for hit in hits if hit.record_id not in relevant and hit.record_id not in tolerated
    )
    footer_only = sum(1 for hit in hits if is_footer_only(hit.content, question.ask))

    seen_groups: set[str] = set()
    redundant = 0
    seen_files: set[str] = set()
    same_file = 0
    for hit in hits:
        record = records.get(hit.record_id or "")
        group = record.dup_group if record else None
        if group:
            if group in seen_groups:
                redundant += 1
            seen_groups.add(group)
        key = hit.file_path or (hit.record_id or "")
        if key:
            if key in seen_files:
                same_file += 1
            seen_files.add(key)

    isolation: int | None = None
    if question.answerable:
        isolation = 0
        for hit in hits:
            record = records.get(hit.record_id or "")
            if record is None or hit.record_id in relevant:
                continue
            if record.project != question.project:
                isolation += 1

    top = hits[0] if hits else None
    return QuestionScore(
        question_id=question.id,
        cls=question.cls,
        variant=question.variant,
        project=question.project,
        answerable=question.answerable,
        returned=len(hits),
        relevant_expected=len(relevant),
        relevant_found=len(found),
        recall_at_k=(len(found) / len(relevant)) if relevant else None,
        top1_relevant=(top.record_id in relevant) if (top and relevant) else None,
        first_relevant_rank=first_relevant_rank,
        irrelevant_injections=injections,
        footer_only_hits=footer_only,
        redundant_results=redundant,
        same_file_repeats=same_file,
        isolation_violations=isolation,
        unresolved_hits=sum(1 for hit in hits if hit.record_id is None),
        abstained=(not hits) if not question.answerable else None,
        recency_trap_top1=(top.record_id in traps) if (top and traps) else None,
        trap_hits=sum(1 for hit in hits if hit.record_id in traps),
        top_score=top.score if top else None,
        payload_tokens=payload_tokens,
        useful_tokens=useful_tokens,
        verdict=verdict,
        best_cosine=max((h.cosine for h in hits if h.cosine is not None), default=None),
        best_keyword=max((h.keyword for h in hits if h.keyword is not None), default=None),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return (numerator / denominator) if denominator else None


def confidence_band(scores: Sequence[QuestionScore]) -> float | None:
    """The score a caller would read as "this is a real answer".

    Median top score across answerable questions that got their top-1 right.
    ``None`` when no such question exists, in which case no
    ``confident_match`` can be claimed either.
    """
    values = [
        float(score.top_score)
        for score in scores
        if score.answerable and score.top1_relevant and score.top_score is not None
    ]
    return statistics.median(values) if values else None


#: The verdicts the delivery-level confidence signal can take, strongest first.
VERDICTS: tuple[str, ...] = ("confident", "weak", "none")


def verdict_matrix(scores: Sequence[QuestionScore]) -> dict[str, Any]:
    """The confusion matrix for the confidence verdict: verdict × answerable.

    The two cells that matter sit diagonally opposite: ``confident`` on a
    no-answer question is a confident wrong answer, and ``none`` on an
    answerable one is a refusal to answer something the store holds. Reported
    with ``relevant_found`` alongside, because a ``none`` on a question whose
    slate contained no relevant record is the verdict being *right* about a
    slate that failed for another reason.
    """
    graded = [score for score in scores if score.verdict is not None]
    matrix: dict[str, Any] = {"graded": len(graded), "by_verdict": {}}
    for verdict in VERDICTS:
        bucket = [score for score in graded if score.verdict == verdict]
        answerable = [score for score in bucket if score.answerable]
        matrix["by_verdict"][verdict] = {
            "answerable": len(answerable),
            "no_answer": len(bucket) - len(answerable),
            "answerable_with_a_relevant_hit": sum(
                1 for score in answerable if score.relevant_found
            ),
        }
    return matrix


def aggregate(scores: Sequence[QuestionScore]) -> dict[str, Any]:
    """Aggregate a mode's per-question scores, with explicit denominators."""
    answerable = [score for score in scores if score.answerable]
    no_answer = [score for score in scores if not score.answerable]
    answered = [score for score in answerable if score.returned]
    band = confidence_band(scores)

    returned_total = sum(score.returned for score in scores)
    expected_total = sum(score.relevant_expected for score in answerable)
    found_total = sum(score.relevant_found for score in answerable)

    trapped = [score for score in scores if score.recency_trap_top1 is not None]
    no_answer_returning = [score for score in no_answer if score.returned]
    confident = [
        score
        for score in no_answer_returning
        if band is not None and score.top_score is not None and score.top_score >= band
    ]

    return {
        "questions": len(scores),
        "answerable_questions": len(answerable),
        "no_answer_questions": len(no_answer),
        "results_delivered": returned_total,
        "relevant": {
            "expected": expected_total,
            "found": found_total,
            "recall_at_k": _rate(found_total, expected_total),
            "questions_with_a_relevant_hit": sum(
                1 for score in answerable if score.relevant_found
            ),
            "questions_with_no_relevant_hit": sum(
                1 for score in answerable if not score.relevant_found
            ),
            "top1_relevant": sum(1 for score in answered if score.top1_relevant),
            "top1_denominator": len(answered),
            "top1_accuracy": _rate(
                sum(1 for score in answered if score.top1_relevant), len(answered)
            ),
        },
        "irrelevant_injections": {
            "results": sum(score.irrelevant_injections for score in scores),
            "result_denominator": returned_total,
            "rate": _rate(
                sum(score.irrelevant_injections for score in scores), returned_total
            ),
            "questions_affected": sum(
                1 for score in scores if score.irrelevant_injections
            ),
        },
        "footer_only": {
            "results": sum(score.footer_only_hits for score in scores),
            "result_denominator": returned_total,
            "rate": _rate(sum(score.footer_only_hits for score in scores), returned_total),
            "questions_affected": sum(1 for score in scores if score.footer_only_hits),
        },
        "redundant": {
            "results": sum(score.redundant_results for score in scores),
            "result_denominator": returned_total,
            "rate": _rate(sum(score.redundant_results for score in scores), returned_total),
            "questions_affected": sum(1 for score in scores if score.redundant_results),
        },
        "same_file_repeats": {
            "results": sum(score.same_file_repeats for score in scores),
            "result_denominator": returned_total,
            "rate": _rate(
                sum(score.same_file_repeats for score in scores), returned_total
            ),
            "questions_affected": sum(1 for score in scores if score.same_file_repeats),
        },
        "project_isolation": {
            "violations": sum(score.isolation_violations or 0 for score in answerable),
            "result_denominator": sum(score.returned for score in answerable),
            "rate": _rate(
                sum(score.isolation_violations or 0 for score in answerable),
                sum(score.returned for score in answerable),
            ),
            "questions_affected": sum(
                1 for score in answerable if score.isolation_violations
            ),
        },
        "abstention": {
            "no_answer_questions": len(no_answer),
            "abstained": sum(1 for score in no_answer if score.abstained),
            "abstention_rate": _rate(
                sum(1 for score in no_answer if score.abstained), len(no_answer)
            ),
            "returned_any": len(no_answer_returning),
            "confidence_band": band,
            "band_distinct_values": len(
                {
                    score.top_score
                    for score in answerable
                    if score.top1_relevant and score.top_score is not None
                }
            ),
            "confident_match": len(confident),
            "confident_match_rate": _rate(len(confident), len(no_answer)),
        },
        "confidence_verdict": verdict_matrix(scores),
        "recency_trap": {
            "questions_with_a_trap": len(trapped),
            "trap_ranked_first": sum(1 for score in trapped if score.recency_trap_top1),
            "rate": _rate(
                sum(1 for score in trapped if score.recency_trap_top1), len(trapped)
            ),
            "trap_results": sum(score.trap_hits for score in scores),
        },
        "context_tokens": {
            "payload": sum(score.payload_tokens for score in scores),
            "useful": sum(score.useful_tokens for score in scores),
            "useful_fraction": _rate(
                sum(score.useful_tokens for score in scores),
                sum(score.payload_tokens for score in scores),
            ),
            "payload_per_question": _rate(
                sum(score.payload_tokens for score in scores), len(scores)
            ),
        },
        "unresolved_hits": sum(score.unresolved_hits for score in scores),
    }


def aggregate_by(
    scores: Sequence[QuestionScore], key: str
) -> dict[str, dict[str, Any]]:
    """Aggregate separately for each value of ``cls`` or ``variant``."""
    buckets: dict[str, list[QuestionScore]] = {}
    for score in scores:
        buckets.setdefault(getattr(score, key), []).append(score)
    order = CLASSES if key == "cls" else sorted(buckets)
    return {
        value: aggregate(buckets[value]) for value in order if value in buckets
    }


def percentile(samples: Iterable[float], pct: float) -> float | None:
    """Nearest-rank percentile, matching ``bench.harness._percentile``."""
    ordered = sorted(samples)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0, min(len(ordered) - 1, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[rank]


__all__ = [
    "VERDICTS",
    "Hit",
    "QuestionScore",
    "aggregate",
    "aggregate_by",
    "confidence_band",
    "content_tokens",
    "is_footer_only",
    "percentile",
    "score_question",
    "verdict_matrix",
]

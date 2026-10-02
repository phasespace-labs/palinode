"""Fast, deterministic tests for the relevance/abstention fixture and scorer.

Nothing here needs a model, a network or a store: the fixture is YAML, the
scorer is pure, and the one pipeline assertion uses the real markdown parser on
a rendered record. These run in CI on every PR.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy

import pytest

from bench.relevance import corpus as corpus_mod
from bench.relevance import report, scoring
from bench.relevance.corpus import Question, Record

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(
    REPO_ROOT,
    "bench",
    "results",
    "relevance-abstention-baseline-2026-09-20",
    "results.json",
)


@pytest.fixture(scope="module")
def fixture():
    return corpus_mod.load_fixture()


# ── fixture schema ───────────────────────────────────────────────────────────


def test_shipped_fixture_loads_and_validates(fixture):
    # load_fixture validates; this pins the versions the numbers belong to.
    assert fixture.corpus_version == corpus_mod.CORPUS_VERSION
    assert fixture.question_set_version == corpus_mod.QUESTION_SET_VERSION
    assert fixture.records
    assert fixture.questions


def test_every_expected_ref_exists_in_the_corpus(fixture):
    ids = set(fixture.by_id)
    for question in fixture.questions:
        for ref in (*question.relevant, *question.tolerated, *question.must_not_be_top):
            assert ref in ids, f"{question.id} names missing record {ref}"


def test_class_counts_meet_the_minimums(fixture):
    counts = corpus_mod.class_counts(fixture)
    assert set(counts) == set(corpus_mod.CLASSES)
    for cls in corpus_mod.CLASSES:
        assert counts[cls]["base"] >= corpus_mod.MIN_BASE_PER_CLASS
    total_base = sum(counts[cls]["base"] for cls in corpus_mod.CLASSES)
    assert total_base >= corpus_mod.MIN_BASE_QUESTIONS

    variants = corpus_mod.variant_counts(fixture)
    assert variants["paraphrase"] >= corpus_mod.MIN_PARAPHRASES
    assert variants["identifier"] >= corpus_mod.MIN_IDENTIFIERS


def test_no_answer_cases_carry_no_expected_refs(fixture):
    no_answer = [q for q in fixture.questions if q.cls == "no_answer"]
    assert no_answer
    for question in no_answer:
        assert question.relevant == ()
        assert not question.answerable


def test_answerable_cases_all_carry_expected_refs(fixture):
    for question in fixture.questions:
        if question.cls != "no_answer":
            assert question.relevant, f"{question.id} has no expected refs"


def test_corpus_carries_every_required_noise_role(fixture):
    counts = corpus_mod.role_counts(fixture)
    for role in sorted(corpus_mod.REQUIRED_ROLES):
        assert counts[role] > 0, f"no record with role {role}"


def test_held_out_variants_derive_from_a_base_question(fixture):
    base_ids = {q.id for q in fixture.questions if q.variant == "base"}
    derived = [q for q in fixture.questions if q.variant != "base" and q.of]
    assert derived
    for question in derived:
        assert question.of in base_ids


def test_identifier_variants_are_bare_identifiers(fixture):
    identifiers = [q for q in fixture.questions if q.variant == "identifier"]
    assert identifiers
    for question in identifiers:
        # An identifier query is a token, not a sentence.
        assert len(question.ask.split()) == 1, question.id


def test_validation_rejects_a_no_answer_case_with_expected_refs(fixture):
    broken = deepcopy(fixture)
    questions = list(broken.questions)
    target = next(i for i, q in enumerate(questions) if q.cls == "no_answer")
    questions[target] = Question(
        id=questions[target].id,
        cls="no_answer",
        variant="base",
        project=questions[target].project,
        ask=questions[target].ask,
        relevant=(fixture.records[0].id,),
    )
    broken = type(broken)(
        corpus_version=broken.corpus_version,
        question_set_version=broken.question_set_version,
        records=broken.records,
        questions=tuple(questions),
        projects=broken.projects,
    )
    with pytest.raises(ValueError, match="no-answer case must have no relevant refs"):
        corpus_mod.validate(broken)


def test_validation_rejects_an_unknown_expected_ref(fixture):
    broken_questions = list(fixture.questions)
    broken_questions[0] = Question(
        id=broken_questions[0].id,
        cls=broken_questions[0].cls,
        variant=broken_questions[0].variant,
        project=broken_questions[0].project,
        ask=broken_questions[0].ask,
        relevant=("no-such-record",),
    )
    broken = type(fixture)(
        corpus_version=fixture.corpus_version,
        question_set_version=fixture.question_set_version,
        records=fixture.records,
        questions=tuple(broken_questions),
        projects=fixture.projects,
    )
    with pytest.raises(ValueError, match="is not a record"):
        corpus_mod.validate(broken)


# ── rendering and materialization ────────────────────────────────────────────


def test_footer_records_render_the_auto_footer_marker(fixture):
    with_entities = [record for record in fixture.records if record.entities]
    assert with_entities
    for record in with_entities:
        body = corpus_mod.render_body(record)
        assert corpus_mod.AUTO_FOOTER_MARKER in body
        assert "## See also" in body


def test_a_sectioned_record_gives_its_footer_its_own_chunk(fixture):
    """The footer-only hit has to be reachable, not hypothetical.

    Uses the real parser: a body over the 2000-char threshold splits on H2, so
    the trailing ``## See also`` block becomes a chunk whose entire content is
    the footer.
    """
    from palinode.core.parser import parse_markdown

    record = next(
        record for record in fixture.records if record.sections and record.entities
    )
    _, sections = parse_markdown(corpus_mod.render_file(record))
    footer_sections = [
        section
        for section in sections
        if corpus_mod.AUTO_FOOTER_MARKER in section["content"]
    ]
    assert len(footer_sections) == 1
    footer = footer_sections[0]
    assert footer["section_id"] == "see-also"
    assert footer["content"].lstrip().startswith("## See also")


def test_materialize_writes_one_file_per_record(tmp_path, fixture):
    paths = corpus_mod.materialize(fixture, str(tmp_path))
    assert len(paths) == len(fixture.records)
    assert len(set(paths.values())) == len(fixture.records)
    for record_id, path in paths.items():
        assert os.path.isfile(path)
        text = open(path, encoding="utf-8").read()
        assert text.startswith("---\n")
        assert f"id: {record_id}" in text


def test_materialize_is_byte_stable(tmp_path, fixture):
    first = tmp_path / "a"
    second = tmp_path / "b"
    corpus_mod.materialize(fixture, str(first))
    corpus_mod.materialize(fixture, str(second))
    for record in fixture.records:
        assert (first / record.rel_path).read_bytes() == (
            second / record.rel_path
        ).read_bytes()


# ── footer-only detection ────────────────────────────────────────────────────


_FOOTER_CHUNK = (
    "## See also\n"
    f"{corpus_mod.AUTO_FOOTER_MARKER}\n"
    "- [[harborlight-release-4-2-0]]\n"
    "- [[nadia-okonkwo]]\n"
)


def test_footer_only_hit_is_detected():
    assert scoring.is_footer_only(_FOOTER_CHUNK, "What is the current Harborlight release?")


def test_a_body_match_is_not_a_footer_only_hit():
    content = (
        "# Harborlight 4.2.0 is the current release\n\n"
        "Harborlight 4.2.0 was cut in May and is the current release.\n\n"
        + _FOOTER_CHUNK
    )
    assert not scoring.is_footer_only(content, "What is the current Harborlight release?")


def test_a_chunk_with_no_footer_is_never_footer_only():
    assert not scoring.is_footer_only("Harborlight release notes", "Harborlight release")


def test_footer_that_shares_no_term_with_the_query_is_not_a_footer_hit():
    assert not scoring.is_footer_only(_FOOTER_CHUNK, "Tidewater settlement window")


def test_content_tokens_drop_function_words_and_tiny_numbers():
    assert scoring.content_tokens("What is the HL-4.2.0 release?") == {"hl", "release"}


# ── scorer arithmetic on a hand-built slate ──────────────────────────────────


def _record(record_id: str, project: str = "alpha", dup_group: str | None = None):
    return Record(
        id=record_id,
        project=project,
        category="decisions",
        type="Decision",
        role="current_state",
        date="2026-01-01",
        title=record_id,
        body="body",
        dup_group=dup_group,
    )


def _hit(rank: int, record_id: str, *, content: str = "body", score: float = 1.0):
    return scoring.Hit(
        rank=rank,
        record_id=record_id,
        section_id="root",
        content=content,
        score=score,
        file_path=f"/tmp/{record_id}.md",
    )


def test_score_question_counts_every_axis_on_a_tiny_case():
    records = {
        "good": _record("good"),
        "dup": _record("dup", dup_group="g"),
        "dup2": _record("dup2", dup_group="g"),
        "other": _record("other", project="beta"),
    }
    question = Question(
        id="q1",
        cls="release_state",
        variant="base",
        project="alpha",
        ask="What is the current Harborlight release?",
        relevant=("good",),
        tolerated=("dup",),
        must_not_be_top=("dup2",),
    )
    hits = [
        _hit(1, "dup2", score=1.0),
        _hit(2, "good", score=0.9),
        _hit(3, "dup", score=0.8),
        _hit(4, "other", score=0.7),
        _hit(5, None, content=_FOOTER_CHUNK, score=0.6),
    ]

    score = scoring.score_question(
        question, hits, records, payload_tokens=100, useful_tokens=20
    )

    assert score.returned == 5
    assert score.relevant_expected == 1
    assert score.relevant_found == 1
    assert score.recall_at_k == 1.0
    assert score.first_relevant_rank == 2
    assert score.top1_relevant is False
    # dup2, other and the unresolved footer chunk; `dup` is tolerated.
    assert score.irrelevant_injections == 3
    assert score.footer_only_hits == 1
    assert score.redundant_results == 1          # dup after dup2, same dup_group
    assert score.same_file_repeats == 0
    assert score.isolation_violations == 1       # `other` belongs to beta
    assert score.unresolved_hits == 1
    assert score.recency_trap_top1 is True
    assert score.trap_hits == 1
    assert score.abstained is None
    assert score.top_score == 1.0
    assert score.payload_tokens == 100
    assert score.useful_tokens == 20


def test_same_file_repeats_counts_second_and_later_chunks_of_one_file():
    records = {"good": _record("good")}
    question = Question(
        id="q1",
        cls="release_state",
        variant="base",
        project="alpha",
        ask="release",
        relevant=("good",),
    )
    hits = [_hit(1, "good"), _hit(2, "good"), _hit(3, "good")]
    score = scoring.score_question(
        question, hits, records, payload_tokens=10, useful_tokens=10
    )
    assert score.same_file_repeats == 2
    assert score.redundant_results == 0  # no dup_group declared


def test_no_answer_question_abstains_and_has_no_isolation_denominator():
    question = Question(
        id="n1",
        cls="no_answer",
        variant="base",
        project="alpha",
        ask="Which region hosts the control plane?",
    )
    score = scoring.score_question(question, [], {}, payload_tokens=4, useful_tokens=0)
    assert score.abstained is True
    assert score.answerable is False
    assert score.recall_at_k is None
    assert score.isolation_violations is None
    assert score.top1_relevant is None


def test_no_answer_question_that_returns_something_is_all_injection():
    records = {"other": _record("other")}
    question = Question(
        id="n1",
        cls="no_answer",
        variant="base",
        project="alpha",
        ask="Which region hosts the control plane?",
    )
    hits = [_hit(1, "other"), _hit(2, "other")]
    score = scoring.score_question(
        question, hits, records, payload_tokens=50, useful_tokens=0
    )
    assert score.abstained is False
    assert score.irrelevant_injections == 2
    assert score.isolation_violations is None


def test_aggregate_arithmetic_and_denominators():
    records = {"good": _record("good"), "other": _record("other", project="beta")}
    answerable = Question(
        id="a1",
        cls="release_state",
        variant="base",
        project="alpha",
        ask="release",
        relevant=("good",),
    )
    missed = Question(
        id="a2",
        cls="release_state",
        variant="base",
        project="alpha",
        ask="release again",
        relevant=("good",),
    )
    absent = Question(
        id="n1", cls="no_answer", variant="base", project="alpha", ask="payroll"
    )

    scores = [
        scoring.score_question(
            answerable,
            [_hit(1, "good", score=0.9)],
            records,
            payload_tokens=100,
            useful_tokens=100,
        ),
        scoring.score_question(
            missed,
            [_hit(1, "other", score=0.5)],
            records,
            payload_tokens=100,
            useful_tokens=0,
        ),
        scoring.score_question(
            absent,
            [_hit(1, "other", score=0.95)],
            records,
            payload_tokens=100,
            useful_tokens=0,
        ),
    ]
    summary = scoring.aggregate(scores)

    assert summary["questions"] == 3
    assert summary["answerable_questions"] == 2
    assert summary["no_answer_questions"] == 1
    assert summary["results_delivered"] == 3
    assert summary["relevant"] == {
        "expected": 2,
        "found": 1,
        "recall_at_k": 0.5,
        "questions_with_a_relevant_hit": 1,
        "questions_with_no_relevant_hit": 1,
        "top1_relevant": 1,
        "top1_denominator": 2,
        "top1_accuracy": 0.5,
    }
    # `other` is an injection for both a2 and n1.
    assert summary["irrelevant_injections"]["results"] == 2
    assert summary["irrelevant_injections"]["result_denominator"] == 3
    # Isolation is measured over answerable questions only: 1 violation of 2
    # answerable results.
    assert summary["project_isolation"] == {
        "violations": 1,
        "result_denominator": 2,
        "rate": 0.5,
        "questions_affected": 1,
    }
    assert summary["abstention"]["abstained"] == 0
    assert summary["abstention"]["abstention_rate"] == 0.0
    assert summary["abstention"]["confidence_band"] == 0.9
    assert summary["abstention"]["band_distinct_values"] == 1
    # 0.95 clears the 0.9 band.
    assert summary["abstention"]["confident_match"] == 1
    assert summary["context_tokens"] == {
        "payload": 300,
        "useful": 100,
        "useful_fraction": 100 / 300,
        "payload_per_question": 100.0,
    }


def test_confidence_band_is_none_without_a_correct_top1():
    records = {"other": _record("other")}
    question = Question(
        id="a1",
        cls="release_state",
        variant="base",
        project="alpha",
        ask="release",
        relevant=("good",),
    )
    score = scoring.score_question(
        question, [_hit(1, "other")], records, payload_tokens=1, useful_tokens=0
    )
    summary = scoring.aggregate([score])
    assert summary["abstention"]["confidence_band"] is None
    assert summary["abstention"]["confident_match"] == 0


def test_percentile_is_nearest_rank():
    assert scoring.percentile([], 50) is None
    assert scoring.percentile([5.0], 95) == 5.0
    # Nearest rank over len-1 intervals, matching bench.harness._percentile:
    # p50 of four samples is index round(0.5 * 3) == 2.
    assert scoring.percentile([1.0, 2.0, 3.0, 4.0], 50) == 3.0
    assert scoring.percentile([1.0, 2.0, 3.0], 50) == 2.0
    assert scoring.percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0


def test_aggregate_by_class_keeps_the_declared_class_order():
    records = {"good": _record("good")}
    questions = [
        Question(
            id="a1",
            cls="release_state",
            variant="base",
            project="alpha",
            ask="release",
            relevant=("good",),
        ),
        Question(
            id="n1", cls="no_answer", variant="base", project="alpha", ask="payroll"
        ),
    ]
    scores = [
        scoring.score_question(q, [], records, payload_tokens=1, useful_tokens=0)
        for q in questions
    ]
    buckets = scoring.aggregate_by(scores, "cls")
    assert list(buckets) == ["release_state", "no_answer"]


# ── the recorded baseline ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def baseline():
    with open(BASELINE, encoding="utf-8") as handle:
        return json.load(handle)


def test_recorded_baseline_changed_no_production_defaults(baseline):
    assert baseline["parameters"]["production_defaults_changed"] is False
    assert baseline["parameters"]["thresholds"] == [0.4, 0.5]
    assert baseline["parameters"]["corpus_version"] == corpus_mod.CORPUS_VERSION
    assert (
        baseline["parameters"]["question_set_version"]
        == corpus_mod.QUESTION_SET_VERSION
    )


def test_recorded_baseline_scores_every_question_in_every_arm_that_ran(
    baseline, fixture
):
    ran = [arm for arm in baseline["arms"] if arm["status"] == "ran"]
    assert ran
    for arm in ran:
        assert len(arm["observations"]) == len(fixture.questions)
        assert arm["summary"]["questions"] == len(fixture.questions)


def test_report_renders_the_recorded_baseline(baseline):
    rendered = report.render(baseline)
    assert rendered.startswith("# Palinode relevance & abstention baseline")
    assert "lexical (keyword-only)" in rendered
    assert "NOT RUN" not in rendered


def test_report_names_an_arm_that_did_not_run():
    with open(BASELINE, encoding="utf-8") as handle:
        results = json.load(handle)
    for arm in results["arms"]:
        if arm["mode"] == "hybrid":
            arm.clear()
            arm.update(
                {
                    "mode": "hybrid",
                    "threshold": 0.4,
                    "status": "not_run",
                    "reason": "no embedding endpoint reachable",
                }
            )
    rendered = report.render(results)
    assert "**NOT RUN**: no embedding endpoint reachable" in rendered

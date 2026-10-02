"""An explicit no-confident-match signal, from the pre-fusion arm scores.

The measured failure: a store with no answer still hands back a full slate,
every correct top-1 carries a fused 1.0, and a caller conditioning on the
delivered score learns nothing — so the delivery cannot say "I do not hold
this". :mod:`palinode.core.confidence` decides that from each arm's own
pre-fusion score instead, and every surface carries the verdict.

Real SQLite under ``tmp_path`` throughout; vectors are hand-authored so a
cosine band is exact rather than approximate (the same contract-fixture
convention the lexical-mode tests use). The MCP handler is exercised through
the captured-POST seam, as elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.core import confidence, store
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from palinode.core.scoring import describe_diagnostics, describe_no_confident_match
from palinode.indexer import reconcile

_DIM = 1024

#: Marker phrases. A record's marker is long; a query's is short. The
#: synthetic embedder splits on length because the identifier case needs the
#: same token in both a record and a query pointing at different vectors.
_QUERY_LEN = 60

RECORD_ID = "Ledger rollout ticket PLNC-4821 closed the retention change for the audit trail."
RECORD_NEAR = "The batch window moved to the small hours after the throughput review last spring."
RECORD_FAR = "Office bicycles are stored behind the goods lift and tagged once a year."

Q_BOTH = "batch window throughput review"
Q_ID = "PLNC-4821"
Q_NEAR = "when does the overnight job run"
Q_FAR = "sonar calibration policy"

#: Cosine each query has with the record it is nearest. 0.95 is above the
#: vector arm's confident mark, 0.55 between the two marks, 0.45 below the
#: weak mark and still above the MCP floor (0.40) — so the last one is
#: delivered and must still be reported as no confident match.
#:
#: ``Q_BOTH`` and ``Q_ID`` share the 0.95 band and differ only in what the
#: *keyword* arm makes of them: a four-term query against the record's own
#: words, and a single identifier token. Both clear the keyword marks now that
#: the arm is priced per query; the identifier one did not while it was priced
#: against a constant, which is what made corroboration expensive on a small
#: store.
_BANDS = {Q_BOTH: (2, 0.95), Q_ID: (1, 0.95), Q_NEAR: (2, 0.55), Q_FAR: (3, 0.45)}
_RECORD_AXES = {RECORD_ID: 1, RECORD_NEAR: 2, RECORD_FAR: 3}

#: Records that answer nothing here. They exist so the store has a corpus to
#: draw IDF from at all: with two or three records FTS5 rates every term as
#: common (``IDF <= 0`` at ``df >= N/2``), and the keyword arm can only report
#: coverage, never rarity.
FILLER = (
    "Standup moved to nine forty in the morning after the timezone complaint.",
    "Printer toner orders go through the facilities portal, not procurement.",
    "The staging database is restored from a nightly dump at three in the morning.",
    "Contractor laptops are wiped and returned within five working days each time.",
    "Design review notes live in the shared drive under quarterly planning.",
    "Fire drill attendance is recorded by the floor warden every single spring.",
    "Expense claims over two hundred need a second approver in the finance tool.",
    "Meeting rooms can be booked a fortnight ahead of the day, and no longer.",
)


def _unit(pairs: dict[int, float]) -> list[float]:
    vec = [0.0] * _DIM
    for axis, value in pairs.items():
        vec[axis] = value
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _axis(index: int) -> list[float]:
    return _unit({index: 1.0})


def _tilted(axis: int, cosine: float) -> list[float]:
    """A unit vector whose cosine with ``_axis(axis)`` is exactly *cosine*."""
    return _unit({axis: cosine, 0: math.sqrt(max(0.0, 1.0 - cosine * cosine))})


def _embed(text: str, backend: str = "local") -> list[float]:
    if len(text) < _QUERY_LEN:
        for marker, (axis, cosine) in _BANDS.items():
            if marker in text:
                return _tilted(axis, cosine)
        # An unnamed query sits on an axis no record uses: cosine 0 with
        # everything, so "the vector arm found nothing" is real rather than an
        # artefact of two unknowns sharing a default vector.
        return _axis(10)
    digest = hashlib.sha256(text.encode()).hexdigest()
    for marker, axis in _RECORD_AXES.items():
        if marker[:40] in text:
            return _axis(axis)
    return _axis(64 + int(digest[:4], 16) % 512)


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_embed):
        _write(tmp_path, "decisions/rollout.md", RECORD_ID)
        _write(tmp_path, "decisions/batch.md", RECORD_NEAR)
        _write(tmp_path, "insights/bicycles.md", RECORD_FAR)
        for index, body in enumerate(FILLER):
            _write(tmp_path, f"insights/filler-{index}.md", body)
        yield tmp_path


def _write(root: Path, rel: str, body: str, **meta) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump(meta or {"status": "active"}, sort_keys=False)
    content = f"---\n{front}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    assert reconcile.reconcile(str(path), content).committed
    return str(path)


@pytest.fixture()
def client(mem):
    with TestClient(app) as c:
        yield c


def _search(client, query: str, **kw) -> dict:
    body = {"query": query, "receipt": True, "threshold": config.search.mcp_threshold, **kw}
    response = client.post("/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _retrieval(payload: dict) -> dict:
    return payload["receipt"]["retrieval"]


# ── the verdict itself ───────────────────────────────────────────────────────


def test_the_synthetic_embedder_can_tell_a_record_from_a_query():
    """The fixture's own invariant, asserted rather than assumed.

    ``_embed`` splits on length, so a record body that drifts under the cut
    would silently embed as an unnamed *query* and collide with every other
    one — which is a fixture that quietly stops testing what it says.
    """
    for body in (RECORD_ID, RECORD_NEAR, RECORD_FAR, *FILLER):
        assert len(body) >= _QUERY_LEN, body
    for query in (Q_BOTH, Q_ID, Q_NEAR, Q_FAR):
        assert len(query) < _QUERY_LEN, query


class TestAssess:
    """The rule, on rows alone: pure, so every band is stated exactly."""

    def test_an_arm_at_its_confident_mark_is_confident(self):
        rows = [{"raw_score": confidence.VECTOR_CONFIDENT,
                 "keyword_score": confidence.KEYWORD_WEAK}]
        assert confidence.assess(rows, active_mode="hybrid")["confidence"] == "confident"

    def test_between_the_marks_is_weak(self):
        rows = [{"raw_score": confidence.VECTOR_CONFIDENT - 0.01, "keyword_score": 0.0}]
        assert confidence.assess(rows, active_mode="hybrid")["confidence"] == "weak"

    def test_below_the_weak_mark_is_none_even_with_rows(self):
        rows = [{"raw_score": confidence.VECTOR_WEAK - 0.01, "keyword_score": 0.01}]
        assert confidence.assess(rows, active_mode="hybrid")["confidence"] == "none"

    def test_an_empty_slate_is_none(self):
        assert confidence.assess([], active_mode="lexical")["confidence"] == "none"

    def test_one_arm_vouching_is_enough(self):
        rows = [{"raw_score": 0.0, "keyword_score": confidence.KEYWORD_CONFIDENT}]
        assert confidence.assess(rows, active_mode="hybrid")["confidence"] == "confident"

    def test_an_arm_with_no_evidence_reports_null_rather_than_zero(self):
        arms = confidence.assess(
            [{"raw_score": None, "keyword_score": 0.5}], active_mode="lexical"
        )["arms"]
        assert arms["vector"]["best"] is None and arms["vector"]["verdict"] == "none"
        assert arms["keyword"]["best"] == 0.5

    def test_recency_ranked_nothing_so_it_gets_no_verdict(self):
        assert confidence.assess([{"raw_score": 0.9}], active_mode="recency") is None


class TestCorroboration:
    """A vector-only enthusiasm is not confidence, where a second arm ran.

    Measured on the packaged fixture with real bge-m3: the three questions
    with no answer in the corpus that the vector arm called confident are all
    queries the keyword arm barely registers (best normalized BM25 0.000,
    0.000, 0.106), while every answerable question in the same cosine band
    clears its weak mark.
    """

    #: The shape of a confabulation: a strong cosine over a query whose terms
    #: are nowhere in the corpus.
    _UNCORROBORATED = [{"raw_score": 0.635, "keyword_score": 0.106}]

    def test_the_vector_arm_may_not_vouch_alone_when_both_arms_ran(self):
        value = confidence.assess(self._UNCORROBORATED, active_mode="hybrid")
        assert value["confidence"] == "weak"
        assert value["corroboration"] == "missing"
        # The arm still reports what it saw: the demotion is the delivery's,
        # not a rewrite of the evidence.
        assert value["arms"]["vector"]["verdict"] == "confident"

    def test_a_keyword_arm_at_its_weak_mark_is_corroboration_enough(self):
        rows = [{"raw_score": 0.635, "keyword_score": confidence.KEYWORD_WEAK}]
        value = confidence.assess(rows, active_mode="hybrid")
        assert value["confidence"] == "confident"
        assert "corroboration" not in value

    def test_an_arm_that_never_ran_is_not_asked_to_dissent(self):
        # `vector` = the caller's own hybrid=false: there is no keyword arm to
        # corroborate with, so the vector arm decides alone, as it always has.
        value = confidence.assess(self._UNCORROBORATED, active_mode="vector")
        assert value["confidence"] == "confident"
        assert "corroboration" not in value

    def test_the_keyword_arm_needs_no_corroboration(self):
        # It produced no confident wrong answer in the measurement, and
        # requiring it would make `confident` unreachable in lexical mode.
        rows = [{"raw_score": None, "keyword_score": confidence.KEYWORD_CONFIDENT}]
        for mode in ("lexical", "hybrid"):
            assert confidence.assess(rows, active_mode=mode)["confidence"] == "confident"

    def test_the_diagnostics_line_says_why_the_verdict_is_below_the_arm(self):
        value = confidence.assess(self._UNCORROBORATED, active_mode="hybrid")
        line = describe_diagnostics(
            {"active_mode": "hybrid", "index_state": "ready", "outcome": "matched", **value}
        )
        assert "match confidence: weak" in line
        assert "uncorroborated" in line


# ── end to end, through a real store ─────────────────────────────────────────


def test_a_hit_both_arms_vouch_for_is_confident(client):
    payload = _search(client, Q_BOTH)
    assert [Path(r["rel_path"]).stem for r in payload["results"]] == ["batch"]
    retrieval = _retrieval(payload)
    assert retrieval["confidence"] == "confident"
    assert retrieval["arms"]["vector"]["best"] == pytest.approx(0.95, abs=1e-3)
    assert retrieval["arms"]["keyword"]["best"] >= confidence.KEYWORD_WEAK
    assert "corroboration" not in retrieval


def test_an_exact_identifier_hit_is_confident_on_a_small_store(client):
    """The identifier case, on a store of eleven records.

    Both arms are certain: the vector arm at 0.95, and the keyword arm because
    the chunk holds the whole query. That second half is what the per-query
    rescale bought — against a constant 25 this delivery was ``weak`` with
    ``corroboration: missing``, because a single identifier token could not
    reach the keyword weak mark on a store this size however exact it was, and
    the corroboration rule then held the vector arm back too.
    """
    payload = _search(client, Q_ID)
    assert [Path(r["rel_path"]).stem for r in payload["results"]] == ["rollout"]
    retrieval = _retrieval(payload)
    assert retrieval["arms"]["vector"]["verdict"] == "confident"
    assert retrieval["arms"]["keyword"]["best"] >= confidence.KEYWORD_CONFIDENT
    assert retrieval["confidence"] == "confident"
    assert "corroboration" not in retrieval


def test_a_near_miss_is_weak(client):
    payload = _search(client, Q_NEAR)
    assert _retrieval(payload)["confidence"] == "weak"
    assert _retrieval(payload)["arms"]["vector"]["best"] == pytest.approx(0.55, abs=1e-3)


def test_a_query_with_no_relevant_record_is_none_and_keeps_its_results(client):
    """The measured case: rows come back, and none of them is the answer.

    The rows are *not* withheld. That is the whole default behaviour of this
    change — the caller is told the slate is worth nothing and is still shown
    it, because an empty slate cannot be told apart from an empty store.
    """
    payload = _search(client, Q_FAR)
    assert payload["results"], "the slate must not be suppressed by default"
    assert _retrieval(payload)["confidence"] == "none"
    assert _retrieval(payload)["outcome"] == "matched"
    assert _retrieval(payload)["arms"]["vector"]["best"] == pytest.approx(0.45, abs=1e-3)


def test_nothing_retrieved_is_none_too(client):
    payload = _search(client, "xylophonemissing", threshold=1.0)
    assert payload["results"] == []
    assert _retrieval(payload)["confidence"] == "none"
    assert _retrieval(payload)["outcome"] == "no_match"


def test_the_keyword_arm_reports_its_own_pre_fusion_score(client):
    """Each delivered row carries both arm scores, not just the fused rank."""
    hit = _search(client, "batch window throughput review")["results"][0]
    assert hit["keyword_score"] is not None and hit["keyword_score"] > 0.0
    assert hit["score"] == pytest.approx(1.0), "the fused score is still a rank"


# ── the surfaces ─────────────────────────────────────────────────────────────


def test_the_mcp_rendering_leads_with_the_verdict(client):
    payload = _search(client, Q_FAR)
    rendered = mcp._format_results(payload["results"], receipt=payload["receipt"])
    first = rendered.splitlines()[0]
    assert first.startswith("No confident match.")
    # The weak results are still listed underneath it.
    assert "bicycles" in rendered


def test_a_confident_delivery_gets_no_banner(client):
    payload = _search(client, Q_BOTH)
    rendered = mcp._format_results(payload["results"], receipt=payload["receipt"])
    assert "No confident match" not in rendered
    assert "match confidence: confident" in rendered


def test_every_surface_reads_the_same_verdict(client):
    """MCP text, the shared diagnostics line (CLI + inspector) and the REST
    receipt are three readings of one server-side decision."""
    payload = _search(client, Q_FAR)
    retrieval = _retrieval(payload)
    rendered = mcp._format_results(payload["results"], receipt=payload["receipt"])
    line = describe_diagnostics(retrieval)
    assert "match confidence: none" in line
    assert line in rendered
    assert retrieval["confidence"] == confidence.delivered_verdict(payload["receipt"])


def test_the_banner_names_the_arm_evidence():
    retrieval = {"confidence": "none", "arms": {
        "vector": {"best": 0.44, "verdict": "none", "confident_at": 0.6, "weak_at": 0.5},
        "keyword": {"best": None, "verdict": "none", "confident_at": 0.22, "weak_at": 0.13},
    }}
    banner = describe_no_confident_match(retrieval, delivered=True)
    assert "best vector 0.44" in banner and "confident at 0.60" in banner
    assert "keyword" not in banner, "an arm with no evidence claims nothing"
    assert describe_no_confident_match({"confidence": "weak"}, delivered=True) == ""


def test_a_record_states_its_own_confidence_beside_the_match(client):
    """The record's `confidence` frontmatter is rendered, not buried in metadata.

    A different question from the delivery verdict — how sure the *author*
    was — and it had never reached the surface an agent reads.
    """
    hit = {"file_path": "decisions/x.md", "score": 1.0, "raw_score": 0.9,
           "snippet": "body", "metadata": {"confidence": 0.4, "epistemic": "open_question"}}
    rendered = mcp._format_results([hit])
    assert "[open question?]" in rendered
    assert "[stated confidence 0.40]" in rendered


# ── the retrieval log ────────────────────────────────────────────────────────


def _log_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_log_carries_the_verdict_on_every_row(client, tmp_path, monkeypatch):
    logger = RetrievalLogger(str(tmp_path))
    monkeypatch.setattr("palinode.api.routers.search._retrieval_logger", logger)
    _search(client, Q_FAR)
    rows = _log_rows(logger.log_path)
    assert rows and all(row["confidence"] == "none" for row in rows)


def test_the_empty_delivery_row_carries_it_too(client, tmp_path, monkeypatch):
    logger = RetrievalLogger(str(tmp_path))
    monkeypatch.setattr("palinode.api.routers.search._retrieval_logger", logger)
    _search(client, "xylophonemissing", threshold=1.0)
    rows = _log_rows(logger.log_path)
    assert [row["file_path"] for row in rows] == [""]
    assert rows[0]["confidence"] == "none"


# ── the opt-in switch ────────────────────────────────────────────────────────


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _envelope(verdict: str) -> dict:
    return {
        "results": [{"file_path": "/store/insights/bicycles.md", "rel_path": "insights/bicycles.md",
                     "score": 1.0, "raw_score": 0.45, "keyword_score": None,
                     "snippet": "Office bicycles are stored behind the goods lift.",
                     "metadata": {}}],
        "receipt": {"bundle_id": "b1", "evaluated_at": "2026-09-22T00:00:00+00:00",
                    "retrieval": {"configured_mode": "hybrid", "active_mode": "hybrid",
                                  "index_state": "ready", "outcome": "matched",
                                  "coverage": "visible_indexed_corpus_only",
                                  "confidence": verdict,
                                  "arms": {"vector": {"best": 0.45, "verdict": verdict,
                                                      "confident_at": 0.6, "weak_at": 0.5},
                                           "keyword": {"best": None, "verdict": "none",
                                                       "confident_at": 0.22, "weak_at": 0.13}}}},
    }


@pytest.fixture()
def mcp_search(monkeypatch):
    from palinode.core.context_prime import ProjectResolution

    async def _fake_post(path, json=None, timeout=30.0, **kw):
        return _Resp(_fake_post.payload)

    monkeypatch.setattr(mcp, "_post", _fake_post)
    monkeypatch.setattr(mcp, "_resolve_scope", lambda: ProjectResolution(None, "none"))
    return _fake_post


@pytest.mark.asyncio
async def test_the_switch_is_off_by_default(mcp_search):
    mcp_search.payload = _envelope("none")
    assert config.search.abstain_on_no_confident_match is False
    rendered = (await mcp._tool_search({"query": "sonar"}))[0].text
    assert "bicycles" in rendered
    assert rendered.splitlines()[1].startswith("No confident match.")


@pytest.mark.asyncio
async def test_the_switch_on_empties_only_a_none_slate(mcp_search, monkeypatch):
    monkeypatch.setattr(config.search, "abstain_on_no_confident_match", True)

    mcp_search.payload = _envelope("none")
    withheld = (await mcp._tool_search({"query": "sonar"}))[0].text
    assert "bicycles" not in withheld
    assert "1 weak result withheld" in withheld
    assert "No results found." in withheld
    # An abstention must not read as an empty store.
    assert "the store was searched and is not empty" in withheld

    mcp_search.payload = _envelope("weak")
    assert "bicycles" in (await mcp._tool_search({"query": "batch"}))[0].text
    mcp_search.payload = _envelope("confident")
    assert "bicycles" in (await mcp._tool_search({"query": "PLNC-4821"}))[0].text

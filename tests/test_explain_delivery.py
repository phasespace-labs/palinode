"""Explaining one delivery — what was supplied, and what is honestly unknowable.

Everything here runs against a real store on ``tmp_path``: real SQLite, real
markdown files, real ``.audit/retrievals.jsonl`` written by the real delivery
path. Nothing is mocked but the embedder, which is replaced by a deterministic
bag-of-words vector so the tests need no Ollama.

The assertions that matter are the honesty ones. A field the log never recorded
must be an explicit ``unavailable`` with a reason, never a guess and never a
silently missing key; "no rows carry this reference" must be distinguishable
from "this surface writes no rows"; a record the caller cannot see must be
counted and not named; and supplied context must never be rendered as evidence
that an agent acted on it.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.core.explain import (
    INSTRUMENTATION_DISABLED,
    LOG_ABSENT,
    NON_LOGGING_SURFACES,
    NOT_RECORDED,
    NO_MATCHING_ROWS,
    PREDATES_RECEIPTS,
    SURFACE_WRITES_NO_ROWS,
    UNAVAILABLE_REASONS,
    WITHHELD_DIAGNOSTICS_ONLY,
    InvalidBundleId,
    explain_delivery,
    format_explanation_text,
    validate_bundle_id,
)
from palinode.indexer import reconcile

_DIM = 1024


def _bow_embed(text: str, backend: str = "local") -> list[float]:
    vec = [0.0] * _DIM
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest(), 16)
        vec[h % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_bow_embed):
        yield tmp_path


@pytest.fixture()
def logger(mem, monkeypatch):
    """A retrieval logger rooted at this test's store, shared by the API.

    ``palinode.api._util._retrieval_logger`` is built at import time against
    whatever memory dir was configured then, so it is rebound here — the real
    class writing a real file, not a stub.
    """
    from palinode.api import _util
    from palinode.api.routers import search as search_router
    from palinode.core.retrieval_log import RetrievalLogger

    fresh = RetrievalLogger(str(mem), enabled=True)
    monkeypatch.setattr(_util, "_retrieval_logger", fresh)
    monkeypatch.setattr(search_router, "_retrieval_logger", fresh)
    from palinode.api.routers import explain as explain_router

    monkeypatch.setattr(explain_router, "_retrieval_logger", fresh)
    return fresh


@pytest.fixture()
def client(mem, logger):
    with TestClient(app) as c:
        yield c


def _write(mem, rel: str, body: str, **meta) -> None:
    path = mem / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    content = f"---\n{fm}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    assert reconcile.reconcile(str(path), content).committed


def _search(client, query: str, **body) -> tuple[list[dict], dict]:
    payload = {"query": query, "threshold": 0.0, "receipt": True, **body}
    data = client.post("/search", json=payload).json()
    return data["results"], data["receipt"]


def _explain(client, bundle_id: str, **params) -> dict:
    resp = client.get(f"/explain/{bundle_id}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _is_unavailable(value, reason: str | None = None) -> bool:
    if not isinstance(value, dict) or value.get("available") is not False:
        return False
    assert value["reason"] in UNAVAILABLE_REASONS
    assert value["detail"], "an unavailable marker must say why"
    return reason is None or value["reason"] == reason


# ── the delivery is explained from what the delivery recorded ────────────────


def test_search_delivery_is_explained_from_its_own_rows(client, mem):
    """The core case: supply a memory, then ask what that delivery supplied."""
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    results, receipt = _search(client, "primary database Postgres", limit=5)
    assert results

    explanation = _explain(client, receipt["bundle_id"])

    assert explanation["status"] == "explained"
    assert explanation["bundle_id"] == receipt["bundle_id"]
    # The evaluation time is the receipt's own, not a clock read at explain time.
    assert explanation["delivery"]["evaluated_at"] == receipt["evaluated_at"]

    refs = [r["ref"] for r in explanation["supplied"]]
    assert "decisions/db" in refs
    record = next(r for r in explanation["supplied"] if r["ref"] == "decisions/db")
    assert record["disposition"] == "selected"
    assert record["revision_basis"] == "index_section_sha256"
    assert record["revision"], "the exact supplied revision must be reported"
    assert explanation["delivery"]["dispositions"]["selected"] >= 1

    # The selection path is what was recorded, and only that.
    assert explanation["selection_path"]["surface"] == "api_search"
    assert explanation["selection_path"]["demand"] == "explicit"
    assert _is_unavailable(
        explanation["selection_path"]["core_trigger_or_recall"], NOT_RECORDED
    )

    # An empty scope chain is a resolved answer, not a blank: the server
    # resolved no scope identity and applied access control only.
    assert explanation["delivery"]["scope"] == []
    assert "no scope identity (access control only)" in format_explanation_text(explanation)


def test_source_revision_is_the_one_that_was_supplied(client, mem):
    """The reported revision equals the receipt's, not a hash computed now."""
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres", resolve="linked", limit=5)
    supplied = {s["ref"]: s for s in receipt["supplied"]}

    explanation = _explain(client, receipt["bundle_id"])
    for record in explanation["supplied"]:
        assert record["revision"] == supplied[record["ref"]]["revision"]


# ── stale source: the file changed after it was delivered ────────────────────


def test_source_change_since_delivery_is_reported(client, mem):
    """Deliver, then edit the file. The explanation must say the source changed.

    And must say it as a *comparison against the recorded revision*, never by
    re-reading what the delivery would return today.
    """
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres", limit=5)

    before = _explain(client, receipt["bundle_id"])
    record = next(r for r in before["supplied"] if r["ref"] == "decisions/db")
    assert record["source_state"]["status"] == "unchanged"

    _write(mem, "decisions/db.md", "# DB\n\nWe moved the primary database to MySQL.",
           type="Decision")

    after = _explain(client, receipt["bundle_id"])
    record = next(r for r in after["supplied"] if r["ref"] == "decisions/db")
    assert record["source_state"]["status"] == "changed"
    assert record["revision"] == before["supplied"][0]["revision"], (
        "the recorded revision must not move when the file does"
    )
    assert "source changed since" in format_explanation_text(after)


# ── budget cap: a delivery qualifier survives into the human view ────────────


def test_budget_capped_delivery_keeps_its_qualifier(client, mem, monkeypatch):
    """An evidence budget that bites is a coverage qualifier, and it survives.

    The delivery is truncated by a real cap (``search.evidence.max_files``):
    the hit contradicts a record that is not itself in the result window, so
    reaching it costs a file read the budget refuses. The receipt folds that
    into ``coverage``, the log row carries it, and the rendered explanation
    says ``partial`` with the reason named.
    """
    monkeypatch.setattr(config.search.evidence, "max_files", 0)
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", contradicts=["decisions/db-old"])
    _write(mem, "decisions/db-old.md", "# DB old\n\nThe primary database was MySQL.",
           type="Decision")

    _, receipt = _search(client, "primary database Postgres", resolve="linked", limit=1)
    assert receipt["coverage"]["status"] == "partial", receipt["coverage"]
    reasons = receipt["coverage"]["reasons"]
    assert any(r.startswith("budget_exhausted:") for r in reasons), reasons

    explanation = _explain(client, receipt["bundle_id"])
    assert explanation["delivery"]["coverage"]["status"] == "partial"
    assert explanation["delivery"]["coverage"]["reasons"] == reasons

    rendered = format_explanation_text(explanation)
    assert "partial" in rendered
    for reason in reasons:
        assert reason in rendered


def test_resolve_bundle_is_budget_capped_and_explainable(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _write(mem, "decisions/cache.md", "# Cache\n\nWe use Redis for the primary cache.",
           type="Decision")

    bundle = client.post(
        "/resolve", json={"query": "primary database", "max_items": 1}
    ).json()
    assert bundle["receipt_ref"]

    explanation = _explain(client, bundle["receipt_ref"])
    assert explanation["status"] == "explained"
    assert explanation["delivery"]["coverage"] == bundle["coverage"]
    assert explanation["delivery"]["budget"] == bundle["budget"]
    assert [(r["ref"], r["revision"]) for r in explanation["supplied"]] == [
        (r["ref"], r["revision"]) for r in bundle["receipt"]["supplied"]
    ]


# ── an empty delivery is "searched, nothing delivered" ───────────────────────


def test_empty_delivery_is_not_reported_as_no_record(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "kangaroo husbandry statutes", threshold=0.99)

    explanation = _explain(client, receipt["bundle_id"])
    assert explanation["status"] == "none_delivered"
    assert explanation["supplied"] == []
    assert "Searched" in explanation["summary"]
    rendered = format_explanation_text(explanation)
    assert "nothing was delivered" in rendered
    assert "not_found" not in rendered


# ── the unavailable matrix ───────────────────────────────────────────────────


def test_unknown_reference_names_every_candidate_cause(client, mem):
    """"No rows for this id" and "this surface does not log" are different."""
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _search(client, "primary database Postgres")

    explanation = _explain(client, "0" * 16)
    assert explanation["status"] == "not_found"
    assert explanation["log"]["reason"] == NO_MATCHING_ROWS

    by_reason = {c["reason"]: c for c in explanation["log"]["candidates"]}
    assert by_reason[NO_MATCHING_ROWS]["checked"] is True
    assert by_reason[NO_MATCHING_ROWS]["holds"] is True
    assert by_reason[SURFACE_WRITES_NO_ROWS]["checked"] is False
    for surface in NON_LOGGING_SURFACES:
        assert surface in by_reason[SURFACE_WRITES_NO_ROWS]["detail"]
    # The log's own horizon is the evidence for the rotation question.
    assert explanation["log"]["oldest_row"]
    assert "rotate" in explanation["log"]["retention"]


def test_a_missing_reference_keeps_the_same_shape(client, mem):
    """A key that vanishes when there is no answer is a silent omission.

    Every field an explained delivery carries is present on a missing one too,
    each as an explicit marker naming the same reason the lookup failed for.
    """
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres")

    explained = _explain(client, receipt["bundle_id"])
    missing = _explain(client, "0" * 16)
    assert set(missing) == set(explained)
    for key in ("delivery", "selection_path", "query", "evidence_records"):
        assert _is_unavailable(missing[key], NO_MATCHING_ROWS)


def test_instrumentation_disabled_is_reported_as_such(mem, monkeypatch):
    monkeypatch.setenv("PALINODE_INSTRUMENTATION_DISABLED", "1")
    explanation = explain_delivery("a" * 16, memory_dir=str(mem))
    assert explanation["log"]["reason"] == INSTRUMENTATION_DISABLED
    by_reason = {c["reason"]: c for c in explanation["log"]["candidates"]}
    assert by_reason[INSTRUMENTATION_DISABLED]["holds"] is True


def test_missing_log_is_reported_as_absent(mem):
    explanation = explain_delivery("b" * 16, memory_dir=str(mem))
    assert explanation["log"]["reason"] == LOG_ABSENT
    by_reason = {c["reason"]: c for c in explanation["log"]["candidates"]}
    assert by_reason[LOG_ABSENT]["holds"] is True
    assert by_reason[NO_MATCHING_ROWS]["holds"] is False


def test_rows_predating_receipts_are_evidenced(client, mem, logger):
    """A row with no bundle_id is real history, and it is reported as a count."""
    from palinode.core.retrieval_log import RetrievalEvent

    logger.record(RetrievalEvent(
        timestamp="2026-01-01T00:00:00+00:00",
        file_path=str(mem / "decisions/db.md"),
        chunk_id=None, mode="explicit", source="palinode_read",
        query=None, rank=None, score=None, session_id=None,
    ))
    explanation = _explain(client, "c" * 16)
    by_reason = {c["reason"]: c for c in explanation["log"]["candidates"]}
    assert by_reason[PREDATES_RECEIPTS]["holds"] is True
    assert "1 row(s)" in by_reason[PREDATES_RECEIPTS]["detail"]


def test_fields_the_log_never_recorded_are_named_not_dropped(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres")
    explanation = _explain(client, receipt["bundle_id"])

    # The lifecycle clock, the project-resolution source and the evidence
    # records are all things the response knew and the log does not keep.
    assert _is_unavailable(explanation["delivery"]["requested_time"], NOT_RECORDED)
    assert _is_unavailable(explanation["evidence_records"], NOT_RECORDED)
    project = explanation["delivery"]["project"]
    if isinstance(project, dict) and project.get("available") is False:
        assert project["reason"] == NOT_RECORDED
    else:
        assert _is_unavailable(project["resolved_by"], NOT_RECORDED)


# ── the caller's own query is diagnostics-only ───────────────────────────────


def test_query_prose_is_withheld_from_the_public_view(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    query = "primary database Postgres"
    _, receipt = _search(client, query)

    public = _explain(client, receipt["bundle_id"])
    assert _is_unavailable(public["query"], WITHHELD_DIAGNOSTICS_ONLY)
    assert query not in json.dumps(public)

    # Loopback bind (the default in this fixture) — the local operator's case.
    diagnostics = _explain(client, receipt["bundle_id"], view="diagnostics")
    assert diagnostics["query"] == query


def test_the_recorded_session_id_is_never_in_the_public_view(client, mem):
    """The bypass this gate would otherwise have.

    The session-match branch accepts a caller-supplied ``session_id`` equal to
    the delivery's own. If the public view disclosed that value, any stranger
    holding a bundle id could read it off one response and replay it into the
    next — so the session id rides the same gate as the query it protects.
    """
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres", session_id="s-private")

    public = _explain(client, receipt["bundle_id"])
    assert _is_unavailable(public["delivery"]["session_id"], WITHHELD_DIAGNOSTICS_ONLY)
    assert "s-private" not in json.dumps(public)


# ── the diagnostics gate on a network-reachable bind ─────────────────────────


@pytest.fixture()
def remote_bind(monkeypatch):
    """Bind the API where the rest of the network can reach it.

    The threat the gate exists for: several clients against one server, one
    shared token or none, and a bundle id that travelled in a receipt someone
    pasted into an issue.
    """
    monkeypatch.setenv("PALINODE_API_HOST", "0.0.0.0")


def test_remote_caller_holding_a_bundle_id_cannot_read_the_query(
    client, mem, remote_bind
):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    query = "primary database Postgres"
    _, receipt = _search(client, query, session_id="s-owner")

    stranger = _explain(client, receipt["bundle_id"], view="diagnostics")
    assert _is_unavailable(stranger["query"], WITHHELD_DIAGNOSTICS_ONLY)
    assert _is_unavailable(stranger["delivery"]["session_id"], WITHHELD_DIAGNOSTICS_ONLY)
    assert query not in json.dumps(stranger)
    assert "s-owner" not in json.dumps(stranger)

    # A stranger presenting somebody else's guess is refused the same way.
    guessed = _explain(client, receipt["bundle_id"], view="diagnostics",
                       session_id="s-not-the-owner")
    assert _is_unavailable(guessed["query"], WITHHELD_DIAGNOSTICS_ONLY)


def test_remote_caller_reading_back_its_own_delivery_sees_the_query(
    client, mem, remote_bind
):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    query = "primary database Postgres"
    _, receipt = _search(client, query, session_id="s-owner")

    owner = _explain(client, receipt["bundle_id"], view="diagnostics",
                     session_id="s-owner")
    assert owner["query"] == query
    assert owner["delivery"]["session_id"] == "s-owner"


def test_a_delivery_with_no_session_id_is_loopback_only(client, mem, remote_bind):
    """Nothing can match an absent session, so the bind is the only way in."""
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres")

    for attempt in ({}, {"session_id": ""}, {"session_id": "anything"}):
        refused = _explain(client, receipt["bundle_id"], view="diagnostics", **attempt)
        assert _is_unavailable(refused["query"], WITHHELD_DIAGNOSTICS_ONLY)


def test_refusal_is_not_a_status_code_and_leaks_no_more_than_the_public_view(
    client, mem, remote_bind
):
    """A refusal must not become a signal of its own.

    Three properties: the refused request is a 200 carrying the ordinary public
    view (a 403 would answer a question about the bundle the caller has not
    earned); it has the same keys as an unknown reference; and the refusal
    wording is *identical* whether or not the delivery recorded a session id,
    because a message that varied would disclose the very fact being withheld.
    """
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, with_session = _search(client, "primary database Postgres", session_id="s-owner")
    _, without_session = _search(client, "Postgres primary database")

    resp = client.get(f"/explain/{with_session['bundle_id']}",
                      params={"view": "diagnostics"})
    assert resp.status_code == 200
    refused_with = resp.json()
    refused_without = _explain(client, without_session["bundle_id"],
                               view="diagnostics")
    unknown = _explain(client, "0" * 16, view="diagnostics")

    assert refused_with["query"] == refused_without["query"], (
        "the refusal wording must not reveal whether a session id was recorded"
    )
    assert set(refused_with) == set(unknown)
    # And the refused response is otherwise the public view, unchanged.
    public = _explain(client, with_session["bundle_id"])
    assert refused_with["supplied"] == public["supplied"]
    assert refused_with["delivery"]["scope"] == public["delivery"]["scope"]


def test_the_inspector_page_and_the_json_route_share_one_bind_predicate():
    """Not a lookalike test — literally the same function object.

    The page is loopback-guarded and shows the query; the JSON route gates the
    same two fields on the same question. Two predicates that could drift is
    how one surface's refusal becomes another surface's disclosure.
    """
    from palinode.api.routers import explain as explain_router
    from palinode.api.ui import router as ui_router

    assert explain_router.bind_is_loopback is ui_router.bind_is_loopback


def test_ui_delivery_page_is_not_reachable_on_a_remote_bind(client, remote_bind):
    """The page's own guard is what makes its unconditional query display safe."""
    assert client.get("/ui/delivery/" + "a" * 16).status_code == 403


# ── visibility: a record the caller cannot see is counted, not named ─────────


def test_a_record_the_caller_cannot_see_is_redacted(client, mem):
    """Explaining a delivery must not become a way to read what is now hidden.

    The memory is delivered while visible and then marked ``private`` — the
    canonical way, in frontmatter — so the delivery genuinely contained it.
    Visibility is decided on the live file, so the explanation must count it
    and refuse to name it.
    """
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    results, receipt = _search(client, "primary database Postgres")
    assert results

    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", visibility="private")

    explanation = _explain(client, receipt["bundle_id"])
    assert explanation["supplied"] == []
    assert explanation["redacted"]["count"] == 1
    assert "decisions/db" not in json.dumps(explanation)
    assert "private" in explanation["redacted"]["note"]


# ── bounded output ───────────────────────────────────────────────────────────


def test_output_is_capped_and_says_how_much_it_left_out(client, mem):
    for index in range(5):
        _write(mem, f"decisions/db-{index}.md",
               f"# DB {index}\n\nWe use Postgres as the primary database, note {index}.",
               type="Decision")
    results, receipt = _search(client, "primary database Postgres", limit=5)
    assert len(results) == 5

    explanation = _explain(client, receipt["bundle_id"], limit=2)
    assert len(explanation["supplied"]) == 2
    assert explanation["not_shown"]["count"] == 3
    assert explanation["not_shown"]["note"] == "3 more not shown"
    assert "3 more not shown" in format_explanation_text(explanation)


# ── the supplied / acted-on separation ───────────────────────────────────────


def test_supplied_is_never_presented_as_acted_on(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres")
    explanation = _explain(client, receipt["bundle_id"])

    assert explanation["acted_on"]["status"] == "not_captured"
    assert explanation["acted_on"]["gap"] == "G3"
    rendered = format_explanation_text(explanation)
    assert "not yet captured (G3)" in rendered
    assert "records no evidence that an agent" in rendered


def test_the_correction_route_is_a_labelled_pointer_not_a_flow(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres")
    explanation = _explain(client, receipt["bundle_id"])

    correction = explanation["correction"]
    assert correction["status"] == "available"
    assert any("palinode corrections preview" in route for route in correction["routes"])
    assert correction["docs"] == "docs/CORRECTIONS.md"
    assert any("palinode save" in route for route in correction["routes"])
    assert any("palinode archive" in route for route in correction["routes"])
    # And every supplied record offers the path to its own source and history.
    record = explanation["supplied"][0]
    assert record["links"]["memory"] == "/ui/memory/decisions/db"
    assert record["links"]["history"] == "/ui/history/decisions/db"
    assert "palinode trace decisions/db.md" in record["links"]["commands"]


# ── the degraded-retrieval question: there is no deadline fallback ───────────


def test_keyword_fallback_is_not_claimed_by_the_explanation(client, mem, monkeypatch):
    """The search path has an outage fallback, not a deadline one — and the log
    records neither.

    A per-input embed rejection degrades the search to its keyword arm. The
    response envelope reports that (``retrieval.active_mode``); the retrieval
    log has no field for it, so the explanation must not carry a retrieval mode
    at all. Asserting the absence is the point: the failure this guards against
    is an invented field that looks recorded.
    """
    from palinode.core import embedder

    def _reject(text: str, backend: str = "local"):
        raise embedder.EmbeddingInputError("nan vector", text_len=len(text),
                                           ollama_message="nan")

    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    with patch("palinode.core.embedder.embed", side_effect=_reject):
        payload = {"query": "primary database Postgres", "threshold": 0.0,
                   "receipt": True}
        data = client.post("/search", json=payload).json()
    assert data["receipt"]["retrieval"]["active_mode"] == "keyword-fallback"

    explanation = _explain(client, data["receipt"]["bundle_id"])
    assert explanation["status"] == "explained"
    assert "retrieval_mode" not in json.dumps(explanation)
    assert "keyword-fallback" not in json.dumps(explanation)


# ── bundle-id shape ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "../../etc/passwd", "not-hex!", "ab",
                                 "g" * 16, "a" * 65, "decisions/db"])
def test_bundle_ids_that_are_not_opaque_digests_are_refused(bad):
    with pytest.raises(InvalidBundleId):
        validate_bundle_id(bad)


def test_bundle_id_is_case_normalised_and_accepted():
    assert validate_bundle_id("AB12CD34EF567890") == "ab12cd34ef567890"


def test_api_refuses_a_malformed_reference(client):
    assert client.get("/explain/not-a-digest").status_code == 422
    # A path-shaped id never reaches the handler: the route takes no slashes.
    assert client.get("/explain/../../etc/passwd").status_code in (404, 422)


# ── the inspector page ───────────────────────────────────────────────────────


def test_ui_delivery_page_renders_the_explanation(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    _, receipt = _search(client, "primary database Postgres")

    page = client.get(f"/ui/delivery/{receipt['bundle_id']}")
    assert page.status_code == 200
    assert "decisions/db" in page.text
    assert "/ui/history/decisions/db" in page.text
    assert "not yet captured (G3)" in page.text
    assert "no scope identity (access control only)" in page.text
    # The operator's page is the one surface that shows the caller's own words.
    assert "primary database Postgres" in page.text


def test_ui_delivery_page_escapes_hostile_content(client, mem, logger):
    """A memory ref and a recorded query are both attacker-influenced text."""
    from palinode.core.retrieval_log import RetrievalEvent

    hostile_query = '<script>alert("q")</script>'
    hostile_rel = "decisions/<img src=x onerror=alert(1)>.md"
    _write(mem, hostile_rel, "# Hostile\n\nA memory whose filename is markup.",
           type="Decision")
    logger.record(RetrievalEvent(
        timestamp="2026-02-02T00:00:00+00:00",
        file_path=str(mem / hostile_rel),
        chunk_id="root", mode="explicit", source="api_search",
        query=hostile_query, rank=0, score=1.0, session_id=None,
        bundle_id="dead" * 4, policy_version="palinode/test", scope=[],
        revision="deadbeef", revision_basis="index_section_sha256",
        disposition="selected",
    ))

    page = client.get("/ui/delivery/deaddeaddeaddead")
    assert page.status_code == 200
    # No tag is ever opened by attacker-supplied text: the angle brackets that
    # would start one are escaped, in element text and in attribute values
    # alike. The inner `onerror=…` characters survive as literal text, which is
    # exactly what escaping means — they cannot become an attribute without a
    # tag to attach to.
    assert "<script" not in page.text
    assert "<img" not in page.text
    assert "&lt;script&gt;alert" in page.text
    assert "&lt;img src=x" in page.text


def test_ui_delivery_page_refuses_a_malformed_reference(client):
    assert client.get("/ui/delivery/not-a-digest").status_code == 422


def test_ui_delivery_page_is_loopback_only(client, monkeypatch):
    monkeypatch.setenv("PALINODE_API_HOST", "0.0.0.0")
    assert client.get("/ui/delivery/" + "a" * 16).status_code == 403


# ── MCP + CLI surfaces, through the real code paths ──────────────────────────


@pytest.mark.asyncio
async def test_mcp_explain_tool_renders_the_public_view(mem, logger, monkeypatch):
    import httpx

    from palinode import mcp

    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    with TestClient(app) as c:
        _, receipt = _search(c, "primary database Postgres")

    monkeypatch.setattr(mcp, "_http_transport", httpx.ASGITransport(app=app))
    monkeypatch.setattr(mcp, "_http_client", None)
    try:
        result = await mcp.call_tool("palinode_explain",
                                     {"bundle_id": receipt["bundle_id"]})
        text = result[0].text
    finally:
        await mcp._close_http()

    assert not text.startswith(mcp.DISPATCH_ERROR_PREFIXES), text
    assert "decisions/db" in text
    assert "[selected]" in text
    assert "not yet captured (G3)" in text
    # The agent-facing surface never carries the caller's own query prose.
    assert "primary database Postgres" not in text
    assert WITHHELD_DIAGNOSTICS_ONLY in text


@pytest.mark.asyncio
async def test_mcp_explain_requires_a_bundle_id():
    from palinode import mcp

    result = await mcp.call_tool("palinode_explain", {})
    assert "bundle_id is required" in result[0].text


def test_cli_explain_is_tty_aware(mem, logger, monkeypatch):
    from click.testing import CliRunner

    from palinode.cli import _api
    from palinode.cli.explain import explain

    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    with TestClient(app) as c:
        _, receipt = _search(c, "primary database Postgres")
        monkeypatch.setattr(_api.api_client, "client", c)

        as_json = CliRunner().invoke(
            explain, [receipt["bundle_id"], "--format", "json"]
        )
        assert as_json.exit_code == 0, as_json.output
        payload = json.loads(as_json.output)
        assert payload["status"] == "explained"
        assert payload["supplied"][0]["ref"] == "decisions/db"

        as_text = CliRunner().invoke(
            explain, [receipt["bundle_id"], "--format", "text"]
        )
        assert as_text.exit_code == 0, as_text.output
        assert "decisions/db" in as_text.output
        assert "[selected]" in as_text.output

        diagnostics = CliRunner().invoke(
            explain, [receipt["bundle_id"], "--format", "json", "--diagnostics"]
        )
        assert json.loads(diagnostics.output)["query"] == "primary database Postgres"


def test_cli_diagnostics_degrades_visibly_against_a_remote_api(
    mem, logger, monkeypatch
):
    """Pointed at a remote API, `--diagnostics` must fail loudly, not quietly.

    The operator asked for the query. They must be told they did not get it and
    why — a bare `unavailable` token reads like "nothing was recorded", which
    is a different and wrong answer.
    """
    from click.testing import CliRunner

    from palinode.cli import _api
    from palinode.cli.explain import explain

    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision")
    with TestClient(app) as c:
        _, receipt = _search(c, "primary database Postgres", session_id="s-owner")
        monkeypatch.setattr(_api.api_client, "client", c)
        monkeypatch.setenv("PALINODE_API_HOST", "0.0.0.0")

        result = CliRunner().invoke(
            explain, [receipt["bundle_id"], "--format", "text", "--diagnostics"]
        )
        assert result.exit_code == 0, result.output
        assert "primary database Postgres" not in result.output
        assert WITHHELD_DIAGNOSTICS_ONLY in result.output
        assert "loopback bind" in result.output

        # The same caller naming its own session gets what it asked for.
        owned = CliRunner().invoke(
            explain, [receipt["bundle_id"], "--format", "json",
                      "--diagnostics", "--session-id", "s-owner"]
        )
        assert json.loads(owned.output)["query"] == "primary database Postgres"

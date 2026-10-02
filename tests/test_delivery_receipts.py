"""Context delivery receipts — supplied evidence, exact revisions, reuse contract.

What a receipt has to be worth: the exact source revision of everything that
was handed over (equal to what ``store.check_freshness`` compares against, not
a lookalike), the known lineage behind derived copies (one group, not N
independent observations), the clock it was evaluated on and the next boundary
that will change the answer — and a reuse key that moves when any of those
move, including across a noon expiry with no file touched.

Real SQLite under ``tmp_path`` through ``TestClient``; no DB mocking, real
files on disk.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import receipt as receipt_mod
from palinode.core import store
from palinode.core.config import config
from palinode.core.receipt import (
    CONFLICT_SIDE,
    DISPOSITIONS,
    EVIDENCE_ONLY,
    REPLACED,
    REVISION_FILE,
    REVISION_INDEX_SECTION,
    SELECTED,
    PolicyVersion,
    build_receipt,
    derive_bundle_id,
    reuse_key,
)
from palinode.core.retrieval_log import RetrievalEvent, RetrievalLogger
from palinode.indexer import reconcile
from palinode.mcp import _format_results

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


def _write(mem, rel: str, body: str, **meta) -> None:
    path = mem / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    content = f"---\n{fm}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    assert reconcile.reconcile(str(path), content).committed


@pytest.fixture()
def client(mem):
    with TestClient(app) as c:
        yield c


def _search(client, **body):
    """A receipt-carrying search: ``(results, receipt)``.

    ``resolve="linked"`` by default, because that is the request shape whose
    receipt is the full public view; without it the response carries only the
    two-field reference (asserted directly in the byte-diff tests below).
    """
    payload = {"threshold": 0.0, "receipt": True, "resolve": "linked", **body}
    data = client.post("/search", json=payload).json()
    return data["results"], data["receipt"]


# ── the delivery is byte-identical without the receipt ───────────────────────


def _stable(rows):
    """Rows minus the recall telemetry that legitimately moves between calls."""
    return [
        {k: v for k, v in r.items()
         if k not in ("recall_count", "last_recalled", "importance")}
        for r in rows
    ]


def test_ordinary_receipt_adds_retrieval_diagnostics(client, mem):
    """`resolve` off: same bytes, plus a bundle id and an evaluation time."""
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    body = {"query": "primary database Postgres", "limit": 1, "threshold": 0.0}

    plain = client.post("/search", json=body).json()
    assert isinstance(plain, list)

    enveloped = client.post("/search", json=body | {"receipt": True}).json()
    # The envelope also names the scope this delivery applied and the source
    # that decided it — the same two keys /context/prime returns.
    assert set(enveloped) == {"results", "receipt", "project", "project_resolved_by"}
    assert enveloped["project"] is None
    assert enveloped["project_resolved_by"] == "none"
    # The delivered rows are the same delivery, byte for byte.
    assert json.dumps(_stable(enveloped["results"]), sort_keys=True) == \
        json.dumps(_stable(plain), sort_keys=True)
    # The compact reference carries additive retrieval/readiness diagnostics.
    assert set(enveloped["receipt"]) == {"bundle_id", "evaluated_at", "retrieval"}


def test_resolve_on_carries_the_public_view(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", superseded_by="decisions/db-v2", date="2026-01-05")
    _write(mem, "decisions/db-v2.md", "# DB v2\n\nThe primary database is SQLite now.",
           type="Decision", date="2026-09-01")
    _, receipt = _search(client, query="primary database Postgres", limit=1,
                         resolve="linked")
    assert set(receipt) == {
        "bundle_id", "policy_version", "scope", "requested_time", "evaluated_at",
        "next_transition", "supplied", "lineage", "coverage", "dispositions", "retrieval",
    }
    by_ref = {s["ref"]: s for s in receipt["supplied"]}
    assert by_ref["decisions/db"]["disposition"] == REPLACED
    assert by_ref["decisions/db-v2"]["disposition"] == SELECTED
    assert receipt["dispositions"]["replaced"] == 1
    assert set(receipt["dispositions"]) == set(DISPOSITIONS)
    assert receipt["coverage"]["status"] in ("complete", "partial")


# ── exact source revisions ───────────────────────────────────────────────────


def test_revisions_equal_check_freshness_raw_hashes(client, mem):
    """The receipt's revision IS the comparand ``check_freshness`` uses."""
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    rows, receipt = _search(client, query="primary database Postgres", limit=3)

    fresh = store.check_freshness([dict(r) for r in rows])
    expected = {r["rel_path"].removesuffix(".md"): r["content_hash"] for r in fresh}
    got = {s["ref"]: s["revision"] for s in receipt["supplied"]}
    assert got == expected
    assert all(f["freshness"] == "valid" for f in fresh)
    assert {s["revision_basis"] for s in receipt["supplied"]} == {REVISION_INDEX_SECTION}


def test_an_edited_file_gets_a_new_revision_and_the_old_receipt_is_unchanged(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    _, first = _search(client, query="primary database Postgres", limit=1)
    before = json.dumps(first, sort_keys=True)

    _write(mem, "decisions/db.md", "# DB\n\nWe use SQLite as the primary database.",
           type="Decision", date="2026-01-05")
    _, second = _search(client, query="primary database", limit=1)

    assert first["supplied"][0]["revision"] != second["supplied"][0]["revision"]
    assert first["bundle_id"] != second["bundle_id"]
    # The first receipt is a record of a past delivery: immutable, not a view.
    assert json.dumps(first, sort_keys=True) == before


# ── lineage: derived copies trace back, unknown stays unknown ────────────────


def _derived_copies(mem) -> None:
    """An observation and two records that cite its span — copies, not witnesses."""
    _write(mem, "observations/deploy-metrics.md",
           "# Deploy metrics\n\nDeploy latency measured at 4 minutes.",
           epistemic="fact", date="2026-08-01", entities=["project/shop"])
    anchor = [{"ref": "observations/deploy-metrics",
               "quote": "Deploy latency measured at 4 minutes."}]
    _write(mem, "projects/shop-snapshot.md",
           "# Shop snapshot\n\nDeploy latency measured at 4 minutes.",
           type="ProjectSnapshot", date="2026-08-02", sources=anchor,
           entities=["project/shop"], backed_by=["observations/deploy-metrics"])
    _write(mem, "daily/2026-08-03.md",
           "# Session summary\n\nDeploy latency measured at 4 minutes.",
           date="2026-08-03", sources=anchor, entities=["project/shop"],
           backed_by=["observations/deploy-metrics"])


def test_derived_copies_are_one_lineage_group_at_the_original_revision(client, mem):
    _derived_copies(mem)
    rows, receipt = _search(client, query="Deploy latency measured at 4 minutes",
                            limit=5, include_daily=True, resolve="linked")

    supplied = {s["ref"]: s for s in receipt["supplied"]}
    group = next(g for g in receipt["lineage"]
                 if g["origin"] == "observations/deploy-metrics")
    # The snapshot and the session summary are copies of ONE origin — one
    # group, not two observations that agree.
    assert sorted(group["members"]) == ["daily/2026-08-03", "projects/shop-snapshot"]
    assert group["status"] == "known" and group["origin_kind"] == "source"
    # …and the group names the original at the exact revision this delivery
    # supplied it at, which is what makes it traceable back.
    assert group["origin_revision"] == supplied["observations/deploy-metrics"]["revision"]
    assert group["origin_revision"]
    # The original itself anchors nothing, so its own lineage stays unknown —
    # it is not folded into the group it is the origin of.
    own = next(g for g in receipt["lineage"]
               if g["members"] == ["observations/deploy-metrics"])
    assert own["status"] == "unknown"


def test_a_record_with_no_anchor_has_unknown_lineage(client, mem):
    _write(mem, "notes/loose.md", "# Loose\n\nAn unanchored note about deploys.",
           date="2026-08-04")
    _, receipt = _search(client, query="unanchored note about deploys", limit=1)
    entry = next(s for s in receipt["supplied"] if s["ref"] == "notes/loose")
    assert entry["origin"] is None and entry["origin_kind"] == "unknown"
    group = next(g for g in receipt["lineage"] if "notes/loose" in g["members"])
    assert group["status"] == "unknown"
    assert group["origin"] is None and group["origin_revision"] is None


# ── evaluation time and the next known transition ────────────────────────────


def test_next_transition_is_the_earliest_expiry_and_null_otherwise(client, mem):
    later = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%d")
    sooner = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d")
    _write(mem, "decisions/freeze-a.md", "# Freeze A\n\nThe deploy freeze holds.",
           type="Decision", date="2026-08-01", expires_at=later)
    _write(mem, "decisions/freeze-b.md", "# Freeze B\n\nThe deploy freeze holds here too.",
           type="Decision", date="2026-08-01", expires_at=sooner)
    _, receipt = _search(client, query="deploy freeze holds", limit=5)
    assert receipt["evaluated_at"]
    assert receipt["next_transition"].startswith(sooner)

    # No dated boundary anywhere → the boundary is unknown, not "never".
    _write(mem, "notes/undated.md", "# Undated\n\nNothing here declares a date.")
    _, plain = _search(client, query="Nothing here declares a date", limit=1)
    assert plain["next_transition"] is None


def test_a_malformed_date_contributes_no_transition(client, mem):
    _write(mem, "notes/broken.md", "# Broken\n\nA record with a nonsense expiry.",
           date="2026-08-01", expires_at="not-a-date")
    _, receipt = _search(client, query="record with a nonsense expiry", limit=1)
    assert receipt["next_transition"] is None


# ── the reuse key ────────────────────────────────────────────────────────────


def _key(**overrides):
    base = dict(
        scope=["project/shop"],
        query_scope={"query": "deploy", "limit": 5},
        policy_version=PolicyVersion(package="1.2.3", projection=1, config="abcd1234"),
        revisions=[("decisions/a", "hash-a"), ("decisions/b", "hash-b")],
        window=(None, "2026-09-12T12:00:00+00:00"),
    )
    return reuse_key(**{**base, **overrides})


def test_reuse_key_is_stable_and_moves_with_each_contract_input():
    assert _key() == _key()
    # …and does not move for telemetry that changes no selection.
    assert _key(query_scope={"query": "deploy", "limit": 5, "session_id": "s-1"}) == _key()

    assert _key(scope=["project/other"]) != _key()
    assert _key(policy_version=PolicyVersion(package="1.2.4", projection=1,
                                             config="abcd1234")) != _key()
    assert _key(policy_version=PolicyVersion(package="1.2.3", projection=2,
                                             config="abcd1234")) != _key()
    assert _key(policy_version=PolicyVersion(package="1.2.3", projection=1,
                                             config="ffff0000")) != _key()
    assert _key(revisions=[("decisions/a", "hash-a2"),
                           ("decisions/b", "hash-b")]) != _key()
    assert _key(query_scope={"query": "deploy", "limit": 6}) != _key()
    assert _key(window=("2026-09-12T12:00:00+00:00", None)) != _key()
    # Revision order is not part of the key; the set of revisions is.
    assert _key(revisions=[("decisions/b", "hash-b"),
                           ("decisions/a", "hash-a")]) == _key()


def test_noon_expiry_changes_the_key_with_no_file_touched(client, mem):
    """11:59 → 12:01 with an unchanged file: an acting state expired anyway."""
    noon = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    _write(mem, "decisions/freeze.md", "# Freeze\n\nThe deploy freeze holds until noon.",
           type="Decision", date="2026-09-01", expires_at=noon.isoformat())
    rows = client.post("/search", json={
        "query": "deploy freeze holds until noon", "limit": 1, "threshold": 0.0,
    }).json()
    assert rows

    def _receipt_at(moment):
        return build_receipt(
            rows, request={"query": "deploy freeze holds until noon", "limit": 1},
            scope=["project/shop"], now=moment, evaluated_at=moment,
            memory_dir=str(mem),
        )

    before = _receipt_at(noon - timedelta(minutes=1))
    after = _receipt_at(noon + timedelta(minutes=1))

    assert before.next_transition == noon.isoformat()
    assert after.next_transition is None
    assert before.revisions() == after.revisions()  # the file did not change
    assert before.reuse_key() != after.reuse_key()
    # Two minutes either side of the boundary stay inside one window.
    assert _receipt_at(noon - timedelta(minutes=2)).reuse_key() == before.reuse_key()


def test_bundle_id_is_deterministic_for_the_same_delivery():
    args = dict(request={"query": "a"}, refs=[("x", "h1")], evaluated_at="2026-09-12T00:00:00+00:00")
    assert derive_bundle_id(**args) == derive_bundle_id(**args)
    assert derive_bundle_id(**{**args, "evaluated_at": "2026-09-12T00:00:01+00:00"}) != \
        derive_bundle_id(**args)
    assert derive_bundle_id(**{**args, "refs": [("x", "h2")]}) != derive_bundle_id(**args)


# ── disclosure: public carries no prose ──────────────────────────────────────


def test_public_view_carries_no_memory_text_and_diagnostics_carries_the_query(client, mem):
    secret = "The staging password rotation ran on Tuesday"
    _write(mem, "notes/sensitive.md", f"# Sensitive\n\n{secret}.", date="2026-08-05")
    rows = client.post("/search", json={
        "query": "staging password rotation", "limit": 1, "threshold": 0.0,
    }).json()
    assert rows and secret in (rows[0].get("content") or "")

    built = build_receipt(rows, request={"query": "staging password rotation"},
                          scope=[], memory_dir=str(mem))
    public = json.dumps(built.public())
    assert secret not in public
    assert "staging password rotation" not in public  # not even the query
    for word in ("content", "snippet", "excerpt", "title"):
        assert word not in public

    diagnostics = json.dumps(built.diagnostics())
    assert "staging password rotation" in diagnostics  # the query may be here
    assert secret not in diagnostics                    # memory prose still is not
    assert built.diagnostics()["reuse_key"] == built.reuse_key()


# ── one delivery, one bundle id, three surfaces ──────────────────────────────


def test_every_surface_carries_the_same_bundle_id(client, mem, monkeypatch):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    body = {"query": "primary database Postgres", "limit": 1, "threshold": 0.0,
            "receipt": True}
    rest = client.post("/search", json=body).json()
    bundle = rest["receipt"]["bundle_id"]

    # MCP renders the same receipt it was handed (the handler unwraps the
    # envelope; this asserts the rendering carries the id).
    mcp_text = _format_results(rest["results"], receipt=rest["receipt"])
    assert f"Receipt: {bundle}" in mcp_text

    # CLI text mode renders it too.
    import importlib

    from click.testing import CliRunner

    # importlib, not `from palinode.cli import search`: the package re-exports
    # the Command under that name and shadows the module.
    cli_search = importlib.import_module("palinode.cli.search")

    class _Fake:
        def search(self, query, **kwargs):
            assert kwargs.get("receipt") is True
            return rest["results"], rest["receipt"]

    monkeypatch.setattr(cli_search, "api_client", _Fake())
    monkeypatch.setattr(cli_search, "_cli_resolve_context", lambda: None)
    out = CliRunner().invoke(cli_search.search, ["primary database", "--format", "text"])
    assert out.exit_code == 0, out.output
    assert bundle in out.output

    # Same request again → a *new* delivery, so a new id: a bundle id names a
    # hand-off, not a query.
    again = client.post("/search", json=body).json()["receipt"]["bundle_id"]
    assert again != bundle


def test_mcp_rendering_without_resolve_has_receipt_and_mode(client, mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    rows, receipt = _search(client, query="primary database Postgres", limit=1,
                            resolve="none")
    plain = _format_results(rows)
    with_receipt = _format_results(rows, receipt=receipt)
    assert with_receipt.startswith(plain)
    extra = with_receipt[len(plain):].strip().splitlines()
    assert len(extra) == 2 and extra[0].startswith(f"Receipt: {receipt['bundle_id']}")
    # Readiness, outcome, and — since the delivery also judges what it found —
    # the match-confidence verdict with the arm scores behind it.
    assert extra[1].startswith("Retrieval: hybrid · index: ready · matched · match confidence: ")
    assert receipt["retrieval"]["confidence"] in extra[1]


# ── persistence: the retrieval log carries the receipt ───────────────────────


def test_retrieval_log_rows_carry_the_receipt_fields(client, mem, monkeypatch):
    import palinode.api.routers.search as search_router

    monkeypatch.setattr(search_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    rows, receipt = _search(client, query="primary database Postgres", limit=1,
                            resolve="none")

    log = (mem / ".audit" / "retrievals.jsonl").read_text(encoding="utf-8").strip()
    entries = [json.loads(line) for line in log.splitlines() if line]
    assert entries
    entry = entries[-1]
    # The fields this log always had are untouched…
    assert entry["mode"] == "explicit" and entry["query"] == "primary database Postgres"
    # …and the receipt rides beside them.
    assert entry["bundle_id"] == receipt["bundle_id"]
    assert entry["timestamp"] == receipt["evaluated_at"]
    assert entry["policy_version"] == PolicyVersion.current().as_str()
    assert entry["revision"] == rows[0]["content_hash"]
    assert entry["revision_basis"] == REVISION_INDEX_SECTION
    assert entry["disposition"] == SELECTED
    assert entry["coverage"] == {"status": "not_requested", "reasons": []}
    # No memory prose in the log beyond the query it always carried.
    assert "Postgres as the primary" not in log


def test_an_empty_search_logs_one_row_with_the_receipt_and_no_ref(client, mem, monkeypatch):
    import palinode.api.routers.search as search_router

    monkeypatch.setattr(search_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    rows, receipt = _search(client, query="zeppelin mooring regulations", limit=3,
                            resolve="none")
    assert rows == []

    log = (mem / ".audit" / "retrievals.jsonl").read_text(encoding="utf-8").strip()
    entries = [json.loads(line) for line in log.splitlines() if line]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["file_path"] == "" and entry["rank"] is None
    assert entry["disposition"] == "none_delivered"
    assert entry["query"] == "zeppelin mooring regulations"
    assert entry["bundle_id"] == receipt["bundle_id"]
    assert entry["timestamp"] == receipt["evaluated_at"]
    assert entry["coverage"] == {"status": "not_requested", "reasons": []}


def test_a_row_written_without_a_receipt_is_unchanged(tmp_path):
    """Additive and migration-free: old rows read back exactly as before."""
    rl = RetrievalLogger(str(tmp_path))
    rl.record(RetrievalEvent(
        timestamp="2026-04-28T00:00:00+00:00", file_path="people/alice.md",
        chunk_id=None, mode="explicit", source="palinode_search",
        query="alice", rank=0, score=0.9, session_id=None,
    ))
    entry = json.loads((tmp_path / ".audit" / "retrievals.jsonl").read_text().strip())
    assert entry["file_path"] == "people/alice.md"
    for field in ("bundle_id", "policy_version", "scope", "revision",
                  "revision_basis", "disposition", "lineage_group",
                  "coverage", "next_transition"):
        assert entry[field] is None


def test_trace_reports_the_deliveries_a_file_was_supplied_in(client, mem, monkeypatch):
    import palinode.api.routers.search as search_router
    from palinode.core.trace import compose_trace, format_trace_text

    monkeypatch.setattr(search_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           type="Decision", date="2026-01-05")
    rows, receipt = _search(client, query="primary database Postgres", limit=1,
                            resolve="none")

    recalled = compose_trace("decisions/db.md", str(mem))["recalled"]
    assert recalled["count"] >= 1
    assert receipt["bundle_id"] in recalled["bundles"]
    assert recalled["dispositions"].get(SELECTED) == 1
    assert recalled["revisions"] == [rows[0]["content_hash"]]
    assert "supplied as selected×1" in format_trace_text(
        compose_trace("decisions/db.md", str(mem))
    )


# ── /context/prime ───────────────────────────────────────────────────────────


def test_prime_receipt_names_supplied_refs_at_file_revisions(client, mem):
    _write(mem, "decisions/core.md", "# Core\n\nThe core decision for this project.",
           type="Decision", core=True, date="2026-08-01")
    body = client.post("/context/prime", json={}).json()
    receipt = body["receipt"]
    refs = {s["ref"] for s in receipt["supplied"]}
    assert "decisions/core" in refs
    entry = next(s for s in receipt["supplied"] if s["ref"] == "decisions/core")
    expected = hashlib.sha256((mem / "decisions/core.md").read_bytes()).hexdigest()
    assert entry["revision"] == expected
    assert entry["revision_basis"] == REVISION_FILE
    assert entry["disposition"] == SELECTED
    assert receipt["coverage"]["status"] == "not_requested"
    # The digest it describes is unchanged beside it.
    assert body["core_memories"] and body["mode"]


def test_prime_receipt_marks_a_contested_row_as_a_conflict_side(client, mem):
    _write(mem, "decisions/other.md", "# Other\n\nThe other side of this.",
           type="Decision", date="2026-08-01")
    _write(mem, "decisions/core.md", "# Core\n\nThe core decision for this project.",
           type="Decision", core=True, date="2026-08-01",
           contradicts=["decisions/other"])
    receipt = client.post("/context/prime", json={}).json()["receipt"]
    entry = next(s for s in receipt["supplied"] if s["ref"] == "decisions/core")
    assert entry["disposition"] == CONFLICT_SIDE
    assert receipt["dispositions"][CONFLICT_SIDE] == 1


# ── evidence records are supplied context too ────────────────────────────────


def test_evidence_records_ride_the_receipt_at_the_revision_they_were_read_at(client, mem):
    """A record carried in an evidence block was read — so it has a revision.

    It is the **whole file** the evidence layer hashed while it had the bytes
    in hand, not the indexed per-section hash a seed row carries, which is why
    the two are reported under different bases and never compared. (The
    evidence payload used to carry no hash at all and this said ``unknown``;
    unknown was honest, and a revision the layer already read is better.)
    """
    import hashlib

    _write(mem, "decisions/deploy.md", "# Deploy\n\nDeploys go to the VPS.",
           type="Decision", date="2026-01-05", entities=["project/shop"])
    _write(mem, "observations/deploy-seen.md", "# Deploy seen\n\nDeploys are going to k8s.",
           epistemic="fact", date="2026-09-01", entities=["project/shop"],
           contradicts=["decisions/deploy"])
    rows, receipt = _search(client, query="Deploys go to the VPS", limit=1,
                            resolve="linked")
    assert rows[0]["resolution"]["outcome"] == "unresolved_conflict"
    by_ref = {s["ref"]: s for s in receipt["supplied"]}
    seed = by_ref["decisions/deploy"]
    assert seed["disposition"] == CONFLICT_SIDE
    assert seed["revision_basis"] == "index_section_sha256"
    other = by_ref["observations/deploy-seen"]
    assert other["disposition"] == CONFLICT_SIDE
    assert other["revision_basis"] == "file_sha256"
    assert other["revision"] == hashlib.sha256(
        (mem / "observations" / "deploy-seen.md").read_text(encoding="utf-8").encode()
    ).hexdigest()
    assert other["revision"] != seed["revision"], "two domains, never one comparison"
    assert EVIDENCE_ONLY in DISPOSITIONS


# ── policy version ───────────────────────────────────────────────────────────


def test_policy_version_moves_with_configured_policy(monkeypatch):
    before = PolicyVersion.current()
    assert before.as_str().startswith("palinode/")
    monkeypatch.setattr(config.search.evidence, "max_depth",
                        config.search.evidence.max_depth + 1)
    after = PolicyVersion.current()
    assert after.config != before.config
    assert after.package == before.package and after.projection == before.projection


def test_policy_version_is_stable_for_unrelated_config(monkeypatch):
    before = PolicyVersion.current()
    monkeypatch.setattr(config.search, "snippet_max_chars",
                        config.search.snippet_max_chars + 1)
    assert PolicyVersion.current() == before


def test_request_scope_drops_telemetry_and_unset_fields():
    scope = receipt_mod.request_scope(
        {"query": "x", "limit": None, "session_id": "s", "receipt": True}
    )
    assert scope == {"query": "x"}

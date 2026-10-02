"""The developer memory lifecycle, walked end to end.

One fictional decision goes through **save → retire → every supported default
delivery path → process restart → full rebuild → consolidation input**, and the
question asked at each stop is the same one: *can this record still reach a
caller as a claim that currently stands?*

What this suite is for is the honest boundary, not the happy path. Four things
are deliberately in the fixture because they are where retirement leaks:

* a **derived copy** — a second memory that quotes the decision verbatim in its
  ``sources[].quote`` span. Retiring the original does not reach it, and
  the archive result names it (reported, never changed) under ``retained_copies``;
* a **neighbouring fact** that must be untouched, because a retirement that
  takes out the record next door is worse than one that misses;
* a **withdrawn forget request** (``forget-withdraw``), the one operation that
  has to *un*-retire several memories at two granularities and retire the
  request record in the same breath;
* **restore**, so "stop using this" is demonstrably reversible.

Vocabulary this suite holds itself to, because conflating them is the failure
mode the issue is about:

``retired``
    Archived / superseded / retracted / expired. The record stays on disk, in
    git and in the index; it stops being *presented as current* by default.
    Every assertion here is about the presentation, never about the bytes.
``erased``
    The bytes are gone. Not what any operation in this file does — see
    ``tests/test_erasure_runbook.py`` for the destructive runbook, which
    runs only against its own throwaway fixture.

Real SQLite, real git, real FTS5, real files, all under ``tmp_path``. Nothing
is mocked but the embedder (a deterministic bag-of-words hash, so token overlap
yields genuine vector similarity) and the write-path security scanner. No LLM
is called: the consolidation checkpoint uses the runner's own deterministic
input selector, which is the function the compaction prompt is built from.

Defects found rather than fixed are ``xfail(strict=True)`` with the reason
spelled out, so this file is also the defect list's executable form. Tracker
numbers live in the changelog entry and the pull request, not here — a bare
number is unfollowable for a reader of the shipped source.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import subprocess
from typing import Any
from unittest.mock import patch

import frontmatter
import pytest

from palinode.core.config import config

EMBED_DIM = 1024

# ── the fixture's fictional world ────────────────────────────────────────────
#
# Invented names throughout. The decision is the record under test; the readout
# quotes it; the cadence note is the neighbour that must survive untouched.

DECISION_TEXT = (
    "Ferrograve Assembly settles every Tidewater Quarry invoice through the "
    "Hollowpine clearing account, with a fortnightly reconciliation against "
    "the quarry ledger."
)
DECISION_REF = "decisions/tidewater-settlement"
DECISION_FILE = f"{DECISION_REF}.md"

READOUT_TEXT = (
    "Quarterly readout for the Ferrograve Assembly finance track. The "
    "settlement route was agreed in full: "
    f"“{DECISION_TEXT}” That route is the one the treasury team "
    "reconciles against each fortnight."
)
READOUT_FILE = "insights/quarterly-readout.md"

NEIGHBOUR_TEXT = (
    "Ferrograve Assembly reviews pavilion staffing rosters every fortnight "
    "and publishes the roster to the volunteer board."
)
NEIGHBOUR_FILE = "decisions/pavilion-cadence.md"

DECISION_QUERY = "Tidewater Quarry invoice settlement Hollowpine clearing"
NEIGHBOUR_QUERY = "pavilion staffing roster volunteer board"


def _hash_embed(text: str, backend: str = "local") -> list[float]:
    """Deterministic bag-of-words embedding: shared tokens → shared components."""
    vec = [0.0] * EMBED_DIM
    for tok in set(text.lower().split()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A git-backed disposable store behind a TestClient.

    Everything the tests touch lives under ``tmp_path``: the markdown files,
    the git repository, the SQLite database, ``.audit/`` and ``.palinode/``.
    No test in this file writes outside it, and none of them are destructive
    beyond the frontmatter flips the lifecycle ops make.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True
    )

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    for key in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)

    import palinode.api.server as srv

    srv = importlib.reload(srv)
    srv._rate_counters.clear()

    # The API's retrieval logger is built at *import* time from the process
    # config, so patching `memory_dir` does not move it: in a full-suite run it
    # still points at whatever store was configured when the first test module
    # imported the API. Every `/search` and `/read` below would append this
    # fixture's queries and file paths to that log. Redirect every module that
    # holds the name by value.
    from palinode.core.retrieval_log import RetrievalLogger

    fixture_logger = RetrievalLogger(str(tmp_path))
    for name in (
        "palinode.api._util",
        "palinode.api.server",
        "palinode.api.routers.memory",
        "palinode.api.routers.search",
        "palinode.api.routers.git_history",
    ):
        module = importlib.import_module(name)
        if hasattr(module, "_retrieval_logger"):
            monkeypatch.setattr(module, "_retrieval_logger", fixture_logger)

    from fastapi.testclient import TestClient

    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", side_effect=_hash_embed),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as client:
            try:
                yield client, str(tmp_path)
            finally:
                # Several tests here call `/reindex`, which constructs a
                # watcher handler and arms debounce timers on their own
                # threads. Left running they fire during a *later* test,
                # against whatever `memory_dir` points at by then. Stop them
                # while this store is still the current one.
                from palinode.indexer import watcher

                watcher.shutdown_handlers()
    srv._rate_counters.clear()


# ── helpers ──────────────────────────────────────────────────────────────────


def _save(client, content: str, mtype: str, slug: str, **extra: Any) -> dict:
    body = {"content": content, "type": mtype, "slug": slug, **extra}
    r = client.post("/save", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _recall_refs(client, query: str) -> list[str]:
    """Memory-relative paths a *default* search delivers for ``query``."""
    r = client.post(
        "/search",
        json={"query": query, "limit": 20, "threshold": 0.0, "hybrid": True},
    )
    assert r.status_code == 200, r.text
    return [h.get("rel_path") or h["file_path"] for h in r.json()]


def _seed_world(client) -> None:
    """The decision, the memory that quotes it, and the untouched neighbour."""
    _save(
        client,
        DECISION_TEXT,
        "Decision",
        "tidewater-settlement",
        project="ferrograve",
    )
    _save(
        client,
        READOUT_TEXT,
        "Insight",
        "quarterly-readout",
        project="ferrograve",
        sources=[{"ref": DECISION_REF, "quote": DECISION_TEXT}],
    )
    _save(
        client, NEIGHBOUR_TEXT, "Decision", "pavilion-cadence", project="ferrograve"
    )


def _archive(client, ref: str, **extra: Any) -> dict:
    r = client.post("/archive", json={"file_path": ref, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _restore(client, ref: str, **extra: Any) -> dict:
    r = client.post("/restore", json={"file_path": ref, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _fm(memory_dir: str, rel: str) -> dict[str, Any]:
    with open(os.path.join(memory_dir, rel), encoding="utf-8") as fh:
        return dict(frontmatter.load(fh).metadata)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Retirement, and every supported default delivery path
# ═════════════════════════════════════════════════════════════════════════════


def test_retired_decision_leaves_default_recall_neighbour_untouched(store):
    """The baseline: retire one record, lose exactly that record from recall."""
    client, memory_dir = store
    _seed_world(client)

    assert DECISION_FILE in _recall_refs(client, DECISION_QUERY)

    result = _archive(client, DECISION_FILE, reason="the clearing account closed")
    assert result["status"] == "archived"
    assert result["chunks_updated"] >= 1

    assert DECISION_FILE not in _recall_refs(client, DECISION_QUERY)
    # The neighbour is a fact about the same fictional org and must be intact.
    assert NEIGHBOUR_FILE in _recall_refs(client, NEIGHBOUR_QUERY)
    assert _fm(memory_dir, NEIGHBOUR_FILE).get("status") != "archived"


def test_explicit_historical_access_stays_available_and_labelled(store):
    """Retired is not gone: the record reads back, and it reads back *marked*."""
    client, memory_dir = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")

    # Surface 1 — the file itself, through the read endpoint, with metadata.
    r = client.get("/read", params={"file_path": DECISION_FILE, "meta": True})
    assert r.status_code == 200, r.text
    read = r.json()
    assert DECISION_TEXT.split(".")[0] in read["content"]
    assert read["frontmatter"]["status"] == "archived"

    # Surface 2 — the store, with the status filter explicitly lifted. The
    # content is retrievable AND every hit is labelled `currency: retired`,
    # so an on-demand historical read cannot be mistaken for current recall.
    from palinode.core import store as store_mod

    hits = store_mod.check_freshness(
        store_mod.search(
            _hash_embed(DECISION_QUERY),
            threshold=0.0,
            top_k=50,
            status_exclude_list=[],
            record_access=False,
        )
    )
    retired = [h for h in hits if h["file_path"].endswith("tidewater-settlement.md")]
    assert retired, "the archived decision is not retrievable even on demand"
    assert all(h["currency"] == "retired" for h in retired)
    assert all(h["currency_reason"] == "status:archived" for h in retired)

    # Surface 3 — the audit sibling names the retirement and its reason.
    history = _fm(memory_dir, "decisions/tidewater-settlement-history.md")
    assert history["status"] == "archived"


def test_resolve_does_not_answer_with_the_retired_decision(store):
    """``POST /resolve`` is the "what stands?" surface; a retired record doesn't."""
    client, _ = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")

    r = client.post("/resolve", json={"ref": DECISION_REF})
    assert r.status_code == 200, r.text
    bundle = r.json()
    blob = json.dumps(bundle)
    # Either the bundle declines to answer or it says the record is retired;
    # what it must never do is present it as a standing claim with no marker.
    assert "retired" in blob or bundle.get("answer") in (None, "", "unknown"), blob


def test_context_prime_does_not_present_the_retired_decision(store):
    """Session-start priming selects through the shared lifecycle classifier."""
    client, _ = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")

    r = client.post("/context/prime", json={"project": "ferrograve"})
    assert r.status_code == 200, r.text
    digest = r.json()
    listed = {
        row["file"]
        for key in (
            "core_memories",
            "recent_decisions",
            "open_action_items",
            "recent_snapshots",
        )
        for row in digest.get(key, [])
    }
    assert DECISION_FILE not in listed


def test_core_only_listing_drops_a_retired_core_record(store):
    """The listing the SessionStart hook injects from stops offering it.

    ``GET /list?core_only=true`` is the one delivery path in this walk that is
    *automatic and unprompted at session start*, so a retired record reaching
    it is injected into every new session on the machine. The listing selects
    through the same lifecycle classifier as the rest of the walk: a retired
    core memory reports ``core: false`` with ``core_retired_reason`` and drops
    out of the filtered listing, while staying listed, readable and searchable.
    """
    client, _ = store
    _save(client, DECISION_TEXT, "Decision", "tidewater-settlement", core=True)
    assert any(
        row["file"] == DECISION_FILE
        for row in client.get("/list", params={"core_only": True}).json()
    )

    _archive(client, DECISION_FILE, reason="the clearing account closed")

    listed = client.get("/list", params={"core_only": True}).json()
    assert DECISION_FILE not in [row["file"] for row in listed]

    # Withheld from injection, not hidden: the unfiltered listing still has the
    # row, marked non-core and saying why.
    row = next(
        r for r in client.get("/list").json() if r["file"] == DECISION_FILE
    )
    assert row["core"] is False
    assert row["core_retired_reason"] == "status:archived"


def test_trigger_delivery_stops_at_a_retired_target(store):
    """A trigger is an *unprompted* assertion — retirement has to silence it.

    Fixed in this PR: ``/check-triggers``'s deliverability predicate consulted
    visibility only, so a memory that had been archived kept being pushed into
    recall by its own trigger. It now consults the same lifecycle classifier
    the digest and the consolidation runner use.
    """
    client, _ = store
    _seed_world(client)

    r = client.post(
        "/triggers",
        json={
            "description": DECISION_QUERY,
            "memory_file": DECISION_FILE,
            "threshold": 0.1,
            "cooldown_hours": 0,
        },
    )
    assert r.status_code == 200, r.text
    trigger_id = r.json()["id"]

    fired = client.post(
        "/check-triggers", json={"query": DECISION_QUERY, "cooldown_bypass": True}
    ).json()
    assert [f["memory_file"] for f in fired] == [DECISION_FILE]

    _archive(client, DECISION_FILE, reason="the clearing account closed")

    fired = client.post(
        "/check-triggers", json={"query": DECISION_QUERY, "cooldown_bypass": True}
    ).json()
    assert DECISION_FILE not in [f["memory_file"] for f in fired], (
        "a retired memory was pushed into recall by its own trigger"
    )
    # The registry still holds the trigger: retirement suppresses delivery, it
    # does not quietly discard the operator's registration.
    assert trigger_id in [t["id"] for t in client.get("/triggers").json()]

    # …and restoring the memory makes it deliverable again, no re-registration.
    _restore(client, DECISION_FILE, reason="the account reopened")
    fired = client.post(
        "/check-triggers", json={"query": DECISION_QUERY, "cooldown_bypass": True}
    ).json()
    assert DECISION_FILE in [f["memory_file"] for f in fired]


def test_retirement_survives_a_new_store_handle(store):
    """Process restart: a fresh connection reads the same retirement."""
    client, _ = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")

    from palinode.core import store as store_mod

    db = store_mod.get_db()  # a handle opened after the mutation
    try:
        rows = db.execute(
            "SELECT json_extract(metadata, '$.status') FROM chunks "
            "WHERE file_path LIKE '%tidewater-settlement.md'"
        ).fetchall()
    finally:
        db.close()
    assert rows and all(row[0] == "archived" for row in rows)
    assert DECISION_FILE not in _recall_refs(client, DECISION_QUERY)


def test_retirement_survives_a_full_rebuild_from_files(store):
    """The index is derived state; rebuilding it from files must not resurrect."""
    client, memory_dir = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")

    db_path = os.path.join(memory_dir, ".palinode.db")
    assert db_path.startswith(memory_dir)  # never outside the fixture
    os.remove(db_path)

    from palinode.core import store as store_mod

    store_mod.init_db()
    r = client.post("/reindex")
    assert r.status_code == 200, r.text

    assert DECISION_FILE not in _recall_refs(client, DECISION_QUERY)
    # and the neighbour came back, so the rebuild really did run.
    assert NEIGHBOUR_FILE in _recall_refs(client, NEIGHBOUR_QUERY)


def test_consolidation_input_withholds_the_retired_decision(store):
    """The compaction prompt's governing-decision block must not carry it.

    No LLM is involved: ``_get_decisions_for_project`` is the deterministic
    selector the prompt is rendered from, and it is where a retired decision
    would re-enter the model's context as a binding constraint.
    """
    client, _ = store
    _seed_world(client)

    from palinode.consolidation.runner import (
        _format_active_decisions,
        _get_decisions_for_project,
    )

    before = {d["ref"] for d in _get_decisions_for_project("ferrograve")}
    assert DECISION_REF in before

    _archive(client, DECISION_FILE, reason="the clearing account closed")

    after = {d["ref"] for d in _get_decisions_for_project("ferrograve")}
    assert DECISION_REF not in after
    assert "decisions/pavilion-cadence" in after
    assert "Hollowpine" not in _format_active_decisions("ferrograve")


def test_a_quoted_copy_is_not_reached_by_retiring_its_source(store):
    """The dependency boundary, stated as a test rather than as a promise.

    Retiring a record does not retire the memories that *quote* it. That is
    correct — the readout is somebody else's memory and archiving it would
    take out unrelated recall — but it means "I retired that decision" is not
    the same sentence as "that wording is out of recall", and the operator has
    to be told which one they got.
    """
    client, memory_dir = store
    _seed_world(client)
    result = _archive(client, DECISION_FILE, reason="the clearing account closed")

    # The quoted copy is still active, still in recall, still carrying the
    # verbatim sentence in its `sources[].quote` span.
    assert _fm(memory_dir, READOUT_FILE).get("status") != "archived"
    assert READOUT_FILE in _recall_refs(client, DECISION_QUERY)
    quotes = [s.get("quote", "") for s in _fm(memory_dir, READOUT_FILE)["sources"]]
    assert any("Hollowpine clearing account" in q for q in quotes)

    # What the archive DID reach is honestly reported: the review flag on the
    # dependent is the one automatic signal that crosses the file boundary.
    assert "review_flagged" in result


def test_archive_result_names_the_copies_it_did_not_reach(store):
    client, _ = store
    _seed_world(client)
    result = _archive(client, DECISION_FILE, reason="the clearing account closed")
    retained = json.dumps(result.get("retained_copies", []))
    assert "quarterly-readout" in retained


# ═════════════════════════════════════════════════════════════════════════════
# 2. Restore — "stop using this" has to be reversible
# ═════════════════════════════════════════════════════════════════════════════


def test_restore_returns_the_decision_and_records_the_resurrection(store):
    client, memory_dir = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")
    assert DECISION_FILE not in _recall_refs(client, DECISION_QUERY)

    result = _restore(client, DECISION_FILE, reason="the account reopened")
    assert result["status"] == "active"
    assert result["restored_from"] == "archived"

    assert DECISION_FILE in _recall_refs(client, DECISION_QUERY)
    meta = _fm(memory_dir, DECISION_FILE)
    assert meta["status"] == "active"
    assert meta["restored_at"]  # the resurrection is visible, never silent
    assert "superseded_by" not in meta


# ═════════════════════════════════════════════════════════════════════════════
# 3. Forget, and taking a forget request back
# ═════════════════════════════════════════════════════════════════════════════


PREF_TEXT = "I settle my own invoices through the Hollowpine clearing account."
FORGET_TEXT = "Please forget that I settle my own invoices through Hollowpine."


@pytest.fixture()
def forget_enabled(monkeypatch):
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)


def test_forget_then_withdraw_round_trips_the_preference(
    store, forget_enabled
):
    """The full retraction round trip, including the request's own retirement."""
    client, memory_dir = store
    _save(client, PREF_TEXT, "Insight", "pref-hollowpine")
    _save(client, NEIGHBOUR_TEXT, "Decision", "pavilion-cadence")

    out = _save(client, FORGET_TEXT, "Insight", "forget-hollowpine")
    assert out["forget"]["detected"] is True
    assert out["forget"]["archived"] == ["insights/pref-hollowpine.md"]
    # The request record itself stays live — it is the tombstone, and the
    # measurement showed removing it is worse than doing nothing.
    assert "insights/forget-hollowpine.md" in _recall_refs(client, "Hollowpine")
    assert "insights/pref-hollowpine.md" not in _recall_refs(client, "Hollowpine")
    # The neighbour shares no content words with the pref phrase: untouched.
    assert _fm(memory_dir, NEIGHBOUR_FILE).get("status") != "archived"

    r = client.post(
        "/forget-withdraw",
        json={"file_path": "insights/forget-hollowpine.md", "reason": "asked back"},
    )
    assert r.status_code == 200, r.text
    withdrawn = r.json()
    assert withdrawn["status"] == "withdrawn"
    assert withdrawn["restored"] == ["insights/pref-hollowpine.md"]
    assert withdrawn["requests_archived"] == ["insights/forget-hollowpine.md"]
    assert "failed" not in withdrawn

    # The preference is back; the request record has taken its place in the
    # retired column, so the store no longer says two things at once.
    assert "insights/pref-hollowpine.md" in _recall_refs(client, "Hollowpine")
    assert _fm(memory_dir, "insights/forget-hollowpine.md")["status"] == "archived"


def test_withdraw_reports_partial_when_a_step_fails(store, forget_enabled, monkeypatch):
    """A composition that half-worked must not report as "withdrawn".

    Fixed in this PR. Each step of a withdrawal is its own mutation and a
    failing one is reported rather than raised — which is right — but the
    top-level ``status`` said ``withdrawn`` regardless, so an operator whose
    memory is still retired was told the request had been taken back.
    """
    client, _ = store
    _save(client, PREF_TEXT, "Insight", "pref-hollowpine")
    _save(client, FORGET_TEXT, "Insight", "forget-hollowpine")

    import palinode.consolidation.archive as archive_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure at the restore step boundary")

    monkeypatch.setattr(archive_mod, "restore_memory", _boom)

    r = client.post(
        "/forget-withdraw", json={"file_path": "insights/forget-hollowpine.md"}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "partial", (
        "a withdrawal whose restore step failed reported itself as complete"
    )
    assert out["restored"] == []
    assert [f["op"] for f in out["failed"]] == ["restore"]


def test_withdraw_partial_is_visible_on_the_cli_and_mcp_surfaces():
    """Parity: the honest word has to reach the reader, not just the JSON."""
    from click.testing import CliRunner

    from palinode.cli.restore import forget_withdraw

    payload = {
        "file": "insights/forget-hollowpine.md",
        "status": "partial",
        "pref": "I settle my own invoices through Hollowpine",
        "restored": [],
        "unretracted": [],
        "requests_archived": [],
        "failed": [{"path": "insights/pref-hollowpine.md", "op": "restore"}],
    }
    with patch(
        "palinode.cli._api.api_client.forget_withdraw", return_value=payload
    ):
        result = CliRunner().invoke(
            forget_withdraw, ["insights/forget-hollowpine.md", "--format", "text"]
        )
    assert result.exit_code == 0, result.output
    assert "Partially withdrawn" in result.output
    assert "still retired" in result.output


# ═════════════════════════════════════════════════════════════════════════════
# 4. Interruption and recovery
# ═════════════════════════════════════════════════════════════════════════════


def test_interrupted_archive_fails_loudly_and_a_rebuild_recovers(store):
    """Fail at a step boundary; get an error, not a plausible-looking success.

    ``archive_memory`` is file-write → history → index push → commit. Breaking
    the index push leaves the frontmatter flipped on disk while the index still
    says the record is current — the worst kind of partial state, because
    default recall keeps delivering it. The contract this pins is that the
    operation *says so*: a 5xx rather than a 200, and a rebuild from files
    (which are the source of truth) reconciles it.
    """
    client, memory_dir = store
    _seed_world(client)

    from palinode.core import store as store_mod

    # A *scoped* patch context, never the test-level `monkeypatch`: undoing the
    # latter would also undo the `store` fixture's `config.memory_dir` /
    # `config.db_path` redirection (pytest hands both the same object) and the
    # reindex below would run against the developer's real store.
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(
            store_mod,
            "set_status_for_path",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("index push interrupted")
            ),
        )
        r = client.post(
            "/archive", json={"file_path": DECISION_FILE, "reason": "interrupted"}
        )
        assert r.status_code >= 500, (
            f"an interrupted archive returned {r.status_code} — a partial "
            "mutation reported as success"
        )

    # The honest, visible residue: the file says archived, the index does not.
    assert _fm(memory_dir, DECISION_FILE)["status"] == "archived"
    assert DECISION_FILE in _recall_refs(client, DECISION_QUERY)

    # The files are the source of truth, so rebuilding the derived state from
    # them is the recovery — and it reconciles without any manual repair.
    assert config.memory_dir == memory_dir  # the fixture's redirection holds
    assert client.post("/reindex").status_code == 200
    assert DECISION_FILE not in _recall_refs(client, DECISION_QUERY)


def test_rollback_previews_before_it_acts(store):
    """``/rollback`` defaults to a dry run and shows the diff it would apply."""
    client, _ = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")

    r = client.post("/rollback", params={"file_path": DECISION_FILE})
    assert r.status_code == 200, r.text
    preview = r.json()["result"]
    assert "Dry Run" in preview
    assert "status: archived" in preview
    # Nothing moved: the preview is a preview.
    assert DECISION_FILE not in _recall_refs(client, DECISION_QUERY)


def test_rollback_across_a_retirement_resurrects_the_claim(store):
    """An *acknowledged* rollback across a retirement resurrects it — evenly.

    ``/rollback`` is a git-level operation and git does not know what a
    retirement is. Reverting the file one commit reverts *the archive itself*:
    the ``status: archived`` line is not restored to ``active``, it is deleted
    outright, so the record comes back as an **unmarked** memory. That is now
    only reachable with ``undo_retirements=true`` (see the next test for the
    refusal without it), and the result names what came back.

    The resurrection used to be *uneven*: frontmatter-reading surfaces saw the
    record as usable again while search did not, because the archive's status
    push changed chunk metadata without moving its hash, so the reverted file
    looked already-indexed. Now every surface agrees with the file.
    """
    client, memory_dir = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")
    assert DECISION_FILE not in _recall_refs(client, DECISION_QUERY)

    r = client.post(
        "/rollback",
        params={
            "file_path": DECISION_FILE, "dry_run": False, "undo_retirements": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "undid_retirements"
    assert [x["record"] for x in r.json()["resurrected"]] == [DECISION_FILE]

    from palinode.consolidation.runner import _get_decisions_for_project
    from palinode.core.lifecycle import eligibility

    meta = _fm(memory_dir, DECISION_FILE)
    assert "status" not in meta, "expected the retirement marker to be reverted away"
    assert eligibility(meta, path=DECISION_FILE).state == "unmarked"

    # Frontmatter surface: the decision is a governing constraint again.
    assert DECISION_REF in {d["ref"] for d in _get_decisions_for_project("ferrograve")}

    # Index surface agrees, with and without an explicit full reindex.
    assert DECISION_FILE in _recall_refs(client, DECISION_QUERY)
    assert client.post("/reindex").status_code == 200
    assert DECISION_FILE in _recall_refs(client, DECISION_QUERY)


def test_rollback_across_a_retirement_warns(store):
    client, _ = store
    _seed_world(client)
    _archive(client, DECISION_FILE, reason="the clearing account closed")

    r = client.post(
        "/rollback", params={"file_path": DECISION_FILE, "dry_run": False}
    )
    assert r.status_code == 200, r.text
    assert "retire" in r.json()["result"].lower()


# ═════════════════════════════════════════════════════════════════════════════
# 5. Honest reporting
# ═════════════════════════════════════════════════════════════════════════════


def test_retirement_is_never_described_as_deletion(store):
    """Local retirement must not be worded as "deleted", anywhere it is rendered."""
    from click.testing import CliRunner

    from palinode.cli.archive import archive as archive_cmd

    client, _ = store
    _seed_world(client)
    payload = _archive(client, DECISION_FILE, reason="the clearing account closed")

    with patch("palinode.cli._api.api_client.archive", return_value=payload):
        result = CliRunner().invoke(
            archive_cmd, [DECISION_FILE, "--format", "text"]
        )
    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "archived" in lowered
    for overclaim in ("deleted", "erased", "removed permanently", "wiped"):
        assert overclaim not in lowered, f"archive output claims {overclaim!r}"
    assert "suppressed from recall" in lowered


#: Which lifecycle mutations can be previewed before they act, as measured by
#: the CLI options that exist. The point of pinning it is that the gap is
#: *known* rather than assumed: adding or removing a preview has to come here
#: and to the documentation's command table in the same change.
PREVIEWABLE = {
    "archive-expired": True,   # --dry-run
    "rollback": True,          # --dry-run, and it is the default
    "consolidate": True,       # --dry-run
    "corrections": True,       # proposals only; never writes
    "archive": True,           # --dry-run; apply stays the default
    "restore": True,           # --dry-run
    "unretract": True,         # --dry-run
    "forget-withdraw": True,   # --dry-run
}


def test_the_preview_inventory_matches_the_shipped_commands():
    """The documented "which mutations preview" table, checked against click."""
    from palinode.cli import main

    measured = {}
    for name in PREVIEWABLE:
        command = main.commands[name]
        options = {
            opt for param in command.params for opt in getattr(param, "opts", [])
        }
        measured[name] = "--dry-run" in options
    # `corrections` proposes and never mutates, so it has no flag to find; it
    # is in the table as a capability, not as an option.
    measured["corrections"] = True
    assert measured == PREVIEWABLE


def test_every_lifecycle_mutation_reports_its_outcome_field(store):
    """completed / already-done / failed must each have a distinct value."""
    client, _ = store
    _seed_world(client)

    assert _archive(client, DECISION_FILE, reason="closed")["status"] == "archived"
    # Idempotent re-run is reported as a no-op, not as a second retirement.
    assert _archive(client, DECISION_FILE)["status"] == "already_archived"
    assert _restore(client, DECISION_FILE)["status"] == "active"
    assert _restore(client, DECISION_FILE)["status"] == "not_archived"

    # An unsupported target is a typed refusal, not a silent success.
    missing = client.post("/archive", json={"file_path": "decisions/nope.md"})
    assert missing.status_code == 404
    outside = client.post("/archive", json={"file_path": "../escape.md"})
    assert outside.status_code in (400, 403)


def test_archive_offers_a_preview_before_it_mutates(store):
    client, _ = store
    _seed_world(client)
    r = client.post(
        "/archive", json={"file_path": DECISION_FILE, "dry_run": True}
    )
    assert r.status_code == 200, r.text
    assert r.json().get("dry_run") is True
    assert DECISION_FILE in _recall_refs(client, DECISION_QUERY)

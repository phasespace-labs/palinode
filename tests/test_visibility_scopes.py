"""ADR-009 Layer 2 — visibility + private/restricted scopes (#108).

Covers the pure predicates (``visible_on_chain`` / ``access_allows``), the
enforcement choke point (``palinode.core.visibility``), and every recall
surface that routes through it: ``GET /list``, ``POST /search`` (semantic and
recency branches), ``POST /search-associative``, and the ``/context/prime``
digest.

The load-bearing properties:

- **Access control is unconditional.** ``private`` / ``restricted`` memories
  are withheld even from surfaces with no scope chain — notably ``GET
  /list?core_only=true``, which the shipped SessionStart hook injects from.
- **Scope isolation needs an identity.** A chain carrying only a
  ``session/<id>`` (ADR-007 recall telemetry) isolates nothing, so
  explicitly-scoped shared memories stay visible.
- **Enforcement reads live frontmatter**, never ``chunks.metadata`` — the
  indexer's unchanged-content fast path leaves that stale after a
  frontmatter-only edit.
- **One path format.** Absolute and memory-dir-relative paths for the same
  file must produce the same verdict.
- ``inherited`` (the default) is byte-identical to Layer 1 ``chain_allows``.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import palinode.core.config as config_module
from palinode.api.routers.memory import collect_memory_files
from palinode.api.routers.search import (
    SearchRequest,
    _fetch_visible,
    _resolve_search_scope_chain,
)
from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.core.context_prime import build_context_digest
from palinode.core.scope import (
    ScopeChain,
    access_allows,
    resolve_scope_chain,
    visible_on_chain,
)
from palinode.core.visibility import filter_visible, is_visible, normalize_memory_path

client = TestClient(app)

# A representative multi-agent chain: agent researcher, in project palinode,
# member alice. No org level set.
CHAIN = ScopeChain(agent="researcher", project="palinode", member="alice")


# ── visible_on_chain: inherited (default / absence-is-neutral) ─────────────


def test_inherited_default_matches_chain_allows_for_unscoped():
    assert visible_on_chain(CHAIN, {}) is True
    assert visible_on_chain(CHAIN, {"core": True}) is True
    assert visible_on_chain(ScopeChain(), {}) is True


def test_inherited_explicit_scope_on_and_off_chain():
    assert visible_on_chain(CHAIN, {"scope": "project/palinode"}) is True
    assert visible_on_chain(CHAIN, {"scope": "agent/researcher"}) is True
    assert visible_on_chain(CHAIN, {"scope": "project/other"}) is False
    assert visible_on_chain(CHAIN, {"scope": "org/phasespace"}) is False


def test_inherited_value_written_explicitly_is_same_as_absent():
    assert visible_on_chain(
        CHAIN, {"scope": "project/palinode", "visibility": "inherited"}
    ) is True
    assert visible_on_chain(
        CHAIN, {"scope": "project/other", "visibility": "inherited"}
    ) is False


def test_inherited_does_not_consult_directory_default():
    # A legacy file must not be hidden by the directory-inferred default.
    assert visible_on_chain(CHAIN, {}, file_path="decisions/legacy.md") is True
    assert visible_on_chain(ScopeChain(), {}, file_path="insights/old.md") is True


# ── visible_on_chain: private ──────────────────────────────────────────────


def test_private_owner_on_chain_is_visible():
    assert visible_on_chain(
        CHAIN, {"scope": "agent/researcher", "visibility": "private"}
    ) is True


def test_private_owner_off_chain_is_hidden():
    assert visible_on_chain(
        CHAIN, {"scope": "agent/implementer", "visibility": "private"}
    ) is False


def test_private_gets_no_unscoped_free_pass():
    # Fails closed. The save path rejects this shape outright (see
    # test_save_rejects_private_without_scope) — this is the read-time
    # backstop for a hand-authored file that never went through the API.
    assert visible_on_chain(CHAIN, {"visibility": "private"}) is False


def test_private_with_empty_chain_is_hidden():
    assert visible_on_chain(
        ScopeChain(), {"scope": "agent/researcher", "visibility": "private"}
    ) is False


# ── visible_on_chain: restricted ───────────────────────────────────────────


def test_restricted_access_intersects_chain_is_visible():
    meta = {
        "scope": "org/phasespace",
        "visibility": "restricted",
        "access": ["member/alice", "harness/cursor"],
    }
    assert visible_on_chain(CHAIN, meta) is True


def test_restricted_no_intersection_is_hidden():
    meta = {"scope": "org/phasespace", "visibility": "restricted",
            "access": ["member/sarah"]}
    assert visible_on_chain(CHAIN, meta) is False


def test_restricted_empty_access_hides_from_everyone():
    meta = {"scope": "project/palinode", "visibility": "restricted"}
    assert visible_on_chain(CHAIN, meta) is False
    assert visible_on_chain(CHAIN, {**meta, "access": []}) is False


def test_restricted_scope_on_chain_does_not_bypass_access():
    meta = {"scope": "project/palinode", "visibility": "restricted",
            "access": ["member/sarah"]}
    assert visible_on_chain(CHAIN, meta) is False


# ── malformed values coerce to inherited ──────────────────────────────────


def test_malformed_visibility_behaves_as_inherited():
    assert visible_on_chain(CHAIN, {"visibility": "secret"}) is True
    assert visible_on_chain(
        CHAIN, {"visibility": "secret", "scope": "project/other"}
    ) is False
    assert visible_on_chain(CHAIN, {"visibility": ["private"]}) is True


def test_malformed_access_on_restricted_hides_memory():
    assert visible_on_chain(CHAIN, {"visibility": "restricted",
                                    "access": "member/alice"}) is False


# ── access_allows: the no-chain rule ──────────────────────────────────────


def test_access_allows_passes_inherited_including_scoped():
    # Scope is a selection preference — it needs a chain. Without one,
    # explicitly-scoped memories still pass.
    assert access_allows({}) is True
    assert access_allows({"scope": "project/other"}) is True
    assert access_allows({"visibility": "inherited", "scope": "harness/cursor"}) is True


def test_access_allows_withholds_private_and_restricted():
    assert access_allows({"scope": "agent/x", "visibility": "private"}) is False
    assert access_allows({"visibility": "restricted", "access": ["member/alice"]}) is False


# ── ScopeChain.has_identity (finding 5) ───────────────────────────────────


def test_has_identity_excludes_session_level():
    assert ScopeChain().has_identity() is False
    # A bare session id is ADR-007 telemetry, not an identity.
    session_only = ScopeChain(session="abc123")
    assert session_only.is_empty() is False
    assert session_only.has_identity() is False
    assert ScopeChain(agent="researcher").has_identity() is True
    assert ScopeChain(project="palinode").has_identity() is True
    assert ScopeChain(org="phasespace").has_identity() is True


def test_session_only_chain_does_not_hide_scoped_memories():
    """REGRESSION: a bare session_id must not activate scope isolation."""
    req = SearchRequest(query="x", session_id="sess-1")
    assert _resolve_search_scope_chain(req) is None
    # ...and via the choke point, a scoped shared memory stays visible.
    assert is_visible(None, "decisions/x.md",
                      metadata={"scope": "project/palinode"}) is True


# ── path normalization (finding 4) ────────────────────────────────────────


def test_normalize_memory_path_absolute_and_relative_agree(tmp_path):
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    try:
        abs_path = os.path.join(str(tmp_path), "decisions", "x.md")
        assert normalize_memory_path(abs_path) == os.path.join("decisions", "x.md")
        assert normalize_memory_path("decisions/x.md") == os.path.join("decisions", "x.md")
        # Root-level file: relative form infers nothing; the absolute form must
        # normalize to the same thing rather than inferring the memory-dir name.
        assert normalize_memory_path(os.path.join(str(tmp_path), "root.md")) == "root.md"
        # Outside the memory dir → no usable relative form.
        assert normalize_memory_path("/etc/passwd") is None
    finally:
        config.memory_dir = old


def test_root_level_private_hidden_identically_on_both_path_forms(tmp_path):
    """REGRESSION (finding 4): absolute vs relative must not diverge.

    Before normalization, an absolute root-level path inferred
    ``project/<memory-dir-basename>`` — which, when the memory dir is named
    after the project, made the search surface *show* a memory the digest
    surface hid.
    """
    old = config.memory_dir
    config.memory_dir = str(tmp_path / "palinode")
    os.makedirs(config.memory_dir, exist_ok=True)
    try:
        abs_path = os.path.join(config.memory_dir, "root-note.md")
        with open(abs_path, "w") as f:
            f.write("---\nvisibility: private\n---\nsecret\n")
        chain = ScopeChain(project="palinode")
        assert is_visible(chain, abs_path) is False
        assert is_visible(chain, "root-note.md") is False
    finally:
        config.memory_dir = old


# ── live frontmatter, never the DB cache (finding 3) ──────────────────────


def test_choke_point_ignores_stale_row_metadata(tmp_path):
    """REGRESSION (finding 3): filtering must read the file, not the row.

    The indexer's unchanged-content fast path never re-upserts chunk metadata
    on a frontmatter-only edit, so a memory just marked private still carries
    non-private metadata in the DB.
    """
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    try:
        os.makedirs(os.path.join(tmp_path, "insights"), exist_ok=True)
        path = os.path.join(str(tmp_path), "insights", "scratch.md")
        with open(path, "w") as f:
            f.write("---\nscope: agent/implementer\nvisibility: private\n---\nbody\n")
        # A search row carrying the pre-edit (stale) cached metadata.
        stale_row = {"file_path": path, "metadata": {}, "content": "body"}
        assert filter_visible(CHAIN, [stale_row]) == []
        # Same row, no chain: access control still withholds it.
        assert filter_visible(None, [stale_row]) == []
    finally:
        config.memory_dir = old


def test_unreadable_file_with_nothing_to_evaluate_fails_closed(tmp_path):
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    try:
        missing = os.path.join(str(tmp_path), "decisions", "gone.md")
        assert is_visible(CHAIN, missing) is False
        assert is_visible(None, missing) is False
        # No path at all is also unevaluable → hidden.
        assert is_visible(CHAIN, None) is False
    finally:
        config.memory_dir = old


def test_unreadable_file_falls_back_to_cached_metadata(tmp_path):
    """Index/disk divergence must not silently empty result sets.

    A chunk whose file is gone (deleted behind a stale index) keeps behaving
    as it did before this layer existed — the cached row metadata decides.
    The fallback is last-resort only: when the file *is* readable, live
    frontmatter wins, which is what closes the stale-cache leak.
    """
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    try:
        missing = os.path.join(str(tmp_path), "inbox", "indexed-only.md")
        assert is_visible(None, missing, fallback_metadata={}) is True
        assert is_visible(
            None, missing, fallback_metadata={"visibility": "private", "scope": "agent/x"}
        ) is False
        # Rows for files that were never written still flow through /search.
        assert len(filter_visible(None, [{"file_path": missing, "metadata": {}}])) == 1
    finally:
        config.memory_dir = old


# ── PALINODE_AGENT isolation (env → chain → predicate) ─────────────────────


def _fresh_config(monkeypatch, env: dict[str, str] | None = None) -> config_module.Config:
    for key in ("PALINODE_ORG", "PALINODE_MEMBER", "PALINODE_HARNESS", "PALINODE_AGENT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return config_module.load_config()


def test_palinode_agent_private_scratch_isolated_between_agents(monkeypatch):
    scratch = {"scope": "agent/researcher", "visibility": "private"}
    shared = {"scope": "project/palinode"}

    researcher_cfg = _fresh_config(monkeypatch, {"PALINODE_AGENT": "researcher"})
    researcher_chain = resolve_scope_chain(researcher_cfg, project="palinode")
    implementer_cfg = _fresh_config(monkeypatch, {"PALINODE_AGENT": "implementer"})
    implementer_chain = resolve_scope_chain(implementer_cfg, project="palinode")

    assert visible_on_chain(researcher_chain, scratch) is True
    assert visible_on_chain(implementer_chain, scratch) is False
    assert visible_on_chain(researcher_chain, shared) is True
    assert visible_on_chain(implementer_chain, shared) is True


# ── shared fixture: a memory dir spanning every visibility ────────────────


@pytest.fixture
def visibility_memory_dir(tmp_path):
    old_memory_dir = config.memory_dir
    config.memory_dir = str(tmp_path)

    os.makedirs(os.path.join(tmp_path, "decisions"))
    os.makedirs(os.path.join(tmp_path, "insights"))

    def _write(rel, front, body="body"):
        with open(os.path.join(tmp_path, rel), "w") as f:
            f.write(f"---\n{front}---\n{body}\n")

    _write("decisions/legacy.md", "name: Legacy\ncore: true\n")
    _write("decisions/shared.md", "name: Shared\ncore: true\nscope: project/palinode\n")
    _write("insights/scratch.md",
           "name: Scratch\nscope: agent/researcher\nvisibility: private\n")
    _write("insights/other-scratch.md",
           "name: OtherScratch\nscope: agent/implementer\nvisibility: private\n")
    # A *core* private memory — the SessionStart-hook injection case.
    _write("decisions/core-private.md",
           "name: CorePrivate\ncore: true\nscope: agent/researcher\nvisibility: private\n")
    _write("decisions/secret-alice.md",
           "name: SecretAlice\ncore: true\nscope: org/phasespace\n"
           "visibility: restricted\naccess:\n  - member/alice\n")
    _write("decisions/secret-sarah.md",
           "name: SecretSarah\ncore: true\nscope: org/phasespace\n"
           "visibility: restricted\naccess:\n  - member/sarah\n")

    yield str(tmp_path)
    config.memory_dir = old_memory_dir


# ── collect_memory_files + GET /list (finding 1) ──────────────────────────


def test_collect_without_chain_withholds_private_and_restricted(visibility_memory_dir):
    """REGRESSION (finding 1): no chain still means no private/restricted."""
    names = {r["name"] for r in collect_memory_files()}
    # Inherited memories all pass (scope needs a chain); access-controlled
    # memories never do.
    assert names == {"Legacy", "Shared"}
    assert "Scratch" not in names
    assert "CorePrivate" not in names
    assert "SecretAlice" not in names


def test_list_endpoint_never_injects_private_core_memories(visibility_memory_dir):
    """REGRESSION (finding 1): the SessionStart hook injects from this call."""
    res = client.get("/list?core_only=true")
    assert res.status_code == 200
    names = {d["name"] for d in res.json()}
    assert names == {"Legacy", "Shared"}
    assert "CorePrivate" not in names
    assert "SecretAlice" not in names
    assert "SecretSarah" not in names


def test_list_endpoint_still_returns_scoped_inherited_memories(visibility_memory_dir):
    # The classic /list contract: scope does not filter here.
    names = {d["name"] for d in client.get("/list").json()}
    assert "Shared" in names and "Legacy" in names


def test_collect_chain_applies_scope_and_access(visibility_memory_dir):
    names = {r["name"] for r in collect_memory_files(scope_chain=CHAIN)}
    assert names == {"Legacy", "Shared", "Scratch", "CorePrivate", "SecretAlice"}


def test_collect_composes_with_core_only(visibility_memory_dir):
    names = {r["name"] for r in collect_memory_files(core_only=True, scope_chain=CHAIN)}
    assert names == {"Legacy", "Shared", "CorePrivate", "SecretAlice"}


# ── /context/prime digest ─────────────────────────────────────────────────


def test_digest_classic_mode_still_withholds_access_controlled(visibility_memory_dir):
    """Classic mode is a selection mode, not a way around access control."""
    classic = build_context_digest(project="palinode")
    files = {row["file"] for row in classic["core_memories"]}
    assert "decisions/legacy.md" in files
    assert "decisions/core-private.md" not in files
    assert "decisions/secret-sarah.md" not in files


def test_digest_scoped_mode_applies_chain(visibility_memory_dir):
    scoped = build_context_digest(project="palinode", scope_chain=CHAIN)
    files = {row["file"] for row in scoped["core_memories"]}
    assert "decisions/secret-alice.md" in files
    assert "decisions/core-private.md" in files  # owner is on the chain
    assert "decisions/secret-sarah.md" not in files


# ── _fetch_visible: adaptive widening (finding 6) ─────────────────────────


def _row(path):
    return {"file_path": path, "metadata": {}}


def test_fetch_visible_single_fetch_when_nothing_hidden(visibility_memory_dir):
    calls = []

    def run(n):
        calls.append(n)
        return [_row(os.path.join(visibility_memory_dir, "decisions/legacy.md"))]

    out = _fetch_visible(None, run, 10)
    assert len(out) == 1
    # Nothing hidden → exactly one fetch, at today's limit (ranking preserved).
    assert calls == [10]


def test_fetch_visible_widens_when_window_starved(visibility_memory_dir):
    """REGRESSION (finding 6): hidden rows must not starve the window."""
    hidden = os.path.join(visibility_memory_dir, "insights/other-scratch.md")
    visible = os.path.join(visibility_memory_dir, "decisions/legacy.md")
    calls = []

    def run(n):
        calls.append(n)
        # The window is saturated with hidden rows; visible matches sit below.
        return [_row(hidden)] * n if n <= 2 else [_row(hidden)] * 2 + [_row(visible)]

    out = _fetch_visible(CHAIN, run, 2)
    assert len(calls) == 2 and calls[1] > calls[0]
    assert len(out) == 1  # the visible match below the original window


def test_fetch_visible_does_not_widen_when_store_exhausted(visibility_memory_dir):
    hidden = os.path.join(visibility_memory_dir, "insights/other-scratch.md")
    calls = []

    def run(n):
        calls.append(n)
        return [_row(hidden)]  # fewer rows than asked → nothing more to fetch

    assert _fetch_visible(CHAIN, run, 10) == []
    assert calls == [10]


# ── _resolve_search_scope_chain ───────────────────────────────────────────


def test_resolve_search_chain_none_without_identity():
    assert _resolve_search_scope_chain(SearchRequest(query="x")) is None


def test_resolve_search_chain_derives_project_from_context():
    chain = _resolve_search_scope_chain(
        SearchRequest(query="x", context=["project/palinode", "person/alice"])
    )
    assert chain is not None and "project/palinode" in chain.as_list()


def test_resolve_search_chain_picks_first_project_ref():
    chain = _resolve_search_scope_chain(
        SearchRequest(query="x", context=["harness/x", "project/first", "project/second"])
    )
    assert "project/first" in chain.as_list()
    assert "project/second" not in chain.as_list()


# ── save-time validation (finding 7) ──────────────────────────────────────


@pytest.fixture
def save_client(tmp_path, monkeypatch):
    """TestClient on a fresh tmp memory_dir; git off.

    ``palinode_dir`` is a read-only property delegating to ``memory_dir``, so
    setting ``memory_dir`` is both necessary and sufficient.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    with TestClient(app) as c:
        yield c


def test_save_rejects_private_without_scope(save_client):
    res = save_client.post("/save", json={
        "content": "scratch reasoning", "type": "Insight",
        "metadata": {"visibility": "private"},
    })
    assert res.status_code == 400
    assert "requires an explicit scope" in res.json()["detail"]


def test_save_accepts_private_with_scope(save_client):
    res = save_client.post("/save", json={
        "content": "scratch reasoning", "type": "Insight", "slug": "ok-private",
        "metadata": {"visibility": "private", "scope": "agent/researcher"},
    })
    assert res.status_code == 200


def test_save_rejects_restricted_without_access(save_client):
    res = save_client.post("/save", json={
        "content": "board notes", "type": "Decision",
        "metadata": {"visibility": "restricted"},
    })
    assert res.status_code == 400
    assert "non-empty access" in res.json()["detail"]


def test_save_rejects_invalid_visibility(save_client):
    res = save_client.post("/save", json={
        "content": "x", "type": "Insight",
        "metadata": {"visibility": "secret"},
    })
    assert res.status_code == 400
    assert "Invalid visibility" in res.json()["detail"]


def test_save_unaffected_when_visibility_absent(save_client):
    res = save_client.post("/save", json={
        "content": "ordinary memory", "type": "Insight", "slug": "ordinary",
    })
    assert res.status_code == 200


# ── _fetch_visible: recall counted once across widened passes ──────────────

_EMBED_DIM = 1024


def _unit(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _graded_embedding(rank: int) -> list[float]:
    """Vector whose cosine distance to ``_graded_embedding(0)`` grows with
    ``rank`` — deterministic similarity order (rank 0 is the closest match)."""
    vec = [0.0] * _EMBED_DIM
    vec[0] = 1.0
    vec[1] = rank * 0.05
    return _unit(vec)


def _recall_count(chunk_id: str) -> int:
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT recall_count FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
    finally:
        db.close()
    return row["recall_count"]


@pytest.fixture
def recall_widen_db(tmp_path, monkeypatch):
    """Real SQLite in tmp_path with a private off-chain memory ranked at the top
    of the window, so the visibility gate starves and widens on search."""
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", os.path.join(memory_dir, ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store._db_checked = False
    os.makedirs(os.path.join(memory_dir, "insights"), exist_ok=True)
    store.init_db()

    now_iso = datetime.now(timezone.utc).isoformat()

    def _write_and_index(rank: int, front: str) -> None:
        path = os.path.join(memory_dir, "insights", f"rank{rank}.md")
        with open(path, "w") as f:
            f.write(f"---\n{front}---\nbody {rank}\n")
        store.upsert_chunks([{
            "id": f"rank{rank}",
            "file_path": path,          # absolute so live-frontmatter read works
            "section_id": None,
            "category": "insights",
            "content": f"body {rank}",
            "metadata": {},
            "created_at": now_iso,
            "last_updated": now_iso,
            "embedding": _graded_embedding(rank),
        }])

    # rank 0 is the closest match AND private/off-chain → hidden from CHAIN,
    # which starves the base window and forces the wider re-fetch.
    _write_and_index(0, "name: Hidden\nscope: agent/implementer\nvisibility: private\n")
    for r in range(1, 6):
        _write_and_index(r, f"name: Visible{r}\n")

    yield memory_dir
    store._db_checked = False


def test_widened_fetch_counts_recall_once(recall_widen_db):
    """REGRESSION (#667): a single retrieval that trips visibility-widening must
    increment recall_count exactly once for a row present in both passes.

    Non-vacuous: the closure records each run() call and the test asserts a
    second, wider fetch actually fired — the pass that pre-fix re-recorded
    recall for already-counted rows is genuinely exercised. The closure defaults
    record_access=True, so a regression that stopped threading the count-once
    flag would double-count rank0/rank1 and fail this test.
    """
    query_emb = _graded_embedding(0)
    calls: list[int] = []

    def run(n: int, record_access: bool = True) -> list[dict]:
        calls.append(n)
        return store.search(
            query_embedding=query_emb,
            top_k=n,
            threshold=0.0,
            record_access=record_access,
        )

    base_limit = 2
    result = _fetch_visible(
        CHAIN, run, base_limit,
        record_recall=lambda ids: store.record_recall(ids, mode="explicit"),
    )

    # Widening actually fired: two fetches, the second strictly wider.
    assert len(calls) == 2 and calls[1] > calls[0]
    # rank0 hidden, rank1..5 visible → the five visible rows come back.
    assert {r["id"] for r in result} == {f"rank{r}" for r in range(1, 6)}

    # rank1 sits in BOTH passes (top-2 initial + top-10 widened): counted once.
    assert _recall_count("rank1") == 1
    # rank0 (hidden, top of window, also in both passes) counted once too.
    assert _recall_count("rank0") == 1
    # a row surfaced only by the widened pass is likewise counted exactly once.
    assert _recall_count("rank3") == 1

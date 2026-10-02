"""Rollback across a retirement, and the index convergence under it.

``rollback`` is a git-level revert and git does not know what a retirement is:
reverting the commit that archived or superseded a memory deletes its
``status: archived`` / ``superseded_by`` along with everything else, so the
record comes back *unmarked* — current again. This suite pins the contract
that replaced the silent version:

1. the preview (dry run is the default) names every retirement the range would
   undo — record, relation, and the commit that retired it;
2. applying refuses unless the caller acknowledges it (``--undo-retirements``
   / ``undo_retirements=true``), writes nothing when it refuses, and points at
   ``restore`` / the correction undo instead;
3. an acknowledged apply names every record it resurrected — never plain
   success;
4. afterwards search, resolve and prime agree with the file about currency.

Item 4 is not rollback-specific. The status push the archive op uses
(``store.set_status_for_path``) rewrote the cached chunk metadata without
moving its ``meta_hash``, so the index held *archived metadata under the
active file's hash*. Any later frontmatter change that brought the file back to
that hash — a rollback, or an operator hand-reverting the same lines — planned
as a no-op and search stayed stale. The first two tests show that without
rollback in the picture.

Real SQLite, real git, real files, all under ``tmp_path``; the embedder is a
deterministic bag-of-words hash and the write-path scanner is patched out.
The audit log and ``PALINODE_DIR`` point at ``tmp_path`` so nothing here
writes to a developer's real store.
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

DECISION_TEXT = (
    "Marrowgate Cooperative routes every Saltmarsh Kiln shipment through the "
    "Brindlecote depot, with a weekly tally against the kiln manifest."
)
DECISION_REF = "decisions/saltmarsh-routing"
DECISION_FILE = f"{DECISION_REF}.md"
DECISION_QUERY = "Saltmarsh Kiln shipment Brindlecote depot routing"

SUCCESSOR_TEXT = (
    "Marrowgate Cooperative now routes Saltmarsh Kiln shipments through the "
    "Quillfen yard; the Brindlecote depot closed."
)
SUCCESSOR_REF = "decisions/saltmarsh-routing-quillfen"


def _hash_embed(text: str, backend: str = "local") -> list[float]:
    vec = [0.0] * EMBED_DIM
    for tok in set(text.lower().split()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A disposable git-backed store behind a TestClient; nothing escapes tmp."""
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "test")

    monkeypatch.setenv("PALINODE_DIR", repo)
    monkeypatch.setattr(config, "memory_dir", repo)
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(
        config.audit, "log_path", str(tmp_path / ".audit" / "mcp-calls.jsonl")
    )
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    for key in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)

    import palinode.api.server as srv

    srv = importlib.reload(srv)
    srv._rate_counters.clear()

    # The retrieval logger is built at import time from the process config;
    # redirect every module holding it so /search and /read log under tmp.
    from palinode.core.retrieval_log import RetrievalLogger

    fixture_logger = RetrievalLogger(repo)
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
                yield client, repo
            finally:
                from palinode.indexer import watcher

                watcher.shutdown_handlers()
    srv._rate_counters.clear()


# ── helpers ──────────────────────────────────────────────────────────────────


def _save(client, content: str, slug: str, **extra: Any) -> None:
    body = {"content": content, "type": "Decision", "slug": slug, **extra}
    r = client.post("/save", json=body)
    assert r.status_code == 200, r.text


def _seed(client) -> None:
    _save(client, DECISION_TEXT, "saltmarsh-routing", project="marrowgate")


def _recall_refs(client, query: str = DECISION_QUERY) -> list[str]:
    r = client.post(
        "/search",
        json={"query": query, "limit": 20, "threshold": 0.0, "hybrid": True},
    )
    assert r.status_code == 200, r.text
    return [h.get("rel_path") or h["file_path"] for h in r.json()]


def _archive(client, ref: str = DECISION_FILE, **extra: Any) -> dict:
    r = client.post("/archive", json={"file_path": ref, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _fm(memory_dir: str, rel: str = DECISION_FILE) -> dict[str, Any]:
    with open(os.path.join(memory_dir, rel), encoding="utf-8") as fh:
        return dict(frontmatter.load(fh).metadata)


def _read_bytes(memory_dir: str, rel: str = DECISION_FILE) -> bytes:
    with open(os.path.join(memory_dir, rel), "rb") as fh:
        return fh.read()


def _index_rows(memory_dir: str) -> list[tuple]:
    """Every chunk row's identity, hashes and cached metadata, in a stable order."""
    from palinode.core import store as store_mod

    db = store_mod.get_db()
    try:
        return [
            tuple(r) for r in db.execute(
                "SELECT id, file_path, content_hash, meta_hash, metadata "
                "FROM chunks ORDER BY id"
            ).fetchall()
        ]
    finally:
        db.close()


def _rollback(client, **params: Any):
    return client.post("/rollback", params={"file_path": DECISION_FILE, **params})


def _currency_everywhere(client) -> dict[str, bool]:
    """Does each surface treat the decision as a current, standing claim?"""
    from palinode.consolidation.runner import _get_decisions_for_project

    in_search = DECISION_FILE in _recall_refs(client)

    r = client.post("/resolve", json={"ref": DECISION_REF})
    assert r.status_code == 200, r.text
    in_resolve = "retired" not in json.dumps(r.json())

    r = client.post("/context/prime", json={"project": "marrowgate"})
    assert r.status_code == 200, r.text
    digest = r.json()
    in_prime = DECISION_FILE in {
        row["file"]
        for key in ("core_memories", "recent_decisions", "open_action_items",
                    "recent_snapshots")
        for row in digest.get(key, [])
    }
    in_selector = DECISION_REF in {
        d["ref"] for d in _get_decisions_for_project("marrowgate")
    }
    return {
        "search": in_search,
        "resolve": in_resolve,
        "prime": in_prime,
        "selector": in_selector,
    }


# ═════════════════════════════════════════════════════════════════════════════
# 1. Index convergence — independent of rollback
# ═════════════════════════════════════════════════════════════════════════════


def test_a_plain_frontmatter_only_edit_reaches_search(store):
    """The baseline the meta-hash exists for: a hand edit of lifecycle
    frontmatter, body untouched, is picked up by a reindex."""
    client, memory_dir = store
    _seed(client)
    assert DECISION_FILE in _recall_refs(client)

    path = os.path.join(memory_dir, DECISION_FILE)
    post = frontmatter.load(path)
    post["status"] = "archived"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter.dumps(post) + "\n")

    assert client.post("/reindex").status_code == 200
    assert DECISION_FILE not in _recall_refs(client)


def test_reverting_frontmatter_after_a_status_push_reaches_search(store):
    """The gap, with no rollback involved.

    Archive pushes ``status`` into the index without a reindex. An operator
    then hand-reverts the file to its exact pre-archive bytes — a
    frontmatter-only edit, body unchanged. The file says current; a reindex
    must make search say so too. Before the fix the status push left the
    row's ``meta_hash`` describing the *pre-archive* frontmatter while its
    metadata said archived, so the reverted file hashed identically and the
    reindex planned a no-op.
    """
    client, memory_dir = store
    _seed(client)
    before = _read_bytes(memory_dir)

    _archive(client, reason="the depot closed")
    assert DECISION_FILE not in _recall_refs(client)

    with open(os.path.join(memory_dir, DECISION_FILE), "wb") as fh:
        fh.write(before)
    assert "status" not in _fm(memory_dir)

    assert client.post("/reindex").status_code == 200
    assert DECISION_FILE in _recall_refs(client)


@pytest.mark.parametrize(
    ("pusher", "arg"),
    [("set_status_for_path", "archived"), ("set_entities_for_path", ["project/x"])],
)
def test_every_metadata_push_moves_meta_hash_with_it(tmp_path, monkeypatch, pusher, arg):
    """The invariant the fix restores, at the store level: after a direct
    metadata push the row's ``meta_hash`` is the hash of the metadata it now
    holds, so the reconcile planner cannot mistake it for a file whose
    frontmatter it does not describe."""
    from palinode.core import store as store_mod

    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    store_mod.init_db()
    original = {"type": "Decision", "category": "decisions"}
    db = store_mod.get_db()
    try:
        db.execute(
            "INSERT INTO chunks (id, file_path, section_id, category, content, "
            "metadata, content_hash, meta_hash) VALUES (?,?,?,?,?,?,?,?)",
            ("c1", "/x/decisions/a.md", "root", "decisions", "body",
             json.dumps(original), "h", store_mod.meta_hash(original)),
        )
        db.commit()
    finally:
        db.close()

    assert getattr(store_mod, pusher)("/x/decisions/a.md", arg) == 1

    db = store_mod.get_db()
    try:
        row = db.execute("SELECT metadata, meta_hash FROM chunks").fetchone()
    finally:
        db.close()
    stored = json.loads(row["metadata"])
    assert stored != original
    assert row["meta_hash"] == store_mod.meta_hash(stored)
    assert row["meta_hash"] != store_mod.meta_hash(original)


# ═════════════════════════════════════════════════════════════════════════════
# 2. The acknowledgement contract
# ═════════════════════════════════════════════════════════════════════════════


def test_preview_names_the_retirement_its_relation_and_commit(store):
    client, memory_dir = store
    _seed(client)
    _save(client, SUCCESSOR_TEXT, "saltmarsh-routing-quillfen", project="marrowgate")
    _archive(client, reason="the depot closed", superseded_by=SUCCESSOR_REF)
    retiring_sha = _git(memory_dir, "log", "-1", "--format=%h", "--", DECISION_FILE)
    head_before = _git(memory_dir, "rev-parse", "HEAD")
    bytes_before = _read_bytes(memory_dir)

    r = _rollback(client)  # dry run is the default
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "preview"
    assert body["resurrected"] == []
    [entry] = body["retirements"]
    assert entry["record"] == DECISION_FILE
    assert entry["relation"] == f"status:archived, superseded_by: {SUCCESSOR_REF}"
    assert entry["superseded_by"] == SUCCESSOR_REF
    assert entry["commit"] == retiring_sha
    assert "supersede" in entry["commit_subject"]

    text = body["result"]
    assert "Dry Run" in text
    assert "would undo 1 retirement" in text
    for needle in (DECISION_FILE, SUCCESSOR_REF, retiring_sha, "palinode restore",
                   "--undo-retirements"):
        assert needle in text, needle

    # A preview is a preview.
    assert _read_bytes(memory_dir) == bytes_before
    assert _git(memory_dir, "rev-parse", "HEAD") == head_before


def test_an_ordinary_edit_names_no_retirement_and_applies_plainly(store):
    client, memory_dir = store
    _seed(client)
    path = os.path.join(memory_dir, DECISION_FILE)
    post = frontmatter.load(path)
    post.content += "\n\nTallies move to Thursdays."
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter.dumps(post) + "\n")
    _git(memory_dir, "commit", "-qam", "edit")

    preview = _rollback(client).json()
    assert preview["status"] == "preview"
    assert preview["retirements"] == []
    assert "retirement" not in preview["result"]

    applied = _rollback(client, dry_run=False).json()
    assert applied["status"] == "rolled_back"
    assert applied["resurrected"] == []
    assert "Thursdays" not in _read_bytes(memory_dir).decode()


def test_apply_without_acknowledgement_refuses_and_writes_nothing(store):
    client, memory_dir = store
    _seed(client)
    _archive(client, reason="the depot closed")
    retiring_sha = _git(memory_dir, "log", "-1", "--format=%h", "--", DECISION_FILE)

    bytes_before = _read_bytes(memory_dir)
    head_before = _git(memory_dir, "rev-parse", "HEAD")
    status_before = _git(memory_dir, "status", "--porcelain")
    index_before = _index_rows(memory_dir)
    assert index_before

    for params in ({"dry_run": False}, {"dry_run": False, "undo_retirements": False}):
        r = _rollback(client, **params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "refused"
        assert body["resurrected"] == []
        assert [e["commit"] for e in body["retirements"]] == [retiring_sha]
        text = body["result"]
        assert text.startswith("Refused")
        assert "Nothing was written" in text
        assert "palinode restore" in text and "corrections undo" in text
        assert "--undo-retirements" in text

    assert _read_bytes(memory_dir) == bytes_before
    assert _git(memory_dir, "rev-parse", "HEAD") == head_before
    assert _git(memory_dir, "status", "--porcelain") == status_before
    assert _index_rows(memory_dir) == index_before
    assert _fm(memory_dir)["status"] == "archived"
    assert DECISION_FILE not in _recall_refs(client)


def test_apply_with_acknowledgement_names_what_it_resurrected(store):
    client, memory_dir = store
    _seed(client)
    _archive(client, reason="the depot closed")
    retiring_sha = _git(memory_dir, "log", "-1", "--format=%h", "--", DECISION_FILE)
    surfaces = ("search", "resolve", "prime", "selector")
    assert _currency_everywhere(client) == dict.fromkeys(surfaces, False)

    r = _rollback(client, dry_run=False, undo_retirements=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "undid_retirements"
    assert body["index_converged"] is True
    [entry] = body["resurrected"]
    assert entry["record"] == DECISION_FILE
    assert entry["relation"] == "status:archived"
    assert entry["commit"] == retiring_sha
    text = body["result"]
    assert "UNDID 1 retirement" in text
    assert DECISION_FILE in text and retiring_sha in text

    # The commit says so too.
    assert "undid 1 retirement" in _git(memory_dir, "log", "-1", "--format=%s")
    assert "status" not in _fm(memory_dir)

    # Every surface agrees with the file: immediately, and after a reindex.
    assert _currency_everywhere(client) == dict.fromkeys(surfaces, True)
    assert client.post("/reindex").status_code == 200
    assert _currency_everywhere(client) == dict.fromkeys(surfaces, True)


def test_a_retracted_mention_is_a_retirement_too(store):
    """In-body retirements count: un-striking a retracted mention is named."""
    client, memory_dir = store
    _seed(client)
    path = os.path.join(memory_dir, DECISION_FILE)
    post = frontmatter.load(path)
    post.content = (
        post.content
        + "\n\nThe tally is weekly. ~~Tallies are read aloud at the depot "
        "gate~~ [RETRACTED 2026-09-20 r:0a1b2c3d]. The manifest is kept."
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter.dumps(post) + "\n")
    _git(memory_dir, "commit", "-qam", "retract a mention")
    retiring_sha = _git(memory_dir, "rev-parse", "--short", "HEAD")

    body = _rollback(client, dry_run=False).json()
    assert body["status"] == "refused"
    assert [(e["relation"], e["commit"]) for e in body["retirements"]] == [
        ("retracted mention r:0a1b2c3d", retiring_sha)
    ]


# ═════════════════════════════════════════════════════════════════════════════
# 3. Parity: CLI and MCP carry the acknowledgement through to the same API
# ═════════════════════════════════════════════════════════════════════════════


def test_cli_refuses_with_exit_1_and_forwards_the_flag(store):
    from click.testing import CliRunner

    from palinode.cli.git import rollback as rollback_cmd

    client, memory_dir = store
    _seed(client)
    _archive(client, reason="the depot closed")
    seen: list[dict] = []

    def _via_api(file_path, commit=None, dry_run=True, undo_retirements=False):
        params = {"file_path": file_path, "dry_run": dry_run,
                  "undo_retirements": undo_retirements}
        seen.append(params)
        return client.post("/rollback", params=params).json()

    with patch("palinode.cli.git.api_client.rollback", side_effect=_via_api):
        refused = CliRunner().invoke(rollback_cmd, [DECISION_FILE, "--no-dry-run"])
        assert refused.exit_code == 1, refused.output
        assert "Refused" in refused.output
        assert _fm(memory_dir)["status"] == "archived"

        applied = CliRunner().invoke(
            rollback_cmd, [DECISION_FILE, "--no-dry-run", "--undo-retirements"]
        )
        assert applied.exit_code == 0, applied.output
        assert "undid_retirements" in applied.output
    assert [p["undo_retirements"] for p in seen] == [False, True]
    assert "status" not in _fm(memory_dir)


def test_cli_client_sends_the_flag_to_the_api():
    """The HTTP client puts ``undo_retirements`` on the wire, both ways."""
    from palinode.cli._api import api_client

    sent: list[dict] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "preview"}

    def _post(path, params=None, timeout=None):
        sent.append(params)
        return _Resp()

    with patch.object(api_client.client, "post", side_effect=_post):
        api_client.rollback(DECISION_FILE)
        api_client.rollback(DECISION_FILE, dry_run=False, undo_retirements=True)
    assert [p["undo_retirements"] for p in sent] == [False, True]


@pytest.mark.asyncio
async def test_mcp_tool_forwards_the_acknowledgement(store, tmp_path, monkeypatch):
    import palinode.mcp as mcp_mod
    from palinode.core.audit import AuditLogger

    client, memory_dir = store
    monkeypatch.setattr(mcp_mod, "_audit", AuditLogger(str(tmp_path), config.audit))
    _seed(client)
    _archive(client, reason="the depot closed")

    async def _post_params(path, params=None, timeout=30.0):
        return client.post(path, params=params)

    monkeypatch.setattr(mcp_mod, "_post_params", _post_params)

    [refused] = await mcp_mod._tool_rollback(
        {"file_path": DECISION_FILE, "dry_run": False}
    )
    assert refused.text.startswith("Refused")
    assert _fm(memory_dir)["status"] == "archived"

    [applied] = await mcp_mod._tool_rollback(
        {"file_path": DECISION_FILE, "dry_run": False, "undo_retirements": True}
    )
    assert "UNDID 1 retirement" in applied.text
    assert "status" not in _fm(memory_dir)

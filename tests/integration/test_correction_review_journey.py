"""The acceptance journey: record A, correct it to B, and prove B is what stands.

Real SQLite, real git, real markdown files under ``tmp_path``. Nothing about
the database is mocked. The only mock in the file is the consolidation model's
response; what this test measures is what the consolidation pass then wrote.

The shape, end to end:

  1. save decision **A** (plus an unaffected neighbour, and the requirement
     that actually supports the correction);
  2. **PREVIEW** the correction — writes nothing, returns A's exact revision;
  3. **APPLY** with that revision and explicit confirmation;
  4. run a consolidation pass;
  5. **restart** — a brand-new store handle, as a fresh session gets;
  6. ask through the real API and MCP code paths: search, ``/resolve``,
     ``/context/prime``, and the ``core_only`` listing the SessionStart hook
     injects from;
  7. repeat 4–6, because a second pass over an already-corrected store is
     where a fork into history would show up.

What "B stands" has to mean, or the test proves nothing: B is returned **with
its evidence** (the separately saved requirement it is `backed_by`), and A is
returned only as labelled history — archived, superseded_by B, and absent from
default recall. The archived original is lineage, not support: a verified quote
establishes what a source said, never that the claim is true.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

_FAKE_VECTOR = [0.01] * 1024

A_SLUG = "harbor-notes-storage"
A_REF = f"decisions/{A_SLUG}.md"
A_TEXT = "Use SQLite for the local prototype because it runs as a single-user desktop app."
B_SLUG = "harbor-notes-storage-shared"
B_REF = f"decisions/{B_SLUG}.md"
B_TEXT = (
    "Use the shared hosted service for storage because concurrent writers need "
    "transactional coordination."
)
REQUIREMENT_SLUG = "harbor-notes-concurrent-write-requirement"
REQUIREMENT_REF = f"decisions/{REQUIREMENT_SLUG}.md"
REQUIREMENT_TEXT = (
    "The shared hosted service requires transactional coordination for concurrent writers."
)
NEIGHBOUR_REF = "decisions/harbor-notes-timestamps.md"
NEIGHBOUR_TEXT = "Store event timestamps in UTC."


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture()
def journey_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A disposable store with a real git repo behind it."""
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", False)
    monkeypatch.setattr(config.services.api, "host", "127.0.0.1")
    monkeypatch.setattr(config.consolidation, "status_log_retention_days", 0)
    if hasattr(config.search, "retrieval_mode"):
        monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    for directory in ("people", "projects", "decisions", "insights", "research", "daily"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "prompts").mkdir(parents=True, exist_ok=True)
    for prompt in ("compaction.md", "nightly-consolidation.md"):
        (tmp_path / "specs" / "prompts" / prompt).write_text(
            "Return consolidation operations as a JSON array.\n", encoding="utf-8"
        )

    from palinode.core import store

    store.init_db()
    return tmp_path


@pytest.fixture()
def api(journey_store: Path) -> TestClient:
    from palinode.api.server import _rate_counters, app

    _rate_counters.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    _rate_counters.clear()


@pytest.fixture()
def mcp(api: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The real MCP dispatcher, over the real in-process API."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query
        if isinstance(query, bytes):
            query = query.decode("latin-1")
        path = request.url.path + (f"?{query}" if query else "")
        response = api.request(
            request.method, path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(
            response.status_code, headers=dict(response.headers), content=response.content
        )

    from palinode import mcp as palinode_mcp

    monkeypatch.setattr(palinode_mcp, "_http_transport", httpx.MockTransport(handler))

    def call(name: str, arguments: dict) -> str:
        return asyncio.run(palinode_mcp._dispatch_tool(name, arguments))[0].text

    return call


def _deterministic(fn):
    """Run *fn* with the embedder and the content scanner stubbed.

    The database is real; these two are the network. A constant vector means
    every hit is cosine 1.0, so retrieval ordering is not what any assertion
    here depends on — presence and absence are.
    """
    def wrapper(*args, **kwargs):
        with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR), patch(
            "palinode.core.store.scan_memory_content", return_value=(True, "OK")
        ):
            return fn(*args, **kwargs)

    return wrapper


def _save(api: TestClient, **body) -> dict:
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR), patch(
        "palinode.core.store.scan_memory_content", return_value=(True, "OK")
    ):
        response = api.post("/save", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _seed(api: TestClient) -> None:
    """A, its unaffected neighbour, and the requirement that supports B."""
    _save(
        api,
        content=A_TEXT,
        type="Decision",
        slug=A_SLUG,
        project="harbor-notes",
        title="Harbor Notes local storage",
    )
    _save(
        api,
        content=NEIGHBOUR_TEXT,
        type="Decision",
        slug="harbor-notes-timestamps",
        project="harbor-notes",
        title="Harbor Notes timestamps",
    )
    _save(
        api,
        content=REQUIREMENT_TEXT,
        type="Decision",
        slug=REQUIREMENT_SLUG,
        project="harbor-notes",
        title="Harbor Notes concurrent-write requirement",
    )


def _restart() -> int:
    """Open a genuinely fresh store handle, the way a new process does.

    ``get_db()`` opens a new connection per call, but ``_db_checked`` is the
    one piece of per-process state that survives — reset it so the next open
    re-runs the DB/memory-dir consistency check a cold process would. Returns
    the chunk count read through the new handle, so this can never be a silent
    no-op that makes the "after restart" assertions meaningless.
    """
    from palinode.core import store

    store._db_checked = False
    db = store.get_db()
    try:
        return int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    finally:
        db.close()


def _consolidation_pass() -> dict:
    """One consolidation pass with a KEEP-only proposal.

    A KEEP-only proposal is deliberate: this test is about whether a *reviewed*
    correction survives consolidation, not about what a model would additionally
    propose. A pass that rewrote the store would make the later assertions
    ambiguous.
    """
    from palinode.consolidation import runner

    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR), patch(
        "palinode.core.store.scan_memory_content", return_value=(True, "OK")
    ):
        return runner.run_consolidation(llm_fn=lambda _s, _u: (json.dumps([]), "fake-model"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── the journey ─────────────────────────────────────────────────────────────
def test_decision_a_corrected_to_b_survives_consolidation_and_restart(
    api: TestClient, journey_store: Path, mcp
) -> None:
    _seed(api)
    neighbour_before = (journey_store / NEIGHBOUR_REF).read_bytes()

    # ── PREVIEW ─────────────────────────────────────────────────────────────
    preview = api.post(
        "/corrections/preview",
        json={
            "target": A_REF,
            "replacement": B_TEXT,
            "reason": "concurrent writers need transactional coordination",
            "project": "harbor-notes",
            "slug": B_SLUG,
            # B's own support. The archived A is lineage, never repurposed as
            # the reason B is right — the failure the Quickstart journey fixed.
            "backed_by": [REQUIREMENT_REF.removesuffix(".md")],
        },
    )
    assert preview.status_code == 200, preview.text
    previewed = preview.json()
    assert previewed["applied"] is False
    assert previewed["action"] == "supersede"
    assert previewed["old_text"].strip().startswith(A_TEXT[:20])
    assert previewed["new_text"] == B_TEXT
    assert previewed["target"]["revision_basis"] == "file_sha256"
    assert previewed["target"]["visibility"]["hidden"] is False
    assert previewed["relation"]["supersedes"] == A_REF
    assert previewed["evidence"]["backed_by"] == [REQUIREMENT_REF.removesuffix(".md")]
    assert previewed["recovery"]["undo_preview_command"].startswith("palinode corrections undo")
    assert "verified quote establishes what a source said" in " ".join(previewed["notes"])
    revision = previewed["confirm"]["expect_revision"]

    # A preview writes nothing: the file is byte-identical after it.
    assert previewed["target"]["status"] == "active"
    assert A_TEXT in _read(journey_store / A_REF)

    # ── APPLY ───────────────────────────────────────────────────────────────
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR), patch(
        "palinode.core.store.scan_memory_content", return_value=(True, "OK")
    ):
        applied = api.post(
            "/corrections/apply",
            json={
                "target": A_REF,
                "replacement": B_TEXT,
                "reason": "concurrent writers need transactional coordination",
                "project": "harbor-notes",
                "slug": B_SLUG,
                "expect_revision": revision,
                "confirm": True,
                "backed_by": [REQUIREMENT_REF.removesuffix(".md")],
            },
        )
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["applied"] is True
    assert result["archived"]["status"] == "archived"
    assert result["archived"]["superseded_by"] == B_REF
    assert result["replacement"]["rel_path"] == B_REF
    assert result["actor"].startswith("reviewed-correction")
    assert result["committed"] is True

    # ── git provenance ──────────────────────────────────────────────────────
    log = subprocess.run(
        ["git", "-C", str(journey_store), "log", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "supersede: decisions/harbor-notes-storage" in log
    assert "actor: reviewed-correction" in log
    history_sibling = journey_store / "decisions" / f"{A_SLUG}-history.md"
    assert history_sibling.exists()
    assert "Superseded by" in _read(history_sibling)

    # The unaffected neighbour is byte-identical.
    assert (journey_store / NEIGHBOUR_REF).read_bytes() == neighbour_before

    # ── two consolidation passes, each followed by a restart ────────────────
    for round_number in (1, 2):
        _consolidation_pass()
        assert _restart() > 0, "the fresh handle must see a populated index"
        _assert_b_stands(api, mcp, journey_store, round_number)


def _assert_b_stands(api: TestClient, mcp, store_dir: Path, round_number: int) -> None:
    """Everything a fresh session can ask, asked through the real code paths."""
    where = f"(after consolidation pass {round_number} + restart)"

    # A on disk: archived, pointing at B, content intact — history, not deletion.
    a_text = _read(store_dir / A_REF)
    assert "status: archived" in a_text, where
    assert f"superseded_by: {B_REF.removesuffix('.md')}" in a_text, where
    assert A_TEXT in a_text, f"the original statement must survive as history {where}"

    # 1. Search — the default recall surface.
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        search = api.post(
            "/search", json={"query": "harbor notes storage shared concurrent writers", "limit": 10}
        )
    assert search.status_code == 200, search.text
    payload = search.json()
    rows = payload["results"] if isinstance(payload, dict) else payload
    files = " ".join(str(row.get("file_path", "")) for row in rows)
    assert B_SLUG in files, f"B must be recallable {where}"
    assert A_TEXT not in json.dumps(rows), f"A must not be served as current {where}"

    # 2. /resolve — the bounded-resolution surface, with evidence.
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        resolved = api.post(
            "/resolve",
            json={"query": "harbor notes shared storage decision", "resolve": "linked"},
        )
    assert resolved.status_code == 200, resolved.text
    resolved_text = json.dumps(resolved.json())
    assert B_SLUG in resolved_text, f"resolution must reach B {where}"

    # 3. /context/prime — what a fresh session is handed at start.
    primed = api.post("/context/prime", json={"project": "harbor-notes"})
    assert primed.status_code == 200, primed.text
    primed_text = json.dumps(primed.json())
    assert A_TEXT not in primed_text, f"a retired decision must not be primed {where}"

    # 4. GET /list?core_only=true — the SessionStart hook's injection source.
    listing = api.get("/list", params={"core_only": "true"})
    assert listing.status_code == 200, listing.text
    assert A_REF not in [row["file"] for row in listing.json()], where

    # 5. The same questions through the real MCP code path.
    read_a = mcp("palinode_read", {"file_path": A_REF, "meta": True})
    assert "archived" in read_a, f"MCP must label A as archived {where}"
    assert B_SLUG in read_a, f"MCP must show A's successor {where}"

    read_b = mcp("palinode_read", {"file_path": B_REF, "meta": True})
    assert REQUIREMENT_SLUG in read_b, f"B must carry its evidence {where}"

    # 6. The unaffected neighbour is still a separate, live record.
    neighbour = mcp("palinode_read", {"file_path": NEIGHBOUR_REF})
    assert NEIGHBOUR_TEXT in neighbour, where
    assert "status: archived" not in _read(store_dir / NEIGHBOUR_REF), where


def test_a_corrected_core_decision_leaves_the_session_start_listing(
    api: TestClient, journey_store: Path
) -> None:
    """The correction must also reach the first thing a fresh session reads."""
    _save(
        api,
        content=A_TEXT,
        type="Decision",
        slug=A_SLUG,
        project="harbor-notes",
        core=True,
        title="Harbor Notes local storage",
    )
    preview = api.post(
        "/corrections/preview",
        json={"target": A_REF, "replacement": B_TEXT, "reason": "concurrent writers"},
    ).json()

    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR), patch(
        "palinode.core.store.scan_memory_content", return_value=(True, "OK")
    ):
        applied = api.post(
            "/corrections/apply",
            json={
                "target": A_REF,
                "replacement": B_TEXT,
                "reason": "concurrent writers",
                "slug": B_SLUG,
                "expect_revision": preview["confirm"]["expect_revision"],
                "confirm": True,
            },
        )
    assert applied.status_code == 200, applied.text
    _restart()

    core_rows = [row["file"] for row in api.get("/list", params={"core_only": "true"}).json()]
    assert A_REF not in core_rows, (
        "a superseded core decision is injected at session start beside its "
        "replacement, with nothing marking which one stands"
    )

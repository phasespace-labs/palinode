"""#186 — fresh-session context validation, end-to-end (ADR-012 Layer 4).

The cross-session test gap the issue named: unit coverage exists for the
digest builder, the endpoint, and each surface in isolation
(``test_context_prime.py`` / ``test_context_prime_262.py`` /
``test_session_start_hook.py``), but nothing exercises the *fresh-session
sequence* whole — a cold client with a populated store getting the right
context, with the failure modes visible rather than silent.

These are integration-style: a realistic multi-project store on disk (real
frontmatter files, real SQLite for the save path — never a mocked DB per the
repo rule), then the exact cold-start sequence a fresh session runs:

  a. the hook-shaped ``POST /context/prime`` returns the right project's
     digest and never bleeds the distractor project;
  b. the same digest reaches the ``palinode_session_init`` MCP tool and the
     CLI renderer (``format_context_digest``);
  c. degraded paths stay sane and *labelled* — no resolvable project →
     core-only, empty store → a coherent "nothing yet" digest, malformed
     frontmatter files don't crash the scan;
  d. cross-session persistence — a memory written through the real ``/save``
     path is visible to a genuinely fresh app/client instance, because the
     on-disk memory dir is the only channel between the two "sessions".

The MCP-surface tests route ``mcp._post`` through the FastAPI ``TestClient``
so ``palinode_session_init`` returns the *real* digest for the *real* store
(the 262 unit test stubs ``_post`` with a hand-built payload; this closes the
loop the issue asked to close).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api import server as srv
from palinode.api.server import app
from palinode.core.config import config
from palinode.core.context_prime import (
    build_context_digest,
    format_context_digest,
)

# Module-level client for the read-only /context/prime path (frontmatter-only;
# no lifespan/DB needed — mirrors test_context_prime_262.py). The /save path
# below opens its own lifespan-managed client because init_db runs at startup.
client = TestClient(app)

_FAKE_VECTOR = [0.01] * 1024


def _seed(memory_dir: Path, rel: str, meta: dict[str, Any], body: str = "body") -> Path:
    """Write a frontmatter memory file (the test_context_prime_262 idiom)."""
    p = memory_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\n{yaml.safe_dump(meta, default_flow_style=False)}---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def _files_in(section: list[dict[str, str]]) -> set[str]:
    return {row["file"] for row in section}


def _all_files(digest: dict[str, Any]) -> set[str]:
    return (
        _files_in(digest["core_memories"])
        | _files_in(digest["recent_decisions"])
        | _files_in(digest["open_action_items"])
        | _files_in(digest["recent_snapshots"])
    )


@pytest.fixture
def populated_store(tmp_path, monkeypatch):
    """A realistic multi-project store.

    * two global ``core: true`` memories (one Insight, one Decision) — the
      "core memories show up" case;
    * project ``alpha`` — the session that resolves — with two Decisions and
      one open + one done ActionItem;
    * project ``beta`` — the distractor — whose rows must never surface in
      alpha's digest.

    No explicit ``scope:`` on any file, so the default ``scoped`` prime mode
    admits all of them (ADR-009 §7: no scope = works as before) — this is the
    ordinary cross-project store, which is exactly what must not bleed.
    """
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.context, "auto_detect", True)

    # Global core memories (not project-scoped — core is a global section).
    _seed(tmp_path, "insights/core-executor.md",
          {"type": "Insight", "core": True, "title": "Deterministic executor",
           "description": "Ops applied deterministically, not by the LLM"})
    _seed(tmp_path, "decisions/adopt-rrf.md",
          {"type": "Decision", "core": True, "title": "Adopt RRF fusion",
           "description": "Hybrid BM25 + vector fused with RRF"})

    # alpha — the resolved project.
    _seed(tmp_path, "decisions/alpha-schema.md",
          {"type": "Decision", "entities": ["project/alpha"],
           "title": "Alpha chunk schema v2", "description": "Chose the v2 schema"})
    _seed(tmp_path, "decisions/alpha-transport.md",
          {"type": "Decision", "entities": ["project/alpha"],
           "title": "Alpha HTTP transport"})
    _seed(tmp_path, "inbox/alpha-open.md",
          {"type": "ActionItem", "entities": ["project/alpha"],
           "title": "Wire the CLI renderer"})
    _seed(tmp_path, "inbox/alpha-done.md",
          {"type": "ActionItem", "entities": ["project/alpha"], "status": "done",
           "title": "Closed alpha task"})

    # beta — the distractor. Same shapes, different project entity.
    _seed(tmp_path, "decisions/beta-secret.md",
          {"type": "Decision", "entities": ["project/beta"],
           "title": "Beta-only decision"})
    _seed(tmp_path, "inbox/beta-open.md",
          {"type": "ActionItem", "entities": ["project/beta"],
           "title": "Beta-only task"})

    yield tmp_path


# ── (a) hook-shaped POST /context/prime: right project, no distractor bleed ──


def test_hook_shaped_prime_returns_right_project_digest(populated_store):
    """The exact ``{cwd, session_id}`` body the SessionStart hook POSTs, against
    a real populated store, must return alpha's digest."""
    res = client.post(
        "/context/prime",
        json={"cwd": "/home/dev/alpha", "session_id": "fresh-abc-123"},
    )
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["project"] == "project/alpha"
    # Core memories are the global core: true files (not project-filtered).
    assert _files_in(data["core_memories"]) == {
        "insights/core-executor.md",
        "decisions/adopt-rrf.md",
    }
    # Recent decisions are alpha's — and only alpha's.
    assert _files_in(data["recent_decisions"]) == {
        "decisions/alpha-schema.md",
        "decisions/alpha-transport.md",
    }
    # Open action items exclude the done one.
    assert _files_in(data["open_action_items"]) == {"inbox/alpha-open.md"}
    # The content-free recall contract rides along (issue point 5: the
    # CLAUDE.md "search first" instruction, made structural).
    assert "palinode_search" in data["_palinode_hint"]


def test_prime_never_bleeds_the_distractor_project(populated_store):
    """No beta-scoped row may appear anywhere in alpha's digest — and vice
    versa. This is the "memory you can't trust is worse than none" guard."""
    alpha = client.post("/context/prime", json={"cwd": "/home/dev/alpha"}).json()
    beta = client.post("/context/prime", json={"cwd": "/home/dev/beta"}).json()

    alpha_files = _all_files(alpha)
    assert "decisions/beta-secret.md" not in alpha_files
    assert "inbox/beta-open.md" not in alpha_files

    beta_files = _all_files(beta)
    assert "decisions/alpha-schema.md" not in beta_files
    assert "decisions/alpha-transport.md" not in beta_files
    assert "inbox/alpha-open.md" not in beta_files
    # beta still gets its own scoped rows + the global core.
    assert _files_in(beta["recent_decisions"]) == {"decisions/beta-secret.md"}
    assert _files_in(beta["open_action_items"]) == {"inbox/beta-open.md"}


def test_hook_shaped_prime_carries_scope_fields(populated_store):
    """The endpoint (not the bare digest core) extends the response with the
    ADR-009 scope fields — a fresh session gets mode + the resolved chain."""
    data = client.post(
        "/context/prime",
        json={"cwd": "/home/dev/alpha", "session_id": "fresh-abc-123"},
    ).json()
    assert data["mode"] == "scoped"  # config default
    assert "project/alpha" in data["scope_chain"]
    assert "session/fresh-abc-123" in data["scope_chain"]


# ── (b) same digest via the MCP session_init tool and the CLI renderer ───────


@pytest.mark.asyncio
async def test_session_init_renders_real_digest(populated_store, monkeypatch):
    """``palinode_session_init`` for a fresh MCP-only client, routed through the
    real endpoint + store, renders alpha's context — no distractor, hint present."""
    import palinode.mcp as mcp

    async def _post_via_testclient(path, json=None, timeout=30.0):
        return client.post(path, json=json or {})

    monkeypatch.setattr(mcp, "_post", _post_via_testclient)

    result = await mcp._dispatch_tool(
        "palinode_session_init", {"cwd": "/home/dev/alpha"}
    )
    text = result[0].text

    assert "## Session context: project/alpha" in text
    assert "Deterministic executor" in text          # a core memory
    assert "Alpha chunk schema v2" in text            # an alpha decision
    assert "Wire the CLI renderer" in text            # the open action item
    assert "Beta-only decision" not in text           # no distractor bleed
    assert "Closed alpha task" not in text            # done item excluded
    assert "palinode_search" in text                  # the recall hint


def test_cli_renderer_on_real_digest(populated_store):
    """The CLI path renders the endpoint's JSON through ``format_context_digest``
    (exactly what ``palinode prime`` does). Same fresh-session content, human
    text — sections labelled, hint present, distractor absent."""
    digest = client.post("/context/prime", json={"cwd": "/home/dev/alpha"}).json()
    text = format_context_digest(digest)

    assert "## Session context: project/alpha" in text
    assert "### Core memories" in text
    assert "### Recent decisions" in text
    assert "### Open action items" in text
    assert "[decisions/alpha-schema.md]" in text
    assert "[inbox/alpha-open.md]" in text
    assert "beta-secret" not in text
    assert "palinode_search" in text


# ── (c) degraded paths — sane and clearly labelled, never a silent crash ─────


def test_no_resolvable_project_is_core_only_and_labelled(populated_store, monkeypatch):
    """No cwd, no project, auto-detect off (the Claude Desktop shape): the
    digest degrades to core memories only and *says so* — it never guesses a
    project and never emits another project's rows."""
    monkeypatch.setattr(config.context, "auto_detect", False)

    data = client.post("/context/prime", json={}).json()
    assert data["project"] is None
    assert _files_in(data["core_memories"]) == {
        "insights/core-executor.md",
        "decisions/adopt-rrf.md",
    }
    assert data["recent_decisions"] == []
    assert data["open_action_items"] == []

    text = format_context_digest(data)
    assert "no project resolved — core memories only" in text


def test_empty_store_gives_a_sane_digest(tmp_path, monkeypatch):
    """A populated-nowhere store: the endpoint 200s, sections are empty, and
    the rendered digest says "nothing yet" rather than looking broken."""
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.context, "auto_detect", False)

    res = client.post("/context/prime", json={})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["project"] is None
    assert data["core_memories"] == []
    assert data["recent_decisions"] == []
    assert data["open_action_items"] == []

    text = format_context_digest(data)
    assert "(no memories in scope yet)" in text
    assert "palinode_search" in text  # the hint still rides along


def test_malformed_frontmatter_does_not_break_the_scan(tmp_path, monkeypatch):
    """A fresh session must not be blinded by one bad file. Broken YAML, a
    non-mapping frontmatter block, and a wrong-typed ``entities`` value are all
    skipped/tolerated — the valid core memory still surfaces and no request 500s."""
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.context, "auto_detect", True)

    # The one good file that must survive the scan.
    _seed(tmp_path, "insights/good-core.md",
          {"type": "Insight", "core": True, "title": "Survivor"})

    # Broken YAML frontmatter — unbalanced brackets.
    (tmp_path / "decisions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "decisions" / "broken-yaml.md").write_text(
        "---\ntitle: [unclosed\n  : :\n---\nbody\n", encoding="utf-8"
    )
    # Frontmatter that is a YAML sequence, not a mapping.
    (tmp_path / "decisions" / "not-a-mapping.md").write_text(
        "---\n- just\n- a\n- list\n---\nbody\n", encoding="utf-8"
    )
    # Valid YAML but wrong-typed entities (string, not list) for the resolved
    # project — must not be treated as carrying that entity, and must not crash.
    _seed(tmp_path, "decisions/wrong-typed-entities.md",
          {"type": "Decision", "entities": "project/alpha", "title": "Mistyped"})

    res = client.post("/context/prime", json={"cwd": "/home/dev/alpha"})
    assert res.status_code == 200, res.text
    data = res.json()

    assert "insights/good-core.md" in _files_in(data["core_memories"])
    scanned = _all_files(data)
    assert "decisions/broken-yaml.md" not in scanned
    assert "decisions/not-a-mapping.md" not in scanned
    # String entities aren't a list membership → not selected as an alpha decision.
    assert "decisions/wrong-typed-entities.md" not in scanned

    # And the builder called directly is equally unbothered (the scan core).
    direct = build_context_digest(cwd="/home/dev/alpha")
    assert "insights/good-core.md" in _files_in(direct["core_memories"])


# ── (d) cross-session persistence: real /save path → genuinely fresh session ──


@pytest.fixture
def save_session(tmp_path, monkeypatch):
    """A first "session": a lifespan-managed client over an empty real store.

    Real SQLite in tmp_path (per the no-mocked-DB rule); the embedding model is
    the only thing stubbed (it is not the database). Git auto-commit is off so
    the save path doesn't need a real repo, and PALINODE_ALLOW_FRESH_DB defuses
    the store's "files but no DB" guard regardless of test ordering.
    """
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.context, "auto_detect", True)
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.git, "auto_push", False)
    # Stub the embed model (network), not the DB. index_file writes real chunks.
    monkeypatch.setattr("palinode.core.embedder.embed", lambda *a, **k: _FAKE_VECTOR)
    srv._rate_counters.clear()
    with TestClient(app) as c:
        yield c
    srv._rate_counters.clear()


def _save(c: TestClient, **body: Any) -> dict[str, Any]:
    res = c.post("/save", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_saved_memory_is_visible_to_a_fresh_session(save_session, tmp_path):
    """Write three memories through the real save path in one "session", then
    prime from a *genuinely fresh* app/client instance — the only channel
    between them is the on-disk memory dir (the source of truth). All three
    must surface, none silently lost."""
    # Session 1: save a global core memory + a project decision + an action item.
    _save(save_session,
          content="Cross-session sentinel: the deterministic executor is the edge.",
          type="Insight", slug="xsession-core", core=True,
          title="Cross-session core insight")
    _save(save_session,
          content="We chose streamable-HTTP transport for the gamma MCP surface.",
          type="Decision", slug="xsession-decision", project="gamma",
          title="Gamma transport decision")
    _save(save_session,
          content="Follow-up: document the gamma transport in the README.",
          type="ActionItem", slug="xsession-todo", project="gamma",
          title="Document gamma transport")

    # The files are on disk; the DB write happened through real SQLite.
    assert (tmp_path / "insights" / "xsession-core.md").exists()
    assert (tmp_path / "decisions" / "xsession-decision.md").exists()
    assert (tmp_path / "inbox" / "xsession-todo.md").exists()

    # Session 2: a fresh client instance. The prime digest holds no in-process
    # cache — it re-scans PALINODE_DIR from cold, exactly like a new session.
    fresh = TestClient(app)
    data = fresh.post(
        "/context/prime",
        json={"cwd": "/home/dev/gamma", "session_id": "second-session"},
    ).json()

    assert data["project"] == "project/gamma"
    assert "insights/xsession-core.md" in _files_in(data["core_memories"])
    assert "decisions/xsession-decision.md" in _files_in(data["recent_decisions"])
    assert "inbox/xsession-todo.md" in _files_in(data["open_action_items"])

    # Rendered for the fresh session, the saved titles are present.
    text = format_context_digest(data)
    assert "Cross-session core insight" in text
    assert "Gamma transport decision" in text
    assert "Document gamma transport" in text


def test_saved_project_memory_does_not_leak_to_other_projects(save_session):
    """A memory saved under project gamma must not appear when a different
    project's fresh session primes — cross-session persistence must respect the
    same scope boundary the read path enforces."""
    _save(save_session,
          content="Gamma-only decision that must stay scoped to gamma.",
          type="Decision", slug="gamma-scoped", project="gamma",
          title="Gamma scoped decision")

    other = TestClient(app)
    data = other.post("/context/prime", json={"cwd": "/home/dev/delta"}).json()
    assert data["project"] == "project/delta"
    assert "decisions/gamma-scoped.md" not in _all_files(data)

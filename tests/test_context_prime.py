"""ADR-009 Layer 1 slice 4 — scope semantics on POST /context/prime (#107).

The frozen client contract: the SessionStart hook POSTs ``{cwd, session_id}``
and discards the body, so a bare hook-shaped request must always 200. The
response is the ADR-012 digest (contract pinned by test_context_prime_262.py)
extended with ``mode`` + ``scope_chain``; ``scoped`` mode (the default
post-flip) drops core memories whose explicit ``scope:`` frontmatter is off
the resolved chain, while unscoped memories always pass (ADR-009 §7).

Note: slice 4's original design returned the raw ``/list`` row shape; the
shipped ADR-012 Layer 4 endpoint (#262) established the digest response
instead, so the classic↔``/list?core_only`` *shape* equivalence is gone —
these tests assert the same *selection* semantics on the digest rows.
"""
import os

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.config import config

client = TestClient(app)

DIGEST_KEYS = {
    "project",
    "core_memories",
    "recent_decisions",
    "open_action_items",
    "recent_snapshots",
    "_palinode_hint",
    "mode",
    "scope_chain",
}


@pytest.fixture
def scoped_memory_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    old_memory_dir = config.memory_dir
    config.memory_dir = str(tmp_path)

    os.makedirs(os.path.join(tmp_path, "decisions"))
    os.makedirs(os.path.join(tmp_path, "insights"))

    with open(os.path.join(tmp_path, "decisions", "legacy.md"), "w") as f:
        f.write("---\ntitle: Legacy\ncore: true\ndescription: unscoped\n---\nbody")

    with open(os.path.join(tmp_path, "decisions", "ours.md"), "w") as f:
        f.write("---\ntitle: Ours\ncore: true\nscope: project/palinode\n---\nbody")

    with open(os.path.join(tmp_path, "decisions", "theirs.md"), "w") as f:
        f.write("---\ntitle: Theirs\ncore: true\nscope: project/other\n---\nbody")

    # On-chain when the harness level matches, but not core — prime's
    # core_memories section never selects it.
    with open(os.path.join(tmp_path, "insights", "prefs.md"), "w") as f:
        f.write("---\ntitle: Prefs\ncore: false\nscope: harness/claude-code\n---\nbody")

    yield str(tmp_path)
    config.memory_dir = old_memory_dir


def _core_files(res) -> set[str]:
    return {row["file"] for row in res.json()["core_memories"]}


# ── the frozen hook contract ──────────────────────────────────────────────


def test_hook_shaped_request_returns_200(scoped_memory_dir):
    res = client.post(
        "/context/prime",
        json={"cwd": "/home/user/projects/palinode", "session_id": "abc-123"},
    )
    assert res.status_code == 200
    body = res.json()
    assert set(body) == DIGEST_KEYS
    assert body["mode"] == "scoped"  # config default post-flip


def test_empty_body_is_accepted(scoped_memory_dir):
    res = client.post("/context/prime", json={})
    assert res.status_code == 200
    # No cwd, no session, no configured scope levels → empty chain → scoped
    # mode admits only unscoped files (classic behavior for legacy memories).
    assert _core_files(res) == {"decisions/legacy.md"}


def test_session_and_project_land_on_the_chain(scoped_memory_dir):
    res = client.post(
        "/context/prime",
        json={"cwd": "/home/user/projects/palinode", "session_id": "abc-123"},
    )
    chain = res.json()["scope_chain"]
    assert "session/abc-123" in chain
    assert "project/palinode" in chain


# ── mode semantics ────────────────────────────────────────────────────────


def test_classic_mode_selects_all_core(scoped_memory_dir):
    res = client.post("/context/prime", json={"mode": "classic"})
    assert res.json()["mode"] == "classic"
    assert _core_files(res) == {
        "decisions/legacy.md",
        "decisions/ours.md",
        "decisions/theirs.md",
    }


def test_scoped_mode_filters_by_cwd_resolved_project(scoped_memory_dir):
    res = client.post("/context/prime", json={"cwd": "/w/palinode"})
    # Theirs is off-chain; Prefs is on no chain level here and not core anyway.
    assert _core_files(res) == {"decisions/legacy.md", "decisions/ours.md"}

    res = client.post("/context/prime", json={"cwd": "/w/unrelated-repo"})
    assert _core_files(res) == {"decisions/legacy.md"}  # both explicit scopes off-chain


def test_request_mode_overrides_config(scoped_memory_dir, monkeypatch):
    monkeypatch.setattr(config.scope, "prime_mode", "classic")
    res = client.post("/context/prime", json={"cwd": "/w/palinode", "mode": "scoped"})
    assert res.json()["mode"] == "scoped"
    assert _core_files(res) == {"decisions/legacy.md", "decisions/ours.md"}


def test_unknown_configured_mode_falls_back_to_scoped(scoped_memory_dir, monkeypatch):
    monkeypatch.setattr(config.scope, "prime_mode", "bananas")
    res = client.post("/context/prime", json={"cwd": "/w/palinode"})
    assert res.status_code == 200
    assert res.json()["mode"] == "scoped"


def test_invalid_request_mode_is_rejected(scoped_memory_dir):
    res = client.post("/context/prime", json={"mode": "smart"})
    assert res.status_code == 422  # Layer 3 mode — not accepted in Layer 1


# ── explicit scope override (ADR-009 §3.5) ────────────────────────────────


def test_explicit_scope_override_drives_the_chain(scoped_memory_dir):
    res = client.post(
        "/context/prime",
        json={"cwd": "/w/palinode", "scope": {"project": "other"}},
    )
    # Override replaces resolution entirely: cwd's project/palinode is ignored.
    assert res.json()["scope_chain"] == ["project/other"]
    assert _core_files(res) == {"decisions/legacy.md", "decisions/theirs.md"}


def test_project_resolution_precedence(scoped_memory_dir, monkeypatch):
    # 1. PALINODE_PROJECT env beats cwd.
    monkeypatch.setenv("PALINODE_PROJECT", "project/palinode")
    res = client.post("/context/prime", json={"cwd": "/w/unrelated"})
    assert "project/palinode" in res.json()["scope_chain"]
    monkeypatch.delenv("PALINODE_PROJECT")

    # 2. project_map beats basename auto-detect.
    monkeypatch.setitem(config.context.project_map, "unrelated", "palinode")
    res = client.post("/context/prime", json={"cwd": "/w/unrelated"})
    assert "project/palinode" in res.json()["scope_chain"]


def test_rows_use_digest_shape(scoped_memory_dir):
    res = client.post("/context/prime", json={"cwd": "/w/palinode"})
    row = next(
        r for r in res.json()["core_memories"] if r["file"] == "decisions/ours.md"
    )
    assert set(row) == {"file", "summary"}
    assert row["summary"].startswith("Ours")


def test_scoped_filter_applies_to_project_sections(scoped_memory_dir, tmp_path):
    # A project-entity Decision with an off-chain explicit scope must not
    # surface in recent_decisions either — the chain filters every section.
    with open(os.path.join(tmp_path, "decisions", "offchain-decision.md"), "w") as f:
        f.write(
            "---\ntitle: OffChain\ntype: Decision\nscope: member/someone-else\n"
            "entities:\n- project/palinode\n---\nbody"
        )
    res = client.post("/context/prime", json={"cwd": "/w/palinode"})
    files = {r["file"] for r in res.json()["recent_decisions"]}
    assert "decisions/offchain-decision.md" not in files


def test_scoped_filter_applies_to_snapshot_section(scoped_memory_dir, tmp_path):
    # recent_snapshots inherits the digest's scope + visibility filtering:
    # a ProjectSnapshot for the resolved project surfaces, but one with an
    # off-chain explicit scope, and a private one whose owner is off-chain,
    # must not — the same chain that filters every other section.
    os.makedirs(os.path.join(tmp_path, "projects"))
    with open(os.path.join(tmp_path, "projects", "ours-snap.md"), "w") as f:
        f.write(
            "---\ntitle: OursSnap\ntype: ProjectSnapshot\n"
            "entities:\n- project/palinode\n---\nbody"
        )
    with open(os.path.join(tmp_path, "projects", "offchain-snap.md"), "w") as f:
        f.write(
            "---\ntitle: OffChainSnap\ntype: ProjectSnapshot\nscope: project/other\n"
            "entities:\n- project/palinode\n---\nbody"
        )
    with open(os.path.join(tmp_path, "projects", "private-snap.md"), "w") as f:
        f.write(
            "---\ntitle: PrivateSnap\ntype: ProjectSnapshot\n"
            "visibility: private\nscope: member/someone-else\n"
            "entities:\n- project/palinode\n---\nbody"
        )
    res = client.post("/context/prime", json={"cwd": "/w/palinode"})
    files = {r["file"] for r in res.json()["recent_snapshots"]}
    assert files == {"projects/ours-snap.md"}


def test_core_snapshot_not_duplicated_across_sections(scoped_memory_dir, tmp_path):
    # A ProjectSnapshot flagged core: true satisfies both the core-memory and
    # the snapshot filters. It must render once, under its purpose-built
    # Recent snapshots section, not on two lines of the resume digest.
    os.makedirs(os.path.join(tmp_path, "projects"))
    with open(os.path.join(tmp_path, "projects", "core-snap.md"), "w") as f:
        f.write(
            "---\ntitle: CoreSnap\ntype: ProjectSnapshot\ncore: true\n"
            "entities:\n- project/palinode\n---\nbody"
        )
    res = client.post("/context/prime", json={"cwd": "/w/palinode"})
    snap_files = {r["file"] for r in res.json()["recent_snapshots"]}
    core_files = {r["file"] for r in res.json()["core_memories"]}
    assert "projects/core-snap.md" in snap_files
    assert "projects/core-snap.md" not in core_files


# ── surface promotion: scoped selection on the MCP + CLI surfaces (slice 5) ──
#
# Neither session-start surface owns a scope resolver — both proxy
# POST /context/prime, so scoped mode (the config default) reaches them for
# free and the digest is built where the memory dir lives (correct for a remote
# API too). These forward a fake transport to the same TestClient the REST
# tests use, exercising the real scoped digest end-to-end through each surface.


@pytest.mark.asyncio
async def test_mcp_session_init_is_scoped(scoped_memory_dir, monkeypatch):
    import palinode.mcp as mcp

    async def _fake_post(path, json=None, timeout=30.0):
        # The httpx TestClient response already exposes .status_code / .json(),
        # which is all the session-init dispatch reads off the _post result.
        return client.post(path, json=json or {})

    monkeypatch.setattr(mcp, "_post", _fake_post)
    result = await mcp._dispatch_tool("palinode_session_init", {"cwd": "/w/palinode"})
    text = result[0].text
    assert "decisions/ours.md" in text  # scope project/palinode — on the chain
    assert "decisions/legacy.md" in text  # unscoped — always passes (ADR-009 §7)
    assert "decisions/theirs.md" not in text  # scope project/other — off-chain


def test_cli_prime_is_scoped(scoped_memory_dir):
    import importlib
    from unittest.mock import patch

    from click.testing import CliRunner

    from palinode.cli import _api

    prime_mod = importlib.import_module("palinode.cli.prime")

    class _Client:
        def post(self, path, json=None, timeout=None):
            return client.post(path, json=json or {})

    fake = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    fake.client = _Client()
    with patch.object(prime_mod, "api_client", fake):
        result = CliRunner().invoke(
            prime_mod.prime, ["--cwd", "/w/palinode", "--format", "text"]
        )
    assert result.exit_code == 0, result.output
    assert "decisions/ours.md" in result.output  # on-chain
    assert "decisions/legacy.md" in result.output  # unscoped
    assert "decisions/theirs.md" not in result.output  # off-chain, dropped

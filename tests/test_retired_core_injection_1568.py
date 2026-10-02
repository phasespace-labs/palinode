"""A retired core memory must not be injected as a current assertion.

The shipped SessionStart hook builds its digest from ``GET /list?core_only=true``.
That listing dropped ``archive/``, non-visible records and *expired* cores, and
consulted nothing else — so a ``core: true`` decision superseded **in place**
(``archive_memory(..., superseded_by=…)`` leaves the file at its path carrying
``status: archived`` + ``superseded_by``) was injected at session start beside
the decision that replaced it, with nothing saying which one stands.

The rule is the one search and ``/resolve`` already use:
:func:`palinode.core.lifecycle.eligibility`. A retired core record is not core
for *injection* purposes — the same demotion an expired core already got — and
its row says why in ``core_retired_reason``. Browse keeps showing it:
``include_retired_core=true`` (what ``palinode list --core`` and
``palinode_list`` pass) selects it anyway.

Real SQLite, real files and real git under ``tmp_path``; only the embedder and
the security scanner are patched (repo rule: never mock the database).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from palinode.core.config import config

REPO_ROOT = Path(__file__).parent.parent
EMBED_DIM = 1024
_FAKE_VECTOR = [0.05] * EMBED_DIM

PAST = "2020-01-01T00:00:00Z"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient over a git-backed tmp memory dir with real SQLite."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    for key in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)

    import palinode.api.server as srv

    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=list(_FAKE_VECTOR)),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as client:
            yield client, str(tmp_path)
    srv._rate_counters.clear()


def _save(client: TestClient, slug: str, **extra: Any) -> str:
    """Save one memory through the product's own save surface; return its rel path."""
    body = {"content": f"Body of {slug}.", "type": "Decision", "slug": slug, "core": True}
    body.update(extra)
    resp = client.post("/save", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["rel_path"]


def _rows(client: TestClient, **params: Any) -> dict[str, dict[str, Any]]:
    resp = client.get("/list", params=params)
    assert resp.status_code == 200, resp.text
    return {row["file"]: row for row in resp.json()}


def _meta(memory_dir: str, rel: str) -> dict[str, Any]:
    from palinode.core import parser

    with open(os.path.join(memory_dir, rel), encoding="utf-8") as f:
        meta, _ = parser.parse_frontmatter(f.read())
    return meta


@pytest.fixture()
def superseded_pair(api_client):
    """Core decision A superseded in place by core decision B, plus neighbour C.

    The supersession is written by the product's own path
    (:func:`palinode.consolidation.archive.archive_memory`), not by hand.
    """
    client, memory_dir = api_client
    a = _save(client, "old-db", content="Use Postgres.", entities=["project/demo"])
    b = _save(client, "new-db", content="Use SQLite instead.", entities=["project/demo"])
    c = _save(client, "neighbour", content="Unrelated standing rule.", type="Insight")

    from palinode.consolidation.archive import archive_memory

    result = archive_memory(a, reason="replaced by the SQLite decision", superseded_by=b)
    assert result["status"] == "archived"
    return client, memory_dir, a, b, c


# ── what the product's supersede path actually writes ────────────────────────


def test_supersede_in_place_leaves_a_core_record_at_its_path(superseded_pair):
    """The precondition for the bug: the file is not moved and stays ``core: true``."""
    _client, memory_dir, a, b, _c = superseded_pair

    assert os.path.isfile(os.path.join(memory_dir, a)), "the record was moved, not retired in place"
    meta = _meta(memory_dir, a)
    assert meta["status"] == "archived"
    assert meta["superseded_by"] == b
    assert meta["core"] is True
    assert "archive" not in a.split("/")


# ── the rule: retired core is not core for injection ─────────────────────────


def test_superseded_core_is_not_injected_beside_its_replacement(superseded_pair):
    client, _memory_dir, a, b, c = superseded_pair

    rows = _rows(client, core_only="true")
    assert b in rows, "the replacement must still be injected"
    assert a not in rows, "the superseded decision was injected as a current assertion"
    assert c in rows


def test_unrelated_neighbouring_core_record_is_unaffected(superseded_pair):
    client, _memory_dir, _a, _b, c = superseded_pair

    row = _rows(client, core_only="true")[c]
    assert row["core"] is True
    assert row["core_retired_reason"] is None


def test_plain_archive_in_place_is_not_injected(api_client):
    client, _memory_dir = api_client
    a = _save(client, "retired-rule")
    b = _save(client, "standing-rule")

    from palinode.consolidation.archive import archive_memory

    archive_memory(a, reason="no longer true")

    rows = _rows(client, core_only="true")
    assert set(rows) == {b}


def test_deprecated_core_is_not_injected(api_client):
    """``status: deprecated`` is settable through the save surface itself."""
    client, _memory_dir = api_client
    a = _save(client, "old-style", metadata={"status": "deprecated"})
    b = _save(client, "current-style")

    assert _rows(client, core_only="true").keys() == {b}
    assert _rows(client)[a]["core_retired_reason"] == "status:deprecated"


def test_retracted_and_superseded_statuses_are_refused_by_the_save_surface(api_client):
    """Not reachable through the product — recorded so the coverage claim is honest."""
    client, _memory_dir = api_client
    for status in ("retracted", "superseded"):
        resp = client.post("/save", json={
            "content": "x", "type": "Decision", "slug": f"bad-{status}",
            "core": True, "metadata": {"status": status},
        })
        assert resp.status_code == 400, resp.text
        assert "status" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("frontmatter", "expected_reason"),
    [
        ({"status": "superseded"}, "status:superseded"),
        ({"status": "retracted"}, "status:retracted"),
        ({"lifecycle": "archived"}, "lifecycle:archived"),
        ({"superseded_by": "decisions/newer.md"}, "superseded_by: decisions/newer.md"),
    ],
    ids=["legacy-superseded", "legacy-retracted", "ku-lifecycle", "pointer-only"],
)
def test_hand_written_retirement_states_are_withheld_too(api_client, frontmatter, expected_reason):
    """The classifier's whole vocabulary, including values only a human writes.

    ``status: superseded`` / ``retracted`` and a bare ``superseded_by`` are not
    produced by any current product path (the save surface rejects the first
    two; ``archive_memory`` always writes ``status: archived`` alongside the
    pointer). The listing must still honour them — the same reason
    :mod:`palinode.core.lifecycle` does.
    """
    client, memory_dir = api_client
    _save(client, "standing")
    meta = {"category": "decisions", "type": "Decision", "core": True,
            "last_updated": "2026-09-01", **frontmatter}
    path = os.path.join(memory_dir, "decisions", "hand-written.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{yaml.safe_dump(meta, default_flow_style=False)}---\n\nBody.\n")

    assert "decisions/hand-written.md" not in _rows(client, core_only="true")
    row = _rows(client)["decisions/hand-written.md"]
    assert row["core"] is False
    assert row["core_retired_reason"] == expected_reason


def test_core_record_under_archive_is_retired_by_location(api_client):
    """``archive/`` is skipped by ``/list``; a caller that widens the skip set still gets the rule."""
    from palinode.api.routers.memory import collect_memory_files

    client, memory_dir = api_client
    _save(client, "standing")
    os.makedirs(os.path.join(memory_dir, "archive"), exist_ok=True)
    with open(os.path.join(memory_dir, "archive", "old.md"), "w", encoding="utf-8") as f:
        f.write("---\ncategory: archive\ntype: Decision\ncore: true\n---\n\nBody.\n")

    assert "archive/old.md" not in {r["file"] for r in collect_memory_files(core_only=True)}
    rows = {r["file"]: r for r in collect_memory_files(skip_dirs=("daily", "inbox"))}
    assert rows["archive/old.md"]["core"] is False
    assert rows["archive/old.md"]["core_retired_reason"] == "path:archive"


# ── the expired case is unchanged ────────────────────────────────────────────


def test_expired_core_behaviour_is_unchanged(api_client):
    client, _memory_dir = api_client
    stale = _save(client, "lapsed", type="Insight", metadata={"expires_at": PAST})
    live = _save(client, "standing")

    assert _rows(client, core_only="true").keys() == {live}
    row = _rows(client)[stale]
    assert row["core"] is False
    assert row["expires_at"] == PAST
    assert row["core_retired_reason"] == "expired"
    assert client.get("/read", params={"file_path": stale}).status_code == 200


def test_expired_core_is_still_reported_once_per_process(api_client, caplog):
    import logging

    client, _memory_dir = api_client
    # A slug of its own: the report-once key is (kind, path, expires_at) and it
    # is process-wide, so re-using another test's path would report nothing.
    stale = _save(client, "lapsed-report-once", type="Insight", metadata={"expires_at": PAST})
    with caplog.at_level(logging.WARNING, logger="palinode.expiry"):
        client.get("/list", params={"core_only": "true"})
        client.get("/list", params={"core_only": "true"})
    hits = [r for r in caplog.records if f"core memory {stale} expired" in r.getMessage()]
    assert len(hits) == 1


# ── the browse view keeps showing it ─────────────────────────────────────────


def test_browse_view_still_shows_the_retired_core_record(superseded_pair):
    client, _memory_dir, a, b, _c = superseded_pair

    rows = _rows(client, core_only="true", include_retired_core="true")
    assert a in rows and b in rows
    assert rows[a]["core"] is False
    assert rows[a]["core_retired_reason"] == f"superseded_by: {b}"
    assert rows[b]["core"] is True

    # And the unfiltered listing is untouched: nothing is hidden anywhere.
    assert a in _rows(client)


def test_cli_list_core_shows_the_retired_record_and_why(superseded_pair, monkeypatch):
    from palinode.cli._api import PalinodeAPI

    # `palinode.cli.list_cmd` the attribute is the click command re-exported by
    # the package __init__; the module itself is reached through importlib.
    list_cmd_mod = importlib.import_module("palinode.cli.list_cmd")

    client, _memory_dir, a, b, _c = superseded_pair
    monkeypatch.setattr(list_cmd_mod, "api_client", PalinodeAPI(client=client))

    result = CliRunner().invoke(list_cmd_mod.list_cmd, ["--core", "--format", "json"])
    assert result.exit_code == 0, result.output
    files = {row["file"]: row for row in json.loads(result.output)}
    assert a in files and b in files
    assert files[a]["core_retired_reason"] == f"superseded_by: {b}"

    text = CliRunner().invoke(list_cmd_mod.list_cmd, ["--core", "--format", "text"])
    assert text.exit_code == 0, text.output
    assert "retired: superseded_by" in text.output
    assert "[core]" in text.output  # the replacement still reads as core


@pytest.mark.asyncio
async def test_mcp_list_core_only_shows_the_retired_record_tagged(superseded_pair, monkeypatch):
    from palinode import mcp

    client, _memory_dir, a, b, _c = superseded_pair
    monkeypatch.setattr(mcp, "_http_transport", httpx.ASGITransport(app=client.app))
    monkeypatch.setattr(mcp, "_http_client", None)
    monkeypatch.setattr(mcp, "_http_client_loop", None)
    try:
        result = await mcp._dispatch_tool("palinode_list", {"core_only": True})
    finally:
        await mcp._close_http()

    text = "\n".join(block.text for block in result)
    assert f"{a} — " in text and "[retired: superseded_by:" in text
    assert f"{b} — " in text and text.count("[core]") == 2  # b and the neighbour


# ── every injection consumer of core_only=true ───────────────────────────────


_LIST_REQUEST_RE = re.compile(r"""["'/]?(/list\?[a-z_=&]+)["']""")

#: The shipped consumers that inject what ``/list?core_only=true`` returns.
INJECTION_CONSUMERS = (
    "examples/hooks/palinode-session-start.sh",
    "palinode/cli/init.py",  # the embedded copy `palinode init` writes
    "plugin/index.ts",
    "plugins/core/src/index.ts",
)


@pytest.mark.parametrize("source", INJECTION_CONSUMERS)
def test_injection_consumers_request_the_withholding_selection(superseded_pair, source):
    """Each consumer's own request string, replayed against the API.

    They are unchanged by this fix and must stay that way: the default is the
    injection-safe one, so a consumer that knows nothing about
    ``include_retired_core`` cannot inject a retired core memory.
    """
    client, _memory_dir, a, b, _c = superseded_pair

    text = (REPO_ROOT / source).read_text(encoding="utf-8")
    requests = set(_LIST_REQUEST_RE.findall(text))
    assert requests == {"/list?core_only=true"}, f"{source} changed its core selection: {requests}"

    rows = {row["file"] for row in client.get(requests.pop()).json()}
    assert b in rows
    assert a not in rows


def test_context_prime_core_section_withholds_the_superseded_record(superseded_pair):
    """``/context/prime`` already routed through the classifier — pinned, not fixed."""
    client, _memory_dir, a, b, c = superseded_pair

    resp = client.post("/context/prime", json={})
    assert resp.status_code == 200, resp.text
    core = {row["file"] for row in resp.json()["core_memories"]}
    assert core == {b, c}
    assert a not in core

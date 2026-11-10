"""Listing and reading follow search's project isolation.

An agent in a project-scoped session could browse every project's records
with ``palinode_list``, open one with ``palinode_read`` and answer with its
value: neither surface said the record belonged to another project, while
the same session's searches withheld or labelled it.

The rule now: a listing with a project is scoped exactly as search is (the
same chain, the same visibility choke point), counts what isolation left out,
and lists those records labelled only on ``include_other_projects``. The MCP
tool sends its session's project; REST and the CLI scope only when asked, so
an unscoped listing is unchanged. A read by a known path stays allowed and
says whose the record is.

Real SQLite and real git under ``tmp_path``.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.routers import memory as memory_router
from palinode.api.routers import search as search_router
from palinode.api.server import app
from palinode.cli._api import api_client
from palinode.cli.list_cmd import list_cmd
from palinode.cli.read import read as cli_read
from palinode.core.config import config
from palinode.core.context_prime import ProjectResolution
from palinode.core.defaults import LIST_OTHER_PROJECTS_WITHHELD_HEADER
from palinode.core.retrieval_log import RetrievalLogger
from tests import test_resolve_bundle as scenarios

mem = scenarios.mem
_write = scenarios._write

ALPHA = "decisions/alpha-cache"
BETA = "decisions/beta-cache"
GLOBAL = "insights/cache-ttl"
SHARED = "decisions/shared-cache"
PRIVATE_ALPHA = "decisions/alpha-private"
PRIVATE_BETA = "decisions/beta-private"
SESSION_SCOPED = "insights/session-note"

BETA_VALUE = "valkeyquartz"
BETA_LABEL = "[other project: project/beta — not project/alpha]"
READ_NOTICE = f"{BETA_LABEL} Context only, not this project's decision."
OTHER_HEADING = (
    "Other projects — context only, not this project's decisions (this project: project/alpha)"
)

UNSCOPED = {ALPHA, BETA, GLOBAL, SHARED, SESSION_SCOPED}
ALPHA_SCOPED = {ALPHA, GLOBAL, SHARED, PRIVATE_ALPHA}


@pytest.fixture()
def store(mem, monkeypatch):
    """Two projects' records, a shared and a global one, private and explicitly scoped ones."""
    monkeypatch.setattr(search_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    monkeypatch.setattr(memory_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    monkeypatch.setattr(config.audit, "log_path", str(mem / ".audit" / "mcp-calls.jsonl"))
    monkeypatch.setattr(config.context, "enabled", True)
    monkeypatch.setattr(config.context, "project_map", {})
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    base = {"status": "active"}
    _write(mem, f"{ALPHA}.md", "# Alpha cache\n\nThe service cache uses redisember.",
           type="Decision", date="2026-09-01", entities=["project/alpha"],
           summary="alpha cache", **base)
    _write(mem, f"{BETA}.md", f"# Beta cache\n\nThe service cache uses {BETA_VALUE}.",
           type="Decision", date="2026-09-02", entities=["project/beta"], core=True,
           summary="beta cache", **base)
    _write(mem, f"{GLOBAL}.md", "# Cache TTL\n\nEvery service cache expires.",
           type="Insight", date="2026-09-03", core=True, summary="cache ttl", **base)
    _write(mem, f"{SHARED}.md", "# Shared cache\n\nThe service cache warms on boot.",
           type="Decision", date="2026-09-04", entities=["project/alpha", "project/beta"],
           summary="shared cache", **base)
    _write(mem, f"{PRIVATE_ALPHA}.md", "# Alpha private\n\nAlpha's own private note.",
           type="Decision", date="2026-09-05", entities=["project/alpha"],
           visibility="private", scope="project/alpha", summary="alpha private", **base)
    _write(mem, f"{PRIVATE_BETA}.md", "# Beta private\n\nBeta's own private note.",
           type="Decision", date="2026-09-06", entities=["project/beta"],
           visibility="private", scope="project/beta", summary="beta private", **base)
    _write(mem, f"{SESSION_SCOPED}.md", "# Session note\n\nScoped to one other session.",
           type="Insight", date="2026-09-07", scope="session/elsewhere",
           summary="session note", **base)
    return mem


def _refs(rows) -> set[str]:
    return {(r.get("file") or r.get("rel_path") or r["file_path"]).removesuffix(".md") for r in rows}


def _labels(rows) -> dict[str, list[str]]:
    return {
        (r.get("file") or r.get("rel_path") or r["file_path"]).removesuffix(".md"): r["other_project"]
        for r in rows if r.get("other_project")
    }


def _get_via(client):
    async def get(path, params=None, **kwargs):
        return client.get(path, params=params)
    return get


# ── REST ─────────────────────────────────────────────────────────────────────


def test_scoped_list_leaves_other_projects_out_and_counts_them(store):
    with TestClient(app) as client:
        resp = client.get("/list", params={"project": "alpha"})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert _refs(rows) == ALPHA_SCOPED
    assert BETA_VALUE not in resp.text
    assert not _labels(rows)
    # Isolation alone is counted: beta's two records. The session-scoped note
    # (explicit scope off the chain) is hidden as search hides it, uncounted.
    assert resp.headers[LIST_OTHER_PROJECTS_WITHHELD_HEADER] == "2"


def test_opted_in_list_labels_other_projects(store):
    with TestClient(app) as client:
        resp = client.get("/list", params={"project": "project/alpha", "include_other_projects": "true"})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    # Access control still applies: beta's private note stays out.
    assert _refs(rows) == ALPHA_SCOPED | {BETA}
    assert _labels(rows) == {BETA: ["project/beta"]}
    assert resp.headers[LIST_OTHER_PROJECTS_WITHHELD_HEADER] == "0"


@pytest.mark.parametrize("params", [{}, {"project": ""}, {"include_other_projects": "true"}])
def test_unscoped_list_is_unchanged(store, monkeypatch, params):
    # Even a server pinned to a project lists every project for a caller
    # that names none, as before.
    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    with TestClient(app) as client:
        resp = client.get("/list", params=params)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert _refs(rows) == UNSCOPED
    assert not _labels(rows)
    assert LIST_OTHER_PROJECTS_WITHHELD_HEADER not in resp.headers


@pytest.mark.parametrize("context", [[], ["project/alpha"], ["project/beta"], ["project/zzz"]])
@pytest.mark.parametrize("wide", [False, True])
def test_list_selects_what_search_selects(store, context, wide):
    """No-project, private and explicitly scoped records: one rule, both surfaces."""
    project = context[0] if context else ""
    with TestClient(app) as client:
        listed = client.get("/list", params={
            "project": project, **({"include_other_projects": "true"} if wide else {}),
        })
        searched = client.post("/search", json={
            "query": "", "context": context, "receipt": True, "limit": 50,
            **({"include_other_projects": True} if wide else {}),
        })
    assert listed.status_code == 200 and searched.status_code == 200, searched.text
    hits = searched.json()["results"]
    assert _refs(listed.json()) == _refs(hits)
    assert _labels(listed.json()) == _labels(hits)
    if context:
        withheld = searched.json()["receipt"]["retrieval"]["other_projects_withheld"]
        assert int(listed.headers[LIST_OTHER_PROJECTS_WITHHELD_HEADER]) == withheld


def test_read_of_another_projects_record_is_labelled(store):
    with TestClient(app) as client:
        foreign = client.get("/read", params={"file_path": BETA, "project": "alpha"})
        private = client.get("/read", params={"file_path": PRIVATE_BETA, "project": "alpha"})
        own = [client.get("/read", params={"file_path": ref, "project": "alpha"})
               for ref in (ALPHA, GLOBAL, SHARED, PRIVATE_ALPHA)]
        unscoped = client.get("/read", params={"file_path": BETA, "project": ""})
    # A known path stays readable, private included, and says whose it is.
    assert foreign.status_code == 200 and BETA_VALUE in foreign.json()["content"]
    assert foreign.json()["other_project"] == ["project/beta"]
    assert private.status_code == 200 and private.json()["other_project"] == ["project/beta"]
    for resp in own:
        assert resp.status_code == 200 and "other_project" not in resp.json()
    assert unscoped.status_code == 200 and "other_project" not in unscoped.json()


# ── MCP ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_list_is_scoped_to_the_session(store, monkeypatch):
    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    sent: list[dict] = []
    with TestClient(app) as client:
        async def get(path, params=None, **kwargs):
            sent.append(dict(params or {}))
            return client.get(path, params=params)
        monkeypatch.setattr(mcp, "_get", get)
        scoped = (await mcp._tool_list({"category": "decisions"}))[0].text
        wide = (await mcp._tool_list({"category": "decisions", "include_other_projects": True}))[0].text
    assert sent[0]["project"] == "project/alpha"
    assert "include_other_projects" not in sent[0]
    assert sent[1]["include_other_projects"] == "true"
    assert BETA not in scoped and PRIVATE_BETA not in scoped
    assert f"{ALPHA}.md" in scoped and f"{SHARED}.md" in scoped
    assert f"{BETA_LABEL} {BETA}.md" in wide
    # Other projects' rows come last, under their own heading.
    assert wide.index(OTHER_HEADING) > wide.index(f"{ALPHA}.md")
    assert wide.index(f"{BETA_LABEL} {BETA}.md") > wide.index(OTHER_HEADING)
    assert PRIVATE_BETA not in wide


@pytest.mark.asyncio
async def test_mcp_list_says_why_a_scoped_listing_came_back_thin(store, monkeypatch):
    monkeypatch.setenv("PALINODE_PROJECT", "quillon-zzz")
    with TestClient(app) as client:
        monkeypatch.setattr(mcp, "_get", _get_via(client))
        thin = (await mcp._tool_list({"category": "decisions"}))[0].text
    assert thin.startswith("No files found.")
    assert (
        "5 records from other projects withheld (scope: project/quillon-zzz). "
        "They are about other projects, not this one." in thin
    )
    assert "include_other_projects" not in thin


@pytest.mark.asyncio
async def test_mcp_list_without_a_project_is_unchanged(store, monkeypatch):
    monkeypatch.setattr(mcp, "_resolve_scope", lambda: ProjectResolution(None, "none"))
    with TestClient(app) as client:
        monkeypatch.setattr(mcp, "_get", _get_via(client))
        text = (await mcp._tool_list({}))[0].text
    listed = {line.split(" — ")[0].removesuffix(".md") for line in text.splitlines()}
    assert listed == UNSCOPED
    assert "[other project" not in text and "withheld" not in text


@pytest.mark.parametrize("meta", [False, True])
@pytest.mark.asyncio
async def test_mcp_read_labels_another_projects_record(store, monkeypatch, meta):
    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    with TestClient(app) as client:
        monkeypatch.setattr(mcp, "_get", _get_via(client))
        foreign = (await mcp._tool_read({"file_path": BETA, "meta": meta}))[0].text
        own = (await mcp._tool_read({"file_path": ALPHA, "meta": meta}))[0].text
        monkeypatch.setattr(mcp, "_resolve_scope", lambda: ProjectResolution(None, "none"))
        unscoped = (await mcp._tool_read({"file_path": BETA, "meta": meta}))[0].text
    assert foreign.startswith(READ_NOTICE + "\n")
    assert BETA_VALUE in foreign
    assert "[other project" not in own
    assert "[other project" not in unscoped and BETA_VALUE in unscoped


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_list_scopes_only_when_asked(store, monkeypatch):
    # The shell's project does not scope a browse; --project does.
    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", client)
        runner = CliRunner()
        default = runner.invoke(list_cmd, ["--format", "json"])
        scoped = runner.invoke(list_cmd, ["--project", "alpha", "--format", "json"])
        wide = runner.invoke(list_cmd, ["--project", "alpha", "--include-other-projects",
                                        "--format", "json"])
        text = runner.invoke(list_cmd, ["--project", "alpha", "--include-other-projects",
                                        "--format", "text"])
    for result in (default, scoped, wide, text):
        assert result.exit_code == 0, result.output
    assert _refs(json.loads(default.stdout)) == UNSCOPED
    assert _refs(json.loads(scoped.stdout)) == ALPHA_SCOPED
    wide_rows = json.loads(wide.stdout)
    assert _refs(wide_rows) == ALPHA_SCOPED | {BETA}
    assert _labels(wide_rows) == {BETA: ["project/beta"]}
    words = " ".join(text.output.split())  # the console wraps at 80 columns
    assert OTHER_HEADING in words
    assert f"{BETA_LABEL} {BETA}.md" in words


def test_cli_list_tells_a_person_how_to_see_withheld_records(store, monkeypatch):
    args = ["--project", "quillon-zzz", "--category", "decisions", "--format"]
    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", client)
        runner = CliRunner()
        text = runner.invoke(list_cmd, [*args, "text"])
        piped = runner.invoke(list_cmd, [*args, "json"])
    assert text.exit_code == 0 and piped.exit_code == 0, text.output
    line = "5 records from other projects withheld (scope: project/quillon-zzz)"
    assert line in " ".join(text.output.split())
    assert "--include-other-projects to see them" in " ".join(text.output.split())
    # Piped: stdout stays the array, the line goes to stderr.
    assert json.loads(piped.stdout) == []
    assert line in piped.stderr


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_cli_read_labels_another_projects_record(store, monkeypatch, fmt):
    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", client)
        runner = CliRunner()
        foreign = runner.invoke(cli_read, [BETA, "--format", fmt])
        own = runner.invoke(cli_read, [ALPHA, "--format", fmt])
    assert foreign.exit_code == 0 and own.exit_code == 0, foreign.output
    if fmt == "json":
        assert json.loads(foreign.stdout)["other_project"] == ["project/beta"]
        assert "other_project" not in json.loads(own.stdout)
    else:
        # On stderr, so stdout stays the file's text for a pipe.
        assert READ_NOTICE in foreign.stderr
        assert BETA_VALUE in foreign.stdout and READ_NOTICE not in foreign.stdout
        assert "[other project" not in own.output

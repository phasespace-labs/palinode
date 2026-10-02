"""Scope automatic metadata links on generation and every file-read presenter."""
from __future__ import annotations

import json

import frontmatter
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.routers import memory
from palinode.api.server import app
from palinode.cli._api import api_client
from palinode.cli.read import read as cli_read
from palinode.core import cross_refs, parser
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from palinode.indexer.watcher import PalinodeHandler
from tests import test_resolve_bundle as scenarios

mem = scenarios.mem
_write = scenarios._write

OWN = "decisions/home-rule"
FOREIGN = "decisions/foreign-rule"
GLOBAL = "decisions/global-rule"
SHARED = "decisions/shared-rule"
REFS = [OWN, FOREIGN, GLOBAL, SHARED]


@pytest.fixture()
def records(mem, monkeypatch):
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config.context, "enabled", True)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", True)
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    monkeypatch.setattr(memory, "_retrieval_logger", RetrievalLogger(str(mem)))
    for ref, entities in zip(REFS, [["project/home"], ["project/foreign"], [],
                                  ["project/home", "project/foreign"]], strict=True):
        _write(mem, ref + ".md", "Target body", title="Shared house rule", entities=entities)
    return mem


@pytest.mark.parametrize("projects,expected", [
    (["project/home"], {OWN, GLOBAL, SHARED}),
    (["project/HOME"], {OWN, GLOBAL, SHARED}),
    (["project/home", "project/foreign"], set(REFS)),
    ([], set(REFS)),
])
def test_save_and_watcher_scope_automatic_links(records, projects, expected):
    with TestClient(app) as client:
        response = client.post("/save", json={
            "content": "The Shared house rule applies here.", "type": "Insight",
            "slug": "source", "entities": projects,
        })
    assert response.status_code == 200, response.text
    source = records / "insights/source.md"
    handler = PalinodeHandler()
    try:
        handler._process_file(str(source))
    finally:
        handler.shutdown()
    assert set(frontmatter.load(source)["cross_refs"]) == expected
    assert cross_refs.update_file_cross_refs(str(source))["changed"] is False


def test_generation_uses_aliases_and_refreshes_target_metadata(records):
    (records / "entity-aliases.yaml").write_text("aliases:\n  project/home:\n    - project/home-alias\n")
    source = records / "insights/source.md"
    source.parent.mkdir(exist_ok=True)
    source.write_text(frontmatter.dumps(frontmatter.Post(
        "Shared house rule", entities=["project/HOME-ALIAS"],
    )))
    cross_refs.update_file_cross_refs(str(source))
    assert set(frontmatter.load(source)["cross_refs"]) == {OWN, GLOBAL, SHARED}
    target = records / (FOREIGN + ".md")
    post = frontmatter.load(target)
    post["entities"] = ["project/home"]
    target.write_text(frontmatter.dumps(post))
    cross_refs.update_file_cross_refs(str(source))
    assert set(frontmatter.load(source)["cross_refs"]) == set(REFS)


@pytest.fixture()
def legacy(records):
    source = records / "insights/legacy.md"
    source.parent.mkdir(exist_ok=True)
    # Stale automatic links are separate from authored body / typed links.
    source.write_text(frontmatter.dumps(frontmatter.Post(
        "Body stays byte-for-byte.\n\n[[decisions/foreign-rule]]\n",
        entities=["project/home"], cross_refs=REFS,
        backed_by=[FOREIGN], contradicts=[FOREIGN],
    )) + "\n")
    return source


@pytest.mark.parametrize("surface", ["api", "mcp", "cli"])
@pytest.mark.parametrize("meta", [False, True])
@pytest.mark.parametrize("tier", [None, "overview", "abstract"])
@pytest.mark.asyncio
async def test_legacy_cross_refs_filtered_on_read(legacy, monkeypatch, surface, meta, tier):
    original = legacy.read_text()
    with TestClient(app) as client:
        if surface == "api":
            response = client.get("/read", params={
                "file_path": "insights/legacy", "meta": meta,
                "project": "project/home", **({"tier": tier} if tier else {}),
            })
            assert response.status_code == 200, response.text
            payload = response.json()
            rendered = payload["content"]
            if meta:
                assert payload["frontmatter"]["cross_refs"] == [OWN, GLOBAL, SHARED]
            assert payload["size_bytes"] == len(original.encode())
        elif surface == "mcp":
            monkeypatch.setenv("PALINODE_PROJECT", "home")
            async def get(path, params=None, **kwargs):
                return client.get(path, params=params)
            monkeypatch.setattr(mcp, "_get", get)
            rendered = (await mcp._tool_read({"file_path": "insights/legacy", "meta": meta,
                                              **({"tier": tier} if tier else {})}))[0].text
        else:
            monkeypatch.setenv("PALINODE_PROJECT", "home")
            monkeypatch.setattr(api_client, "client", client)
            args = ["insights/legacy", "--format", "json"]
            if meta:
                args.append("--meta")
            if tier:
                args.extend(["--tier", tier])
            result = CliRunner().invoke(cli_read, args)
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            rendered = payload["content"]
            if meta:
                assert payload["frontmatter"]["cross_refs"] == [OWN, GLOBAL, SHARED]
        if tier != "abstract" or (surface == "mcp" and meta):
            filtered_meta, _ = parser.parse_frontmatter(rendered)
            assert filtered_meta["cross_refs"] == [OWN, GLOBAL, SHARED]
            assert filtered_meta["backed_by"] == [FOREIGN]
            assert filtered_meta["contradicts"] == [FOREIGN]
            if tier != "abstract":
                assert "[[decisions/foreign-rule]]" in rendered
    assert legacy.read_text() == original


@pytest.mark.parametrize("surface", ["api", "mcp", "cli"])
@pytest.mark.asyncio
async def test_explicit_read_of_other_project_stays_allowed(records, monkeypatch, surface):
    """An explicit read may cross projects, like explicit search; only automatic delivery is isolated."""
    monkeypatch.setenv("PALINODE_PROJECT", "home")
    with TestClient(app) as client:
        if surface == "api":
            response = client.get("/read", params={"file_path": FOREIGN})
            assert response.status_code == 200
            assert "Target body" in response.json()["content"]
        elif surface == "mcp":
            async def get(path, params=None, **kwargs):
                return client.get(path, params=params)
            monkeypatch.setattr(mcp, "_get", get)
            result = (await mcp._tool_read({"file_path": FOREIGN}))[0].text
            assert "Target body" in result
        else:
            monkeypatch.setattr(api_client, "client", client)
            result = CliRunner().invoke(cli_read, [FOREIGN])
            assert result.exit_code == 0 and "Target body" in result.output


def test_automatic_read_withholds_other_project(records, monkeypatch):
    monkeypatch.setenv("PALINODE_PROJECT", "home")
    with TestClient(app) as client:
        withheld = client.get("/read", params={"file_path": FOREIGN, "automatic": "true"})
        assert withheld.status_code == 404
        assert "another project" in withheld.json()["detail"]
        own = client.get("/read", params={"file_path": OWN, "automatic": "true"})
        assert own.status_code == 200


def test_reader_scope_is_independent_of_source_and_server_pin(legacy, monkeypatch):
    monkeypatch.setenv("PALINODE_PROJECT", "foreign")
    with TestClient(app) as client:
        home = client.get("/read", params={"file_path": "insights/legacy", "project": "home"})
        assert parser.parse_frontmatter(home.json()["content"])[0]["cross_refs"] == [OWN, GLOBAL, SHARED]
        unscoped = client.get("/read", params={"file_path": "insights/legacy", "project": ""})
        assert parser.parse_frontmatter(unscoped.json()["content"])[0]["cross_refs"] == REFS
        assert client.get("/read", params={"file_path": FOREIGN, "project": ""}).status_code == 200
        # A global source still filters links by its reader's project.
        post = frontmatter.load(legacy)
        post["entities"] = []
        legacy.write_text(frontmatter.dumps(post))
        response = client.get("/read", params={"file_path": "insights/legacy", "meta": True})
        assert response.json()["frontmatter"]["cross_refs"] == [FOREIGN, GLOBAL, SHARED]


def test_filtered_targets_use_live_metadata_and_drop_invalid_refs(legacy):
    post = frontmatter.load(legacy)
    post["cross_refs"] = REFS + ["../outside", "/etc/passwd", "decisions/missing"]
    legacy.write_text(frontmatter.dumps(post))
    # Frontmatter-only edits need no index refresh.
    target = legacy.parent.parent / (OWN + ".md")
    other = frontmatter.load(target)
    other["entities"] = ["project/foreign"]
    target.write_text(frontmatter.dumps(other))
    with TestClient(app) as client:
        result = client.get("/read", params={"file_path": "insights/legacy", "project": "home", "meta": True})
    assert result.json()["frontmatter"]["cross_refs"] == [GLOBAL, SHARED]


@pytest.mark.parametrize("surface", ["mcp", "cli"])
@pytest.mark.asyncio
async def test_client_read_scope_overrides_remote_server_pin(legacy, monkeypatch, surface):
    from palinode.core import context_prime

    monkeypatch.setenv("PALINODE_PROJECT", "foreign")
    real_resolve = context_prime.resolve_context
    def resolve(cwd=None, project=None):
        if cwd is not None:
            return context_prime.ProjectResolution("project/home", "explicit")
        return real_resolve(project=project)
    monkeypatch.setattr(context_prime, "resolve_context", resolve)
    monkeypatch.setattr(mcp, "_resolve_scope", lambda: context_prime.ProjectResolution("project/home", "explicit"))
    with TestClient(app) as client:
        if surface == "mcp":
            async def get(path, params=None, **kwargs):
                return client.get(path, params=params)
            monkeypatch.setattr(mcp, "_get", get)
            content = (await mcp._tool_read({"file_path": "insights/legacy"}))[0].text
        else:
            monkeypatch.setattr(api_client, "client", client)
            result = CliRunner().invoke(cli_read, ["insights/legacy", "--format", "json"])
            assert result.exit_code == 0, result.output
            content = json.loads(result.output)["content"]
    assert parser.parse_frontmatter(content)[0]["cross_refs"] == [OWN, GLOBAL, SHARED]

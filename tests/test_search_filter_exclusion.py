"""An empty search that ``where`` or ``types`` emptied says so, on every surface.

The measured failure: an agent searched with ``where: ["project=<slug>"]``.
Records carry their project in ``entities``, no record has a ``project:``
frontmatter key, and a missing key never matches, so every candidate was
excluded. The reply said "Nothing in the visible indexed corpus matched this
query", which blames the query; the agent concluded nothing had been saved.

The contract these tests pin:

1. a ``where`` no record in scope satisfies is named, and so is each of its
   keys that no record in scope has, without running the query;
2. a ``where`` or ``types`` filter that removes every match the query has
   reports how many it removed, and names the filter that did it;
3. a filter that leaves some matches changes nothing;
4. a query with no matches at all keeps the old wording;
5. every count and every "no record has the key" is judged over the records
   the caller may see: another project's records reach neither;
6. the search without the filters records no recall.

``POST /search`` carries ``receipt.retrieval.filter_exclusion``; the MCP tool
text, the CLI's text and JSON output render it.

Real SQLite + tmp_path through ``TestClient``, in lexical retrieval mode so no
embedder is involved, except one hybrid case under a fake embedder.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from palinode import mcp
from palinode.api import server
from palinode.cli import main
from palinode.cli._api import api_client
from palinode.core import embedder, store
from palinode.core.config import config
from palinode.core.context_prime import ProjectResolution
from palinode.core.scoring import describe_filter_exclusion
from palinode.indexer import reconcile
from palinode.indexer.index_file import index_file

# The package re-exports the click command under the module's name.
cli_search = importlib.import_module("palinode.cli.search")

_SCOPE = ProjectResolution("project/alpha", "environment")
_OLD_WORDING = "Nothing in the visible indexed corpus matched this query."

MISSING_PROJECT_KEY = (
    "No record in scope matches where: project=alpha; no record in scope has the "
    "key `project`. The filter, not the query, emptied this result."
)


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    no_model = Mock(side_effect=AssertionError("a model was contacted"))
    monkeypatch.setattr(embedder, "embed", no_model)
    monkeypatch.setattr(embedder, "embed_many", no_model)
    monkeypatch.setattr(reconcile, "get_ollama_client", no_model)
    store.init_db()
    return tmp_path


def _write(memory_dir: Path, rel: str, body: str, front_matter: str) -> str:
    """A record written by hand, then indexed; returns its memory-relative path."""
    path = memory_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    slug = path.stem
    path.write_text(
        f"---\nid: {path.parent.name}-{slug}\n{front_matter}---\n\n{body}\n",
        encoding="utf-8",
    )
    index_file(str(path))
    return rel


@pytest.fixture()
def records(memory_dir) -> dict[str, str]:
    """One alpha record and two beta records about the ledger; one global note.

    Beta's records carry ``owner``, a key nothing alpha may see has, and two of
    them so that a count leaking them reads 3, not 1.
    """
    return {
        "alpha": _write(
            memory_dir, "decisions/alpha-ledger.md",
            "The kestrel ledger migration runs in batch kestrel-a66c449c.",
            "category: decisions\ntype: Decision\nstatus: open\n"
            "entities: [project/alpha]\n",
        ),
        "beta-1": _write(
            memory_dir, "decisions/beta-ledger-1.md",
            "The kestrel ledger migration for beta runs nightly.",
            "category: decisions\ntype: Decision\nstatus: open\nowner: beta-team\n"
            "entities: [project/beta]\n",
        ),
        "beta-2": _write(
            memory_dir, "decisions/beta-ledger-2.md",
            "Beta kestrel ledger migration rollback plan.",
            "category: decisions\ntype: Decision\nstatus: open\nowner: beta-team\n"
            "entities: [project/beta]\n",
        ),
        "global": _write(
            memory_dir, "insights/office-note.md",
            "Office bicycles are stored behind the goods lift.",
            "category: insights\ntype: Insight\nstatus: resolved\n",
        ),
    }


@pytest.fixture()
def api(records, monkeypatch):
    server._rate_counters.clear()
    with TestClient(server.app, raise_server_exceptions=False) as client:
        yield client
    server._rate_counters.clear()


def _search(api, **body) -> dict:
    body.setdefault("context", ["project/alpha"])
    body.setdefault("threshold", 0.0)
    body["receipt"] = True
    server._rate_counters.clear()
    response = api.post("/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _exclusion(payload: dict):
    return payload["receipt"]["retrieval"].get("filter_exclusion")


# ── surfaces against the real app ───────────────────────────────────────────

@pytest.fixture()
def mcp_tool(api, monkeypatch):
    """``palinode_search`` through the MCP handler, answered by the real app."""

    async def handle(request: httpx.Request) -> httpx.Response:
        server._rate_counters.clear()
        reply = api.request(
            request.method, request.url.raw_path.decode("ascii"),
            content=request.content, headers=dict(request.headers),
        )
        return httpx.Response(reply.status_code, content=reply.content, request=request)

    monkeypatch.setattr(mcp, "_http_transport", httpx.MockTransport(handle))
    monkeypatch.setattr(mcp, "_http_client", None)
    monkeypatch.setattr(mcp, "_http_client_loop", None)
    monkeypatch.setattr(mcp, "_resolve_scope", lambda: _SCOPE)

    def call(**arguments) -> str:
        return asyncio.run(mcp._dispatch_tool("palinode_search", arguments))[0].text

    return call


@pytest.fixture()
def cli(api, monkeypatch):
    """``palinode search`` through the CLI, answered by the real app."""

    def handler(request: httpx.Request) -> httpx.Response:
        server._rate_counters.clear()
        reply = api.request(
            request.method, request.url.raw_path.decode("ascii"),
            content=request.content, headers=dict(request.headers),
        )
        return httpx.Response(reply.status_code, content=reply.content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
    monkeypatch.setattr(api_client, "client", client)
    monkeypatch.setattr(cli_search, "_cli_resolve_scope", lambda: _SCOPE)

    def run(*args: str):
        return CliRunner().invoke(main, ["search", *args, "--threshold", "0"])

    yield run
    client.close()


# ── 1. a where no record in scope satisfies ─────────────────────────────────

def test_a_where_on_a_key_no_record_has_says_the_key_is_absent(api):
    """The eval's call: the query would find the alpha record, the filter
    removes it, and no record has a ``project`` key."""
    assert _search(api, query="ledger migration")["results"], "premise: the query matches"

    payload = _search(api, query="ledger migration", where=["project=alpha"])

    assert payload["results"] == []
    assert _exclusion(payload) == {
        "by": ["where"], "where": ["project=alpha"], "matches": None,
        "matches_at_least": False, "missing_keys": ["project"],
    }
    assert payload["receipt"]["retrieval"]["outcome"] == "no_match"


def test_mcp_names_the_filter_and_the_missing_key(mcp_tool):
    text = mcp_tool(query="ledger migration", where=["project=alpha"], tier="full")

    assert text.splitlines()[:3] == [
        "Scope: project/alpha (environment)",
        f"No confident match. {MISSING_PROJECT_KEY}",
        "No results found.",
    ]
    assert _OLD_WORDING not in text


def test_cli_text_names_the_filter_and_the_missing_key(cli):
    result = cli("ledger migration", "--where", "project=alpha", "--format", "text")

    assert result.exit_code == 0, result.output
    assert MISSING_PROJECT_KEY in " ".join(result.output.split())
    assert "No results found." in result.output


def test_cli_json_keeps_the_array_on_stdout_and_says_why_on_stderr(cli):
    result = cli("ledger migration", "--where", "project=alpha", "--format", "json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert result.stderr.strip() == MISSING_PROJECT_KEY


def test_cli_json_diagnostics_carry_the_structured_field(cli):
    result = cli(
        "ledger migration", "--where", "project=alpha", "--format", "json", "--diagnostics",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["results"] == []
    assert payload["receipt"]["retrieval"]["filter_exclusion"]["missing_keys"] == ["project"]
    assert result.stderr == ""


def test_a_key_that_exists_with_another_value_is_not_called_absent(api):
    payload = _search(api, query="ledger migration", where=["status=nonexistent"])

    assert _exclusion(payload)["missing_keys"] == []
    assert describe_filter_exclusion(payload["receipt"]["retrieval"]) == (
        "No record in scope matches where: status=nonexistent. "
        "The filter, not the query, emptied this result."
    )


def test_a_key_only_another_project_has_reads_as_absent_in_scope(api):
    """Beta's records carry ``owner``. From alpha the key is absent: the reply
    must not say that some record outside the scope has it."""
    payload = _search(api, query="ledger migration", where=["owner=beta-team"])

    assert payload["results"] == []
    assert _exclusion(payload)["missing_keys"] == ["owner"]
    # Unchanged: the withheld count was already reported for this case.
    assert payload["receipt"]["retrieval"]["other_projects_withheld"] == 2


def test_an_empty_query_listing_says_so_too(api, cli):
    """The recency listing (REST and CLI; the MCP tool requires a query) carries
    no confidence verdict, and the filter line is still there."""
    payload = _search(api, query="", where=["project=alpha"])
    result = cli("", "--where", "project=alpha", "--format", "text")

    assert payload["results"] == []
    assert _exclusion(payload)["missing_keys"] == ["project"]
    assert MISSING_PROJECT_KEY in " ".join(result.output.split())


# ── 2. a filter that removes every match the query has ──────────────────────

def test_a_where_that_excludes_every_match_reports_how_many(api, mcp_tool):
    """``status=resolved`` matches the office note, which the query does not
    find; without the filter the query finds alpha's record and nothing else
    the caller may see."""
    payload = _search(api, query="kestrel ledger", where=["status=resolved"])

    assert payload["results"] == []
    assert _exclusion(payload) == {
        "by": ["where"], "where": ["status=resolved"], "matches": 1,
        "matches_at_least": False, "missing_keys": [],
    }
    line = (
        "1 match for this search was excluded by where: status=resolved. "
        "The filter, not the query, emptied this result."
    )
    assert f"No confident match. {line}" in mcp_tool(
        query="kestrel ledger", where=["status=resolved"],
    )


def test_another_projects_matches_are_not_counted(api):
    """Unscoped, the same search counts all three ledger records; scoped to
    alpha, only alpha's."""
    unscoped = _search(api, query="kestrel ledger", where=["status=resolved"], context=[])
    scoped = _search(api, query="kestrel ledger", where=["status=resolved"])

    assert _exclusion(unscoped)["matches"] == 3
    assert _exclusion(scoped)["matches"] == 1


def test_a_types_filter_that_excludes_every_match_is_named(api, cli):
    payload = _search(api, query="kestrel ledger", types=["Insight", "ProjectSnapshot"])
    line = (
        "1 match for this search was excluded by types: Insight, ProjectSnapshot. "
        "The filter, not the query, emptied this result."
    )

    assert payload["results"] == []
    assert _exclusion(payload) == {
        "by": ["types"], "types": ["Insight", "ProjectSnapshot"], "matches": 1,
        "matches_at_least": False, "missing_keys": [],
    }
    result = cli("kestrel ledger", "--types", "Insight", "--types", "ProjectSnapshot",
                 "--format", "text")
    assert line in " ".join(result.output.split())


def test_where_and_types_each_able_alone_name_both(api):
    payload = _search(
        api, query="kestrel ledger", where=["status=resolved"], types=["Insight"],
    )

    assert _exclusion(payload)["by"] == ["where", "types"]
    assert describe_filter_exclusion(payload["receipt"]["retrieval"]) == (
        "1 match for this search was excluded by where: status=resolved and types: "
        "Insight. The filters, not the query, emptied this result."
    )


def test_a_filter_that_excludes_the_lone_type_names_only_that_filter(api):
    """``where`` keeps alpha's record and ``types`` drops it: the reply names
    ``types`` alone."""
    payload = _search(api, query="kestrel ledger", where=["status=open"], types=["Insight"])

    assert _exclusion(payload)["by"] == ["types"]
    assert "where" not in _exclusion(payload)


def test_the_search_without_the_filters_records_no_recall(api, records):
    def recalls() -> int:
        db = store.get_db()
        try:
            return db.execute(
                "SELECT coalesce(sum(recall_count), 0) FROM chunks WHERE file_path LIKE ?",
                (f"%{records['alpha']}",),
            ).fetchone()[0]
        finally:
            db.close()

    before = recalls()
    payload = _search(api, query="kestrel ledger", where=["status=resolved"])

    assert _exclusion(payload)["matches"] == 1
    assert recalls() == before


# ── 3. a filter that leaves some matches ────────────────────────────────────

def test_a_filter_that_leaves_matches_is_unchanged(api, mcp_tool, cli):
    payload = _search(api, query="kestrel ledger", where=["status=open"], types=["Decision"])
    text = mcp_tool(query="kestrel ledger", where=["status=open"], types=["Decision"])
    result = cli("kestrel ledger", "--where", "status=open", "--format", "text")

    assert [row["rel_path"] for row in payload["results"]] == ["decisions/alpha-ledger.md"]
    assert "filter_exclusion" not in payload["receipt"]["retrieval"]
    assert "excluded by" not in text
    assert "excluded by" not in result.output


# ── 4. no match at all ──────────────────────────────────────────────────────

def test_a_query_with_no_matches_keeps_the_old_wording(api, mcp_tool, cli):
    """The filter matches records, and without it the query still finds nothing:
    the query is the reason."""
    payload = _search(api, query="sonar calibration", where=["status=open"])
    text = mcp_tool(query="sonar calibration", where=["status=open"])
    result = cli("sonar calibration", "--where", "status=open", "--format", "json")

    assert payload["results"] == []
    assert "filter_exclusion" not in payload["receipt"]["retrieval"]
    assert f"No confident match. {_OLD_WORDING}" in text
    assert result.stderr == ""


def test_a_query_with_no_matches_and_no_filter_keeps_the_old_wording(mcp_tool):
    assert f"No confident match. {_OLD_WORDING}" in mcp_tool(query="sonar calibration")


def test_matches_only_in_another_project_are_not_reported_as_excluded(api):
    """Beta's ``rollback`` record is the only one the query finds. From alpha it
    is invisible, so the filter emptied nothing the caller could have seen."""
    payload = _search(api, query="rollback", types=["Insight"])

    assert payload["results"] == []
    assert "filter_exclusion" not in payload["receipt"]["retrieval"]


def test_the_bare_array_shape_is_unchanged(api):
    """Without ``receipt`` the response is the frozen bare array."""
    server._rate_counters.clear()
    response = api.post("/search", json={
        "query": "ledger migration", "where": ["project=alpha"],
        "context": ["project/alpha"], "threshold": 0.0,
    })

    assert response.status_code == 200, response.text
    assert response.json() == []


# ── hybrid retrieval ────────────────────────────────────────────────────────

def test_hybrid_types_exclusion_counts_the_unfiltered_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.search, "retrieval_mode", "hybrid")
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(embedder, "embed", MagicMock(return_value=[1.0] + [0.0] * 1023))
    store.init_db()
    server._rate_counters.clear()
    with TestClient(server.app, raise_server_exceptions=False) as api:
        for n in range(3):
            server._rate_counters.clear()
            saved = api.post("/save", json={
                "content": f"harborlight insight {n}", "type": "Insight", "slug": f"note-{n}",
            })
            assert saved.status_code == 200, saved.text
        payload = _search(api, query="harborlight", types=["Decision"], context=[], limit=2)
    server._rate_counters.clear()

    assert payload["results"] == []
    exclusion = _exclusion(payload)
    assert exclusion["by"] == ["types"]
    assert exclusion["matches"] == 3


# ── the line itself ─────────────────────────────────────────────────────────

def test_a_full_window_is_reported_as_a_lower_bound():
    value = {"filter_exclusion": {
        "by": ["where"], "where": ["status=open"], "matches": 25,
        "matches_at_least": True, "missing_keys": [],
    }}

    assert describe_filter_exclusion(value) == (
        "At least 25 matches for this search were excluded by where: status=open. "
        "The filter, not the query, emptied this result."
    )


def test_several_missing_keys_are_all_named():
    value = {"filter_exclusion": {
        "by": ["where"], "where": ["project=alpha", "owner=me"], "matches": None,
        "matches_at_least": False, "missing_keys": ["project", "owner"],
    }}

    assert describe_filter_exclusion(value) == (
        "No record in scope matches where: project=alpha, owner=me; no record in scope "
        "has any of the keys `project`, `owner`. The filter, not the query, emptied "
        "this result."
    )


def test_no_block_no_line():
    assert describe_filter_exclusion({"outcome": "no_match"}) == ""

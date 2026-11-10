"""How an opted-in record from another project is presented.

In a live evaluation an agent scoped to ``project/ledger-4a1f20`` searched with
``include_other_projects``, read a decision labelled
``[other project: project/ledger-9c3e57]`` (the tag sat mid-line, after the
match score) and wrote that decision's value into its own config. The two
slugs share a base name; only one of them was printed.

Every opt-in surface now puts other projects' records after the request's own,
under a heading that says they are context only, and opens each such row with
both projects: ``[other project: project/ledger-9c3e57 — not
project/ledger-4a1f20]``. The REST payload keeps its order and shape: the
``other_project`` field on a row and the envelope's ``project`` are the
grouping, and the text renderers build on them.

Real SQLite and real git under ``tmp_path``.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.routers import search as search_router
from palinode.api.server import app
from palinode.core.bundle import BundleRequest, build_bundle
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from palinode.core.scope import ScopeChain
from palinode.core.scoring import (
    other_project_label,
    other_projects_heading,
    split_other_projects,
)
from tests import test_resolve_bundle as scenarios

mem = scenarios.mem
_write = scenarios._write

THIS = "ledger-4a1f20"
OTHER = "ledger-9c3e57"
QUESTION = "which batch id does the ledger migration use"
OWN = "decisions/ledger-exports"
FOREIGN = "decisions/data-team-assigned-batch-id"
FOREIGN_VALUE = "atlas-5b2d0c19"
LABEL = f"[other project: project/{OTHER} — not project/{THIS}]"
HEADING = (
    "Other projects — context only, not this project's decisions "
    f"(this project: project/{THIS})"
)


@pytest.fixture()
def store(mem, monkeypatch):
    """This project's own note, and another project's decision on the same question."""
    monkeypatch.setattr(search_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    monkeypatch.setattr(config.audit, "log_path", str(mem / ".audit" / "mcp-calls.jsonl"))
    monkeypatch.setenv("PALINODE_PROJECT", THIS)
    _write(mem, f"{OWN}.md",
           "# Ledger exports\n\nThe ledger migration exports run nightly at 02:00.",
           type="Insight", status="active", date="2026-10-08",
           entities=[f"project/{THIS}"])
    _write(mem, f"{FOREIGN}.md",
           f"# Batch ID\n\nData team assigned batch ID {FOREIGN_VALUE} for the ledger migration.",
           type="Decision", status="active", date="2026-10-28",
           entities=[f"project/{OTHER}"])
    return mem


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


# ── the shared pieces ────────────────────────────────────────────────────────


def test_label_names_both_projects_even_when_they_share_a_base_name():
    assert other_project_label([f"project/{OTHER}"], THIS) == LABEL
    assert other_project_label([f"project/{OTHER}"], f"project/{THIS}") == LABEL
    # A request with no project has nothing to contrast with.
    assert other_project_label([f"project/{OTHER}"], None) == f"[other project: project/{OTHER}]"


def test_heading_names_this_project():
    assert other_projects_heading(THIS) == HEADING
    assert other_projects_heading(f"project/{THIS}") == HEADING


def test_split_keeps_delivered_order_within_each_group():
    rows = [{"id": 1, "other_project": ["project/x"]}, {"id": 2}, {"id": 3},
            {"id": 4, "other_project": ["project/y"]}]
    own, other = split_other_projects(rows)
    assert [r["id"] for r in own] == [2, 3]
    assert [r["id"] for r in other] == [1, 4]


# ── REST: order and shape unchanged; the fields carry the grouping ───────────


def test_rest_search_keeps_its_shape(store):
    with TestClient(app) as client:
        resp = client.post("/search", json={
            "query": QUESTION, "context": [f"project/{THIS}"], "receipt": True,
            "limit": 10, "threshold": 0.0, "include_other_projects": True,
        })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project"] == f"project/{THIS}"
    foreign = [h for h in body["results"] if h.get("other_project")]
    assert [h["other_project"] for h in foreign] == [[f"project/{OTHER}"]]
    own = [h for h in body["results"] if not h.get("other_project")]
    assert own, body["results"]


# ── MCP search ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_search_groups_and_labels_the_other_project(store, monkeypatch):
    with TestClient(app) as client:
        async def _fake_post(path, json=None, timeout=30.0):
            return _Resp(client.post(path, json=json).json())

        monkeypatch.setattr(mcp, "_post", _fake_post)
        out = await mcp._tool_search({
            "query": QUESTION, "threshold": 0.0, "include_other_projects": True,
        })
    text = out[0].text
    assert FOREIGN_VALUE in text
    # The foreign row opens with both projects, before its path and score.
    assert f"\n{LABEL} [{FOREIGN}.md]" in text, text
    # It comes after this project's own result, under the heading.
    assert text.index(f"[{OWN}.md]") < text.index(HEADING) < text.index(LABEL)
    # The old mid-line tag is gone.
    assert f"[other project: project/{OTHER}]" not in text


@pytest.mark.asyncio
async def test_mcp_search_without_opt_in_has_no_heading(store, monkeypatch):
    with TestClient(app) as client:
        async def _fake_post(path, json=None, timeout=30.0):
            return _Resp(client.post(path, json=json).json())

        monkeypatch.setattr(mcp, "_post", _fake_post)
        out = await mcp._tool_search({"query": QUESTION, "threshold": 0.0})
    assert FOREIGN_VALUE not in out[0].text
    assert "Other projects —" not in out[0].text


def test_mcp_renderer_with_only_foreign_rows_still_heads_them():
    rows = [{"file_path": f"/m/{FOREIGN}.md", "rel_path": f"{FOREIGN}.md", "score": 0.7,
             "snippet": f"Data team assigned batch ID {FOREIGN_VALUE}.",
             "other_project": [f"project/{OTHER}"]}]
    text = mcp._format_results(rows, project=f"project/{THIS}")
    assert f"{HEADING}\n\n{LABEL} [{FOREIGN}.md]" in text, text


# ── CLI search ───────────────────────────────────────────────────────────────


def test_cli_search_groups_and_labels_the_other_project(store, monkeypatch):
    from palinode.cli._api import api_client
    from palinode.cli.search import search as cli_search

    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", client)
        result = CliRunner().invoke(cli_search, [
            QUESTION, "--limit", "10", "--threshold", "0",
            "--include-other-projects", "--format", "text",
        ])
    assert result.exit_code == 0, result.output
    # The console wraps at the terminal width; compare the words.
    out = " ".join(result.output.split())
    # Own result, then the heading, then the labelled row and its text.
    assert (out.index("Ledger exports") < out.index(HEADING)
            < out.index(LABEL) < out.index(FOREIGN_VALUE)), out
    assert f"[other project: project/{OTHER}]" not in out


# ── resolve bundle (MCP, REST, CLI and the hook all render it) ───────────────


def test_resolve_bundle_row_opens_with_both_projects(store):
    data = build_bundle(
        BundleRequest(query=QUESTION, include_other_projects=True),
        chain=ScopeChain(project=THIS),
    ).to_dict()
    line = next(line for line in data["text"].splitlines() if FOREIGN in line)
    assert line.startswith(f"- {LABEL} [{FOREIGN}]"), line
    # The structured payload is unchanged: the label's second slug is not a field.
    item = next(s for s in data["selected"] if s["ref"] == FOREIGN)
    assert item["other_project"] == [f"project/{OTHER}"]
    assert "this_project" not in item

"""A project-scoped request does not get another project's daily-note entries.

``session_end`` writes each summary twice: to its own record, tagged
``project/<slug>``, and to the day's ``daily/<date>.md``, which every project
shares. Project isolation decides by a file's frontmatter, and a daily note
names no project, so its entries were served as global records: a search
scoped to one project handed it another project's decisions. Two projects
ending sessions on the same day was enough; an evaluation with several projects in one store found it.

The rule now: each daily entry says whose it is (session-end writes a
``**Project:** project/<slug>`` line into it), and a daily row belongs to the
projects its own entry names. On a project-scoped request an entry naming
none is withheld and counted, since it may be any project's. Unscoped
requests are unchanged.

Real SQLite and real git under ``tmp_path``, through the real session-end
endpoint.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.routers import search as search_router
from palinode.api.server import app
from palinode.consolidation.runner import _note_mentions
from palinode.indexer import reconcile
from palinode.core.bundle import BundleRequest, build_bundle
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from palinode.core.scope import ScopeChain
from palinode.core import store as store_mod
from palinode.core.visibility import daily_entry_projects, filter_visible, is_row_visible
from tests import test_project_isolation as isolation
from tests import test_resolve_bundle as scenarios

mem = scenarios.mem

ALPHA_ID = "alphabatchquill"
BETA_ID = "betabatchmarrow"
LEGACY_ID = "legacybatchfennel"
QUESTION = "which batch id should the migration use"


def _session_end(client, project: str | None, batch: str, **extra) -> dict:
    body = {
        "summary": "Planned the migration batch",
        "decisions": [f"Use batch ID {batch} for the migration"],
        "source": "test", **extra,
    }
    if project:
        body["project"] = project
    resp = client.post("/session-end", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _daily(mem) -> str:
    [path] = (mem / "daily").glob("*.md")
    return str(path)


def store_rows(mem) -> list[str]:
    db = store_mod.get_db()
    try:
        return [r[0] for r in db.execute(
            "SELECT content FROM chunks WHERE file_path LIKE ?", (f"%{os.sep}daily{os.sep}%",),
        )]
    finally:
        db.close()


def _reindex(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        reconcile.reconcile(path, fh.read())


@pytest.fixture()
def store(mem, monkeypatch):
    """Alpha and beta each end a session today; one older entry is untagged."""
    monkeypatch.setattr(search_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    monkeypatch.setattr(config.audit, "log_path", str(mem / ".audit" / "mcp-calls.jsonl"))
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    with TestClient(app) as client:
        _session_end(client, "alpha", ALPHA_ID)
        _session_end(client, "beta", BETA_ID)
    daily = _daily(mem)
    with open(daily, "a", encoding="utf-8") as fh:
        fh.write(
            "\n## Session End — 2026-10-01T09:00:00Z\n\n**Source:** mcp\n\n"
            f"**Summary:** Planned it\n\n**Decisions:**\n- Use batch ID {LEGACY_ID}\n"
        )
    _reindex(daily)
    return mem


def _search(client, context, **extra) -> dict:
    resp = client.post("/search", json={
        "query": QUESTION, "context": context, "receipt": True, "limit": 20,
        "threshold": 0.0, **extra,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _daily_hits(payload: dict) -> list[dict]:
    return [h for h in payload["results"] if "/daily/" in h["file_path"]
            or h["file_path"].startswith("daily/")]


# ── the write ────────────────────────────────────────────────────────────────


def test_each_daily_entry_names_its_project(store):
    text = open(_daily(store), encoding="utf-8").read()
    alpha, beta = (e for e in text.split("## Session End")[1:] if "**Project:**" in e)
    assert "**Project:** project/alpha" in alpha and ALPHA_ID in alpha
    assert "**Project:** project/beta" in beta and BETA_ID in beta


def test_a_session_with_no_project_writes_no_project_line(mem):
    with TestClient(app) as client:
        result = _session_end(client, None, ALPHA_ID, dry_run=True)
    assert "**Project:**" not in result["entry"]


def test_consolidation_groups_the_entry_under_its_project(store):
    """The tag is the ``project/<slug>`` text the consolidation pass groups on."""
    text = open(_daily(store), encoding="utf-8").read()
    alpha = next(e for e in text.split("## Session End")[1:] if ALPHA_ID in e)
    assert "project/alpha" in _note_mentions({}, alpha)


def test_only_the_label_line_tags_an_entry():
    row = {
        "file_path": "daily/2026-11-01.md",
        "content": "## Session End\n**Summary:** ported a fix from project/beta\n",
    }
    assert daily_entry_projects(row) == [None]
    assert daily_entry_projects({**row, "file_path": "decisions/x.md"}) is None
    assert daily_entry_projects({**row, "file_path": "archive/2026/2026-11-01.md"}) == [None]


def test_a_short_note_of_several_entries_is_indexed_one_entry_per_row(store):
    rows = store_rows(store)
    assert len(rows) == 3
    assert all(c.count("## Session End") == 1 for c in rows)


def test_a_row_indexed_before_the_split_is_seen_only_if_every_entry_is_the_requests():
    """An older index can hold one row for a whole day's note."""
    both = {
        "file_path": "daily/2026-10-30.md", "metadata": {},
        "content": (
            "## Session End — a\n**Project:** project/alpha\n- Use batch ID one\n\n"
            "## Session End — b\n**Project:** project/beta\n- Use batch ID two\n"
        ),
    }
    alpha_only = {**both, "content": both["content"].split("\n\n")[0] + "\n"}
    alpha = ScopeChain(project="alpha")
    assert daily_entry_projects(both) == ["project/alpha", "project/beta"]
    assert not is_row_visible(alpha, both)
    assert is_row_visible(alpha, alpha_only)
    assert is_row_visible(None, both)
    withheld: set[str] = set()
    assert filter_visible(alpha, [both], other_projects=withheld) == []
    assert withheld == {"daily/2026-10-30.md"}


# ── search ───────────────────────────────────────────────────────────────────


def test_scoped_search_gets_only_its_own_daily_entry(store):
    with TestClient(app) as client:
        payload = _search(client, ["project/alpha"])
    dumped = json.dumps(payload["results"])
    assert ALPHA_ID in dumped
    assert BETA_ID not in dumped
    assert LEGACY_ID not in dumped
    assert payload["receipt"]["retrieval"]["other_projects_withheld"] >= 1


def test_unscoped_search_still_sees_every_entry(store):
    with TestClient(app) as client:
        payload = _search(client, [])
    dumped = json.dumps(_daily_hits(payload))
    assert ALPHA_ID in dumped and BETA_ID in dumped and LEGACY_ID in dumped
    assert all("other_project" not in hit for hit in payload["results"])


def test_include_other_projects_labels_the_daily_entry(store):
    with TestClient(app) as client:
        payload = _search(client, ["project/alpha"], include_other_projects=True)
    hits = _daily_hits(payload)
    beta = next(h for h in hits if BETA_ID in h["content"])
    assert beta["other_project"] == ["project/beta"]
    alpha = next(h for h in hits if ALPHA_ID in h["content"])
    assert "other_project" not in alpha


@pytest.mark.asyncio
async def test_mcp_search_scoped_by_the_project_env(store, monkeypatch):
    """The route the rerun's agents took: their own scoped ``palinode_search``."""
    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    with TestClient(app) as client:
        async def _fake_post(path, json=None, timeout=30.0):
            return isolation._Resp(client.post(path, json=json).json())

        monkeypatch.setattr(mcp, "_post", _fake_post)
        out = await mcp._tool_search({"query": QUESTION, "threshold": 0.0, "limit": 20})
    text = out[0].text
    assert ALPHA_ID in text
    assert BETA_ID not in text and LEGACY_ID not in text


# ── resolve ──────────────────────────────────────────────────────────────────


def test_scoped_resolve_never_carries_another_projects_entry(store):
    data = build_bundle(
        BundleRequest(query=QUESTION), chain=ScopeChain(project="alpha"),
    ).to_dict()
    dumped = json.dumps(data)
    assert BETA_ID not in dumped and LEGACY_ID not in dumped


def test_rest_resolve_scoped_by_the_requester_project(store):
    with TestClient(app) as client:
        scoped = client.post("/resolve", json={"query": QUESTION, "project": "beta"})
        unscoped = client.post("/resolve", json={"query": QUESTION})
    assert scoped.status_code == unscoped.status_code == 200
    assert ALPHA_ID not in scoped.text and LEGACY_ID not in scoped.text
    assert BETA_ID in scoped.text
    assert unscoped.json()["text"]

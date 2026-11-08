# ruff: noqa: F811
"""Chosen paths stay discoverable and cannot pre-create ordinary-save aliases."""
from __future__ import annotations

from pathlib import Path

import pytest

from palinode.core import store
from palinode.core.save import SaveValidationError, save_memory
from test_save_path import (  # noqa: F401
    _api_save, _cli_save, _mcp_save, cli_via_api, client,
    mcp_via_api, memory_dir, no_network,
)

_REFUSED = [
    "Decisions/why.md", "People/person.md", "PROJECTS/project.md",
    "Insights/note.md", "Research/paper.md", "Inbox/task.md",
    "Daily/2026-11-08.md", "Archive/note.md", "insights/Keep.md",
    "decisions/My-Decision.md", "team/.draft.md", "team/.drafts/x.md",
    "team/.gitkeep-notes/x.md", "a/.b/c.d.md",
]


@pytest.mark.parametrize("chosen", _REFUSED)
def test_api_refuses_before_creating_files_folders_or_index_rows(client, memory_dir, chosen):
    response = _api_save(client, path=chosen)
    assert response.status_code == 400, response.text
    assert list(memory_dir.rglob("*.md")) == []
    assert not any(p.is_dir() for p in memory_dir.iterdir())
    with store.get_db() as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


@pytest.mark.parametrize("chosen", _REFUSED)
def test_core_refuses_the_same_names(memory_dir, chosen):
    with pytest.raises(SaveValidationError):
        save_memory(content="Quokka path boundary.", type="Insight", path=chosen)


@pytest.mark.parametrize("chosen", ["Decisions/why.md", "insights/Keep.md", "team/.draft.md", "a/.b/x.md"])
def test_mcp_refuses_the_same_names(mcp_via_api, memory_dir, chosen):
    response = _mcp_save(path=chosen)
    assert "path" in response.lower(), response
    assert list(memory_dir.rglob("*.md")) == []


@pytest.mark.parametrize("chosen", ["Decisions/why.md", "insights/Keep.md", "team/.draft.md", "a/.b/x.md"])
def test_cli_refuses_the_same_names(cli_via_api, memory_dir, chosen):
    response = _cli_save("--path", chosen)
    assert response.exit_code != 0, response.output
    assert list(memory_dir.rglob("*.md")) == []


@pytest.mark.parametrize("chosen", [
    "Team/Keep.md", "team/draft.md", "insights/keep.md",
    "insights/Case_Name.md", "team/role.v2.md", "a/b/c.d.md",
])
def test_visible_distinct_names_stay_indexed_after_reindex(client, memory_dir, chosen):
    response = _api_save(client, path=chosen)
    assert response.status_code == 200, response.text
    with store.get_db() as db:
        before = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert before > 0
    reindexed = client.post("/reindex")
    assert reindexed.status_code == 200, reindexed.text
    with store.get_db() as db:
        after = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert after == before
    assert (memory_dir / chosen).is_file()


@pytest.mark.parametrize("chosen,kind", [
    ("decisions/why.md", "Decision"), ("insights/keep.md", "Insight"),
])
def test_path_then_slug_uses_one_index_identity(client, memory_dir, chosen, kind):
    first = _api_save(client, path=chosen, type=kind, content="First quokka statement.")
    assert first.status_code == 200, first.text
    second = _api_save(client, slug=Path(chosen).stem, type=kind, content="Second marmalade statement.")
    assert second.status_code == 200, second.text
    with store.get_db() as db:
        paths = [row[0] for row in db.execute("SELECT DISTINCT file_path FROM chunks")]
    assert paths == [str(memory_dir / chosen)]

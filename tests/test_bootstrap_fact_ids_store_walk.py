from __future__ import annotations

from pathlib import Path

import pytest

from palinode.api.routers.consolidation import bootstrap_fact_ids_api
from palinode.core.config import config

DRAFT = "# draft\n\n- a hidden draft fact\n"


@pytest.fixture()
def people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    folder = tmp_path / "people"
    folder.mkdir()
    (folder / "alice.md").write_text("# alice\n\n- alice fact\n", encoding="utf-8")
    (folder / ".draft.md").write_text(DRAFT, encoding="utf-8")
    return folder


def test_bootstrap_tags_only_memory_files(people: Path) -> None:
    assert bootstrap_fact_ids_api(None) == {"files": 1, "facts_tagged": 1}


def test_bootstrap_leaves_a_dot_file_unchanged(people: Path) -> None:
    bootstrap_fact_ids_api(None)

    assert (people / ".draft.md").read_text(encoding="utf-8") == DRAFT

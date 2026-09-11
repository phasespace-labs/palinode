"""``bootstrap-ids --file <rel-path>`` tags exactly one memory file.

The whole-store walk is the wrong instrument for the case that needs it: one
status document fed by session-end holds hundreds of untagged bullets while
every curated file around it is tagged, or deliberately not. `doctor` names the
inert document; this is the command that fixes that document and nothing else.

Real files under ``tmp_path`` with a real git repo — the tagging path commits
with provenance, and a test that mocked that away would not be testing the
thing the operator runs.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from palinode.api.routers.consolidation import router as consolidation_router
from palinode.consolidation.fact_ids import bootstrap_fact_ids_for_file
from palinode.core.config import config
from palinode.core.path_guard import PathTraversalError

UNTAGGED = (
    "---\nid: projects-palinode-status\n---\n\n"
    "# Palinode Status\n\n"
    "- [2026-09-09] Shipped the thing.\n"
    "- [2026-09-10] Shipped the other thing.\n"
)


def _git(memory_dir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=memory_dir, check=True, capture_output=True)


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """A git-backed store with one untagged status doc and one untagged insight."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", True)

    (tmp_path / "projects").mkdir()
    (tmp_path / "insights").mkdir()
    (tmp_path / "projects" / "palinode-status.md").write_text(UNTAGGED, encoding="utf-8")
    (tmp_path / "insights" / "other.md").write_text(
        "---\nid: insights-other\n---\n\n- An untouched bullet.\n", encoding="utf-8"
    )

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Palinode Tests")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(consolidation_router)
    return TestClient(app)


def _markers(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("<!-- fact:")


# ── The function ─────────────────────────────────────────────────────────────


def test_tags_exactly_the_named_file(store):
    result = bootstrap_fact_ids_for_file("projects/palinode-status.md")

    assert result["facts_tagged"] == 2
    assert result["files"] == 1
    assert result["file"] == "projects/palinode-status.md"
    assert _markers(store / "projects" / "palinode-status.md") == 2
    assert _markers(store / "insights" / "other.md") == 0, "the walk stayed off"


def test_it_commits_with_provenance(store):
    bootstrap_fact_ids_for_file("projects/palinode-status.md")

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=store,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "bootstrap fact ids" in log
    assert "palinode-status.md" in log


def test_it_is_idempotent(store):
    bootstrap_fact_ids_for_file("projects/palinode-status.md")

    second = bootstrap_fact_ids_for_file("projects/palinode-status.md")

    assert second["facts_tagged"] == 0
    assert _markers(store / "projects" / "palinode-status.md") == 2


@pytest.mark.parametrize(
    "bad_path",
    ["../outside.md", "projects/../../outside.md", "/etc/passwd"],
)
def test_it_refuses_paths_outside_the_store(store, bad_path, tmp_path):
    (tmp_path.parent / "outside.md").write_text("- A bullet.\n", encoding="utf-8")

    with pytest.raises(PathTraversalError):
        bootstrap_fact_ids_for_file(bad_path)

    assert "<!-- fact:" not in (tmp_path.parent / "outside.md").read_text()


def test_a_missing_file_is_not_found(store):
    with pytest.raises(FileNotFoundError):
        bootstrap_fact_ids_for_file("projects/nope.md")


# ── The endpoint ─────────────────────────────────────────────────────────────


def test_endpoint_tags_one_file(store, client):
    response = client.post(
        "/bootstrap-fact-ids", json={"file": "projects/palinode-status.md"}
    )

    assert response.status_code == 200
    assert response.json()["facts_tagged"] == 2
    assert _markers(store / "insights" / "other.md") == 0


def test_endpoint_without_a_body_still_walks_the_store(store, client):
    """The default behaviour is unchanged — every existing caller posts nothing."""
    response = client.post("/bootstrap-fact-ids")

    assert response.status_code == 200
    assert response.json()["facts_tagged"] == 3
    assert _markers(store / "insights" / "other.md") == 1


def test_endpoint_rejects_traversal(store, client):
    response = client.post("/bootstrap-fact-ids", json={"file": "../outside.md"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid path"


def test_endpoint_404s_on_a_missing_file(store, client):
    response = client.post("/bootstrap-fact-ids", json={"file": "projects/nope.md"})

    assert response.status_code == 404


# ── The CLI ──────────────────────────────────────────────────────────────────


def test_cli_passes_the_file_through(monkeypatch):
    """``--file`` reaches the client as a single-file call, not the full walk."""
    from click.testing import CliRunner

    from palinode.cli import manage

    calls: dict[str, str] = {}

    def _fake_file(file_path: str) -> dict:
        calls["file"] = file_path
        return {"files": 1, "facts_tagged": 2, "file": file_path}

    def _fake_all() -> dict:  # pragma: no cover — must not be reached
        calls["all"] = "called"
        return {}

    monkeypatch.setattr(manage.api_client, "bootstrap_ids_file", _fake_file, raising=False)
    monkeypatch.setattr(manage.api_client, "bootstrap_ids", _fake_all, raising=False)

    result = CliRunner().invoke(
        manage.bootstrap_ids,
        ["--file", "projects/palinode-status.md", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == {"file": "projects/palinode-status.md"}


def test_cli_without_the_flag_still_walks(monkeypatch):
    from click.testing import CliRunner

    from palinode.cli import manage

    calls: list[str] = []

    monkeypatch.setattr(
        manage.api_client,
        "bootstrap_ids",
        lambda: (calls.append("all"), {"files": 2, "facts_tagged": 5})[1],
        raising=False,
    )

    result = CliRunner().invoke(manage.bootstrap_ids, ["--format", "json"])

    assert result.exit_code == 0, result.output
    assert calls == ["all"]

"""Tests for the prompt versioning system (API endpoints + consolidation exclusion)."""
from __future__ import annotations

import logging
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.routers import session
from palinode.api.server import app
from palinode.core.config import config

client = TestClient(app)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_memory_dir(tmp_path):
    """Redirect config.memory_dir to a temp directory for isolation."""
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    yield str(tmp_path)
    config.memory_dir = old


def _write_prompt(
    prompts_dir: str,
    name: str,
    task: str = "compaction",
    model: str = "olmo-3.1:32b",
    version: str = "1.0",
    active: bool = False,
    body: str = "You are a helpful assistant.",
) -> str:
    """Helper: write a prompt markdown file and return its path."""
    os.makedirs(prompts_dir, exist_ok=True)
    fm = {
        "type": "prompt",
        "task": task,
        "model": model,
        "version": version,
        "active": active,
    }
    content = f"---\n{yaml.dump(fm)}---\n\n{body}\n"
    path = os.path.join(prompts_dir, f"{name}.md")
    with open(path, "w") as f:
        f.write(content)
    return path


# ── GET /prompts ───────────────────────────────────────────────────────────────

def test_list_prompts_empty(mock_memory_dir):
    resp = client.get("/prompts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_prompts_returns_all(mock_memory_dir):
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", active=True)
    _write_prompt(prompts_dir, "extraction-v1", task="extraction", active=False)

    resp = client.get("/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {p["name"] for p in data}
    assert names == {"compaction-v1", "extraction-v1"}


def test_list_prompts_logs_unreadable_file(mock_memory_dir, monkeypatch, caplog):
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    broken_path = _write_prompt(prompts_dir, "broken")
    _write_prompt(prompts_dir, "healthy")
    read_prompt_file = session._read_prompt_file

    def read_with_one_failure(filepath):
        if filepath == broken_path:
            raise PermissionError("permission denied")
        return read_prompt_file(filepath)

    monkeypatch.setattr(session, "_read_prompt_file", read_with_one_failure)
    with caplog.at_level(logging.WARNING, logger="palinode.api"):
        resp = client.get("/prompts")

    assert resp.status_code == 200
    assert [prompt["name"] for prompt in resp.json()] == ["healthy"]
    warnings = [
        record for record in caplog.records
        if record.name == "palinode.api" and record.levelno == logging.WARNING
        and broken_path in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "permission denied" in warnings[0].getMessage()


def test_list_prompts_filter_by_task(mock_memory_dir):
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "compaction-v1", task="compaction")
    _write_prompt(prompts_dir, "compaction-v2", task="compaction")
    _write_prompt(prompts_dir, "extraction-v1", task="extraction")

    resp = client.get("/prompts", params={"task": "compaction"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(p["task"] == "compaction" for p in data)


def test_list_prompts_metadata_fields(mock_memory_dir):
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(
        prompts_dir, "compaction-v1",
        task="compaction", model="olmo-3.1:32b", version="1.0",
        active=True, body="Compact these memories.",
    )

    resp = client.get("/prompts")
    assert resp.status_code == 200
    p = resp.json()[0]
    assert p["name"] == "compaction-v1"
    assert p["task"] == "compaction"
    assert p["model"] == "olmo-3.1:32b"
    assert p["version"] == "1.0"
    assert p["active"] is True
    assert "file" in p


# ── GET /prompts/{name} ────────────────────────────────────────────────────────

def test_get_prompt_not_found(mock_memory_dir):
    os.makedirs(os.path.join(mock_memory_dir, "prompts"), exist_ok=True)
    resp = client.get("/prompts/nonexistent")
    assert resp.status_code == 404


def test_get_prompt_by_name(mock_memory_dir):
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", body="The prompt body.")

    resp = client.get("/prompts/compaction-v1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "compaction-v1"
    assert data["content"] == "The prompt body."


def test_get_prompt_by_name_with_md_extension(mock_memory_dir):
    """Requesting with .md suffix should also resolve."""
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "extraction-v2", task="extraction")

    resp = client.get("/prompts/extraction-v2.md")
    assert resp.status_code == 200
    assert resp.json()["name"] == "extraction-v2"


def test_get_prompt_path_traversal_rejected(mock_memory_dir):
    """Path traversal should be rejected (404 since file won't exist outside prompts/)."""
    resp = client.get("/prompts/../../../etc/passwd")
    # Either 404 (no such prompt) or resolved to a safe path
    assert resp.status_code in (404, 403)


# ── POST /prompts/{name}/activate ─────────────────────────────────────────────

def test_activate_prompt_not_found(mock_memory_dir):
    os.makedirs(os.path.join(mock_memory_dir, "prompts"), exist_ok=True)
    resp = client.post("/prompts/missing-prompt/activate")
    assert resp.status_code == 404


def test_activate_prompt_sets_active(mock_memory_dir):
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", active=False)

    resp = client.post("/prompts/compaction-v1/activate")
    assert resp.status_code == 200
    assert resp.json()["activated"] == "compaction-v1"
    assert resp.json()["task"] == "compaction"

    # Verify the file was updated
    get_resp = client.get("/prompts/compaction-v1")
    assert get_resp.status_code == 200
    assert get_resp.json()["active"] is True


def test_activate_prompt_deactivates_others_same_task(mock_memory_dir):
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", active=True)
    _write_prompt(prompts_dir, "compaction-v2", task="compaction", active=False)
    # Different task — should not be touched
    _write_prompt(prompts_dir, "extraction-v1", task="extraction", active=True)

    resp = client.post("/prompts/compaction-v2/activate")
    assert resp.status_code == 200

    # v1 should now be inactive
    v1 = client.get("/prompts/compaction-v1").json()
    assert v1["active"] is False

    # v2 should be active
    v2 = client.get("/prompts/compaction-v2").json()
    assert v2["active"] is True

    # extraction-v1 should be untouched
    ext = client.get("/prompts/extraction-v1").json()
    assert ext["active"] is True


def test_activate_prompt_idempotent(mock_memory_dir):
    """Activating an already-active prompt should succeed cleanly."""
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", active=True)

    resp1 = client.post("/prompts/compaction-v1/activate")
    assert resp1.status_code == 200

    resp2 = client.post("/prompts/compaction-v1/activate")
    assert resp2.status_code == 200

    get_resp = client.get("/prompts/compaction-v1")
    assert get_resp.json()["active"] is True


# ── Integration: /list excludes prompts/ ─────────────────────────────────────

def test_list_memory_excludes_prompts_dir(mock_memory_dir):
    """GET /list should not return files from the prompts/ directory."""

    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(prompts_dir, "compaction-v1", task="compaction")

    # Patch scan so /save doesn't fail during the test
    resp = client.get("/list")
    assert resp.status_code == 200
    files = [item["file"] for item in resp.json()]
    assert not any("prompts/" in f for f in files), f"prompts/ leaked into /list: {files}"


# ── Consolidation exclusion ────────────────────────────────────────────────────

def test_consolidation_skip_dirs_includes_prompts():
    """The runner's skip set must include 'prompts' to prevent compaction of prompt files."""
    from palinode.consolidation.runner import _CONSOLIDATION_SKIP_DIRS
    assert "prompts" in _CONSOLIDATION_SKIP_DIRS


# ── Frontmatter content validation ────────────────────────────────────────────

def test_prompt_frontmatter_is_stored_correctly(mock_memory_dir):
    """Writing a prompt file should persist all frontmatter fields."""
    prompts_dir = os.path.join(mock_memory_dir, "prompts")
    _write_prompt(
        prompts_dir, "update-v1",
        task="update", model="qwen3:30b", version="2.1", active=False,
        body="Update existing facts when new info contradicts them.",
    )

    resp = client.get("/prompts/update-v1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task"] == "update"
    assert data["model"] == "qwen3:30b"
    assert data["version"] == "2.1"
    assert data["active"] is False
    assert "Update existing facts" in data["content"]

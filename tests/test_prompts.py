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
from palinode.prompts import store_prompts_dir

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


# ── Which directory the prompt surface reads ─────────────────────────────────

def test_prompts_dir_is_the_store_prompts_dir(mock_memory_dir):
    """One definition of the prompts directory, shared with everything else.

    ``_prompts_dir`` used to return ``<memory_dir>/prompts``, which nothing in
    palinode writes, so on a real store ``GET /prompts`` listed nothing while
    ``specs/prompts`` held all nine.
    """
    assert session._prompts_dir() == str(store_prompts_dir(mock_memory_dir))
    assert session._prompts_dir().endswith(os.path.join("specs", "prompts"))


def test_list_prompts_ignores_the_legacy_top_level_prompts_dir(mock_memory_dir):
    """A prompt in ``<memory_dir>/prompts`` is not what consolidation reads.

    Listing it would put the store back in the state the bug created: an
    operator activating a version nothing consumes.
    """
    _write_prompt(os.path.join(mock_memory_dir, "prompts"), "compaction-v1")

    resp = client.get("/prompts")
    assert resp.status_code == 200
    assert resp.json() == []
    assert client.get("/prompts/compaction-v1").status_code == 404


def test_list_prompts_after_prompt_sync_lists_every_packaged_prompt(mock_memory_dir):
    """The reported symptom, end to end: sync a real store, then list it.

    Goes through ``prompt sync``'s own plan/apply functions rather than copying
    files by hand, so the test fails if either side of the pair drifts onto a
    different directory again.
    """
    from palinode.cli.prompt import apply_sync_plan, sync_plan
    from palinode.prompts import iter_packaged_prompts, packaged_prompt_names

    store_dir = store_prompts_dir(mock_memory_dir)
    apply_sync_plan(sync_plan(store_dir), store_dir)

    resp = client.get("/prompts")
    assert resp.status_code == 200
    data = resp.json()

    expected = {name[:-3] for name in packaged_prompt_names()}
    assert {prompt["name"] for prompt in data} == expected
    assert all(
        prompt["file"] == os.path.join("specs", "prompts", f"{prompt['name']}.md")
        for prompt in data
    )

    # Every prompt that declares a version reports it — the field `prompt list`
    # prints and the operator uses to tell a synced store from a stale one.
    declared = {}
    for source in iter_packaged_prompts():
        text = source.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        declared[source.stem] = yaml.safe_load(text.split("---")[1])["version"]
    assert declared, "no packaged prompt declares frontmatter — fixture assumption broke"

    reported = {prompt["name"]: prompt["version"] for prompt in data}
    for name, version in declared.items():
        assert str(reported[name]) == str(version)


def test_activate_writes_the_file_the_consolidation_runner_reads(mock_memory_dir):
    """``prompt activate`` must land on the runner's copy, not a parallel one."""
    from palinode.cli.prompt import apply_sync_plan, sync_plan
    from palinode.consolidation.runner import _system_prompt
    from palinode.prompts import resolve_prompt

    store_dir = store_prompts_dir(mock_memory_dir)
    apply_sync_plan(sync_plan(store_dir), store_dir)

    assert client.post("/prompts/compaction/activate").status_code == 200

    resolved, from_store = resolve_prompt("compaction.md", mock_memory_dir)
    assert from_store is True
    assert resolved == store_dir / "compaction.md"
    assert "active: true" in resolved.read_text(encoding="utf-8")

    # And the runner's own resolution reads that same file's body.
    body = _system_prompt("compaction.md")
    assert body
    assert body in resolved.read_text(encoding="utf-8")


# ── GET /prompts ───────────────────────────────────────────────────────────────

def test_list_prompts_empty(mock_memory_dir):
    resp = client.get("/prompts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_prompts_returns_all(mock_memory_dir):
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", active=True)
    _write_prompt(prompts_dir, "extraction-v1", task="extraction", active=False)

    resp = client.get("/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {p["name"] for p in data}
    assert names == {"compaction-v1", "extraction-v1"}


def test_list_prompts_logs_unreadable_file(mock_memory_dir, monkeypatch, caplog):
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
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
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-v1", task="compaction")
    _write_prompt(prompts_dir, "compaction-v2", task="compaction")
    _write_prompt(prompts_dir, "extraction-v1", task="extraction")

    resp = client.get("/prompts", params={"task": "compaction"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(p["task"] == "compaction" for p in data)


def test_list_prompts_metadata_fields(mock_memory_dir):
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
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
    os.makedirs(store_prompts_dir(mock_memory_dir), exist_ok=True)
    resp = client.get("/prompts/nonexistent")
    assert resp.status_code == 404


def test_get_prompt_by_name(mock_memory_dir):
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", body="The prompt body.")

    resp = client.get("/prompts/compaction-v1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "compaction-v1"
    assert data["content"] == "The prompt body."


def test_get_prompt_by_name_with_md_extension(mock_memory_dir):
    """Requesting with .md suffix should also resolve."""
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
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
    os.makedirs(store_prompts_dir(mock_memory_dir), exist_ok=True)
    resp = client.post("/prompts/missing-prompt/activate")
    assert resp.status_code == 404


def test_activate_prompt_sets_active(mock_memory_dir):
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
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
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
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
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-v1", task="compaction", active=True)

    resp1 = client.post("/prompts/compaction-v1/activate")
    assert resp1.status_code == 200

    resp2 = client.post("/prompts/compaction-v1/activate")
    assert resp2.status_code == 200

    get_resp = client.get("/prompts/compaction-v1")
    assert get_resp.json()["active"] is True


def _active_names(prompts_dir: str, task: str) -> list[str]:
    """Read active state straight off disk for every prompt of one task."""
    names = []
    for entry in sorted(os.listdir(prompts_dir)):
        if not entry.endswith(".md"):
            continue
        with open(os.path.join(prompts_dir, entry)) as f:
            fm = yaml.safe_load(f.read().split("---")[1])
        if fm.get("task") == task and fm.get("active") is True:
            names.append(entry)
    return names


@pytest.mark.parametrize("failing", ["compaction-a", "compaction-b", "compaction-c"])
def test_activate_prompt_fails_closed_when_a_sibling_write_fails(
    mock_memory_dir, monkeypatch, failing
):
    """A failed deactivation must not leave two prompts active for one task.

    Parametrised over the first, middle and last sibling, because the loop
    aborts at a different point in each and the surviving state differs.
    """
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-a", task="compaction", active=True)
    _write_prompt(prompts_dir, "compaction-b", task="compaction", active=False)
    _write_prompt(prompts_dir, "compaction-c", task="compaction", active=False)
    _write_prompt(prompts_dir, "compaction-target", task="compaction", active=False)

    real_write = session.git_tools.write_memory_file

    def flaky_write(file_path, text):
        if os.path.basename(file_path) == f"{failing}.md":
            raise OSError("disk went away")
        return real_write(file_path, text)

    monkeypatch.setattr(session.git_tools, "write_memory_file", flaky_write)

    resp = client.post("/prompts/compaction-target/activate")

    assert resp.status_code == 409
    assert f"{failing}.md" in resp.json()["detail"]

    # The invariant: at most one active prompt for the task. Zero is a valid
    # outcome when the previously active sibling was deactivated before the
    # failure, and both states are safe to retry from.
    assert len(_active_names(prompts_dir, "compaction")) <= 1

    # The target is the thing that must not have been activated.
    assert "compaction-target.md" not in _active_names(prompts_dir, "compaction")


def test_activate_prompt_fails_closed_when_the_target_write_fails(
    mock_memory_dir, monkeypatch
):
    """The target is activated last, and its own write failure fails closed too."""
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-a", task="compaction", active=True)
    _write_prompt(prompts_dir, "compaction-target", task="compaction", active=False)

    real_write = session.git_tools.write_memory_file

    def flaky_write(file_path, text):
        if os.path.basename(file_path) == "compaction-target.md":
            raise OSError("disk went away")
        return real_write(file_path, text)

    monkeypatch.setattr(session.git_tools, "write_memory_file", flaky_write)

    resp = client.post("/prompts/compaction-target/activate")

    assert resp.status_code == 409
    assert "compaction-target.md" in resp.json()["detail"]
    assert _active_names(prompts_dir, "compaction") == []


def test_activate_prompt_commits_partial_writes_before_failing(
    mock_memory_dir, monkeypatch
):
    """_set_active writes immediately, so whatever reached disk still gets committed."""
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-a", task="compaction", active=True)
    _write_prompt(prompts_dir, "compaction-b", task="compaction", active=True)
    _write_prompt(prompts_dir, "compaction-target", task="compaction", active=False)

    real_write = session.git_tools.write_memory_file
    committed: list[str] = []

    def flaky_write(file_path, text):
        if os.path.basename(file_path) == "compaction-b.md":
            raise OSError("disk went away")
        return real_write(file_path, text)

    monkeypatch.setattr(session.git_tools, "write_memory_file", flaky_write)
    monkeypatch.setattr(
        session.git_tools,
        "commit_memory_file",
        lambda fp, msg: committed.append(os.path.basename(fp)),
    )
    monkeypatch.setattr(config.git, "auto_commit", True)

    resp = client.post("/prompts/compaction-target/activate")

    assert resp.status_code == 409
    assert committed == ["compaction-a.md"]


def test_activate_prompt_leaves_other_tasks_alone_when_it_fails(
    mock_memory_dir, monkeypatch
):
    """A failure in one task must not touch a prompt belonging to another."""
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
    _write_prompt(prompts_dir, "compaction-a", task="compaction", active=True)
    _write_prompt(prompts_dir, "compaction-target", task="compaction", active=False)
    _write_prompt(prompts_dir, "extraction-v1", task="extraction", active=True)

    real_write = session.git_tools.write_memory_file

    def flaky_write(file_path, text):
        if os.path.basename(file_path) == "compaction-a.md":
            raise OSError("disk went away")
        return real_write(file_path, text)

    monkeypatch.setattr(session.git_tools, "write_memory_file", flaky_write)

    resp = client.post("/prompts/compaction-target/activate")

    assert resp.status_code == 409
    assert _active_names(prompts_dir, "extraction") == ["extraction-v1.md"]


# ── Integration: /list excludes prompts/ ─────────────────────────────────────

def test_list_memory_excludes_prompts_dir(mock_memory_dir):
    """GET /list should not return files from a top-level ``prompts/`` directory.

    Deliberately the legacy location, not ``specs/prompts``: the subject here is
    ``collect_memory_files``' top-level skip set, which names ``prompts``. Stores
    carrying that directory predate the prompts API reading ``specs/prompts``
    and must still not have it browsed as memory.
    """

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
    prompts_dir = str(store_prompts_dir(mock_memory_dir))
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

"""Focused policy tests: persistence, API gates, and secret-negative paths."""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.capture_policy import (
    POLICY_FILENAME,
    evaluate_capture_policy,
    load_capture_policy,
    update_capture_policy,
)
from palinode.core.config import config


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


def test_controls_persist_and_pause_resume_api_capture(client: TestClient, tmp_path: Path) -> None:
    initial = client.get("/controls")
    assert initial.status_code == 200
    assert initial.json()["capture_paused"] is False

    paused = client.post("/controls", json={"capture_paused": True})
    assert paused.status_code == 200
    assert paused.json()["capture_paused"] is True
    # No process cache: this represents a fresh API process loading the store.
    assert load_capture_policy().capture_paused is True

    denied = client.post("/save", json={"content": "ordinary explicit write", "type": "Decision"})
    assert denied.status_code == 403
    assert denied.json() == {"detail": "capture_paused"}

    resumed = client.post("/controls", json={"capture_paused": False})
    assert resumed.status_code == 200
    with patch("palinode.api.routers.memory.save_memory", return_value={"file_path": "decisions/ok.md"}) as saved:
        allowed = client.post("/save", json={"content": "ordinary explicit write", "type": "Decision"})
    assert allowed.status_code == 200
    saved.assert_called_once()
    assert (tmp_path / POLICY_FILENAME).exists()
    secret = "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789"
    invalid_bool = client.post("/controls", json={"capture_paused": "yes", "secret": secret})
    invalid_extra = client.post("/controls/check", json={
        "action": "capture", "automatic": "true", "secret": secret,
    })
    assert invalid_bool.status_code == invalid_extra.status_code == 422
    assert invalid_bool.json() == invalid_extra.json() == {"detail": "Invalid controls request"}
    assert secret not in invalid_bool.text + invalid_extra.text

    # Parallel patch-style updates retain both mutations; a lock spans the
    # read, atomic write, and provenance commit rather than only the writer.
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(update_capture_policy, capture_paused=True)
        second = pool.submit(update_capture_policy, excluded_projects=["racing-project"])
        first.result()
        second.result()
    raced = load_capture_policy()
    assert raced.capture_paused is True
    assert raced.excluded_projects == ("racing-project",)


def test_capture_only_pause_rejects_writes_without_changing_markdown_or_head_and_keeps_recall(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture-only pause is the enforceable recall-only setup, not prose."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Policy Test"], cwd=tmp_path, check=True)
    existing = tmp_path / "decisions" / "existing.md"
    existing.parent.mkdir()
    existing.write_text("---\ntype: Decision\n---\nExisting recall fixture.\n", encoding="utf-8")
    subprocess.run(["git", "add", "decisions/existing.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "existing memory"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setattr(config.git, "auto_commit", True)

    paused = client.post("/controls", json={"capture_paused": True, "recall_paused": False})
    assert paused.status_code == 200
    assert paused.json()["capture_paused"] is True
    assert paused.json()["recall_paused"] is False
    markdown_before = {
        path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*.md")
    }
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout

    def assert_capture_is_unchanged() -> None:
        assert {
            path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*.md")
        } == markdown_before
        assert subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout == head_before

    denied_save = client.post("/save", json={"content": "must not persist", "type": "Decision"})
    assert denied_save.status_code == 403
    assert denied_save.json() == {"detail": "capture_paused"}
    assert_capture_is_unchanged()

    denied_session_end = client.post("/session-end", json={"summary": "must not persist"})
    assert denied_session_end.status_code == 403
    assert denied_session_end.json() == {"detail": "capture_paused"}
    assert_capture_is_unchanged()

    with patch("palinode.api.routers.search.store.list_recent", return_value=[] ) as recalled:
        search = client.post("/search", json={"query": ""})
    assert search.status_code == 200
    assert search.json() == []
    recalled.assert_called_once()


def test_status_and_history_reads_remain_available_when_both_pauses_are_active(
    client: TestClient, tmp_path: Path
) -> None:
    """Pause gates future capture/recall, never the read-only control evidence."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Policy Test"], cwd=tmp_path, check=True)
    memory = tmp_path / "decisions" / "visible.md"
    memory.parent.mkdir()
    memory.write_text("---\ntype: Decision\n---\nVisible history fixture.\n", encoding="utf-8")
    subprocess.run(["git", "add", "decisions/visible.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "visible memory"], cwd=tmp_path, check=True, capture_output=True)

    assert client.post("/controls", json={"capture_paused": True, "recall_paused": True}).status_code == 200

    assert client.get("/status").status_code == 200
    history = client.get("/history/decisions/visible.md")
    assert history.status_code == 200
    assert history.json()["history"]


def test_automatic_secret_session_capture_is_denied_before_files_logs_or_models(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789"
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Policy Test"], cwd=tmp_path, check=True)
    config.git.auto_commit = True
    assert client.post("/controls", json={"excluded_paths": [str(tmp_path)]}).status_code == 200

    with patch("palinode.api.routers.session.git_tools.write_memory_file") as write_file, \
            patch("palinode.core.embedder.embed") as embed:
        denied = client.post("/session-end", json={
            "summary": secret,
            "source": "claude-code-hook",
            "cwd": str(tmp_path),
            "source_path": str(tmp_path / "private-transcript.jsonl"),
        })
        for source in ("pi-extension", "cline-plugin", "openclaw-plugin"):
            assert client.post("/session-end", json={
                "summary": secret, "source": source, "cwd": str(tmp_path),
                "source_path": str(tmp_path / "private-transcript.jsonl"),
            }).status_code == 403

    assert denied.status_code == 403
    assert denied.json() == {"detail": "excluded_path"}
    assert secret not in denied.text
    write_file.assert_not_called()
    embed.assert_not_called()
    assert secret not in caplog.text
    assert all(secret.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    history = subprocess.run(
        ["git", "log", "-p", "--", POLICY_FILENAME], cwd=tmp_path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert secret not in history


def test_automatic_recall_pause_stops_embedding_and_explicit_recall_cannot_bypass(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert client.post("/controls", json={"recall_paused": True}).status_code == 200
    with patch("palinode.core.embedder.embed") as embed:
        automatic = client.post("/search", json={
            "query": "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789",
            "mode": "passive", "automatic": True, "cwd": str(tmp_path),
        })
        explicit = client.post("/search", json={"query": "anything"})
    assert automatic.status_code == 403
    assert explicit.status_code == 403
    assert "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789" not in automatic.text
    assert "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789" not in caplog.text
    embed.assert_not_called()
    # The pause middleware covers the session-init alias and every advanced
    # recall route before request parsing can reach an embedding/model seam.
    advanced = {
        "/search-associative": {"query": "secret-like query"},
        "/dedup-suggest": {"content": "secret-like query"},
        "/orphan-repair": {"broken_link": "secret-like query"},
        "/cluster-neighbors": {"file_path": "decisions/secret-like.md"},
        "/topic-coverage": {"query": "secret-like query"},
        "/context/prime": {"cwd": str(tmp_path)},
    }
    for path, body in advanced.items():
        assert client.post(path, json=body).status_code == 403


def test_project_path_and_linked_worktree_preflight_scope(client: TestClient, tmp_path: Path) -> None:
    repo = tmp_path / "policy-project"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Policy Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("fixture", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
    linked = tmp_path / "linked-worktree"
    subprocess.run(["git", "worktree", "add", "-b", "policy-linked", str(linked)], cwd=repo, check=True, capture_output=True)

    assert client.post("/controls", json={
        "excluded_projects": ["policy-project"],
        "excluded_paths": [str(repo)],
    }).status_code == 200
    project_denied = client.post("/controls/check", json={
        "action": "capture", "automatic": True, "cwd": str(linked),
    }).json()
    assert project_denied["allowed"] is False
    assert project_denied["reason"] == "excluded_project"
    assert project_denied["project"] == "policy-project"
    session_init_denied = client.post("/context/prime", json={
        "automatic": True, "cwd": str(linked),
    })
    assert session_init_denied.status_code == 403
    assert session_init_denied.json() == {"detail": "excluded_project"}

    # A direct path exclusion applies below the excluded root, while a symlink
    # is invalid rather than being resolved into a potentially surprising scope.
    nested = repo / "private" / "transcript.jsonl"
    nested.parent.mkdir()
    nested.write_text("fixture", encoding="utf-8")
    path_denied = evaluate_capture_policy(
        "capture", automatic=True, source_path=str(nested)
    )
    assert path_denied.reason == "excluded_path"
    link = tmp_path / "malicious-link"
    link.symlink_to(nested)
    invalid = client.post("/controls/check", json={
        "action": "capture", "automatic": True, "source_path": str(link),
    }).json()
    assert invalid == {"allowed": False, "reason": "invalid_scope", "policy_version": 1, "project": None}
    # A caller with no usable project/path cannot bypass configured automatic
    # exclusions, and a resolver failure is equally unknown rather than allow.
    unknown = client.post("/controls/check", json={"action": "capture", "automatic": True}).json()
    assert unknown["reason"] == "unknown_scope"
    with patch("palinode.core.context_prime.resolve_context", side_effect=RuntimeError("offline")):
        unresolved = evaluate_capture_policy("capture", automatic=True, cwd=str(linked))
    assert unresolved.reason == "unknown_scope"


@pytest.fixture()
def unreadable_transcript(tmp_path: Path):
    """A transcript path under a directory this process cannot search (EACCES).

    The shape of a hook's ``source_path`` when the API runs as another user:
    the client's transcript lives under ``/root/.claude`` or a 0700 home.
    """
    import os

    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    locked = tmp_path / "client-home"
    (locked / ".claude" / "projects").mkdir(parents=True)
    path = locked / ".claude" / "projects" / "session.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    locked.chmod(0)
    yield str(path)
    locked.chmod(0o700)


def test_unverifiable_source_path_does_not_refuse_capture_without_exclusions(
    client: TestClient, tmp_path: Path, unreadable_transcript: str
) -> None:
    cwd = tmp_path / "workspace-a1b2c3"
    cwd.mkdir()
    check = client.post("/controls/check", json={
        "action": "capture", "automatic": True,
        "cwd": str(cwd), "source_path": unreadable_transcript,
    }).json()
    assert check["allowed"] is True and check["reason"] is None
    # The readable cwd still scopes the capture.
    assert check["project"] == evaluate_capture_policy(
        "capture", automatic=True, cwd=str(cwd)).project

    written = client.post("/session-end", json={
        "summary": "Auto-captured (other, 7 messages). Topic: start the migration\n"
                   "Ending: use the assigned batch cedar-1111aaaa next session.",
        "project": "workspace-a1b2c3", "source": "claude-code-hook",
        "trigger": "session-end-hook", "automatic": True,
        "cwd": str(cwd), "source_path": unreadable_transcript,
        "decisions": [], "blockers": [],
    })
    assert written.status_code == 200
    daily = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "daily").glob("*.md"))
    assert "cedar-1111aaaa" in daily


def test_unverifiable_path_still_fails_closed_when_exclusions_exist(
    client: TestClient, tmp_path: Path, unreadable_transcript: str
) -> None:
    cwd = tmp_path / "workspace-a1b2c3"
    cwd.mkdir()
    for exclusions in ({"excluded_paths": [str(tmp_path / "private")]},
                       {"excluded_projects": ["some-other-project"]}):
        assert client.post("/controls", json={"excluded_paths": [], "excluded_projects": [],
                                              **exclusions}).status_code == 200
        check = client.post("/controls/check", json={
            "action": "capture", "automatic": True,
            "cwd": str(cwd), "source_path": unreadable_transcript,
        }).json()
        assert check == {"allowed": False, "reason": "invalid_scope", "policy_version": 1, "project": None}


def test_unverifiable_cwd_is_dropped_not_resolved_through(
    client: TestClient, tmp_path: Path
) -> None:
    real = tmp_path / "real-project"
    real.mkdir()
    link = tmp_path / "linked-cwd"
    link.symlink_to(real)
    decision = evaluate_capture_policy("capture", automatic=True, cwd=str(link))
    assert decision.allowed is True
    assert decision.project is None  # never scoped through the symlink


def test_path_exclusions_apply_to_automatic_capture_and_recall(
    client: TestClient, tmp_path: Path
) -> None:
    excluded = tmp_path / "private" / "source.md"
    excluded.parent.mkdir()
    excluded.write_text("fixture", encoding="utf-8")
    assert client.post("/controls", json={"excluded_paths": [str(excluded.parent)]}).status_code == 200

    for action in ("capture", "recall"):
        automatic = evaluate_capture_policy(action, automatic=True, source_path=str(excluded))
        explicit = evaluate_capture_policy(action, automatic=False, source_path=str(excluded))
        assert automatic.reason == "excluded_path"
        assert explicit.allowed is True


def test_malformed_persisted_policy_fails_closed(
    client: TestClient, tmp_path: Path
) -> None:
    external = tmp_path / "external-policy.yaml"
    policy_path = tmp_path / POLICY_FILENAME
    real_open = __import__("os").open

    for dangling in (False, True):
        if policy_path.exists() or policy_path.is_symlink():
            policy_path.unlink()
        if external.exists():
            external.unlink()
        if not dangling:
            external.write_text("external: never read or changed", encoding="utf-8")
        policy_path.symlink_to(external)

        def guarded_open(path, *args, **kwargs):
            assert Path(path) != external
            return real_open(path, *args, **kwargs)

        with patch("palinode.core.capture_policy.os.open", side_effect=guarded_open), \
                patch("palinode.core.capture_policy.git_tools.write_memory_file") as write_file:
            assert client.get("/controls").status_code == 503
            denied = client.post("/controls/check", json={"action": "capture", "automatic": True})
            updated = client.post("/controls", json={"capture_paused": True})
        assert denied.json()["reason"] == "invalid_policy"
        assert updated.status_code == 400
        write_file.assert_not_called()
        if not dangling:
            assert external.read_text(encoding="utf-8") == "external: never read or changed"

    policy_path.unlink()
    (tmp_path / POLICY_FILENAME).write_text("not: [valid", encoding="utf-8")
    automatic = client.post("/controls/check", json={"action": "capture", "automatic": True})
    assert automatic.json()["allowed"] is False
    assert automatic.json()["reason"] == "invalid_policy"


def test_resolved_processing_policy_refreshes_between_passes(tmp_path, monkeypatch):
    from palinode.core.capture_policy import (
        automatic_note_processing_allowed as allowed,
        resolve_processing_policy,
    )

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    aliases = tmp_path / "entity-aliases.yaml"
    aliases.write_text("aliases:\n  project/private:\n    - project/nickname\n")
    update_capture_policy(excluded_projects=["private"])
    first = resolve_processing_policy()
    mixed = {"entities": ["project/public", "project/NICKNAME"]}
    assert not allowed(mixed, resolved=first)
    assert not allowed({}, project_refs=["PROJECT/NICKNAME"], resolved=first)
    assert allowed({}, resolved=first)
    aliases.write_text("aliases:\n  project/public:\n    - project/nickname\n    - project/another\n")
    assert allowed(mixed, resolved=resolve_processing_policy())
    assert not allowed(mixed, resolved=first)
    assert allowed(mixed)
    update_capture_policy(excluded_projects=["public"])
    assert not allowed(mixed)
    assert not allowed(mixed, resolved=resolve_processing_policy())
    update_capture_policy(excluded_projects=[])
    assert allowed(mixed)


def test_processing_policy_denial_and_recovery(tmp_path, monkeypatch):
    from palinode.core.capture_policy import (
        automatic_note_processing_allowed as allowed,
        resolve_processing_policy,
    )

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    path = tmp_path / POLICY_FILENAME
    path.write_text("invalid: policy\n")
    denied = resolve_processing_policy()
    assert not allowed({}, resolved=denied)
    assert not allowed({})
    path.unlink()
    assert allowed({})
    assert not allowed({}, resolved=denied)
    with patch("palinode.core.capture_policy.os.open", side_effect=PermissionError):
        path.write_text("invalid: policy\n")
        assert not allowed({})
    path.unlink()
    assert allowed({})


def test_processing_policy_without_exclusions_does_not_load_aliases(tmp_path, monkeypatch):
    from palinode.core.capture_policy import (
        automatic_note_processing_allowed as allowed,
        resolve_processing_policy,
    )

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    with patch("palinode.core.scope.alias_index") as index:
        resolved = resolve_processing_policy()
        assert allowed({"entities": ["project/public"]}, resolved=resolved)
        assert allowed({}, project_refs=["project/private"], resolved=resolved)
        index.assert_not_called()


def test_unverifiable_path_fails_closed_when_is_symlink_swallows_errors(
    client: TestClient, tmp_path: Path, unreadable_transcript: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 3.14's ``Path.is_symlink()`` returns False on EACCES; CI runs older Pythons."""
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    cwd = tmp_path / "workspace-a1b2c3"
    cwd.mkdir()
    assert client.post("/controls", json={"excluded_paths": [str(tmp_path / "private")],
                                          "excluded_projects": []}).status_code == 200
    check = client.post("/controls/check", json={
        "action": "capture", "automatic": True,
        "cwd": str(cwd), "source_path": unreadable_transcript,
    }).json()
    assert check == {"allowed": False, "reason": "invalid_scope", "policy_version": 1, "project": None}

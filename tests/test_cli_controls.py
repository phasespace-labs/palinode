"""Contract-shaped tests for the API-backed ``palinode controls`` surface."""
from __future__ import annotations

import json
import importlib
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli._api import PalinodeAPI
from palinode.cli.controls import _observed_project_setup, _redact_remote_line, redact_destination
from palinode.core import disclosure


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch) -> Path:
    """``controls status`` reads user-level Claude settings; never the real ones."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    return home


def test_controls_api_methods_use_the_published_routes() -> None:
    requests: list[httpx.Request] = []
    public = {
        "capture_paused": False,
        "recall_paused": False,
        "excluded_projects": [],
        "excluded_paths": [],
        "policy_version": 1,
        "updated_at": None,
        "provenance": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=public)
        if request.url.path == "/controls/check":
            return httpx.Response(200, json={"allowed": False, "reason": "capture_paused", "policy_version": 1, "project": None})
        return httpx.Response(200, json={**public, "capture_paused": True, "committed": True})

    api = PalinodeAPI(client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test"))
    assert api.get_controls() == public
    assert api.set_controls(capture_paused=True)["committed"] is True
    assert api.check_controls("capture", cwd="/project", automatic=True)["allowed"] is False
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/controls"),
        ("POST", "/controls"),
        ("POST", "/controls/check"),
    ]
    assert json.loads(requests[1].content) == {"capture_paused": True}
    assert json.loads(requests[2].content) == {
        "action": "capture",
        "automatic": True,
        "cwd": "/project",
    }


def test_redact_destination_masks_http_ssh_and_query_credentials() -> None:
    assert redact_destination("https://token:secret@example.test/v1?api_key=leak#fragment") == "https://***@example.test/v1?***"
    assert redact_destination("ssh://git:secret@example.test:2222/group/repo.git?private=1") == "ssh://***@example.test:2222/group/repo.git?***"
    assert redact_destination("git@github.com:owner/repo.git") == "git@github.com:owner/repo.git"
    assert redact_destination("alice:secret@example.test:owner/repo.git?token=leak") == "***@example.test:owner/repo.git?***"
    assert _redact_remote_line("origin\thttps://token:secret@example.test/store.git?key=leak (fetch)") == (
        "origin https://***@example.test/store.git?*** (fetch)"
    )
    assert redact_destination("https://user:secret@[2001:db8::1]:9443/embed?api_key=leak") == (
        "https://***@[2001:db8::1]:9443/embed?***"
    )
    assert redact_destination("https://user:secret@example.test:not-a-port/embed?key=leak") == (
        "https://***@example.test/embed?***"
    )
    assert redact_destination("ssh://git:secret@[2001:db8::2]/store.git?private=1") == (
        "ssh://***@[2001:db8::2]/store.git?***"
    )
    assert redact_destination("https://user:secret@") == "https://***"
    malformed = redact_destination("https://user:secret@[invalid-v6?api_key=leak")
    assert malformed == "https://***@[invalid-v6?***"
    assert "secret" not in malformed
    assert "api_key=leak" not in malformed
    assert redact_destination("https://[invalid-v6?api_key=leak") == "invalid destination"


def test_observed_setup_does_not_claim_a_client_is_running(tmp_path: Path) -> None:
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "palinode-session-start.sh").write_text("#!/bin/sh", encoding="utf-8")
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": "palinode-session-start.sh"}]}]}}),
        encoding="utf-8",
    )
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"palinode": {}}}), encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Use Palinode through MCP.", encoding="utf-8")

    observed = _observed_project_setup(tmp_path, home=tmp_path / "home")

    assert observed["claude_hooks"] == [
        {"event": "SessionStart", "scope": "project", "settings_registration_observed": True, "script_observed": True}
    ]
    assert observed["mcp_configured"] is True
    assert observed["codex_instructions"] is True
    assert observed["running_client_state"].startswith("unknown")


def test_observed_setup_ignores_malformed_hook_settings_shape(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": {"hooks": None}, "SessionEnd": [None, {"hooks": None}]}}),
        encoding="utf-8",
    )

    observed = _observed_project_setup(tmp_path, home=tmp_path / "home")

    assert observed["claude_hooks"] == []


class _ControlsAPI:
    def __init__(self) -> None:
        self.state = {
            "capture_paused": False,
            "recall_paused": False,
            "excluded_projects": [],
            "excluded_paths": [],
            "policy_version": "test-v1",
            "updated_at": "2026-09-15T00:00:00Z",
        }

    def get_controls(self) -> dict:
        return dict(self.state)

    def set_controls(self, **changes: object) -> dict:
        self.state.update({key: value for key, value in changes.items() if value is not None})
        return dict(self.state)

    def get_status(self) -> dict:
        return {
            "memory_dir": "/fixture/store",
            "embedding_url": "https://key@example.test/embed?secret=x",
            "auto_summary_enabled": True,
            "auto_summary_primary_url": "https://summary:key@[invalid-v6?secret=x",
            "auto_summary_llm_fallbacks": ["https://summary-fallback:key@example.test/v1?secret=x"],
            "consolidation_enabled": False,
            "consolidation_url": "https://consolidation:key@example.test/v1?secret=x",
            "consolidation_llm_fallbacks": ["https://consolidation-fallback:key@example.test/v1?secret=x"],
            "transcriptor_url": "https://transcriptor:key@example.test/v1?secret=x",
            "git_remotes": ["origin https://git:key@example.test/store.git?secret=x (fetch)"],
        }

    def check_controls(self, *args: object, **kwargs: object) -> dict:
        return {"allowed": True, "policy_version": "test-v1", "project": "fixture-project"}


def test_cli_pause_resume_and_bounded_exclusions_use_control_api(monkeypatch) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")

    api = _ControlsAPI()
    monkeypatch.setattr(controls_module, "api_client", api)
    runner = CliRunner()

    paused = runner.invoke(main, ["controls", "pause", "--format", "json"])
    assert paused.exit_code == 0, paused.output
    assert json.loads(paused.output)["capture_paused"] is True
    assert api.state["recall_paused"] is True

    excluded = runner.invoke(main, ["controls", "exclude-project", "fixture-project", "--format", "json"])
    assert excluded.exit_code == 0, excluded.output
    assert json.loads(excluded.output)["excluded_projects"] == ["fixture-project"]

    resumed = runner.invoke(main, ["controls", "resume", "--no-recall", "--format", "json"])
    assert resumed.exit_code == 0, resumed.output
    assert api.state["capture_paused"] is False
    assert api.state["recall_paused"] is True


def test_cli_status_discloses_fixture_state_without_leaking_destination_secret(monkeypatch, tmp_path: Path) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")
    monkeypatch.setattr(controls_module, "api_client", _ControlsAPI())
    result = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["effective_project"] == "fixture-project"
    assert payload["controls"]["policy_version"] == "test-v1"
    assert payload["destinations"]["embedding"] == "https://***@example.test/embed?***"
    assert payload["destinations"]["auto_summary"] == {
        "enabled": True,
        "primary": "https://***@[invalid-v6?***",
        "llm_fallbacks": ["https://***@example.test/v1?***"],
    }
    assert payload["destinations"]["consolidation"]["enabled"] is False
    assert payload["destinations"]["consolidation"]["llm_fallbacks"] == ["https://***@example.test/v1?***"]
    assert payload["destinations"]["transcriptor"] == "https://***@example.test/v1?***"
    assert payload["destinations"]["git_remotes"] == ["origin https://***@example.test/store.git?*** (fetch)"]
    assert "key@example" not in result.output
    assert "secret=x" not in result.output
    assert payload["observed_setup"]["running_client_state"].startswith("unknown")


def test_human_status_includes_destinations_opt_in_and_scope(monkeypatch, tmp_path: Path) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")
    monkeypatch.setattr(controls_module, "api_client", _ControlsAPI())

    result = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "text"])

    assert result.exit_code == 0, result.output
    rendered = " ".join(result.output.split())
    assert "Destinations: API" in result.output
    assert "Transcript capture: opt-in" in result.output
    assert "Auto-summary: enabled; primary https://***@[invalid-v6?***; fallbacks https://***@example.test/v1?***" in rendered
    assert "Consolidation: disabled; primary https://***@example.test/v1?***; fallbacks https://***@example.test/v1?***" in rendered
    assert "transcriptor https://***@example.test/v1?***" in result.output
    assert "origin https://***@example.test/store.git?*** (fetch)" in result.output
    assert "Scope: pauses affect future API-routed capture and recall requests only" in rendered
    assert "do not delete stored history" in rendered
    assert "exclusions: project exclusions apply to automatic capture and recall" in rendered
    assert "key@example" not in result.output
    assert "secret=x" not in result.output


def test_human_status_marks_absent_enabled_flags_unknown(monkeypatch, tmp_path: Path) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")
    api = _ControlsAPI()
    original_status = api.get_status

    def old_status() -> dict:
        status = original_status()
        status.pop("auto_summary_enabled")
        status.pop("consolidation_enabled")
        return status

    monkeypatch.setattr(api, "get_status", old_status)
    monkeypatch.setattr(controls_module, "api_client", api)

    result = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "text"])

    assert result.exit_code == 0, result.output
    rendered = " ".join(result.output.split())
    assert "Auto-summary: unknown" in rendered
    assert "Consolidation: unknown" in rendered


def test_status_failure_does_not_echo_endpoint_credentials(monkeypatch, tmp_path: Path) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")

    class _FailingAPI:
        def get_controls(self) -> dict:
            raise RuntimeError("GET https://user:token@example.test/controls?api_key=leak failed")

    monkeypatch.setattr(controls_module, "api_client", _FailingAPI())
    result = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path)])

    assert result.exit_code != 0
    assert "Unable to read controls from the API" in result.output
    assert "token" not in result.output
    assert "api_key" not in result.output


def test_runtime_disclosure_uses_server_configured_store_and_redacts_remotes(monkeypatch, tmp_path: Path) -> None:
    from palinode.core.config import config

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.embeddings.primary, "url", "https://embed:token@[2001:db8::3]/v1?secret=x")
    monkeypatch.setattr(config.consolidation, "llm_url", "https://llm:token@example.test/v1?secret=x")
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(config.auto_summary, "ollama_url", "https://summary:token@example.test/v1?secret=x")
    monkeypatch.setattr(config.auto_summary, "llm_fallbacks", [
        {"model": "backup", "url": "https://summary-fallback:token@example.test/v1?secret=x"},
        {"model": "missing-url"},
        "not-a-config-dict",
    ])
    monkeypatch.setattr(config.consolidation, "enabled", False)
    monkeypatch.setattr(config.consolidation, "llm_fallbacks", [
        {"model": "backup", "url": "https://consolidation-fallback:token@example.test/v1?secret=x"},
    ])
    monkeypatch.setattr(config.ingestion.transcriptor, "url", "https://transcriptor:token@example.test/v1?secret=x")
    monkeypatch.setattr(config.git, "auto_push", True)

    def fake_run(argv: list[str], **kwargs: object) -> object:
        assert argv[:3] == ["git", "-C", str(tmp_path)]
        return type("Result", (), {
            "returncode": 0,
            "stdout": "origin\tssh://git:token@[2001:db8::4]/store.git?private=1 (fetch)\n",
        })()

    monkeypatch.setattr(disclosure.subprocess, "run", fake_run)
    payload = disclosure.runtime_disclosure()

    rendered = json.dumps(payload)
    assert payload["memory_dir"] == str(tmp_path)
    assert payload["git_push_policy"].startswith("automatic")
    assert payload["auto_summary_enabled"] is False
    assert payload["auto_summary_primary_url"] == "https://***@example.test/v1?***"
    assert payload["auto_summary_llm_fallbacks"] == ["https://***@example.test/v1?***"]
    assert payload["consolidation_enabled"] is False
    assert payload["consolidation_llm_fallbacks"] == ["https://***@example.test/v1?***"]
    assert payload["transcriptor_url"] == "https://***@example.test/v1?***"
    assert "token" not in rendered
    assert "secret=x" not in rendered
    assert "private=1" not in rendered


# ── Which Claude Code hooks the disclosure sees, and what they send ─────────


def _register(settings: Path, commands: dict[str, str]) -> None:
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    event: [{"hooks": [{"type": "command", "command": command}]}]
                    for event, command in commands.items()
                }
            }
        ),
        encoding="utf-8",
    )


def _install(hooks_dir: Path, *names: str) -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (hooks_dir / name).write_text("#!/bin/sh", encoding="utf-8")


def test_user_level_hooks_are_observed(tmp_path: Path) -> None:
    """The documented floor setup lives in ~/.claude, not the project."""
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    _register(
        home / ".claude" / "settings.json",
        {
            "SessionEnd": "~/.claude/hooks/palinode-session-end.sh",
            "PostCompact": "~/.claude/hooks/palinode-post-compact.sh",
            "Stop": "~/.claude/hooks/another-tool.sh",
        },
    )
    _install(home / ".claude" / "hooks", "palinode-session-end.sh", "palinode-post-compact.sh")

    observed = _observed_project_setup(project, home=home)

    assert observed["claude_hooks"] == [
        {"event": "SessionEnd", "scope": "user", "settings_registration_observed": True, "script_observed": True},
        {"event": "PostCompact", "scope": "user", "settings_registration_observed": True, "script_observed": True},
    ]


def test_a_local_settings_registration_is_observed_and_its_script_reported_once(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _register(project / ".claude" / "settings.local.json", {"SessionEnd": "palinode-session-end.sh"})
    _install(project / ".claude" / "hooks", "palinode-session-end.sh")

    observed = _observed_project_setup(project, home=tmp_path / "home")

    assert observed["claude_hooks"] == [
        {"event": "SessionEnd", "scope": "project", "settings_registration_observed": False, "script_observed": True},
        {"event": "SessionEnd", "scope": "project_local", "settings_registration_observed": True, "script_observed": True},
    ]


def test_run_from_home_each_hook_is_reported_once(tmp_path: Path) -> None:
    _register(tmp_path / ".claude" / "settings.json", {"SessionEnd": "palinode-session-end.sh"})
    _install(tmp_path / ".claude" / "hooks", "palinode-session-end.sh")

    observed = _observed_project_setup(tmp_path, home=tmp_path)

    assert [(hook["event"], hook["scope"]) for hook in observed["claude_hooks"]] == [("SessionEnd", "user")]


def test_every_hook_init_installs_is_observed(tmp_path: Path) -> None:
    from palinode.cli.init import _HOOK_EVENTS

    home = tmp_path / "home"
    _register(home / ".claude" / "settings.json", {event: script for event, script in _HOOK_EVENTS})
    _install(home / ".claude" / "hooks", *(script for _event, script in _HOOK_EVENTS))

    observed = _observed_project_setup(tmp_path, home=home)

    assert [hook["event"] for hook in observed["claude_hooks"]] == [event for event, _script in _HOOK_EVENTS]


def test_status_discloses_what_the_compaction_checkpoint_sends(monkeypatch, tmp_path: Path) -> None:
    """The cap quoted to the user is the one the installed hook applies."""
    import re

    from palinode.cli.init import POST_COMPACT_HOOK_SCRIPT

    controls_module = importlib.import_module("palinode.cli.controls")
    monkeypatch.setattr(controls_module, "api_client", _ControlsAPI())
    cap = re.search(r"PALINODE_HOOK_COMPACT_MAX_CHARS:-(\d+)", POST_COMPACT_HOOK_SCRIPT).group(1)

    result = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    sent = json.loads(result.output)["transcript_capture"]["observed_range"]
    assert "SessionEnd hook sends a message count and a 200-character first-prompt topic hint" in sent
    assert f"PostCompact hook sends the summary Claude Code writes when it compacts a session, up to {cap} characters" in sent


def test_human_status_lists_observed_hooks_and_what_capture_sends(
    monkeypatch, tmp_path: Path, isolated_home: Path
) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")
    monkeypatch.setattr(controls_module, "api_client", _ControlsAPI())
    _register(
        isolated_home / ".claude" / "settings.json",
        {"SessionEnd": "~/.claude/hooks/palinode-session-end.sh", "PostCompact": "~/.claude/hooks/palinode-post-compact.sh"},
    )
    _install(isolated_home / ".claude" / "hooks", "palinode-session-end.sh", "palinode-user-prompt-submit.sh")

    result = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "text"])

    assert result.exit_code == 0, result.output
    rendered = " ".join(result.output.split())
    assert (
        "Claude Code hooks observed: SessionEnd (user), UserPromptSubmit (user, not registered), "
        "PostCompact (user, script not found)"
    ) in rendered
    assert "Automatic capture sends: the SessionEnd hook sends a message count" in rendered
    assert "the PostCompact hook sends the summary Claude Code writes" in rendered


def test_human_status_says_when_no_hooks_are_observed(monkeypatch, tmp_path: Path) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")
    monkeypatch.setattr(controls_module, "api_client", _ControlsAPI())

    result = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "text"])

    assert result.exit_code == 0, result.output
    assert "Claude Code hooks observed: none in user or project settings" in " ".join(result.output.split())


def _scaffold(project: Path, *flags: str) -> None:
    from palinode.cli.init import init

    result = CliRunner().invoke(init, [
        "--dir", str(project), *flags, "--no-mcp", "--no-claudemd",
        "--no-slash", "--no-prompts", "--no-skill",
    ])
    assert result.exit_code == 0, result.output


def test_every_codex_cursor_and_agy_hook_init_installs_is_observed(tmp_path: Path) -> None:
    """What ``palinode init`` registers is what status discloses."""
    from palinode.cli.init import _ANTIGRAVITY_HOOK_EVENTS, _CODEX_HOOK_EVENTS, _CURSOR_HOOK_EVENTS

    project = tmp_path / "project"
    project.mkdir()
    _scaffold(project, "--codex-hook", "--cursor-hooks", "--agy-hooks")

    observed = _observed_project_setup(project, home=tmp_path / "home")

    for key, pairs in (
        ("codex_hooks", _CODEX_HOOK_EVENTS),
        ("cursor_hooks", _CURSOR_HOOK_EVENTS),
        ("agy_hooks", _ANTIGRAVITY_HOOK_EVENTS),
    ):
        assert observed[key] == [
            {"event": event, "scope": "project", "settings_registration_observed": True, "script_observed": True}
            for event, _script in pairs
        ], key
    assert "SessionEnd" in [hook["event"] for hook in observed["codex_hooks"]]


def test_unregistered_or_absent_recall_hooks_are_reported_as_such(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _install(project / ".cursor" / "hooks", "palinode-session-start.sh")
    (project / ".agents").mkdir(parents=True)
    (project / ".agents" / "hooks.json").write_text(json.dumps({"other": {"SessionStart": [
        {"type": "command", "command": "not-ours.sh"}]}}), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {
        "beforeSubmitPrompt": [{"command": "~/.cursor/hooks/palinode-user-prompt-submit.sh"}]}}), encoding="utf-8")

    observed = _observed_project_setup(project, home=home)

    assert observed["cursor_hooks"] == [
        {"event": "beforeSubmitPrompt", "scope": "user", "settings_registration_observed": True, "script_observed": False},
        {"event": "sessionStart", "scope": "project", "settings_registration_observed": False, "script_observed": True},
    ]
    assert observed["agy_hooks"] == []


def test_status_names_codex_capture_and_recall_only_hooks(monkeypatch, tmp_path: Path, isolated_home: Path) -> None:
    controls_module = importlib.import_module("palinode.cli.controls")
    monkeypatch.setattr(controls_module, "api_client", _ControlsAPI())
    project = tmp_path / "project"
    project.mkdir()
    _scaffold(project, "--codex-hook", "--cursor-hooks")

    text = CliRunner().invoke(main, ["controls", "status", "--cwd", str(project), "--format", "text"])
    data = CliRunner().invoke(main, ["controls", "status", "--cwd", str(project), "--format", "json"])

    assert text.exit_code == 0 and data.exit_code == 0, text.output + data.output
    rendered = " ".join(text.output.split())
    assert "Codex hooks observed: SessionStart (project), UserPromptSubmit (project), SessionEnd (project)" in rendered
    assert "Cursor hooks observed (recall only): sessionStart (project), beforeSubmitPrompt (project)" in rendered
    assert "Antigravity hooks observed (recall only): none in project .agents/hooks.json" in rendered
    source = json.loads(data.output)["transcript_capture"]["observed_source"]
    assert "the Codex SessionEnd hook" in source
    assert "Cursor and Antigravity hooks are recall only" in source


def test_status_states_the_servers_unverifiable_path_rule(monkeypatch, tmp_path: Path) -> None:
    """The server's drop-the-path rule comes from the API, or is marked unreported."""
    controls_module = importlib.import_module("palinode.cli.controls")
    api = _ControlsAPI()
    monkeypatch.setattr(controls_module, "api_client", api)
    old = api.get_status()

    unreported = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "text"])
    assert unreported.exit_code == 0, unreported.output
    assert (
        f"Unverifiable paths: {disclosure.UNVERIFIABLE_PATH_POLICY_UNREPORTED}"
        in " ".join(unreported.output.split())
    )

    monkeypatch.setattr(api, "get_status", lambda: {**old, "unverifiable_path_policy": disclosure.UNVERIFIABLE_PATH_POLICY})
    reported = CliRunner().invoke(main, ["controls", "status", "--cwd", str(tmp_path), "--format", "json"])
    assert reported.exit_code == 0, reported.output
    assert json.loads(reported.output)["limits"]["unverifiable_paths"] == disclosure.UNVERIFIABLE_PATH_POLICY


def test_runtime_disclosure_reports_the_unverifiable_path_rule() -> None:
    rule = disclosure.runtime_disclosure()["unverifiable_path_policy"]
    assert "dropped" in rule and "invalid_scope" in rule and "symlink" in rule

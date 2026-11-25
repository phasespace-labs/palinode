"""Drift in installed hooks: current, stale or edited, and missing registrations (ADR-023).

* A script copy matching a shipped release is stale and refreshed; one
  matching no release is someone's edit, warned about, and replaced only
  with ``--force`` (§3).
* A registration's fields follow the same rule: fields an earlier release
  wrote are repaired, fields no release wrote are the user's and are kept,
  with a note, unless ``--force`` (§5).
* A missing registration whose script was never installed, in a file
  ``palinode init`` writes, is added with its script. One whose script is
  still there was removed, and a hand-assembled file is never changed
  unasked: both are noted, and ``--force`` adds them (§6).

Real temp directories throughout.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli.hooks import hooks
from palinode.cli.init import init
from palinode.diagnostics.checks.hooks_current import hooks_current
from palinode.diagnostics.types import DoctorContext
from palinode.hooks import packaged_scripts
from palinode.hooks.codex import TRUST_NOTE
from palinode.hooks.installed import STATE_EDITED, STATE_STALE, hook_statuses
from tests._hook_helpers import ship

START = "palinode-session-start.sh"
END = "palinode-session-end.sh"
PROMPT = "palinode-user-prompt-submit.sh"
POST = "palinode-post-compact.sh"
OLD = "#!/bin/bash\n# palinode-session-end.sh, as an older release shipped it\nexit 0\n"
EDITED = "#!/bin/bash\n# palinode-session-end.sh, with my local tweak\nexit 0\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.chdir(project)
    return home, project.resolve()


def _init(project: Path, *flags: str) -> None:
    res = CliRunner().invoke(init, [
        "--dir", str(project), *flags, "--no-mcp", "--no-claudemd", "--no-slash", "--no-prompts",
        "--no-skill",
    ])
    assert res.exit_code == 0, res.output


def _update(*args: str) -> dict:
    out = CliRunner().invoke(hooks, ["update", "--format", "json", *args])
    assert out.exit_code == 0, out.output
    return json.loads(out.output)


def _doctor():
    return hooks_current(DoctorContext(config=None))


def _settings(project: Path) -> Path:
    return project / ".claude" / "settings.json"


def _handlers(path: Path, event: str) -> list[dict]:
    data = json.loads(path.read_text())
    return [h for group in data["hooks"].get(event, []) for h in group["hooks"]]


def _drop(path: Path, event: str) -> None:
    data = json.loads(path.read_text())
    del data["hooks"][event]
    path.write_text(json.dumps(data, indent=2))


# -- scripts (§3) ----------------------------------------------------------------


def test_a_copy_an_older_release_shipped_is_refreshed(env, monkeypatch):
    _, project = env
    _init(project, "--hook")
    ship(monkeypatch, END, OLD)
    (project / ".claude" / "hooks" / END).write_text(OLD)

    assert not _doctor().passed
    entry = next(h for h in _update()["hooks"] if h["name"] == END)
    assert entry["state"] == STATE_STALE and entry["action"] == "updated" and entry["backup"]
    assert (project / ".claude" / "hooks" / END).read_text() == packaged_scripts()[END]
    assert _doctor().passed


def test_an_edited_copy_is_warned_about_and_replaced_only_with_force(env):
    _, project = env
    _init(project, "--hook")
    script = project / ".claude" / "hooks" / END
    script.write_text(EDITED)

    result = _doctor()
    assert not result.passed and "edited" in result.message and "--force" in result.remediation
    entry = next(h for h in _update()["hooks"] if h["name"] == END)
    assert entry["state"] == STATE_EDITED and entry["action"] == "skipped"
    assert "--force" in entry["reason"]
    assert script.read_text() == EDITED

    forced = next(h for h in _update("--force")["hooks"] if h["name"] == END)
    assert forced["action"] == "updated" and Path(forced["backup"]).read_text() == EDITED
    assert script.read_text() == packaged_scripts()[END]
    assert _doctor().passed


# -- registration fields (§5) -------------------------------------------------------


def test_a_timeout_an_earlier_release_wrote_is_reset(env):
    _, project = env
    _init(project, "--hook")
    data = json.loads(_settings(project).read_text())
    data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 15  # v0.7.1-v0.8.10
    _settings(project).write_text(json.dumps(data))

    assert not _doctor().passed
    [reg] = _update()["registrations"]
    assert reg["changes"] == ["set the SessionEnd timeout to 35s"] and reg["backup"]
    assert _handlers(_settings(project), "SessionEnd")[0]["timeout"] == 35
    assert _doctor().passed and _update()["registrations"] == []


def test_a_timeout_no_release_wrote_is_the_users_and_is_kept(env):
    _, project = env
    _init(project, "--hook")
    data = json.loads(_settings(project).read_text())
    data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 60
    _settings(project).write_text(json.dumps(data))

    result = _doctor()
    assert result.passed and "timeout 60" in result.message
    out = _update()
    assert out["registrations"] == []
    assert [(k["event"], k["state"]) for k in out["registrations_kept"]] == [("SessionEnd", STATE_EDITED)]
    assert _handlers(_settings(project), "SessionEnd")[0]["timeout"] == 60

    [reg] = _update("--force")["registrations"]
    assert "set the SessionEnd timeout to 35s" in reg["changes"]
    assert _handlers(_settings(project), "SessionEnd")[0]["timeout"] == 35


def test_an_old_command_is_still_quoted_when_the_user_changed_its_timeout(env):
    _, project = env
    _init(project, "--hook")
    data = json.loads(_settings(project).read_text())
    handler = data["hooks"]["SessionEnd"][0]["hooks"][0]
    handler["command"] = f"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/{END}"
    handler["timeout"] = 60
    _settings(project).write_text(json.dumps(data))

    assert not _doctor().passed
    [reg] = _update()["registrations"]
    assert reg["changes"] == ["quoted ${CLAUDE_PROJECT_DIR}"]
    [end] = _handlers(_settings(project), "SessionEnd")
    assert end["command"] == f'"${{CLAUDE_PROJECT_DIR}}"/.claude/hooks/{END}' and end["timeout"] == 60


# -- missing registrations (§6) -----------------------------------------------------


def test_a_registration_never_offered_is_added_with_its_script(env):
    """An install from before PostCompact existed: no registration, no script."""
    _, project = env
    _init(project, "--hook")
    _drop(_settings(project), "PostCompact")
    (project / ".claude" / "hooks" / POST).unlink()

    result = _doctor()
    assert not result.passed and "PostCompact" in result.message and "0.24.0" in result.message
    [reg] = _update()["registrations"]
    assert reg["changes"] == ["registered PostCompact"]
    [post] = _handlers(_settings(project), "PostCompact")
    assert post == {"type": "command", "command": f'"${{CLAUDE_PROJECT_DIR}}"/.claude/hooks/{POST}',
                    "timeout": 35}
    assert (project / ".claude" / "hooks" / POST).read_text() == packaged_scripts()[POST]
    assert _doctor().passed and _update()["registrations"] == []


def test_a_registration_the_user_removed_is_kept_removed_unless_forced(env):
    _, project = env
    _init(project, "--hook")
    _drop(_settings(project), "PostCompact")  # the script stays installed

    result = _doctor()
    assert result.passed and "PostCompact" in result.message
    out = _update()
    assert out["registrations"] == []
    assert [(k["event"], k["state"]) for k in out["registrations_kept"]] == [("PostCompact", "removed")]
    assert "PostCompact" not in json.loads(_settings(project).read_text())["hooks"]

    [reg] = _update("--force")["registrations"]
    assert reg["changes"] == ["registered PostCompact"]
    assert len(_handlers(_settings(project), "PostCompact")) == 1


def test_a_hand_assembled_user_install_is_never_extended_unasked(env):
    """Three events registered by hand at user level, no session-start hook."""
    home, _ = env
    hooks_dir = home / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    events = {"SessionEnd": END, "UserPromptSubmit": PROMPT, "PostCompact": POST}
    for name in events.values():
        (hooks_dir / name).write_text(packaged_scripts()[name])
        (hooks_dir / name).chmod(0o755)
    settings = home / ".claude" / "settings.json"
    timeouts = {"SessionEnd": 35, "UserPromptSubmit": 20, "PostCompact": 35}
    settings.write_text(json.dumps({"hooks": {
        event: [{"hooks": [{"type": "command", "command": f"~/.claude/hooks/{name}",
                            "timeout": timeouts[event]}]}]
        for event, name in events.items()
    }}))
    before = settings.read_text()

    result = _doctor()
    assert result.passed and "SessionStart" in result.message
    out = _update()
    assert out["registrations"] == []
    assert [(k["event"], k["state"]) for k in out["registrations_kept"]] == [("SessionStart", "not registered")]
    assert settings.read_text() == before and not (hooks_dir / START).exists()

    [reg] = _update("--force")["registrations"]
    assert reg["changes"] == ["registered SessionStart"]
    [start] = _handlers(settings, "SessionStart")
    assert start["command"] == f"~/.claude/hooks/{START}"  # the documented user-level form
    assert (hooks_dir / START).read_text() == packaged_scripts()[START]


def test_a_codex_registration_is_appended_after_every_existing_group(env):
    _, project = env
    _init(project, "--codex-hook")
    hooks_json = project / ".codex" / "hooks.json"
    data = json.loads(hooks_json.read_text())
    del data["hooks"]["SessionEnd"]
    data["hooks"]["UserPromptSubmit"].insert(0, {"hooks": [{"type": "command", "command": "other.sh"}]})
    hooks_json.write_text(json.dumps(data))
    (project / ".codex" / "hooks" / END).unlink()
    before = json.loads(hooks_json.read_text())

    out = _update()
    assert [r["changes"] for r in out["registrations"]] == [["registered SessionEnd"]]
    assert out["notes"] == [TRUST_NOTE]
    after = json.loads(hooks_json.read_text())
    for event, groups in before["hooks"].items():
        assert after["hooks"][event] == groups  # nothing already there moved
    assert (project / ".codex" / "hooks" / END).read_text() == packaged_scripts()[END]


def test_a_fresh_init_install_has_nothing_to_report(env):
    _, project = env
    _init(project, "--hook", "--codex-hook", "--cursor-hooks", "--agy-hooks")
    result = _doctor()
    assert result.passed and "Left as you set them" not in result.message
    out = _update()
    assert out["registrations"] == [] and out["registrations_kept"] == []
    assert all(h["action"] == "skipped" for h in out["hooks"])
    assert {s.state for s in hook_statuses(*env)} == {"current"}

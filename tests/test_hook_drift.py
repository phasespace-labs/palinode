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


# -- run from the home directory: a user file is not a project file ------------
#
# From $HOME, a harness's project config file and its user config file can be
# the same file. It keeps its user-level rules: nothing is added unasked, an
# added registration uses the user form, and a user file palinode never walks
# is not walked under its project name either.


def _home_install(home: Path, monkeypatch) -> tuple[Path, str]:
    """The hand-assembled user-level Claude Code install, with cwd = $HOME."""
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
    monkeypatch.chdir(home)
    return settings, settings.read_text()


def _doctor_at(home: Path):
    return hooks_current(DoctorContext(config=None, home=home, cwd=home))


def test_from_home_the_user_settings_file_is_not_extended_unasked(env, monkeypatch):
    home, _ = env
    settings, before = _home_install(home, monkeypatch)

    result = _doctor_at(home)
    assert result.passed and "SessionStart" in result.message, result.message
    out = _update()
    assert out["registrations"] == []
    assert [(k["event"], k["state"]) for k in out["registrations_kept"]] == [("SessionStart", "not registered")]
    assert settings.read_text() == before
    assert not (home / ".claude" / "hooks" / START).exists()


def test_from_home_force_adds_the_user_form_not_the_project_form(env, monkeypatch):
    home, _ = env
    settings, _ = _home_install(home, monkeypatch)

    [reg] = _update("--force")["registrations"]
    assert reg["changes"] == ["registered SessionStart"]
    [start] = _handlers(settings, "SessionStart")
    assert start["command"] == f"~/.claude/hooks/{START}"
    assert "CLAUDE_PROJECT_DIR" not in settings.read_text()


def _from_project(home: Path, flag: str, rel_config: str, drop: str, drop_script: str,
                  monkeypatch) -> tuple[Path, str]:
    """Copy a project install of one harness into $HOME, minus one registration and its script."""
    project = home.parent / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    _init(project, flag)
    config_rel, hooks_rel = rel_config.rsplit("/", 1)[0], rel_config.rsplit("/", 1)[0] + "/hooks"
    (home / config_rel).mkdir(parents=True, exist_ok=True)
    (home / hooks_rel).mkdir(parents=True, exist_ok=True)
    data = json.loads((project / rel_config).read_text())
    del data["hooks"][drop]
    (home / rel_config).write_text(json.dumps(data, indent=2))
    for script in (project / hooks_rel).iterdir():
        if script.name != drop_script:
            (home / hooks_rel / script.name).write_text(script.read_text())
            (home / hooks_rel / script.name).chmod(0o755)
    monkeypatch.chdir(home)
    return home / rel_config, (home / rel_config).read_text()


def test_from_home_codex_user_hooks_json_is_not_extended(env, monkeypatch):
    """Codex's user hooks.json is the project one when CODEX_HOME is unset and cwd is $HOME."""
    home, _ = env
    hooks_json, before = _from_project(home, "--codex-hook", ".codex/hooks.json", "SessionEnd", END,
                                       monkeypatch)
    for args in ((), ("--force",)):
        out = _update(*args)
        assert out["registrations"] == [], args
        assert hooks_json.read_text() == before, args
    assert not (home / ".codex" / "hooks" / END).exists()


def test_from_home_cursor_user_hooks_json_is_not_walked(env, monkeypatch):
    """~/.cursor/hooks.json is never walked; from $HOME it must not be walked as a project file."""
    home, _ = env
    hooks_json, before = _from_project(home, "--cursor-hooks", ".cursor/hooks.json", "beforeSubmitPrompt", PROMPT,
                                       monkeypatch)
    out = _update("--force")
    assert out["registrations"] == [] and out["registrations_kept"] == []
    assert hooks_json.read_text() == before


@pytest.mark.parametrize("spec", __import__("palinode.hooks.harnesses", fromlist=["HARNESSES"]).HARNESSES,
                         ids=lambda s: s.name)
def test_distinct_config_files_take_each_path_once_and_the_user_entry_wins(spec, tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path
    files = spec.distinct_config_files(home, home)
    paths = [c.path(home, home) for c in files]
    assert len(paths) == len(set(paths))
    by_path = {}
    for c in spec.config_files:
        by_path.setdefault(c.path(home, home), c)
    assert files == list(by_path.values())  # first declared wins
    for c in files:
        if any(u.scope == "user" and u.path(home, home) == c.path(home, home) for u in spec.config_files):
            assert c.scope == "user" and not c.init_writes


def test_from_home_force_after_removing_a_project_form_entry_writes_the_user_form(env, monkeypatch):
    """The cleanup the changelog gives for an install v0.25.0 damaged from $HOME."""
    home, _ = env
    settings, _ = _home_install(home, monkeypatch)
    data = json.loads(settings.read_text())
    data["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "timeout": 20,
        "command": f'"${{CLAUDE_PROJECT_DIR}}"/.claude/hooks/{START}'}]}]
    settings.write_text(json.dumps(data))
    del data["hooks"]["SessionStart"]          # the user removes it
    settings.write_text(json.dumps(data))
    _update("--force")
    [start] = _handlers(settings, "SessionStart")
    assert start["command"] == f"~/.claude/hooks/{START}"

"""Codex hooks: scaffolding, discovery and the payload Codex sends.

Codex runs the same scripts Claude Code does (the session-end floor's Codex
rollout handling is pinned in ``test_codex_session_end_floor.py``). Its SessionStart and
UserPromptSubmit payloads carry the fields the scripts read, and it takes the
same ``hookSpecificOutput.additionalContext`` back. These tests pin that with
the payloads codex-cli 0.165.0 actually sent to a marker hook (recorded
below), and pin the parts that are Codex's own:

- ``palinode init --codex-hook`` writes ``.codex/hooks/`` and merges
  ``.codex/hooks.json`` after any existing entries. Codex keys hook trust on a
  hook's position, so another tool's entries must not move.
- the registered command finds the script from a subdirectory: Codex runs it
  through ``$SHELL -lc`` in the session's cwd and has no project-dir variable.
- ``palinode hooks update``, doctor's ``hooks_current`` and ``controls status``
  see the Codex copies.

Real files under ``tmp_path`` throughout; the scripts run under a stub
``curl`` here and against a real server in ``test_codex_hooks_live.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli.controls import _observed_project_setup
from palinode.cli.hooks import STATE_CURRENT, STATE_STALE, hook_statuses, hooks, packaged_hooks
from palinode.cli.init import (
    CODEX_HOOKS_BLOCK,
    HOOK_SCRIPT,
    SESSION_START_HOOK_SCRIPT,
    USER_PROMPT_SUBMIT_HOOK_SCRIPT,
    init,
)
from palinode.diagnostics.checks.hooks_current import hooks_current
from palinode.diagnostics.types import DoctorContext
from tests import test_session_start_hook as start_stub

REPO_ROOT = Path(__file__).resolve().parent.parent
START = "palinode-session-start.sh"
PROMPT = "palinode-user-prompt-submit.sh"
END = "palinode-session-end.sh"

needs_shell_tools = pytest.mark.skipif(
    not (shutil.which("jq") and shutil.which("bash") and shutil.which("git")),
    reason="the hooks need jq, bash and git",
)

#: What codex-cli 0.165.0 wrote to a SessionStart hook's stdin (`codex exec`
#: from a repository subdirectory; paths and IDs replaced). Codex adds model and
#: permission_mode to the Claude Code fields.
CODEX_SESSION_START = {
    "session_id": "11111111-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
    "transcript_path": "/home/example/.codex/sessions/2026/01/01/rollout-2026-01-01T00-00-00-11111111.jsonl",
    "cwd": "/repo/sub",
    "hook_event_name": "SessionStart",
    "model": "gpt-6.1-sol",
    "permission_mode": "bypassPermissions",
    "source": "startup",
}

#: The same session's UserPromptSubmit payload: turn_id is Codex's addition.
CODEX_USER_PROMPT_SUBMIT = {
    "session_id": "11111111-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
    "turn_id": "22222222-bbbb-7bbb-8bbb-bbbbbbbbbbbb",
    "transcript_path": "/home/example/.codex/sessions/2026/01/01/rollout-2026-01-01T00-00-00-11111111.jsonl",
    "cwd": "/repo/sub",
    "hook_event_name": "UserPromptSubmit",
    "model": "gpt-6.1-sol",
    "permission_mode": "bypassPermissions",
    "prompt": "how do production deploys roll back?",
}

#: The shape Orca (a third-party Codex wrapper) writes to hooks.json.
_ORCA_COMMAND = ": orca-agent-hook-form=1; /bin/sh \"$HOME/.orca/agent-hooks/codex-hook.sh\" || :"
ORCA_HOOKS = {
    "description": "orca agent hooks",
    "hooks": {
        event: [{"hooks": [{"type": "command", "command": _ORCA_COMMAND, "timeout": 10}]}]
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop")
    },
}


def _init(project: Path, *args: str):
    return CliRunner().invoke(
        init,
        ["--dir", str(project), "--no-mcp", "--no-claudemd", "--no-slash",
         "--no-prompts", "--no-skill", *args],
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    return home, project


# -- scaffolding -----------------------------------------------------------


def test_codex_hook_writes_the_shared_scripts_and_the_registration(env):
    _, project = env
    res = _init(project, "--codex-hook")
    assert res.exit_code == 0, res.output

    hooks_json = json.loads((project / ".codex" / "hooks.json").read_text())
    assert hooks_json == CODEX_HOOKS_BLOCK
    for name, content in ((START, SESSION_START_HOOK_SCRIPT), (PROMPT, USER_PROMPT_SUBMIT_HOOK_SCRIPT),
                          (END, HOOK_SCRIPT)):
        script = project / ".codex" / "hooks" / name
        assert script.read_text(encoding="utf-8") == content, name
        assert os.access(script, os.X_OK)
    # Codex only: no Claude Code hooks without --hook.
    assert not (project / ".claude" / "hooks").exists()
    assert not (project / ".claude" / "settings.json").exists()
    # The install is not done until Codex trusts it, and the output says how.
    assert "/hooks" in res.output


def test_codex_registration_carries_no_matcher_and_all_three_events():
    """SessionStart's source filter is the script's (PALINODE_HOOK_START_SOURCES);
    Codex ignores a UserPromptSubmit matcher anyway. Codex clamps a SessionEnd
    hook to 3 seconds (1 by default), so the floor asks for exactly 3."""
    hooks_block = CODEX_HOOKS_BLOCK["hooks"]
    assert set(hooks_block) == {"SessionStart", "UserPromptSubmit", "SessionEnd"}
    for event, groups in hooks_block.items():
        assert "matcher" not in groups[0]
        assert groups[0]["hooks"][0]["timeout"] == (3 if event == "SessionEnd" else 20)


def test_example_codex_hooks_json_is_the_registration_init_writes():
    example = json.loads((REPO_ROOT / "examples" / "hooks" / "codex-hooks.json").read_text())
    assert example == CODEX_HOOKS_BLOCK


def test_merge_appends_after_existing_entries_and_moves_nothing(env):
    _, project = env
    hooks_json = project / ".codex" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text(json.dumps(ORCA_HOOKS, indent=2))

    res = _init(project, "--codex-hook")
    assert res.exit_code == 0, res.output
    assert ".codex/hooks.json: merged" in res.output

    merged = json.loads(hooks_json.read_text())
    assert merged["description"] == ORCA_HOOKS["description"]
    for event, groups in ORCA_HOOKS["hooks"].items():
        # Every existing group keeps its index: Codex's trust key is
        # <file>:<event>:<group>:<handler>, so a shifted group is an untrusted one.
        assert merged["hooks"][event][: len(groups)] == groups, event
    for event in ("SessionStart", "UserPromptSubmit"):
        assert len(merged["hooks"][event]) == 2
        assert merged["hooks"][event][1] == CODEX_HOOKS_BLOCK["hooks"][event][0]
    assert merged["hooks"]["PreToolUse"] == ORCA_HOOKS["hooks"]["PreToolUse"]
    assert merged["hooks"]["Stop"] == ORCA_HOOKS["hooks"]["Stop"]


def test_rerun_is_idempotent(env):
    _, project = env
    hooks_json = project / ".codex" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text(json.dumps(ORCA_HOOKS, indent=2))
    assert _init(project, "--codex-hook").exit_code == 0
    before = hooks_json.read_bytes()

    res = _init(project, "--codex-hook")
    assert res.exit_code == 0, res.output
    assert "palinode hooks already registered" in res.output
    assert hooks_json.read_bytes() == before


def test_a_later_plain_init_keeps_the_codex_choice(env):
    """Like --hook: once installed, a re-run without the flag keeps it on."""
    _, project = env
    assert _init(project, "--codex-hook").exit_code == 0
    hooks_json = project / ".codex" / "hooks.json"
    hooks_json.write_text(json.dumps(ORCA_HOOKS))  # someone replaced the file

    res = _init(project)
    assert res.exit_code == 0, res.output
    merged = json.loads(hooks_json.read_text())
    assert merged["hooks"]["SessionStart"][-1] == CODEX_HOOKS_BLOCK["hooks"]["SessionStart"][0]


def test_without_the_flag_nothing_codex_is_written(env):
    _, project = env
    res = _init(project)
    assert res.exit_code == 0, res.output
    assert not (project / ".codex").exists()


def test_no_codex_hook_skips_an_existing_install(env):
    _, project = env
    assert _init(project, "--codex-hook").exit_code == 0
    hooks_json = project / ".codex" / "hooks.json"
    hooks_json.write_text(json.dumps(ORCA_HOOKS))
    before = hooks_json.read_bytes()
    assert _init(project, "--no-codex-hook").exit_code == 0
    assert hooks_json.read_bytes() == before


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps(["a list"]), json.dumps({"hooks": []}),
     json.dumps({"hooks": {"SessionStart": {"hooks": []}}})],
    ids=["invalid-json", "not-an-object", "hooks-not-an-object", "event-not-a-list"],
)
def test_a_file_that_cannot_be_merged_safely_is_left_alone(env, content):
    _, project = env
    hooks_json = project / ".codex" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text(content)

    res = _init(project, "--codex-hook")
    assert res.exit_code == 0, res.output
    assert ".codex/hooks.json: skipped" in res.output
    assert hooks_json.read_text() == content


def test_dry_run_lists_the_codex_writes_and_writes_nothing(env):
    _, project = env
    res = _init(project, "--codex-hook", "--dry-run")
    assert res.exit_code == 0, res.output
    assert ".codex/hooks.json" in res.output
    assert f".codex/hooks/{START}" in res.output
    assert f".codex/hooks/{PROMPT}" in res.output
    assert not (project / ".codex").exists()


# -- the registered command, run the way Codex runs it ----------------------


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@needs_shell_tools
def test_the_registered_command_finds_the_script_from_a_subdirectory(tmp_path, monkeypatch):
    """Codex runs ``$SHELL -lc '<command>'`` in the session cwd. The command
    resolves the checkout root itself; nothing like CLAUDE_PROJECT_DIR is set."""
    project = _git_repo(tmp_path / "proj")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _init(project, "--codex-hook").exit_code == 0
    sub = project / "src" / "deep"
    sub.mkdir(parents=True)

    for event in ("SessionStart", "UserPromptSubmit", "SessionEnd"):
        command = CODEX_HOOKS_BLOCK["hooks"][event][0]["hooks"][0]["command"]
        proc = subprocess.run(
            ["/bin/sh", "-c", command], cwd=sub, input="{}", capture_output=True, text=True,
            # No palinode API here: the script must fail silent, exit 0, which
            # also proves it was found and run (a missing script exits 127).
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
                 "PALINODE_API_URL": "http://127.0.0.1:9"},
        )
        assert proc.returncode == 0, (event, proc.stderr)
        assert proc.stdout == "", event


# -- payload shape (stub curl) ---------------------------------------------


#: Records each request's endpoint and JSON body (``-d``), allows recall, and
#: answers /context/prime and /resolve from canned files.
_STUB_CURL = """\
#!/bin/bash
url="" body=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d) body="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
endpoint="${url#http*://*/}"
endpoint="${endpoint%%\\?*}"
echo "$endpoint" >> "$STUB_DIR/curl-called"
[ -n "$body" ] && printf '%s' "$body" > "$STUB_DIR/body-${endpoint//\\//_}.json"
case "$endpoint" in
  controls/check) echo '{"allowed":true}' ;;
  context/prime) cat "$STUB_DIR/prime.json" ;;
  resolve) cat "$STUB_DIR/resolve.json" ;;
esac
exit 0
"""

_BUNDLE = {
    "selected": [{"ref": "decisions/deploy-rollback.md"}],
    "conflicts": [], "replaced": [], "insufficient": [], "omitted_conflicts": 0,
    "retrieval": {"confidence": "confident"},
    "text": "Current (1):\n- [decisions/deploy-rollback.md] Rollback is git revert + reindex.",
}


def _run_stubbed(tmp_path: Path, script: str, payload: dict, **env: str):
    """Run a shipped script on a Codex payload; return (proc, endpoints, bodies)."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "curl").write_text(_STUB_CURL)
    (stub_dir / "curl").chmod(0o755)
    (stub_dir / "prime.json").write_text(json.dumps({"core_memories": start_stub._CORE_LIST}))
    (stub_dir / "resolve.json").write_text(json.dumps(_BUNDLE))
    proc = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "examples" / "hooks" / script)],
        input=json.dumps(payload), capture_output=True, text=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "STUB_DIR": str(stub_dir),
             "HOME": str(tmp_path), **env},
    )
    log = stub_dir / "curl-called"
    endpoints = log.read_text().split() if log.exists() else []
    bodies = {
        f.name[len("body-"):-len(".json")]: json.loads(f.read_text())
        for f in stub_dir.glob("body-*.json")
    }
    return proc, endpoints, bodies


@needs_shell_tools
def test_session_start_injects_the_prime_digest_for_a_codex_payload(tmp_path):
    repo = _git_repo(tmp_path / "proj")
    sub = repo / "pkg"
    sub.mkdir()
    proc, endpoints, bodies = _run_stubbed(
        tmp_path, START, {**CODEX_SESSION_START, "cwd": str(sub)})
    assert proc.returncode == 0, proc.stderr

    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "[decisions/adopt-rrf.md]" in out["hookSpecificOutput"]["additionalContext"]
    assert "context/prime" in endpoints
    assert not any(e.startswith("list") for e in endpoints)
    # A subdirectory cwd reaches the server as the checkout root, and Codex's
    # own fields (model, permission_mode, transcript_path) go nowhere.
    assert bodies["context_prime"] == {
        "cwd": str(repo), "session_id": CODEX_SESSION_START["session_id"]}


@needs_shell_tools
@pytest.mark.parametrize("source", ["resume", "fork", "compact"])
def test_session_start_skips_sources_that_carry_prior_context(tmp_path, source):
    """Codex adds ``fork`` to Claude Code's sources; the default allowlist
    (startup, clear) skips it like ``resume``."""
    proc, endpoints, _ = _run_stubbed(
        tmp_path, START, {**CODEX_SESSION_START, "cwd": str(tmp_path), "source": source})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "context/prime" not in endpoints


@needs_shell_tools
def test_user_prompt_submit_resolves_a_codex_prompt_with_the_sessions_scope(tmp_path):
    repo = _git_repo(tmp_path / "proj")
    sub = repo / "pkg"
    sub.mkdir()
    proc, endpoints, bodies = _run_stubbed(
        tmp_path, PROMPT, {**CODEX_USER_PROMPT_SUBMIT, "cwd": str(sub)},
        PALINODE_HOOK_RECALL_TRIGGERS="0")
    assert proc.returncode == 0, proc.stderr

    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "decisions/deploy-rollback.md" in out["hookSpecificOutput"]["additionalContext"]
    assert endpoints.count("controls/check") == 2  # preflight and output boundary
    resolve = bodies["resolve"]
    assert resolve["query"] == CODEX_USER_PROMPT_SUBMIT["prompt"]
    # A subdirectory cwd reaches the server as the checkout root.
    assert resolve["cwd"] == str(repo)
    assert "project" not in resolve
    # Codex's own fields are not forwarded anywhere.
    assert not {"turn_id", "model", "permission_mode", "transcript_path"} & set(resolve)


@needs_shell_tools
def test_user_prompt_submit_sends_an_explicit_project(tmp_path):
    proc, _, bodies = _run_stubbed(
        tmp_path, PROMPT, {**CODEX_USER_PROMPT_SUBMIT, "cwd": str(tmp_path)},
        PALINODE_HOOK_RECALL_TRIGGERS="0", PALINODE_PROJECT="project/alpha")
    assert proc.returncode == 0, proc.stderr
    assert bodies["resolve"]["project"] == "alpha"
    assert bodies["controls_check"]["project"] == "alpha"


# -- hooks update / doctor / controls status --------------------------------


def test_doctor_and_update_cover_the_codex_copies(env):
    _, project = env
    hooks_json = project / ".codex" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text(json.dumps(ORCA_HOOKS, indent=2))
    assert _init(project, "--codex-hook").exit_code == 0
    orca_bytes = hooks_json.read_bytes()

    statuses = {Path(s.path).name: s for s in hook_statuses(Path.home(), project)}
    assert set(statuses) == {START, PROMPT, END}
    assert statuses[START].path == str(project / ".codex" / "hooks" / START)
    assert all(s.state == STATE_CURRENT for s in statuses.values())
    assert hooks_current(DoctorContext(config=None)).passed

    stale = project / ".codex" / "hooks" / PROMPT
    stale.write_text("#!/bin/bash\n# palinode-user-prompt-submit.sh — older\nexit 0\n")
    result = hooks_current(DoctorContext(config=None))
    assert not result.passed
    assert str(stale) in result.message

    res = CliRunner().invoke(hooks, ["update", "--format", "json"])
    assert res.exit_code == 0, res.output
    entry = next(h for h in json.loads(res.output)["hooks"] if h["name"] == PROMPT)
    assert entry["state"] == STATE_STALE and entry["action"] == "updated"
    assert stale.read_text(encoding="utf-8") == packaged_hooks()[PROMPT]
    assert hooks_current(DoctorContext(config=None)).passed
    # The registration and the other tool's entries are never rewritten.
    assert hooks_json.read_bytes() == orca_bytes


def test_user_level_codex_hooks_honour_codex_home(env, tmp_path, monkeypatch):
    home, project = env
    codex_home = tmp_path / "custom-codex-home"
    script = codex_home / "hooks" / START
    script.parent.mkdir(parents=True)
    script.write_text(packaged_hooks()[START])
    (codex_home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": f'"{script}"'}]}]}}))

    assert hook_statuses(home, project) == []  # default ~/.codex has nothing
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    [status] = hook_statuses(home, project)
    assert status.path == str(script) and status.state == STATE_CURRENT


def test_hooks_in_codex_config_toml_are_discovered(env):
    """Codex also takes ``[[hooks.<Event>]]`` tables in config.toml; the
    ``[hooks.state]`` trust table beside them is not a registration."""
    home, project = env
    script = home / "bin" / PROMPT
    script.parent.mkdir()
    script.write_text("#!/bin/bash\n# palinode-user-prompt-submit.sh — older\nexit 0\n")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        '[[hooks.UserPromptSubmit]]\n'
        '[[hooks.UserPromptSubmit.hooks]]\n'
        'type = "command"\n'
        f'command = "{script}"\n'
        '\n'
        '[hooks.state."/x/hooks.json:session_start:0:0"]\n'
        'trusted_hash = "sha256:abc"\n'
    )
    [status] = hook_statuses(home, project)
    assert status.path == str(script) and status.state == STATE_STALE


def test_an_unquoted_toplevel_substitution_is_expanded(env):
    home, project = env
    (project / ".codex").mkdir()
    (project / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command",
                    "command": f"bash $(git rev-parse --show-toplevel)/.codex/hooks/{START}"}]}]}}))
    [status] = hook_statuses(home, project)
    assert status.path == str(project / ".codex" / "hooks" / START)


def test_controls_status_observes_the_codex_hooks(env):
    home, project = env
    assert _init(project, "--codex-hook").exit_code == 0
    observed = _observed_project_setup(project, home=home)
    assert observed["codex_hooks"] == [
        {"event": "SessionStart", "scope": "project", "settings_registration_observed": True, "script_observed": True},
        {"event": "UserPromptSubmit", "scope": "project", "settings_registration_observed": True, "script_observed": True},
        {"event": "SessionEnd", "scope": "project", "settings_registration_observed": True, "script_observed": True},
    ]
    assert observed["claude_hooks"] == []

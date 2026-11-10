"""Cursor hooks: the adapter, the `palinode init --cursor-hooks` scaffold, drift.

Cursor's ``sessionStart`` and ``beforeSubmitPrompt`` hooks run the same
session-start and per-turn recall scripts Claude Code does, through
``palinode-cursor-hook.sh``, which translates Cursor's payload and answer.
What was verified against a real cursor-agent before any of this was built
(payload fields, which events fire, which output fields reach the model) is
recorded on the dev issue; these tests pin the adapter to that contract:

- in:  ``workspace_roots[0]`` → ``cwd``, ``conversation_id`` → ``session_id``,
  ``prompt`` passed through;
- out: ``{"continue": true}``, plus ``additional_context`` only when the
  script produced some, and never anything that could block a prompt.

The live tests at the bottom run a real uvicorn with real SQLite and git, real
curl and jq, and the shipped scripts, and let the server resolve the project
from the workspace root the adapter sends, with no ``PALINODE_PROJECT``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli.hooks import (
    STATE_CURRENT,
    STATE_STALE,
    hook_statuses,
    hooks,
    packaged_hooks,
)
from palinode.cli.init import (
    CURSOR_HOOK_SCRIPT,
    SESSION_START_HOOK_SCRIPT,
    USER_PROMPT_SUBMIT_HOOK_SCRIPT,
    init,
)
from palinode.diagnostics.checks.hooks_current import hooks_current
from palinode.diagnostics.types import DoctorContext
from tests import test_resolve_bundle as scenarios
from tests import test_resolve_hook_live as live

mem = scenarios.mem
live_api = live.live_api

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = "palinode-cursor-hook.sh"
START = "palinode-session-start.sh"
PROMPT = "palinode-user-prompt-submit.sh"

needs_jq = pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("jq")),
    reason="the hooks need curl and jq on PATH",
)

#: Stands in for a palinode script: echoes the payload it was handed back as
#: its additionalContext, so a test can read exactly what the adapter sent.
ECHO_SCRIPT = """#!/bin/bash
IN=$(cat)
jq -n --arg ctx "$IN" '{hookSpecificOutput: {hookEventName: "X", additionalContext: $ctx}}'
"""

#: A user-level entry like the one Orca keeps in ~/.cursor/hooks.json; init
#: must leave entries like it exactly as they were.
OTHER_ENTRY = {"command": "/bin/sh -c 'echo {\"continue\":true}'", "timeout": 10}


def _adapter(tmp_path: Path, script_body: str | None, script_name: str = START) -> tuple[Path, Path]:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    adapter = hooks_dir / ADAPTER
    adapter.write_text(CURSOR_HOOK_SCRIPT)
    script = hooks_dir / script_name
    if script_body is not None:
        script.write_text(script_body)
    return adapter, script


def _run_adapter(adapter: Path, script_arg: str, payload: dict, cwd: Path, **env) -> dict:
    proc = subprocess.run(
        ["/bin/bash", str(adapter), script_arg],
        input=json.dumps(payload), capture_output=True, text=True, cwd=cwd,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), **env},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# -- the adapter -----------------------------------------------------------


@needs_jq
@pytest.mark.parametrize("script_name,event", [(START, "SessionStart"), (PROMPT, "UserPromptSubmit")])
def test_the_adapter_translates_cursors_payload_into_the_scripts(tmp_path, script_name, event):
    adapter, script = _adapter(tmp_path, ECHO_SCRIPT, script_name)
    out = _run_adapter(adapter, str(script), {
        "conversation_id": "conv-1", "session_id": "conv-1", "generation_id": "gen-1",
        "hook_event_name": "beforeSubmitPrompt", "prompt": "what did we decide?",
        "workspace_roots": ["/work/alpha", "/work/other"],
    }, tmp_path)
    assert out["continue"] is True
    sent = json.loads(out["additional_context"])
    assert sent == {
        "session_id": "conv-1", "cwd": "/work/alpha", "hook_event_name": event,
        "source": "startup", "prompt": "what did we decide?",
    }


@needs_jq
def test_without_workspace_roots_the_cursor_project_dir_is_the_cwd(tmp_path):
    adapter, script = _adapter(tmp_path, ECHO_SCRIPT)
    out = _run_adapter(adapter, str(script), {"conversation_id": "c"}, tmp_path,
                       CURSOR_PROJECT_DIR="/work/beta")
    assert json.loads(out["additional_context"])["cwd"] == "/work/beta"


@needs_jq
def test_a_relative_script_resolves_beside_the_adapter(tmp_path):
    """Registered relative to the project root; found beside the adapter if run elsewhere."""
    adapter, _ = _adapter(tmp_path, ECHO_SCRIPT)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = _run_adapter(adapter, f".cursor/hooks/{START}", {"conversation_id": "c"}, elsewhere)
    assert "additional_context" in out


@needs_jq
@pytest.mark.parametrize("body", [
    "#!/bin/bash\ncat >/dev/null\nexit 0\n",           # silent: nothing to inject
    "#!/bin/bash\ncat >/dev/null\nexit 3\n",           # failed
    "#!/bin/bash\ncat >/dev/null\necho 'not json'\n",  # dry-run style text
])
def test_no_context_means_continue_and_nothing_else(tmp_path, body):
    adapter, script = _adapter(tmp_path, body)
    assert _run_adapter(adapter, str(script), {"conversation_id": "c"}, tmp_path) == {"continue": True}


@needs_jq
def test_a_missing_or_foreign_script_never_blocks(tmp_path):
    adapter, _ = _adapter(tmp_path, None)
    assert _run_adapter(adapter, str(tmp_path / "hooks" / START), {}, tmp_path) == {"continue": True}
    foreign, script = _adapter(tmp_path, ECHO_SCRIPT, "my-lint.sh")
    assert _run_adapter(foreign, str(script), {}, tmp_path) == {"continue": True}


def test_without_jq_the_adapter_still_allows_the_prompt(tmp_path):
    adapter, script = _adapter(tmp_path, ECHO_SCRIPT)
    bare = tmp_path / "bin"
    bare.mkdir()
    for tool in ("cat", "dirname", "basename"):
        found = shutil.which(tool)
        if found:
            (bare / tool).symlink_to(found)
    proc = subprocess.run(
        ["/bin/bash", str(adapter), str(script)], input="{}", capture_output=True, text=True,
        env={"PATH": str(bare)},
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"continue": True}


@needs_jq
def test_an_unreachable_server_injects_nothing(tmp_path):
    adapter, script = _adapter(tmp_path, SESSION_START_HOOK_SCRIPT)
    out = _run_adapter(adapter, str(script), {"conversation_id": "c", "workspace_roots": [str(tmp_path)]},
                       tmp_path, PALINODE_API_URL="http://127.0.0.1:9")
    assert out == {"continue": True}


# -- palinode init --cursor-hooks -------------------------------------------


def _init(project: Path, *args: str):
    result = CliRunner().invoke(init, ["--dir", str(project), "--no-prompts", "--no-mcp", *args])
    assert result.exit_code == 0, result.output
    return result


def test_cursor_hooks_are_opt_in(tmp_path):
    (tmp_path / ".cursor").mkdir()
    _init(tmp_path)
    assert not (tmp_path / ".cursor" / "hooks.json").exists()
    assert not (tmp_path / ".cursor" / "hooks").exists()


def test_init_installs_the_cursor_hooks(tmp_path):
    _init(tmp_path, "--cursor-hooks")
    hooks_dir = tmp_path / ".cursor" / "hooks"
    for name, content in ((ADAPTER, CURSOR_HOOK_SCRIPT), (START, SESSION_START_HOOK_SCRIPT),
                          (PROMPT, USER_PROMPT_SUBMIT_HOOK_SCRIPT)):
        assert (hooks_dir / name).read_text() == content
        assert os.access(hooks_dir / name, os.X_OK)
    config = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert config["version"] == 1
    assert config["hooks"]["sessionStart"] == [{
        "command": f".cursor/hooks/{ADAPTER} .cursor/hooks/{START}", "timeout": 20}]
    assert config["hooks"]["beforeSubmitPrompt"] == [{
        "command": f".cursor/hooks/{ADAPTER} .cursor/hooks/{PROMPT}", "timeout": 20}]
    # Cursor hooks only: no Claude Code hooks ride along.
    assert not (tmp_path / ".claude" / "hooks").exists()


def test_init_merges_into_an_existing_hooks_json(tmp_path):
    hooks_json = tmp_path / ".cursor" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text(json.dumps({"version": 1, "hooks": {
        "beforeSubmitPrompt": [OTHER_ENTRY], "stop": [OTHER_ENTRY]}}))
    result = _init(tmp_path, "--cursor-hooks")
    assert ".cursor/hooks.json: merged" in result.output
    config = json.loads(hooks_json.read_text())
    assert config["hooks"]["stop"] == [OTHER_ENTRY]
    assert config["hooks"]["beforeSubmitPrompt"][0] == OTHER_ENTRY
    assert PROMPT in config["hooks"]["beforeSubmitPrompt"][1]["command"]
    assert START in config["hooks"]["sessionStart"][0]["command"]


def test_re_running_init_changes_nothing(tmp_path):
    _init(tmp_path, "--cursor-hooks")
    before = (tmp_path / ".cursor" / "hooks.json").read_bytes()
    result = _init(tmp_path, "--cursor-hooks")
    assert "skipped (palinode hooks already registered)" in result.output
    assert (tmp_path / ".cursor" / "hooks.json").read_bytes() == before


def test_an_unparseable_hooks_json_is_left_alone(tmp_path):
    hooks_json = tmp_path / ".cursor" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text("{not json")
    result = _init(tmp_path, "--cursor-hooks")
    assert "not valid JSON" in result.output
    assert hooks_json.read_text() == "{not json"


def test_dry_run_lists_the_cursor_writes_and_writes_nothing(tmp_path):
    result = _init(tmp_path, "--cursor-hooks", "--dry-run")
    assert ".cursor/hooks.json" in result.output
    assert f".cursor/hooks/{ADAPTER}" in result.output
    assert not (tmp_path / ".cursor").exists()


# -- drift: hooks_current and hooks update -----------------------------------


@pytest.fixture
def cursor_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    _init(project, "--cursor-hooks", "--no-claudemd", "--no-slash", "--no-skill")
    return home, project


def test_the_adapter_is_a_packaged_hook():
    assert packaged_hooks()[ADAPTER] == CURSOR_HOOK_SCRIPT
    assert CURSOR_HOOK_SCRIPT == (REPO_ROOT / "examples" / "hooks" / ADAPTER).read_text()


def test_discovery_finds_both_scripts_on_each_cursor_command(cursor_project):
    home, project = cursor_project
    statuses = {s.path: s for s in hook_statuses(home, project)}
    hooks_dir = project / ".cursor" / "hooks"
    assert set(statuses) == {str(hooks_dir / n) for n in (ADAPTER, START, PROMPT)}
    assert all(s.state == STATE_CURRENT for s in statuses.values())
    assert all(s.settings == [str(project / ".cursor" / "hooks.json")] for s in statuses.values())


def test_doctor_warns_on_a_stale_cursor_hook_and_update_refreshes_it(cursor_project):
    home, project = cursor_project
    stale = project / ".cursor" / "hooks" / ADAPTER
    stale.write_text("#!/bin/bash\n# palinode-cursor-hook.sh — an older release\nexit 0\n")

    result = hooks_current(DoctorContext(config=None))
    assert not result.passed and result.severity == "warn"
    assert str(stale) in result.message

    out = CliRunner().invoke(hooks, ["update", "--format", "json"])
    assert out.exit_code == 0, out.output
    entries = {e["path"]: e for e in json.loads(out.output)["hooks"]}
    assert entries[str(stale)]["action"] == "updated"
    assert stale.read_text() == CURSOR_HOOK_SCRIPT
    assert hooks_current(DoctorContext(config=None)).passed
    assert all(s.state == STATE_CURRENT for s in hook_statuses(home, project))


def test_other_cursor_hooks_are_never_reported(cursor_project):
    home, project = cursor_project
    hooks_json = project / ".cursor" / "hooks.json"
    config = json.loads(hooks_json.read_text())
    config["hooks"]["stop"] = [OTHER_ENTRY]
    hooks_json.write_text(json.dumps(config))
    names = {Path(s.path).name for s in hook_statuses(home, project)}
    assert names == {ADAPTER, START, PROMPT}
    assert STATE_STALE not in {s.state for s in hook_statuses(home, project)}


# -- live: a real server, the shipped scripts, the adapter -----------------

ALPHA_ID = "alphacursorquill"
BETA_ID = "betacursormarrow"
GLOBAL_ID = "globalcursorfennel"


@pytest.fixture()
def two_projects(mem):
    scenarios._write(
        mem, "decisions/alpha-batch.md", f"Use batch ID {ALPHA_ID} for the migration.",
        type="Decision", title="Alpha batch ID", description=f"Batch ID {ALPHA_ID}",
        core=True, entities=["project/alpha"],
    )
    scenarios._write(
        mem, "decisions/beta-batch.md", f"Use batch ID {BETA_ID} for the migration.",
        type="Decision", title="Beta batch ID", description=f"Batch ID {BETA_ID}",
        core=True, entities=["project/beta"],
    )
    scenarios._write(
        mem, "insights/house-style.md", f"House style marker {GLOBAL_ID}.",
        type="Insight", title="House style", description=f"Marker {GLOBAL_ID}",
        core=True,
    )
    return mem


def _installed(tmp_path_factory, name: str) -> Path:
    """A project named *name*, outside the store's git repo, with init's Cursor hooks.

    Outside the store because the session-start script maps a git checkout to
    its main worktree; a workspace inside the store's repo would resolve as
    the store, not as *name*.
    """
    project = tmp_path_factory.mktemp("ws") / name
    project.mkdir()
    _init(project, "--cursor-hooks", "--no-claudemd", "--no-slash", "--no-skill")
    return project


def _cursor_event(project: Path, api_url: str, event: str, **payload) -> str:
    """Run the registered command for *event* the way Cursor does: from the project root."""
    command = json.loads((project / ".cursor" / "hooks.json").read_text())["hooks"][event][0]["command"]
    proc = subprocess.run(
        command, shell=True, cwd=project, capture_output=True, text=True,
        input=json.dumps({
            "conversation_id": "conv-live", "session_id": "conv-live", "hook_event_name": event,
            "workspace_roots": [str(project)], **payload,
        }),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(project),
            "PALINODE_API_URL": api_url,
            "PALINODE_HOOK_START_TIMEOUT": live._GENEROUS_CURL_TIMEOUT_S,
            "PALINODE_HOOK_RECALL_TIMEOUT": live._GENEROUS_CURL_TIMEOUT_S,
            "PALINODE_HOOK_RESOLVE_DEADLINE": live._GENEROUS_DEADLINE_MS,
            "PALINODE_HOOK_RECALL_TRIGGERS": "0",
        },
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["continue"] is True
    return out.get("additional_context", "")


@needs_jq
def test_live_session_start_gets_its_projects_and_global_core_never_anothers(
    two_projects, live_api, tmp_path_factory,
):
    alpha = _cursor_event(_installed(tmp_path_factory, "alpha"), live_api, "sessionStart")
    assert ALPHA_ID in alpha and GLOBAL_ID in alpha
    assert BETA_ID not in alpha

    beta = _cursor_event(_installed(tmp_path_factory, "beta"), live_api, "sessionStart")
    assert BETA_ID in beta and GLOBAL_ID in beta
    assert ALPHA_ID not in beta


@needs_jq
def test_live_session_start_in_neither_project_gets_only_the_global_one(
    two_projects, live_api, tmp_path_factory,
):
    context = _cursor_event(_installed(tmp_path_factory, "gamma"), live_api, "sessionStart")
    assert GLOBAL_ID in context
    assert ALPHA_ID not in context and BETA_ID not in context


@needs_jq
def test_live_before_submit_prompt_recalls_for_the_prompt(mem, live_api, tmp_path_factory):
    scenarios.seed_current(mem)
    context = _cursor_event(
        _installed(tmp_path_factory, "demo"), live_api, "beforeSubmitPrompt",
        prompt="which endpoint does production serve traffic from?",
    )
    assert "decisions/endpoint-v2" in context
    assert "Production serves traffic from endpoint bravo." in context

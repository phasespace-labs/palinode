"""Antigravity hooks: the adapter, `palinode init --agy-hooks`, drift.

Antigravity's ``SessionStart`` and ``PreInvocation`` hooks run the same
session-start and per-turn recall scripts Claude Code does, through
``palinode-agy-hook.sh``. What was verified against the real ``agy``
CLI before any of this was built is recorded on the dev issue; these tests pin
the adapter to it:

- in:  ``workspacePaths[0]`` → ``cwd``, ``conversationId`` → ``session_id``,
  ``lastUserInput`` → ``prompt``;
- out: SessionStart context as an ``injectSteps`` ``systemMessage`` (it stays
  in the conversation), per-prompt context as an ``ephemeralMessage`` (it does
  not carry into later turns), ``{}`` when there is nothing to say;
- ``PreInvocation`` fires before every model call, tool steps included, so
  recall runs only when ``invocationNum`` is 0.

The live tests run a real uvicorn with real SQLite and git, real curl and jq,
and the shipped scripts, with the registered command run from ``.agents/`` as
Antigravity runs it and no ``PALINODE_PROJECT``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli.hooks import STATE_CURRENT, hook_statuses, hooks, packaged_hooks
from palinode.cli.init import (
    ANTIGRAVITY_HOOK_SCRIPT,
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
ADAPTER = "palinode-agy-hook.sh"
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

#: The kind of hook another tool keeps in a hooks.json (Orca keeps one like
#: it in the global ~/.gemini/config/hooks.json); init must leave it alone.
OTHER_HOOK = {"PreInvocation": [{"type": "command", "command": "/bin/true", "timeout": 10}],
              "PreToolUse": [{"matcher": "*", "hooks": [{"command": "/bin/true"}]}]}


def _adapter(tmp_path: Path, script_body: str | None, script_name: str) -> tuple[Path, Path]:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    adapter = hooks_dir / ADAPTER
    adapter.write_text(ANTIGRAVITY_HOOK_SCRIPT)
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


def _payload(**extra) -> dict:
    """The common fields every Antigravity hook receives (observed on agy 1.4.9)."""
    return {
        "conversationId": "conv-1", "workspacePaths": ["/work/alpha", "/work/other"],
        "lastUserInput": "what did we decide?", "modelName": "gemini-3.8-flash-low",
        "transcriptPath": "/x/transcript_full.jsonl", "artifactDirectoryPath": "/x", **extra,
    }


# -- the adapter -----------------------------------------------------------


@needs_jq
def test_session_start_context_goes_back_as_a_system_message(tmp_path):
    adapter, script = _adapter(tmp_path, ECHO_SCRIPT, START)
    out = _run_adapter(adapter, str(script), _payload(), tmp_path)
    (step,) = out["injectSteps"]
    sent = json.loads(step["systemMessage"]["systemMessage"])
    assert sent == {
        "session_id": "conv-1", "cwd": "/work/alpha", "hook_event_name": "SessionStart",
        "source": "startup", "prompt": "what did we decide?",
    }


@needs_jq
def test_per_prompt_context_goes_back_as_an_ephemeral_message(tmp_path):
    adapter, script = _adapter(tmp_path, ECHO_SCRIPT, PROMPT)
    out = _run_adapter(adapter, str(script), _payload(invocationNum=0, initialNumSteps=4), tmp_path)
    (step,) = out["injectSteps"]
    sent = json.loads(step["ephemeralMessage"])
    assert sent["hook_event_name"] == "UserPromptSubmit"
    assert sent["prompt"] == "what did we decide?"
    assert sent["cwd"] == "/work/alpha"


@needs_jq
def test_later_model_calls_of_a_turn_do_not_recall_again(tmp_path):
    marker = tmp_path / "ran"
    adapter, script = _adapter(tmp_path, f"#!/bin/bash\ncat >/dev/null\ntouch {marker}\n", PROMPT)
    assert _run_adapter(adapter, str(script), _payload(invocationNum=1), tmp_path) == {}
    assert not marker.exists(), "the recall script ran after a tool step"


@needs_jq
def test_without_workspace_paths_the_parent_of_agents_is_the_cwd(tmp_path):
    adapter, script = _adapter(tmp_path, ECHO_SCRIPT, START)
    agents = tmp_path / "beta" / ".agents"
    agents.mkdir(parents=True)
    out = _run_adapter(adapter, str(script), {"conversationId": "c"}, agents)
    sent = json.loads(out["injectSteps"][0]["systemMessage"]["systemMessage"])
    assert sent["cwd"] == str((tmp_path / "beta").resolve())


@needs_jq
@pytest.mark.parametrize("body", [
    "#!/bin/bash\ncat >/dev/null\nexit 0\n",
    "#!/bin/bash\ncat >/dev/null\nexit 3\n",
    "#!/bin/bash\ncat >/dev/null\necho 'not json'\n",
])
def test_no_context_means_an_empty_answer(tmp_path, body):
    adapter, script = _adapter(tmp_path, body, START)
    assert _run_adapter(adapter, str(script), _payload(), tmp_path) == {}


@needs_jq
def test_a_missing_or_foreign_script_answers_empty(tmp_path):
    adapter, _ = _adapter(tmp_path, None, START)
    assert _run_adapter(adapter, str(tmp_path / "hooks" / START), _payload(), tmp_path) == {}
    foreign, script = _adapter(tmp_path, ECHO_SCRIPT, "my-lint.sh")
    assert _run_adapter(foreign, str(script), _payload(), tmp_path) == {}


@needs_jq
def test_an_unreachable_server_injects_nothing(tmp_path):
    adapter, script = _adapter(tmp_path, SESSION_START_HOOK_SCRIPT, START)
    out = _run_adapter(adapter, str(script), _payload(workspacePaths=[str(tmp_path)]), tmp_path,
                       PALINODE_API_URL="http://127.0.0.1:9")
    assert out == {}


# -- palinode init --agy-hooks ---------------------------------------


def _init(project: Path, *args: str):
    result = CliRunner().invoke(init, ["--dir", str(project), "--no-prompts", "--no-mcp", *args])
    assert result.exit_code == 0, result.output
    return result


def test_agy_hooks_are_opt_in(tmp_path):
    (tmp_path / ".agents").mkdir()
    _init(tmp_path)
    assert not (tmp_path / ".agents" / "hooks.json").exists()
    assert not (tmp_path / ".agents" / "hooks").exists()


def test_init_installs_the_agy_hooks(tmp_path):
    _init(tmp_path, "--agy-hooks")
    hooks_dir = (tmp_path / ".agents" / "hooks").resolve()
    for name, content in ((ADAPTER, ANTIGRAVITY_HOOK_SCRIPT), (START, SESSION_START_HOOK_SCRIPT),
                          (PROMPT, USER_PROMPT_SUBMIT_HOOK_SCRIPT)):
        assert (hooks_dir / name).read_text() == content
        assert os.access(hooks_dir / name, os.X_OK)
    config = json.loads((tmp_path / ".agents" / "hooks.json").read_text())
    assert set(config) == {"palinode"}
    assert config["palinode"]["SessionStart"] == [{
        "type": "command", "command": f"{hooks_dir / ADAPTER} {hooks_dir / START}", "timeout": 20}]
    assert config["palinode"]["PreInvocation"] == [{
        "type": "command", "command": f"{hooks_dir / ADAPTER} {hooks_dir / PROMPT}", "timeout": 20}]


def test_init_merges_beside_existing_hooks(tmp_path):
    hooks_json = tmp_path / ".agents" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text(json.dumps({"orca-status": OTHER_HOOK}))
    result = _init(tmp_path, "--agy-hooks")
    assert ".agents/hooks.json: merged" in result.output
    config = json.loads(hooks_json.read_text())
    assert config["orca-status"] == OTHER_HOOK
    assert set(config["palinode"]) == {"SessionStart", "PreInvocation"}


def test_re_running_init_changes_nothing(tmp_path):
    _init(tmp_path, "--agy-hooks")
    before = (tmp_path / ".agents" / "hooks.json").read_bytes()
    result = _init(tmp_path, "--agy-hooks")
    assert "skipped (palinode hooks already registered)" in result.output
    assert (tmp_path / ".agents" / "hooks.json").read_bytes() == before


def test_an_unparseable_hooks_json_is_left_alone(tmp_path):
    hooks_json = tmp_path / ".agents" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text("{not json")
    result = _init(tmp_path, "--agy-hooks")
    assert "not valid JSON" in result.output
    assert hooks_json.read_text() == "{not json"


def test_a_path_with_spaces_is_quoted(tmp_path):
    project = tmp_path / "my project"
    project.mkdir()
    _init(project, "--agy-hooks")
    command = json.loads((project / ".agents" / "hooks.json").read_text())["palinode"]["SessionStart"][0]["command"]
    assert command.startswith("'") and "my project" in command


# -- drift: hooks_current and hooks update -----------------------------------


@pytest.fixture
def agy_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    _init(project, "--agy-hooks", "--no-claudemd", "--no-slash", "--no-skill")
    return home, project.resolve()


def test_the_adapter_is_a_packaged_hook():
    assert packaged_hooks()[ADAPTER] == ANTIGRAVITY_HOOK_SCRIPT
    assert ANTIGRAVITY_HOOK_SCRIPT == (REPO_ROOT / "examples" / "hooks" / ADAPTER).read_text()


def test_discovery_finds_both_scripts_on_each_command_and_ignores_other_hooks(agy_project):
    home, project = agy_project
    hooks_json = project / ".agents" / "hooks.json"
    config = json.loads(hooks_json.read_text())
    config["orca-status"] = OTHER_HOOK
    hooks_json.write_text(json.dumps(config))
    statuses = {s.path: s for s in hook_statuses(home, project)}
    hooks_dir = project / ".agents" / "hooks"
    assert set(statuses) == {str(hooks_dir / n) for n in (ADAPTER, START, PROMPT)}
    assert all(s.state == STATE_CURRENT for s in statuses.values())


def test_doctor_warns_on_a_stale_agy_hook_and_update_refreshes_it(agy_project):
    home, project = agy_project
    stale = project / ".agents" / "hooks" / START
    stale.write_text("#!/bin/bash\n# palinode-session-start.sh — an older release\nexit 0\n")

    result = hooks_current(DoctorContext(config=None))
    assert not result.passed and result.severity == "warn"
    assert str(stale) in result.message

    out = CliRunner().invoke(hooks, ["update", "--format", "json"])
    assert out.exit_code == 0, out.output
    entries = {e["path"]: e for e in json.loads(out.output)["hooks"]}
    assert entries[str(stale)]["action"] == "updated"
    assert stale.read_text() == SESSION_START_HOOK_SCRIPT
    assert hooks_current(DoctorContext(config=None)).passed


# -- live: a real server, the shipped scripts, the adapter -----------------

ALPHA_ID = "alphaagyquill"
BETA_ID = "betaagymarrow"
GLOBAL_ID = "globalagyfennel"


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
    """A project named *name*, outside the store's git repo, with init's Antigravity hooks."""
    project = tmp_path_factory.mktemp("ws") / name
    project.mkdir()
    _init(project, "--agy-hooks", "--no-claudemd", "--no-slash", "--no-skill")
    return project


def _agy_event(project: Path, api_url: str, event: str, **payload) -> dict:
    """Run the registered command for *event* the way agy does: from ``.agents/``."""
    command = json.loads((project / ".agents" / "hooks.json").read_text())["palinode"][event][0]["command"]
    proc = subprocess.run(
        command, shell=True, cwd=project / ".agents", capture_output=True, text=True,
        input=json.dumps({"conversationId": "conv-live", "workspacePaths": [str(project)], **payload}),
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
    return json.loads(proc.stdout)


def _session_context(out: dict) -> str:
    return out["injectSteps"][0]["systemMessage"]["systemMessage"] if out else ""


@needs_jq
def test_live_session_start_gets_its_projects_and_global_core_never_anothers(
    two_projects, live_api, tmp_path_factory,
):
    alpha = _session_context(_agy_event(_installed(tmp_path_factory, "alpha"), live_api, "SessionStart"))
    assert ALPHA_ID in alpha and GLOBAL_ID in alpha
    assert BETA_ID not in alpha

    beta = _session_context(_agy_event(_installed(tmp_path_factory, "beta"), live_api, "SessionStart"))
    assert BETA_ID in beta and GLOBAL_ID in beta
    assert ALPHA_ID not in beta


@needs_jq
def test_live_session_start_in_neither_project_gets_only_the_global_one(
    two_projects, live_api, tmp_path_factory,
):
    context = _session_context(_agy_event(_installed(tmp_path_factory, "gamma"), live_api, "SessionStart"))
    assert GLOBAL_ID in context
    assert ALPHA_ID not in context and BETA_ID not in context


@needs_jq
def test_live_pre_invocation_recalls_on_the_first_call_of_a_turn_only(mem, live_api, tmp_path_factory):
    scenarios.seed_current(mem)
    project = _installed(tmp_path_factory, "demo")
    prompt = "which endpoint does production serve traffic from?"
    out = _agy_event(project, live_api, "PreInvocation",
                     lastUserInput=prompt, invocationNum=0, initialNumSteps=1)
    context = out["injectSteps"][0]["ephemeralMessage"]
    assert "decisions/endpoint-v2" in context
    assert "Production serves traffic from endpoint bravo." in context

    after_tool = _agy_event(project, live_api, "PreInvocation",
                            lastUserInput=prompt, invocationNum=1, initialNumSteps=4)
    assert after_tool == {}

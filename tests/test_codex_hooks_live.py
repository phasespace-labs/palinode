"""The Codex recall hooks against a live server, run the way Codex runs them.

``palinode init --codex-hook`` scaffolds a repository named after a project.
Each command it registered in ``.codex/hooks.json`` is then run through a
shell from a subdirectory of that checkout, with the payload codex-cli 0.165.0
sends (``tests/test_codex_hooks.py``) on stdin: Codex runs hooks as
``$SHELL -lc '<command>'`` in the session's cwd and sets no project-directory
variable, so this is the path that has to find the script and the project.

Nothing names the project but the checkout: no ``PALINODE_PROJECT``. The
server resolves it from the cwd the hook sends, and a core memory or a record
tagged to another project in the same store must not reach the session.

Real uvicorn on an ephemeral port, the real app, real SQLite and git under
``tmp_path``, real ``curl`` and ``jq``, the shipped scripts. The checkouts
live on paths with no symlink component: the controls check refuses a cwd
that has one (macOS ``/tmp`` is a symlink), and the hooks then exit silent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli.init import CODEX_HOOKS_BLOCK, init
from tests import test_resolve_bundle as scenarios
from tests import test_resolve_hook_live as live
from tests import test_session_start_hook_live as start_live
from tests.test_codex_hooks import CODEX_SESSION_START, CODEX_USER_PROMPT_SUBMIT

mem = scenarios.mem
live_api = live.live_api
two_projects = start_live.two_projects

pytestmark = pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("jq") and shutil.which("git")),
    reason="the hooks need curl, jq and git on PATH",
)


@pytest.fixture()
def checkout(tmp_path_factory):
    """Make ``<name>/`` a git checkout scaffolded with the Codex hooks; return
    a subdirectory of it, which is where Codex would start the hooks."""
    root = tmp_path_factory.mktemp("codex-checkouts").resolve()

    def make(name: str) -> Path:
        repo = root / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        res = CliRunner().invoke(init, [
            "--dir", str(repo), "--codex-hook", "--no-mcp", "--no-claudemd",
            "--no-slash", "--no-prompts", "--no-skill",
        ])
        assert res.exit_code == 0, res.output
        sub = repo / "src" / "pkg"
        sub.mkdir(parents=True)
        assert sub == Path(os.path.realpath(sub)), "a symlinked cwd is refused by /controls/check"
        return sub

    return make


def _run_registered(event: str, cwd: Path, payload: dict, api_url: str, **env) -> str:
    """Run the command .codex/hooks.json registers for *event*; return additionalContext."""
    registered = json.loads((cwd.parents[1] / ".codex" / "hooks.json").read_text())
    [group] = registered["hooks"][event]
    [handler] = group["hooks"]
    assert handler == CODEX_HOOKS_BLOCK["hooks"][event][0]["hooks"][0]
    proc = subprocess.run(
        ["/bin/sh", "-c", handler["command"]],
        cwd=cwd, input=json.dumps({**payload, "cwd": str(cwd)}),
        capture_output=True, text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd),
            "PALINODE_API_URL": api_url,
            "PALINODE_HOOK_START_TIMEOUT": live._GENEROUS_CURL_TIMEOUT_S,
            "PALINODE_HOOK_RECALL_TIMEOUT": live._GENEROUS_CURL_TIMEOUT_S,
            "PALINODE_HOOK_RESOLVE_DEADLINE": live._GENEROUS_DEADLINE_MS,
            "PALINODE_HOOK_RECALL_TRIGGERS": "0",
            **env,
        },
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return ""
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == event
    return out["hookSpecificOutput"]["additionalContext"]


# -- SessionStart: the scoped prime digest ----------------------------------


def test_codex_session_start_gets_its_projects_core_memories_never_anothers(
    two_projects, live_api, checkout,
):
    context = _run_registered("SessionStart", checkout("alpha"), CODEX_SESSION_START, live_api)
    assert start_live.ALPHA_ID in context
    assert start_live.GLOBAL_ID in context
    assert start_live.BETA_ID not in context


def test_codex_session_start_in_the_other_project(two_projects, live_api, checkout):
    context = _run_registered("SessionStart", checkout("beta"), CODEX_SESSION_START, live_api)
    assert start_live.BETA_ID in context and start_live.GLOBAL_ID in context
    assert start_live.ALPHA_ID not in context


def test_codex_session_start_in_neither_project_gets_only_the_global_one(
    two_projects, live_api, checkout,
):
    context = _run_registered("SessionStart", checkout("gamma"), CODEX_SESSION_START, live_api)
    assert start_live.GLOBAL_ID in context
    assert start_live.ALPHA_ID not in context and start_live.BETA_ID not in context


def test_codex_session_start_on_resume_injects_nothing(two_projects, live_api, checkout):
    context = _run_registered(
        "SessionStart", checkout("alpha"), {**CODEX_SESSION_START, "source": "resume"}, live_api)
    assert context == ""


# -- UserPromptSubmit: per-prompt resolution --------------------------------

_PROMPT = {**CODEX_USER_PROMPT_SUBMIT, "prompt": "which endpoint does production serve traffic from?"}


def test_codex_prompt_recall_delivers_the_projects_current_record(mem, live_api, checkout):
    scenarios.seed_current(mem)
    context = _run_registered("UserPromptSubmit", checkout("demo"), _PROMPT, live_api)
    assert "decisions/endpoint-v2" in context
    assert "Production serves traffic from endpoint bravo." in context
    assert "endpoint alpha" not in context, "the replaced wording reached the session"


def test_codex_prompt_recall_withholds_another_projects_record(mem, live_api, checkout):
    """Same store, same prompt, a session in another checkout. The global
    record that also answers is the control: it proves the hook resolved and
    delivered, so the missing record was withheld rather than never asked for."""
    scenarios.seed_current(mem)
    # Worded close to the prompt so its match is confident: the hook injects
    # nothing on a weak one, which would make this test pass vacuously.
    scenarios._write(
        mem, "decisions/endpoint-policy.md",
        "# Endpoint\n\nWhich endpoint does production serve traffic from: endpoint charlie.",
        type="Decision", status="active", date="2026-09-02", epistemic="fact",
    )
    context = _run_registered("UserPromptSubmit", checkout("home"), _PROMPT, live_api)
    assert "endpoint charlie" in context, context
    assert "endpoint bravo" not in context
    assert "decisions/endpoint-v2" not in context
    # Same store, same prompt, the record's own project: it is delivered there,
    # so its absence above is the scope's doing, not the ranking's.
    own = _run_registered("UserPromptSubmit", checkout("demo"), _PROMPT, live_api)
    assert "endpoint bravo" in own, own

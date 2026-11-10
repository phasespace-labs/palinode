"""The SessionStart hook against a live server: whose core memories a session gets.

A ``core: true`` memory is injected without anyone asking, so it is the one
record a project-scoped session must never receive from another project. The
hook used to discard the scoped ``/context/prime`` digest and inject the
unscoped ``GET /list?core_only=true`` instead, and every session in a shared
store opened with every project's core decisions, unlabeled. An evaluation with
several projects in one store found an agent writing another project's core
batch ID into its own config.

Real uvicorn on an ephemeral port, the real app, real SQLite and git under
``tmp_path``, real ``curl`` and ``jq``, the shipped script. The core plugin's
side is pinned in ``plugins/core/test/core.test.ts``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from palinode.cli.init import SESSION_START_HOOK_SCRIPT
from tests import test_resolve_bundle as scenarios
from tests import test_resolve_hook_live as live

mem = scenarios.mem
live_api = live.live_api

pytestmark = pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("jq")),
    reason="the hook needs curl and jq on PATH",
)

ALPHA_ID = "alphacorequill"
BETA_ID = "betacoremarrow"
GLOBAL_ID = "globalcorefennel"


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


def _session_start(tmp_path, api_url: str, **env) -> str:
    hook = tmp_path / "session-start.sh"
    hook.write_text(SESSION_START_HOOK_SCRIPT)
    proc = subprocess.run(
        ["/bin/bash", str(hook)],
        input=json.dumps({"session_id": "s1", "cwd": str(tmp_path), "source": "startup"}),
        capture_output=True, text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "PALINODE_API_URL": api_url,
            "PALINODE_HOOK_START_TIMEOUT": live._GENEROUS_CURL_TIMEOUT_S,
            **env,
        },
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return ""
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return out["hookSpecificOutput"]["additionalContext"]


def test_a_session_gets_its_own_and_global_core_memories_never_another_projects(
    two_projects, live_api, tmp_path,
):
    context = _session_start(tmp_path, live_api, PALINODE_PROJECT="alpha")
    assert ALPHA_ID in context
    assert GLOBAL_ID in context
    assert BETA_ID not in context


def test_the_other_project_gets_its_own(two_projects, live_api, tmp_path):
    context = _session_start(tmp_path, live_api, PALINODE_PROJECT="beta")
    assert BETA_ID in context and GLOBAL_ID in context
    assert ALPHA_ID not in context


def test_a_session_in_neither_project_gets_only_the_global_one(two_projects, live_api, tmp_path):
    """No PALINODE_PROJECT: the server resolves the project from the cwd."""
    context = _session_start(tmp_path, live_api)
    assert GLOBAL_ID in context
    assert ALPHA_ID not in context and BETA_ID not in context


def test_the_unscoped_listing_still_lists_both(two_projects, live_api):
    """``/list`` stays the unscoped browse surface: the fix is in what is injected."""
    out = subprocess.run(
        ["curl", "-s", "-f", f"{live_api}/list?core_only=true"],
        capture_output=True, text=True, check=True,
    )
    files = {row["file"] for row in json.loads(out.stdout)}
    assert {"decisions/alpha-batch.md", "decisions/beta-batch.md"} <= files

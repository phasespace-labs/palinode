"""
Cross-surface session-end timeout consistency tests.

All three surfaces that call POST /session-end (CLI, MCP, hook) must use the
same timeout budget defined in ``palinode.core.defaults.SESSION_END_TIMEOUT_SECONDS``.
These tests assert that:

  1. The constant exists and equals the sentinel (no accidental drift).
  2. The CLI ``_api.py`` module loads without assertion error (the module-level
     drift guard fires at import time if there is a mismatch).
  3. The MCP module loads without assertion error (same guard).
  4. The hook script sources the constant via PALINODE_HOOK_TIMEOUT env var
     and defaults to 30 seconds (the hook-side default chosen to leave head-
     room below the 35s Claude Code runner timeout).
  5. The settings.json hook runner timeout is strictly greater than the hook's
     default curl max-time.

No database, no Ollama, no real API server — these are import-time / static
source assertions. The import-time ones run in a subprocess so they never
reload or evict a ``palinode.*`` module in the test session (the session-end
test-isolation fix).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent


# ── 1–3. Import-time guards, run in a subprocess ─────────────────────────────
#
# These assert what a *fresh* import does under a controlled environment. The
# previous version got its fresh import by deleting ``palinode.cli._api`` and
# ``palinode.core.defaults`` from ``sys.modules`` and re-importing, which
# splits module identity for the rest of the session: ``palinode.cli.
# session_end`` still held the original ``api_client`` singleton while every
# later ``from palinode.cli._api import api_client`` produced a new one. The
# e2e fixture then swapped the HTTP client on the new singleton, the CLI
# command used the old one, and five integration tests failed with
# ``ECONNREFUSED`` whenever this file ran before them (the session-end
# test-isolation fix). A subprocess
# is the only fresh import that leaves the parent interpreter untouched.


def _fresh_import(code: str, *, env_override: str | None) -> subprocess.CompletedProcess:
    """Run *code* in a new interpreter with PALINODE_SESSION_END_TIMEOUT
    removed (``None``) or set to *env_override*. Quiet config logging so the
    only stderr worth reading is a traceback."""
    env = {k: v for k, v in os.environ.items() if k != "PALINODE_SESSION_END_TIMEOUT"}
    if env_override is not None:
        env["PALINODE_SESSION_END_TIMEOUT"] = env_override
    env.setdefault("PALINODE_LOG_LEVEL", "ERROR")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def test_session_end_timeout_constant_matches_sentinel():
    """SESSION_END_TIMEOUT_SECONDS must equal the sentinel when no env override."""
    proc = _fresh_import(
        "import palinode.core.defaults as d; "
        "print(d.SESSION_END_TIMEOUT_SECONDS, d._SESSION_END_TIMEOUT_SENTINEL)",
        env_override=None,
    )
    assert proc.returncode == 0, proc.stderr
    value, sentinel = proc.stdout.split()[-2:]
    assert value == sentinel, (
        f"Constant ({value}) != sentinel ({sentinel}); update defaults.py"
    )


def test_session_end_timeout_env_override():
    """PALINODE_SESSION_END_TIMEOUT env var overrides the default at import."""
    proc = _fresh_import(
        "import palinode.core.defaults as d; print(d.SESSION_END_TIMEOUT_SECONDS)",
        env_override="120",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split()[-1] == "120.0"


def test_cli_api_module_loads_without_drift_assertion():
    """palinode.cli._api loads cleanly — sentinel assertion does not fire."""
    proc = _fresh_import("import palinode.cli._api", env_override=None)
    assert proc.returncode == 0, f"cli/_api.py drift guard fired:\n{proc.stderr}"
    assert "AssertionError" not in proc.stderr


def test_mcp_module_loads_without_drift_assertion():
    """palinode.mcp loads cleanly — sentinel assertion does not fire."""
    proc = _fresh_import("import palinode.mcp", env_override=None)
    assert proc.returncode == 0, f"mcp.py drift guard fired:\n{proc.stderr}"
    assert "AssertionError" not in proc.stderr


# ── 4. Hook script default curl max-time ────────────────────────────────────


def test_hook_script_uses_hook_timeout_variable():
    """examples/hooks/palinode-session-end.sh must use ${HOOK_TIMEOUT} in curl."""
    hook = REPO_ROOT / "examples" / "hooks" / "palinode-session-end.sh"
    assert hook.exists(), f"Hook not found: {hook}"
    source = hook.read_text(encoding="utf-8")
    # Must set HOOK_TIMEOUT from env with a default
    assert re.search(r'HOOK_TIMEOUT=.*PALINODE_HOOK_TIMEOUT', source), (
        "Hook must set HOOK_TIMEOUT from PALINODE_HOOK_TIMEOUT env var"
    )
    # curl call must reference ${HOOK_TIMEOUT}
    assert "--max-time \"${HOOK_TIMEOUT}\"" in source or "--max-time ${HOOK_TIMEOUT}" in source, (
        "curl --max-time must use ${HOOK_TIMEOUT}, not a hardcoded literal"
    )


def test_hook_script_default_is_less_than_runner_timeout():
    """Hook curl default (30s) must be < Claude Code runner timeout (35s).

    This structural invariant ensures the curl exits before the hook runner
    kills it, giving the || true a chance to run.
    """
    hook = REPO_ROOT / "examples" / "hooks" / "palinode-session-end.sh"
    source = hook.read_text(encoding="utf-8")
    # Extract the default from HOOK_TIMEOUT="${PALINODE_HOOK_TIMEOUT:-N}"
    match = re.search(r'HOOK_TIMEOUT=.*:-(\d+)', source)
    assert match, "Could not find HOOK_TIMEOUT default in hook script"
    hook_default = int(match.group(1))

    settings = REPO_ROOT / "examples" / "hooks" / "settings.json"
    runner_timeout = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]

    assert hook_default < runner_timeout, (
        f"Hook curl default ({hook_default}s) must be < runner timeout ({runner_timeout}s) "
        "so curl exits cleanly before the runner kills it"
    )

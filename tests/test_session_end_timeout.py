"""
Cross-surface session-end timeout consistency tests (#377).

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
source assertions.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent


# ── 1. Constant integrity ────────────────────────────────────────────────────


def test_session_end_timeout_constant_matches_sentinel():
    """SESSION_END_TIMEOUT_SECONDS must equal the sentinel when no env override."""
    env_key = "PALINODE_SESSION_END_TIMEOUT"
    prior = os.environ.pop(env_key, None)
    try:
        # Force reimport without the override so we get the raw default.
        import palinode.core.defaults as d
        importlib.reload(d)
        assert d.SESSION_END_TIMEOUT_SECONDS == d._SESSION_END_TIMEOUT_SENTINEL, (
            f"Constant ({d.SESSION_END_TIMEOUT_SECONDS}) != sentinel "
            f"({d._SESSION_END_TIMEOUT_SENTINEL}); update defaults.py #377"
        )
    finally:
        if prior is not None:
            os.environ[env_key] = prior
        import palinode.core.defaults as d
        importlib.reload(d)


def test_session_end_timeout_env_override():
    """PALINODE_SESSION_END_TIMEOUT env var overrides the default at import."""
    env_key = "PALINODE_SESSION_END_TIMEOUT"
    prior = os.environ.get(env_key)
    os.environ[env_key] = "120"
    try:
        import palinode.core.defaults as d
        importlib.reload(d)
        assert d.SESSION_END_TIMEOUT_SECONDS == 120.0
    finally:
        if prior is None:
            del os.environ[env_key]
        else:
            os.environ[env_key] = prior
        import palinode.core.defaults as d
        importlib.reload(d)


# ── 2 & 3. Module-load drift guards ─────────────────────────────────────────


def test_cli_api_module_loads_without_drift_assertion():
    """palinode.cli._api loads cleanly — sentinel assertion does not fire."""
    # Remove from sys.modules to force fresh import; no env override → must match.
    env_key = "PALINODE_SESSION_END_TIMEOUT"
    prior = os.environ.pop(env_key, None)
    for mod in list(sys.modules):
        if "palinode.cli._api" in mod or "palinode.core.defaults" in mod:
            del sys.modules[mod]
    try:
        import palinode.cli._api  # noqa: F401 — import for side-effect check
    except AssertionError as e:
        raise AssertionError(f"cli/_api.py drift guard fired: {e}") from e
    finally:
        if prior is not None:
            os.environ[env_key] = prior
        for mod in list(sys.modules):
            if "palinode.cli._api" in mod or "palinode.core.defaults" in mod:
                del sys.modules[mod]


def test_mcp_module_loads_without_drift_assertion():
    """palinode.mcp loads cleanly — sentinel assertion does not fire."""
    env_key = "PALINODE_SESSION_END_TIMEOUT"
    prior = os.environ.pop(env_key, None)
    for mod in list(sys.modules):
        if "palinode.mcp" in mod or "palinode.core.defaults" in mod:
            del sys.modules[mod]
    try:
        import palinode.mcp  # noqa: F401
    except AssertionError as e:
        raise AssertionError(f"mcp.py drift guard fired: {e}") from e
    finally:
        if prior is not None:
            os.environ[env_key] = prior
        for mod in list(sys.modules):
            if "palinode.mcp" in mod or "palinode.core.defaults" in mod:
                del sys.modules[mod]


# ── 4. Hook script default curl max-time ────────────────────────────────────


def test_hook_script_uses_hook_timeout_variable():
    """examples/hooks/palinode-session-end.sh must use ${HOOK_TIMEOUT} in curl."""
    hook = REPO_ROOT / "examples" / "hooks" / "palinode-session-end.sh"
    assert hook.exists(), f"Hook not found: {hook}"
    source = hook.read_text()
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
    source = hook.read_text()
    # Extract the default from HOOK_TIMEOUT="${PALINODE_HOOK_TIMEOUT:-N}"
    match = re.search(r'HOOK_TIMEOUT=.*:-(\d+)', source)
    assert match, "Could not find HOOK_TIMEOUT default in hook script"
    hook_default = int(match.group(1))

    settings = REPO_ROOT / "examples" / "hooks" / "settings.json"
    runner_timeout = json.loads(settings.read_text())["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]

    assert hook_default < runner_timeout, (
        f"Hook curl default ({hook_default}s) must be < runner timeout ({runner_timeout}s) "
        "so curl exits cleanly before the runner kills it"
    )


# ── 5. Init.py mirrors canonical sources ────────────────────────────────────


def test_init_py_hook_mirrors_canonical_hook():
    """palinode/cli/init.py HOOK_SCRIPT must use ${HOOK_TIMEOUT} (not hardcoded)."""
    init_py = REPO_ROOT / "palinode" / "cli" / "init.py"
    source = init_py.read_text()
    # Find the HOOK_SCRIPT string literal block
    assert "PALINODE_HOOK_TIMEOUT" in source, (
        "init.py HOOK_SCRIPT must reference PALINODE_HOOK_TIMEOUT (#377)"
    )
    assert 'max-time "${HOOK_TIMEOUT}"' in source or "max-time ${HOOK_TIMEOUT}" in source, (
        "init.py HOOK_SCRIPT curl --max-time must use ${HOOK_TIMEOUT} (#377)"
    )


def test_init_py_settings_timeout_matches_canonical():
    """palinode/cli/init.py SETTINGS_HOOK_BLOCK timeout must match examples/hooks/settings.json."""
    settings = REPO_ROOT / "examples" / "hooks" / "settings.json"
    canonical_timeout = json.loads(settings.read_text())["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]

    # Extract from init.py via import
    sys.path.insert(0, str(REPO_ROOT))
    from palinode.cli.init import SETTINGS_HOOK_BLOCK
    init_timeout = SETTINGS_HOOK_BLOCK["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]

    assert init_timeout == canonical_timeout, (
        f"init.py SETTINGS_HOOK_BLOCK timeout ({init_timeout}) != "
        f"examples/hooks/settings.json timeout ({canonical_timeout}) — "
        "keep them in sync (#377)"
    )


def test_hook_script_byte_identical_to_canonical():
    """`palinode init` embeds the session-end hook as a string constant because
    an installed package cannot read examples/. Pin the embedded HOOK_SCRIPT
    byte-for-byte to examples/hooks/palinode-session-end.sh so the two can't
    silently drift — the #633 failure mode, where the embedded copy carried the
    /wrap-skip dedup gate, PALINODE_HOOK_DRYRUN, and the fallback-log-on-failure
    path while the manual-install examples copy carried none of them. Mirrors
    the session-start guard test_embedded_init_copy_matches_canonical_script.
    Edit the canonical examples file first, then mirror HOOK_SCRIPT."""
    from palinode.cli.init import HOOK_SCRIPT

    canonical = REPO_ROOT / "examples" / "hooks" / "palinode-session-end.sh"
    assert HOOK_SCRIPT == canonical.read_text(), (
        "palinode/cli/init.py HOOK_SCRIPT has drifted from "
        "examples/hooks/palinode-session-end.sh — re-sync them byte-for-byte."
    )

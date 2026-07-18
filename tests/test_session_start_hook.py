"""Tests for the SessionStart hook — `palinode-session-start.sh` (#261, ADR-012 Layer 3).

The hook does two fail-silent actions on Claude Code SessionStart:

  1. POST /context/prime — forward-compatible server-side context warming
     (a harmless 404 until the endpoint ships).
  2. GET /list?core_only=true — inject a bounded core-memory digest into the
     session via ``additionalContext`` (the #528 escalation: deterministic
     recall grounding that doesn't depend on the agent remembering to search).

These tests run the canonical script (examples/hooks/) through bash with a
stub ``curl`` on PATH that records invocations and serves a canned /list
response. A sync test pins the embedded ``palinode init`` copy to the
canonical file so the two can't drift.

Requires `jq` + `bash` (present on CI ubuntu runners and macOS).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "examples" / "hooks" / "palinode-session-start.sh"

pytestmark = pytest.mark.skipif(
    not HOOK.exists() or shutil.which("jq") is None or shutil.which("bash") is None,
    reason="hook script absent or jq/bash unavailable",
)

_CORE_LIST = [
    {"file": "decisions/adopt-rrf.md", "name": "adopt-rrf",
     "category": "decisions", "core": True,
     "summary": "Hybrid search fuses BM25 + vector with RRF.",
     "last_updated": "2026-07-01", "entities": [], "size_bytes": 512},
    {"file": "projects/palinode-status.md", "name": "palinode-status",
     "category": "projects", "core": True, "summary": "",
     "last_updated": "2026-06-20", "entities": [], "size_bytes": 256},
    {"file": "insights/embed-timeouts.md", "name": "embed-timeouts",
     "category": "insights", "core": True,
     "summary": "Cold Ollama needs 90s embed timeouts.",
     "last_updated": "2026-06-01", "entities": [], "size_bytes": 300},
]

# Stub curl: log every invocation, serve the canned /list body on GET /list,
# fail with exit 22 (curl -f style) when CURL_FAIL=1.
_STUB_CURL = """\
#!/bin/bash
echo "$@" >> "$STUB_DIR/curl-called"
[ "${CURL_FAIL:-0}" = "1" ] && exit 22
case "$@" in
  *"/list"*) cat "$STUB_DIR/list-response.json" ;;
esac
exit 0
"""


def _run_hook(tmp_path, *, env=None, source="startup", session_id="s1",
              core_list=_CORE_LIST):
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "curl").write_text(_STUB_CURL)
    (stub_dir / "curl").chmod(0o755)
    (stub_dir / "list-response.json").write_text(json.dumps(core_list))

    payload = json.dumps({
        "session_id": session_id,
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "source": source,
    })
    full_env = {
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "STUB_DIR": str(stub_dir),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "HOME": str(tmp_path),
    }
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["/bin/bash", str(HOOK)],
        input=payload, capture_output=True, text=True, env=full_env,
    )
    return proc, (stub_dir / "curl-called")


def _context_of(proc) -> str:
    """Parse the hook's stdout as SessionStart JSON and return additionalContext."""
    out = json.loads(proc.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    return hso["additionalContext"]


# ---- The two actions ----------------------------------------------------


def test_prime_posted_with_cwd_and_session(tmp_path):
    proc, curl_called = _run_hook(tmp_path)
    assert proc.returncode == 0, proc.stderr
    calls = curl_called.read_text()
    assert "/context/prime" in calls
    # Payload carries the cwd + session_id from the hook stdin.
    assert str(tmp_path) in calls
    assert "s1" in calls


def test_injects_core_digest(tmp_path):
    proc, _ = _run_hook(tmp_path)
    assert proc.returncode == 0, proc.stderr
    ctx = _context_of(proc)
    assert "Palinode memory" in ctx
    # Deterministic recall reminder — the doc-only recall guard, made structural.
    assert "palinode_search" in ctx
    # One line per core file: [file] name — summary (summary optional).
    assert "- [decisions/adopt-rrf.md] adopt-rrf — Hybrid search" in ctx
    assert "- [projects/palinode-status.md] palinode-status" in ctx
    assert "- [insights/embed-timeouts.md] embed-timeouts" in ctx


# ---- Fail-silent + gates ------------------------------------------------


def test_api_down_is_fail_silent(tmp_path):
    proc, curl_called = _run_hook(tmp_path, env={"CURL_FAIL": "1"})
    assert proc.returncode == 0, "hook must never block session start"
    assert proc.stdout.strip() == "", "no context injected when the API is down"
    assert curl_called.exists(), "curl should have been attempted"


def test_non_allowlisted_source_skips(tmp_path):
    proc, curl_called = _run_hook(tmp_path, source="resume")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert not curl_called.exists(), "resume is not in the default source allowlist"


def test_source_allowlist_env_override(tmp_path):
    proc, curl_called = _run_hook(
        tmp_path, source="compact",
        env={"PALINODE_HOOK_START_SOURCES": "startup clear compact"})
    assert proc.returncode == 0, proc.stderr
    assert curl_called.exists()
    assert "- [decisions/adopt-rrf.md]" in _context_of(proc)


def test_inject_disabled_is_prime_only(tmp_path):
    proc, curl_called = _run_hook(
        tmp_path, env={"PALINODE_HOOK_INJECT_MAX_FILES": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "MAX_FILES=0 must not inject"
    calls = curl_called.read_text()
    assert "/context/prime" in calls, "prime still fires in prime-only mode"
    assert "/list" not in calls, "core-list GET skipped when injection disabled"


def test_empty_core_list_injects_nothing(tmp_path):
    proc, _ = _run_hook(tmp_path, core_list=[])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_dryrun_touches_nothing(tmp_path):
    proc, curl_called = _run_hook(tmp_path, env={"PALINODE_HOOK_DRYRUN": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "DRYRUN" in proc.stdout
    assert not curl_called.exists(), "dry-run must not call the API"


# ---- Bounds -------------------------------------------------------------


def test_max_files_cap(tmp_path):
    proc, _ = _run_hook(tmp_path, env={"PALINODE_HOOK_INJECT_MAX_FILES": "2"})
    ctx = _context_of(proc)
    assert "adopt-rrf" in ctx and "palinode-status" in ctx
    assert "embed-timeouts" not in ctx, "third file must be dropped at MAX_FILES=2"


def test_max_chars_cap(tmp_path):
    proc, _ = _run_hook(tmp_path, env={"PALINODE_HOOK_INJECT_MAX_CHARS": "80"})
    ctx = _context_of(proc)
    assert len(ctx) <= 80


# ---- Auth ---------------------------------------------------------------


def test_bearer_token_sent_when_configured(tmp_path):
    proc, curl_called = _run_hook(
        tmp_path, env={"PALINODE_API_TOKEN": "sekrit-token"})
    assert proc.returncode == 0, proc.stderr
    calls = curl_called.read_text()
    assert "Authorization: Bearer sekrit-token" in calls


def test_no_auth_header_by_default(tmp_path):
    proc, curl_called = _run_hook(tmp_path)
    assert "Authorization" not in curl_called.read_text()


# ---- Drift guards -------------------------------------------------------


def test_embedded_init_copy_matches_canonical_script():
    """`palinode init` embeds the hook as a string constant (installed packages
    can't read examples/). This pins the two byte-for-byte so they can't drift."""
    from palinode.cli.init import SESSION_START_HOOK_SCRIPT

    assert SESSION_START_HOOK_SCRIPT == HOOK.read_text()


def test_examples_settings_registers_both_hooks():
    settings = json.loads((HOOK.parent / "settings.json").read_text())
    hooks = settings["hooks"]
    start_cmds = [h["command"] for e in hooks["SessionStart"] for h in e["hooks"]]
    end_cmds = [h["command"] for e in hooks["SessionEnd"] for h in e["hooks"]]
    assert any("palinode-session-start.sh" in c for c in start_cmds)
    assert any("palinode-session-end.sh" in c for c in end_cmds)
    # Hook-runner timeout must exceed the script's worst case (2 curls × 8 s).
    start_timeout = hooks["SessionStart"][0]["hooks"][0]["timeout"]
    assert start_timeout >= 17

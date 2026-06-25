"""Tests for the SessionEnd floor-capture hook prototype (#378).

The hook (the local dev-only session-end-floor script) is a prototype: when a
session ends without `/wrap`, it writes a lightweight floor
session-end so the session isn't lost. These tests exercise the GATES in
dry-run mode (no palinode call, no network) via subprocess:

  - meaningful session (>=2 user turns AND >=1 tool use, no /wrap) → fires
  - trivial session (1 turn / no tools)                            → skips
  - already-wrapped (transcript has palinode_session_end)          → skips
  - disabled (PALINODE_SESSION_FLOOR=0)                            → skips

Skipped entirely in the public repo (the dev-only `.claude/hooks/` is absent).
Requires `jq` + `bash` (present on CI ubuntu runners).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / ".claude" / "hooks" / "session-end-floor.sh"

pytestmark = pytest.mark.skipif(
    not HOOK.exists() or shutil.which("jq") is None or shutil.which("bash") is None,
    reason="dev-only hook absent (public repo) or jq/bash unavailable",
)

_MEANINGFUL = (
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"refactor the wrap bug"}]}}\n'
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Read","input":{}}]}}\n'
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"now PR it"}]}}\n'
)
_TRIVIAL = (
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"what is 2+2"}]}}\n'
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"4"}]}}\n'
)
_WRAPPED = (
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"/wrap"}]}}\n'
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"palinode_session_end","input":{}}]}}\n'
)


def _run(tmp_path: Path, transcript_body: str, extra_env: dict | None = None) -> str:
    t = tmp_path / "transcript.jsonl"
    t.write_text(transcript_body, encoding="utf-8")
    payload = (
        '{"transcript_path":"%s","session_id":"sid","cwd":"%s","reason":"clear"}'
        % (t, tmp_path)
    )
    env = {"PALINODE_SESSION_FLOOR_DRYRUN": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, f"hook must always exit 0; got {proc.returncode}: {proc.stderr}"
    return proc.stdout


def test_meaningful_session_fires(tmp_path):
    out = _run(tmp_path, _MEANINGFUL)
    assert "DRYRUN" in out and "session-end" in out
    assert "--trigger hook" in out          # floor captures are tagged
    assert "refactor the wrap bug" in out   # derived from the first user ask


def test_trivial_session_skipped(tmp_path):
    assert _run(tmp_path, _TRIVIAL).strip() == ""


def test_already_wrapped_skipped(tmp_path):
    assert _run(tmp_path, _WRAPPED).strip() == ""


def test_disabled_skipped(tmp_path):
    out = _run(tmp_path, _MEANINGFUL, extra_env={"PALINODE_SESSION_FLOOR": "0"})
    assert out.strip() == ""

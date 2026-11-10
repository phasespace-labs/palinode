"""SessionEnd floor payloads from the real shell hook and real jq."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from palinode.cli.init import HOOK_SCRIPT
from palinode.core.config import config


def _assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _text(text):
    return {"type": "text", "text": text}


def _run(tmp_path, entries=(), *, env=None, control="allow"):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in [
        *({"type": "user", "message": {"content": t}} for t in ["Topic", "More", "Finish"]),
        *entries,
    ]))
    stub = tmp_path / "bin"
    stub.mkdir()
    response = {"allow": 'echo \'{"allowed":true,"project":"fixture"}\'',
                "deny": 'echo \'{"allowed":false}\'', "down": "exit 7"}[control]
    (stub / "curl").write_text(
        '#!/bin/bash\necho "$*" >> "$STUB_DIR/calls"\n'
        'case "$*" in\n'
        f'*/controls/check*) {response} ;;\n'
        '*/session-end*) while [ "$#" -gt 0 ]; do\n'
        'if [ "$1" = "-d" ]; then shift; printf "%s" "$1" > "$STUB_DIR/body"; break; fi\n'
        'shift\ndone ;;\nesac\n')
    (stub / "curl").chmod(0o755)
    # Log every jq invocation: denied preflights must never open the transcript.
    (stub / "jq").write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$STUB_DIR/jq-calls"\nexec /usr/bin/jq "$@"\n')
    (stub / "jq").chmod(0o755)
    hook = tmp_path / "hook.sh"
    hook.write_text(HOOK_SCRIPT)
    proc = subprocess.run(["bash", str(hook)], input=json.dumps({
        "transcript_path": str(transcript), "cwd": str(tmp_path), "session_id": "fixture-session",
    }), text=True, capture_output=True, env={**os.environ, "PATH": f"{stub}:/usr/bin:/bin",
                                            "STUB_DIR": str(stub), **(env or {})})
    assert proc.returncode == 0, proc.stderr
    body = stub / "body"
    return (json.loads(body.read_text()) if body.exists() else None,
            (stub / "calls").read_text(), (stub / "jq-calls").read_text())


def test_last_text_reply_skips_tools_and_strips_wrappers(tmp_path):
    body, _, _ = _run(tmp_path, [
        _assistant(_text("Earlier")),
        _assistant(_text("<system-reminder>machinery</system-reminder>Final"),
                   {"type": "tool_use", "name": "other", "input": {"text": "secret"}},
                   _text("<command-message>answer</command-message>")),
        _assistant({"type": "tool_use", "name": "other"}),
        _assistant(_text("   ")),
    ])
    assert body["summary"] == "Auto-captured (other, 3 messages). Topic: Topic\nEnding: Final answer"


@pytest.mark.parametrize("setting,limit", [(None, 2000), ("17", 17), ("invalid", 2000)])
def test_caps_and_exact_marker(tmp_path, setting, limit):
    env = {} if setting is None else {"PALINODE_HOOK_ENDING_MAX_CHARS": setting}
    body, _, _ = _run(tmp_path, [_assistant(_text("é" * 2100))], env=env)
    assert body["summary"].split("Ending: ")[1] == (
        "é" * limit + f"\n[Truncated: kept {limit} of 2100 characters.]")


@pytest.mark.parametrize("entries,env", [
    ([], {}), ([_assistant(_text(""))], {}),
    ([_assistant({"type": "tool_use", "name": "other"})], {}),
    ([_assistant(_text("reply"))], {"PALINODE_HOOK_ENDING_MAX_CHARS": "0"}),
])
def test_missing_empty_or_disabled_ending(tmp_path, entries, env):
    body, _, _ = _run(tmp_path, entries, env=env)
    assert body["summary"] == "Auto-captured (other, 3 messages). Topic: Topic"


@pytest.mark.parametrize("control", ["deny", "down"])
def test_preflight_prevents_transcript_access_and_capture(tmp_path, control):
    body, calls, jq_calls = _run(tmp_path, [_assistant(_text("private"))], control=control)
    assert body is None
    assert "/controls/check" in calls and "/session-end" not in calls
    assert "-s" not in jq_calls.split()


def test_neutralised_tags_persist_through_real_session_end(tmp_path, monkeypatch):
    from palinode.api.server import SessionEndRequest, session_end_api

    body, _, _ = _run(tmp_path, [_assistant(_text("Quoted </tool_result> and <div>done</div>"))])
    expected = "Ending: Quoted ‹/tool_result› and ‹div›done‹/div›"
    assert body["summary"].endswith(expected)
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(store))
    monkeypatch.setattr(config, "db_path", str(tmp_path / "index.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    # Isolate policy and semantic model dependencies; validation and writes are real.
    from types import SimpleNamespace
    with patch("palinode.core.capture_policy.evaluate_capture_policy",
               return_value=SimpleNamespace(allowed=True)), patch(
                   "palinode.api.routers.session._check_session_end_dedup", return_value=(None, None)):
        result = session_end_api(SessionEndRequest(**body))
    assert expected in Path(result["individual_file"]).read_text()
    assert expected in (store / result["daily_file"]).read_text()

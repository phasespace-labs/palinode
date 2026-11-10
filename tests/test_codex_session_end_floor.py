"""The session-end floor on Codex, against a live server, run the way Codex runs it.

``palinode init --codex-hook`` registers ``palinode-session-end.sh`` for
Codex's ``SessionEnd``. Codex's payload names a rollout transcript, not a
Claude Code one, so the script rewrites the rollout into the shape it reads;
everything after that (the triviality gate, the ending cap, the capture
preflight, the project tag) is the code Claude Code runs.

The rollout below is trimmed from what codex-cli 0.165.0 wrote for a
prior-session prompt (``codex exec``, one user input, two tool calls, one
reply), with account identifiers dropped and session and turn IDs replaced.
Codex's SessionEnd payload carries
``session_id``, ``transcript_path``, ``cwd``, ``hook_event_name`` and
``reason`` ("other" for ``codex exec``) and no message text.

Real uvicorn, the real app, real SQLite and git under ``tmp_path``, real
``curl`` and ``jq``, the shipped script.
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

mem = scenarios.mem
live_api = live.live_api

pytestmark = pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("jq") and shutil.which("git")),
    reason="the hook needs curl, jq and git on PATH",
)

BATCH = "cedar-1111aaaa"
SESSION_ID = "33333333-cccc-7ccc-8ccc-cccccccccccc"
TURN_ID = "44444444-dddd-7ddd-8ddd-dddddddddddd"
PROMPT = (
    "We're starting the ledger migration in this repo. The data team assigned us "
    f"batch {BATCH}; that's the id we'll use. Don't change any files today — read "
    "migrate/ and summarise in two sentences what needs doing."
)
REPLY = (
    "`migrate/` contains only `config.py`, where `BATCH_ID` is currently `None`; next "
    f"session, set it to the data-team-assigned ID `{BATCH}`. No files were changed today."
)


def _rollout(*, user_text: str = PROMPT, tool_calls: int = 2, extra: list[dict] | None = None) -> list[dict]:
    turn = TURN_ID
    lines: list[dict] = [
        {"type": "session_meta", "payload": {"id": SESSION_ID,
                                             "cli_version": "0.165.0", "originator": "codex_exec"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        # Codex's own context blocks arrive as user-role response items, never
        # as UserMessage items, so they are not mistaken for the ask.
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/w</cwd>\n</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": user_text}]}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": user_text, "text_elements": []}]}}},
    ]
    for n in range(tool_calls):
        lines += [
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                  "call_id": f"call_{n}", "arguments": "{\"cmd\":\"ls migrate\"}"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": f"call_{n}",
                                                  "output": "config.py"}},
        ]
    lines += extra or []
    lines += [
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": {
            "type": "AgentMessage", "content": [{"type": "Text", "text": REPLY}], "phase": "final_answer"}}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": REPLY}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn, "last_agent_message": REPLY}},
    ]
    return lines


@pytest.fixture()
def codex_checkout(tmp_path_factory):
    """A git checkout named after its project, scaffolded with ``--codex-hook``."""
    root = tmp_path_factory.mktemp("codex-floor").resolve()
    repo = root / "ledgerwork-a1b2c3"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    res = CliRunner().invoke(init, [
        "--dir", str(repo), "--codex-hook", "--no-mcp", "--no-claudemd",
        "--no-slash", "--no-prompts", "--no-skill",
    ])
    assert res.exit_code == 0, res.output
    assert repo == Path(os.path.realpath(repo))
    return repo


def _session_end(repo: Path, transcript: Path, api_url: str) -> subprocess.CompletedProcess:
    """Run the command ``.codex/hooks.json`` registers for SessionEnd, as Codex does."""
    registered = json.loads((repo / ".codex" / "hooks.json").read_text())
    [group] = registered["hooks"]["SessionEnd"]
    [handler] = group["hooks"]
    assert handler == CODEX_HOOKS_BLOCK["hooks"]["SessionEnd"][0]["hooks"][0]
    payload = {"session_id": SESSION_ID, "transcript_path": str(transcript),
               "cwd": str(repo), "hook_event_name": "SessionEnd", "reason": "other"}
    proc = subprocess.run(
        ["/bin/sh", "-c", handler["command"]], cwd=repo, input=json.dumps(payload),
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
             "PALINODE_API_URL": api_url, "PALINODE_HOOK_TIMEOUT": live._GENEROUS_CURL_TIMEOUT_S},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _write_rollout(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def _daily(mem: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((mem / "daily").glob("*.md")))


def test_a_codex_session_that_never_saved_reaches_the_store(mem, live_api, codex_checkout, tmp_path):
    rollout = _write_rollout(tmp_path / "codex-home" / "sessions" / "rollout.jsonl", _rollout())
    _session_end(codex_checkout, rollout, live_api)
    daily = _daily(mem)
    assert BATCH in daily
    assert "Topic: We're starting the ledger migration" in daily
    assert "<environment_context>" not in daily  # Codex's context block is not the topic
    assert "Ending: `migrate/` contains only `config.py`" in daily
    assert "**Project:** project/ledgerwork-a1b2c3" in daily
    assert "Auto-captured (other, 3 messages)" in daily  # 1 input + 2 tool outputs, as for Claude Code


def test_a_codex_session_that_ran_session_end_itself_is_not_captured_twice(
    mem, live_api, codex_checkout, tmp_path
):
    explicit = [{"type": "response_item", "payload": {
        "type": "function_call", "name": "mcp__palinode__palinode_session_end", "call_id": "call_se",
        "arguments": "{}"}}]
    rollout = _write_rollout(tmp_path / "rollout.jsonl", _rollout(extra=explicit))
    _session_end(codex_checkout, rollout, live_api)
    assert BATCH not in _daily(mem)


def test_session_end_called_from_code_mode_is_not_captured_twice(mem, live_api, codex_checkout, tmp_path):
    """In code mode the call is an ``exec`` script; only the McpToolCall item names the tool.

    Shape from a codex-cli 0.165.0 rollout (``gpt-6-luna``): the
    ``custom_tool_call`` is named ``exec`` and its ``item_completed`` event
    carries ``{type: McpToolCall, server, tool}`` with no ``invocation``.
    """
    turn = TURN_ID
    explicit = [
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "status": "completed", "call_id": "call_se", "name": "exec",
            "input": "const a=await tools.mcp__palinode__palinode_session_end({project:\"ledgerwork-a1b2c3\"});\n"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": {
            "type": "McpToolCall", "id": "exec-1", "server": "palinode", "tool": "palinode_session_end",
            "arguments": {"project": "ledgerwork-a1b2c3", "summary": "Read migrate/."}}}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "call_se",
            "output": [{"type": "input_text", "text": "Script completed"}]}},
    ]
    rollout = _write_rollout(tmp_path / "rollout.jsonl", _rollout(extra=explicit))
    _session_end(codex_checkout, rollout, live_api)
    assert BATCH not in _daily(mem)


def test_another_mcp_tool_from_code_mode_does_not_skip_the_floor(mem, live_api, codex_checkout, tmp_path):
    turn = TURN_ID
    other = [{"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": {
        "type": "McpToolCall", "id": "exec-2", "server": "palinode", "tool": "palinode_search",
        "arguments": {"query": "ledger"}}}}]
    rollout = _write_rollout(tmp_path / "rollout.jsonl", _rollout(extra=other))
    _session_end(codex_checkout, rollout, live_api)
    assert BATCH in _daily(mem)


def test_a_trivial_codex_session_is_skipped(mem, live_api, codex_checkout, tmp_path):
    rollout = _write_rollout(tmp_path / "rollout.jsonl", _rollout(tool_calls=0))
    _session_end(codex_checkout, rollout, live_api)
    assert BATCH not in _daily(mem)


def test_a_paused_store_gets_nothing_from_codex(mem, live_api, codex_checkout, tmp_path):
    subprocess.run(["curl", "-sS", "-f", "-X", "POST", f"{live_api}/controls", "-H",
                    "Content-Type: application/json", "-d", json.dumps({"capture_paused": True})],
                   check=True, capture_output=True, timeout=30)
    rollout = _write_rollout(tmp_path / "rollout.jsonl", _rollout())
    _session_end(codex_checkout, rollout, live_api)
    assert BATCH not in _daily(mem)

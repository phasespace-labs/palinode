"""PostCompact checkpoint hook: Claude Code's compaction summary, saved per session.

The hook runs with a stub ``curl`` on PATH (real ``jq``), so these tests pin the
wiring without an API: the capture preflight comes first and fails closed, and
the save payload is one ProjectSnapshot per session, marked as a model's
inference. The last tests send the hook's own payload through the real save
route into a temporary store, to show that a second compaction replaces the
session's checkpoint rather than adding one.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from palinode.cli.init import (
    POST_COMPACT_HOOK_SCRIPT,
    SETTINGS_HOOK_BLOCK,
    _merge_settings,
)
from palinode.core.config import config

SESSION = "55555555-eeee-4eee-8eee-eeeeeeeeeeee"
SUMMARY = "Shipped the ranker fix. The reviewer approved merging the PR. Next: run the smoke tests."


def _run(tmp_path, *, summary=SUMMARY, session_id=SESSION, trigger="auto",
         env=None, control="allow"):
    """Run the hook with a stub curl. Returns (proc, calls, save_payload)."""
    hook = tmp_path / "hook.sh"
    hook.write_text(POST_COMPACT_HOOK_SCRIPT, encoding="utf-8")
    stub = tmp_path / "stub"
    stub.mkdir()
    responses = {
        "allow": 'echo \'{"allowed":true,"project":"fixture-project"}\'',
        "allow-no-project": 'echo \'{"allowed":true}\'',
        "deny": 'echo \'{"allowed":false,"reason":"capture_paused"}\'',
        "down": "exit 7",
    }
    (stub / "curl").write_text(
        "#!/bin/bash\n"
        'echo "$*" >> "$STUB_DIR/calls"\n'
        'case "$*" in\n'
        f'  *"/controls/check"*) {responses[control]} ;;\n'
        '  *"/save"*) cat > "$STUB_DIR/save.json" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (stub / "curl").chmod(0o755)
    payload = {"session_id": session_id, "transcript_path": str(tmp_path / "t.jsonl"),
               "cwd": str(tmp_path), "hook_event_name": "PostCompact",
               "trigger": trigger, "compact_summary": summary}
    full_env = {"PATH": f"{stub}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                "STUB_DIR": str(stub), "HOME": str(tmp_path)}
    full_env.update(env or {})
    proc = subprocess.run(["/bin/bash", str(hook)], input=json.dumps(payload),
                          capture_output=True, text=True, env=full_env)
    calls = (stub / "calls").read_text() if (stub / "calls").exists() else ""
    saved = stub / "save.json"
    return proc, calls, (json.loads(saved.read_text()) if saved.exists() else None)


# ── The payload ──────────────────────────────────────────────────────────────


def test_saves_one_project_snapshot_per_session(tmp_path):
    proc, calls, body = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert calls.index("/controls/check") < calls.index("/save"), calls
    assert body["type"] == "ProjectSnapshot"
    assert body["slug"] == f"compaction-checkpoint-{SESSION}"
    assert body["project"] == "fixture-project"
    assert body["epistemic"] == "inference"
    assert body["source"] == "claude-code-compaction"
    assert body["metadata"]["session_id"] == SESSION
    assert body["metadata"]["compaction_trigger"] == "auto"
    assert body["metadata"]["harness"] == "claude-code"
    assert "core" not in body


def test_the_summary_sits_under_a_header_that_scopes_its_approvals(tmp_path):
    """A recalled checkpoint must not read as standing permission."""
    _proc, _calls, body = _run(tmp_path)
    header, _, rest = body["content"].partition("\n\n---\n\n")
    assert rest == SUMMARY
    assert "Nobody reviewed it" in header
    assert "applied to that session only" in header
    assert "not standing permission" in header
    assert f"`{SESSION}`" in header


def test_a_manual_compaction_is_recorded_as_manual(tmp_path):
    _proc, _calls, body = _run(tmp_path, trigger="manual")
    assert body["metadata"]["compaction_trigger"] == "manual"
    assert "(manual, " in body["content"]


def test_no_project_from_the_preflight_sends_none(tmp_path):
    _proc, _calls, body = _run(tmp_path, control="allow-no-project")
    assert "project" not in body


def test_a_long_summary_is_cut_with_a_marker(tmp_path):
    _proc, _calls, body = _run(tmp_path, summary="x" * 500,
                               env={"PALINODE_HOOK_COMPACT_MAX_CHARS": "100"})
    rest = body["content"].partition("\n\n---\n\n")[2]
    assert rest.startswith("x" * 100)
    assert "x" * 101 not in rest
    assert rest.endswith("[Truncated: kept 100 of 500 characters.]")


def test_a_non_numeric_cap_falls_back_to_the_default(tmp_path):
    _proc, _calls, body = _run(tmp_path, summary="y" * 500,
                               env={"PALINODE_HOOK_COMPACT_MAX_CHARS": "lots"})
    assert body["content"].endswith("y" * 500)


def test_summary_text_is_carried_verbatim(tmp_path):
    """Quotes, dollars and backslashes never pass through shell quoting."""
    tricky = 'It\'s "done" — $HOME `ls` \\n and a literal \\ backslash'
    _proc, _calls, body = _run(tmp_path, summary=tricky)
    assert body["content"].partition("\n\n---\n\n")[2] == tricky


# ── When it sends nothing ────────────────────────────────────────────────────


@pytest.mark.parametrize("control", ["deny", "down"])
def test_a_denied_or_unreachable_preflight_sends_nothing(tmp_path, control):
    proc, calls, body = _run(tmp_path, control=control)
    assert proc.returncode == 0, proc.stderr
    assert "/controls/check" in calls
    assert "/save" not in calls
    assert body is None


@pytest.mark.parametrize("kwargs", [{"summary": ""}, {"session_id": ""}])
def test_nothing_to_name_or_keep_makes_no_call_at_all(tmp_path, kwargs):
    proc, calls, _body = _run(tmp_path, **kwargs)
    assert proc.returncode == 0, proc.stderr
    assert calls == ""


def test_dry_run_prints_the_payload_and_saves_nothing(tmp_path):
    proc, calls, body = _run(tmp_path, env={"PALINODE_HOOK_DRYRUN": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "DRYRUN" in proc.stdout
    assert "/save" not in calls and body is None
    printed = json.loads(proc.stdout.split("\n", 1)[1])
    assert printed["slug"] == f"compaction-checkpoint-{SESSION}"


# ── Registration ─────────────────────────────────────────────────────────────


def test_init_registers_the_hook_on_post_compact_synchronously(tmp_path):
    """Claude Code cancels an async hook, so the registration must not be async."""
    settings = tmp_path / "settings.json"
    _merge_settings(settings, force=False)
    entries = json.loads(settings.read_text())["hooks"]["PostCompact"]
    hooks = [h for e in entries for h in e["hooks"]]
    assert any(h["command"].endswith("/palinode-post-compact.sh") for h in hooks)
    assert not any(h.get("async") for h in hooks)


def test_init_rerun_makes_an_async_registration_synchronous(tmp_path):
    """A 0.24.0 install registered PostCompact async; re-running init repairs only that handler."""
    settings = tmp_path / "settings.json"
    other = {"type": "command", "command": "my-own-compact-hook.sh", "async": True}
    settings.write_text(json.dumps({"hooks": {"PostCompact": [
        {"hooks": [{"type": "command",
                    "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/palinode-post-compact.sh",
                    "timeout": 35, "async": True}]},
        {"hooks": [other]},
    ]}}), encoding="utf-8")
    assert _merge_settings(settings, force=False) == "merged"
    entries = json.loads(settings.read_text())["hooks"]["PostCompact"]
    handlers = [h for e in entries for h in e["hooks"]]
    palinode = [h for h in handlers if h["command"].endswith("/palinode-post-compact.sh")]
    assert len(palinode) == 1 and "async" not in palinode[0] and palinode[0]["timeout"] == 35
    assert other in handlers  # someone else's async hook is left alone


def test_example_settings_match_init_registration():
    canonical = json.loads(
        (Path(__file__).parent.parent / "examples" / "hooks" / "settings.json")
        .read_text(encoding="utf-8")
    )
    assert canonical["hooks"]["PostCompact"] == SETTINGS_HOOK_BLOCK["hooks"]["PostCompact"]


def test_hook_timeout_exceeds_the_curl_timeout():
    entry = SETTINGS_HOOK_BLOCK["hooks"]["PostCompact"][0]["hooks"][0]
    assert entry["timeout"] > 30


# ── Through the real save route ──────────────────────────────────────────────


@pytest.fixture
def memory_dir(tmp_path):
    old_dir, old_commit, old_db = config.memory_dir, config.git.auto_commit, config.db_path
    config.memory_dir = str(tmp_path / "store")
    config.git.auto_commit = False
    config.db_path = str(tmp_path / ".palinode.db")
    os.makedirs(config.memory_dir)
    try:
        yield Path(config.memory_dir)
    finally:
        config.memory_dir, config.git.auto_commit, config.db_path = old_dir, old_commit, old_db


def _save_through_api(body):
    from palinode.api.server import SaveRequest, save_api

    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        return save_api(SaveRequest(**body))


def test_a_second_compaction_replaces_the_sessions_checkpoint(tmp_path, memory_dir):
    from palinode.core.parser import parse_frontmatter

    # Each hook run needs its own directory for the script and the curl stub.
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    _p, _c, first = _run(tmp_path / "first", summary="First summary.")
    _save_through_api(first)
    _p, _c, second = _run(tmp_path / "second", summary="Second summary.", trigger="manual")
    _save_through_api(second)

    files = sorted(memory_dir.rglob("compaction-checkpoint-*.md"))
    assert [f.name for f in files] == [f"compaction-checkpoint-{SESSION}.md"]
    meta, body = parse_frontmatter(files[0].read_text(encoding="utf-8"))
    assert "Second summary." in body and "First summary." not in body
    assert meta["type"] == "ProjectSnapshot"
    assert meta["epistemic"] == "inference"
    assert meta["source"] == "claude-code-compaction"
    assert meta["session_id"] == SESSION
    assert meta["compaction_trigger"] == "manual"
    assert not meta.get("core")


def test_checkpoint_writes_no_daily_note_or_status_line(tmp_path, memory_dir):
    _p, _c, body = _run(tmp_path, summary="A summary.")
    _save_through_api(body)
    assert not (memory_dir / "daily").exists() or not any((memory_dir / "daily").iterdir())
    assert not list(memory_dir.rglob("*-status.md"))


#: The shape Claude Code writes: an analysis block, then the summary block, each
#: wrapped in whole-line tags, so the content ends with ``</summary>``.
CLAUDE_SHAPED = (
    "<analysis>\nChronological walk-through of the session.\n</analysis>\n\n"
    "<summary>\n1. Primary request: ship the ranker fix.\n"
    "2. Pending: run the smoke tests.\n</summary>"
)


def test_claude_code_wrapper_tags_become_headings(tmp_path):
    _proc, _calls, body = _run(tmp_path, summary=CLAUDE_SHAPED)
    kept = body["content"].partition("\n\n---\n\n")[2]
    assert "<summary>" not in kept and "</summary>" not in kept and "<analysis>" not in kept
    assert "## Analysis\nChronological walk-through" in kept
    assert "## Summary\n1. Primary request: ship the ranker fix." in kept


def test_a_claude_shaped_summary_passes_the_save_routes_envelope_guard(tmp_path, memory_dir):
    """A trailing </summary> was refused as tool-envelope markup (a 400) before."""
    _p, _c, body = _run(tmp_path, summary=CLAUDE_SHAPED)
    _save_through_api(body)
    assert [f.name for f in memory_dir.rglob("compaction-checkpoint-*.md")] == [
        f"compaction-checkpoint-{SESSION}.md"
    ]

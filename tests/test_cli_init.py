"""Tests for `palinode init` — the zero-friction scaffolding command.

These are regression guards for two things:

1. The deterministic slash commands (`/ps` and `/wrap`). If someone refactors
   `init.py` and accidentally reintroduces smart-dispatch, these tests fail.
2. The idempotent install flow — re-running init must not corrupt existing
   files, and merging into existing JSON must not stomp unrelated keys.
"""
import json
from pathlib import Path

from click.testing import CliRunner

import subprocess
import tempfile

from palinode.cli import main
from palinode.cli.init import (
    HOOK_SCRIPT,
    PS_COMMAND_BODY,
    WRAP_COMMAND_BODY,
    _slugify,
)


# ---- Slug ---------------------------------------------------------------


def test_slugify_basic():
    assert _slugify("my-project") == "my-project"
    assert _slugify("My Project") == "my-project"
    assert _slugify("My Project!") == "my-project"
    assert _slugify("palinode") == "palinode"
    assert _slugify("foo_bar.baz") == "foo_bar-baz"


def test_slugify_falls_back_to_project():
    assert _slugify("") == "project"
    assert _slugify("!!!") == "project"


# ---- Deterministic prompt guards ----------------------------------------


def test_ps_command_is_deterministic():
    """/ps must always call palinode_save with type=ProjectSnapshot, never session_end."""
    body = PS_COMMAND_BODY
    assert "palinode_save" in body
    assert '"ProjectSnapshot"' in body
    assert "This command is deterministic" in body
    assert "Do not call any other tool" in body
    # Must NOT contain smart dispatch instructions
    assert "palinode_session_end" not in body or "use `/wrap`" in body
    assert "Pick the right tool" not in body


def test_wrap_command_is_deterministic():
    """/wrap must call palinode_push then palinode_session_end, never palinode_save.

    Since #353, /wrap is a two-step deterministic command: Step 1 push,
    Step 2 session-end.  The old "Do not call any other tool" invariant no
    longer holds — /wrap deliberately calls two tools in order.
    """
    body = WRAP_COMMAND_BODY
    assert "palinode_session_end" in body
    assert "palinode_push" in body, "wrap command must include palinode_push step (#353)"
    assert "summary" in body
    assert "decisions" in body
    assert "blockers" in body
    assert "This command is deterministic" in body
    # Must tell the agent what to say after saving
    assert "safe to /clear now" in body
    # Push must precede session-end
    assert body.find("palinode_push") < body.find("palinode_session_end"), (
        "palinode_push must appear before palinode_session_end (#353)"
    )
    # Must NOT dispatch to palinode_save
    assert "palinode_save" not in body or "use `/ps`" in body


def test_ps_and_wrap_are_different():
    """The two commands must be distinct operations, not aliases."""
    assert PS_COMMAND_BODY != WRAP_COMMAND_BODY


# ---- Hook script slurp-based extraction (#151, #267, mirrors #257) -------


def test_hook_script_uses_slurp_extraction():
    """Both MSG_COUNT and FIRST_PROMPT must use `jq -s` (slurp) extraction.
    The earlier piped patterns (`jq | grep -c '.'` and `jq | head -1 | cut`)
    were fragile under `set -o pipefail`: downstream early-exit triggers
    SIGPIPE on jq. #151 patched MSG_COUNT with `|| true`; #267 + #257 moved
    both to slurp, which has no early-exit downstream consumer and thus
    no SIGPIPE class to swallow. Guard against regression."""
    # Old fragile patterns must be absent
    assert "grep -c '.' || true" not in HOOK_SCRIPT
    assert "head -1 | cut -c1-200)" not in HOOK_SCRIPT
    assert "head -1 | cut -c1-200 || true" not in HOOK_SCRIPT
    # New slurp patterns must be present
    assert "jq -r -s 'map(select(.type == \"user\") | .message.content // empty) | length'" in HOOK_SCRIPT
    assert "jq -r -s 'map(select(.type == \"user\") | .message.content // empty) | .[0] // \"\"'" in HOOK_SCRIPT
    # Safe default for MSG_COUNT must remain
    assert "MSG_COUNT=${MSG_COUNT:-0}" in HOOK_SCRIPT


def test_hook_script_drops_empty_transcript(tmp_path):
    """Empty transcript ⇒ MSG_COUNT=0 ⇒ filter drops, no save attempted.
    Exercises the slurp extraction directly to catch shell-quoting regressions."""
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("")
    snippet = (
        f'set -euo pipefail; '
        f'TRANSCRIPT_PATH={transcript}; '
        f'MSG_COUNT=$(jq -r -s \'map(select(.type == "user") | .message.content // empty) | length\' '
        f'  "$TRANSCRIPT_PATH" 2>/dev/null || echo 0); '
        f'MSG_COUNT=${{MSG_COUNT:-0}}; '
        f'echo "result=$MSG_COUNT"; '
        f'[ "$MSG_COUNT" -lt 3 ] && echo "drops" || echo "saves"'
    )
    proc = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "result=0" in proc.stdout
    assert "drops" in proc.stdout


def test_hook_script_counts_user_messages_correctly(tmp_path):
    """Five user messages mixed with assistant lines ⇒ count=5 ⇒ saves."""
    transcript = tmp_path / "mixed.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"hi"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        '{"type":"user","message":{"content":"how"}}\n'
        '{"type":"user","message":{"content":"are"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"fine"}]}}\n'
        '{"type":"user","message":{"content":"you"}}\n'
        '{"type":"user","message":{"content":"?"}}\n'
    )
    snippet = (
        f'set -euo pipefail; '
        f'TRANSCRIPT_PATH={transcript}; '
        f'MSG_COUNT=$(jq -r -s \'map(select(.type == "user") | .message.content // empty) | length\' '
        f'  "$TRANSCRIPT_PATH" 2>/dev/null || echo 0); '
        f'MSG_COUNT=${{MSG_COUNT:-0}}; '
        f'echo "result=$MSG_COUNT"'
    )
    proc = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "result=5" in proc.stdout


def test_hook_script_first_prompt_extracts_correctly(tmp_path):
    """Multi-message transcript ⇒ FIRST_PROMPT extracts the FIRST user message
    and survives `set -euo pipefail` cleanly (no SIGPIPE because slurp has
    no early-exit downstream consumer). Regression guard for #267."""
    transcript = tmp_path / "multi.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"first message — must surface"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"reply"}]}}\n'
        '{"type":"user","message":{"content":"second"}}\n'
        '{"type":"user","message":{"content":"third"}}\n'
        '{"type":"user","message":{"content":"fourth"}}\n'
        '{"type":"user","message":{"content":"fifth"}}\n'
    )
    snippet = (
        f'set -euo pipefail; '
        f'TRANSCRIPT_PATH={transcript}; '
        f'FIRST_PROMPT=$(jq -r -s \'map(select(.type == "user") | .message.content // empty) | .[0] // ""\' '
        f'  "$TRANSCRIPT_PATH" 2>/dev/null | cut -c1-200); '
        f'echo "result=$FIRST_PROMPT"'
    )
    proc = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "result=first message — must surface" in proc.stdout


# ---- Hook script reason filter (#149) -----------------------------------


def test_hook_script_has_reason_case_guard():
    """The script must filter SessionEnd by reason — settings.json carries
    no `matcher`, so this script-side guard is the only thing keeping
    unwanted reasons (e.g. `resume`, `bypass_permissions_disabled`) from
    triggering captures. If this assertion breaks because someone removed
    the guard, you have re-introduced #149."""
    assert "ALLOWED_REASONS=" in HOOK_SCRIPT
    assert "PALINODE_HOOK_REASONS:-" in HOOK_SCRIPT
    # case-statement word-boundary match on space-padded allowlist
    assert 'case " $ALLOWED_REASONS " in' in HOOK_SCRIPT
    assert '*" $SOURCE_REASON "*' in HOOK_SCRIPT


def test_hook_script_default_reason_allowlist_is_broad():
    """Default allowlist captures clear, logout, exit, and normal exits.
    Skips `resume` (old session content typically already saved before resume)
    and `bypass_permissions_disabled` (state change, not lifecycle end). If
    you tighten or broaden the default, update both this assertion and the
    rationale captured in #149's resolution comment."""
    # The default value should appear verbatim in the script
    assert ":-clear logout prompt_input_exit other}" in HOOK_SCRIPT
    # And NOT include the two we deliberately skip
    assert ":-clear logout prompt_input_exit other resume" not in HOOK_SCRIPT
    assert "bypass_permissions_disabled}" not in HOOK_SCRIPT


# ---- Scaffolding flow ---------------------------------------------------


def test_init_creates_all_files(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "hooks" / "palinode-session-end.sh").exists()
    assert (tmp_path / ".claude" / "commands" / "ps.md").exists()
    assert (tmp_path / ".claude" / "commands" / "wrap.md").exists()
    assert (tmp_path / ".mcp.json").exists()


def test_init_uses_directory_name_as_slug(tmp_path: Path):
    proj = tmp_path / "my-awesome-project"
    proj.mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(proj)])
    assert result.exit_code == 0

    content = (proj / ".claude" / "CLAUDE.md").read_text()
    assert "my-awesome-project" in content


def test_init_explicit_project_slug_wins(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "init", "--dir", str(tmp_path), "--project", "custom-slug",
    ])
    assert result.exit_code == 0

    content = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert "custom-slug" in content


def test_init_dry_run_writes_nothing(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output

    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_init_is_idempotent(tmp_path: Path):
    runner = CliRunner()
    first = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert first.exit_code == 0

    ps_content = (tmp_path / ".claude" / "commands" / "ps.md").read_text()
    settings_content = (tmp_path / ".claude" / "settings.json").read_text()

    second = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert second.exit_code == 0
    assert "skipped" in second.output

    # Files unchanged
    assert (tmp_path / ".claude" / "commands" / "ps.md").read_text() == ps_content
    assert (tmp_path / ".claude" / "settings.json").read_text() == settings_content


def test_init_appends_to_existing_claude_md(tmp_path: Path):
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# Pre-existing header\n\nSome project rules here.\n")

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-hook", "--no-mcp", "--no-slash"])
    assert result.exit_code == 0

    content = claude_md.read_text()
    assert "# Pre-existing header" in content
    assert "Some project rules here." in content
    assert "## Memory (Palinode)" in content


def test_init_merges_into_existing_settings_json(tmp_path: Path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "other.sh"}]}]},
        "unrelated_key": "should_survive",
    }, indent=2))

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-claudemd", "--no-mcp", "--no-slash"])
    assert result.exit_code == 0

    merged = json.loads(settings.read_text())
    assert merged["unrelated_key"] == "should_survive"
    assert "PreToolUse" in merged["hooks"]
    assert "SessionEnd" in merged["hooks"]
    assert len(merged["hooks"]["SessionEnd"]) == 1


def test_init_scope_flags(tmp_path: Path):
    """--no-claudemd --no-hook --no-mcp should only write slash commands."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "init", "--dir", str(tmp_path),
        "--no-claudemd", "--no-hook", "--no-mcp",
    ])
    assert result.exit_code == 0

    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "hooks").exists()
    assert not (tmp_path / ".mcp.json").exists()
    assert (tmp_path / ".claude" / "commands" / "ps.md").exists()
    assert (tmp_path / ".claude" / "commands" / "wrap.md").exists()

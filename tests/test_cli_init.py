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
    # Must NOT dispatch to palinode_save (a pointer to the tool for
    # mid-session checkpoints is fine; the /save //ps commands are removed)
    assert "palinode_save" not in body or "mid-session checkpoint" in body


# Hook script slurp-based extraction (mirrors) -------


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


# Hook script reason filter -----------------------------------


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


# Hook script floor gates (dedup / dry-run / fallback) ----------


def _run_hook(tmp_path, transcript_text, *, env=None, reason="clear",
              session_id="s1"):
    """Render HOOK_SCRIPT to disk and run it with a stub PATH (curl + jq).

    Returns (CompletedProcess, fallback_path). The stub `curl` records its
    invocation to ``$STUB_DIR/curl-called`` and honours ``CURL_FAIL=1`` to
    simulate an API failure so the fallback path can be exercised.
    """
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(transcript_text)

    hook = tmp_path / "hook.sh"
    hook.write_text(HOOK_SCRIPT)

    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    # Stub curl: record the call; fail if CURL_FAIL=1 (exit 22, like curl -f 4xx).
    (stub_dir / "curl").write_text(
        '#!/bin/bash\n'
        'echo "$@" >> "$STUB_DIR/curl-called"\n'
        '[ "${CURL_FAIL:-0}" = "1" ] && exit 22\n'
        'exit 0\n'
    )
    (stub_dir / "curl").chmod(0o755)

    real_jq = subprocess.run(["command", "-v", "jq"], capture_output=True,
                             text=True, executable="/bin/bash").stdout.strip()
    payload = json.dumps({
        "transcript_path": str(transcript),
        "session_id": session_id,
        "cwd": str(tmp_path),
        "reason": reason,
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
        ["/bin/bash", str(hook)],
        input=payload, capture_output=True, text=True, env=full_env,
    )
    fallback = tmp_path / ".claude" / "session-floor-fallback.jsonl"
    return proc, fallback, (stub_dir / "curl-called")


_NONTRIVIAL = (
    '{"type":"user","message":{"content":"first ask"}}\n'
    '{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\n'
    '{"type":"user","message":{"content":"second"}}\n'
    '{"type":"user","message":{"content":"third"}}\n'
)


def test_hook_skips_when_wrap_already_ran(tmp_path):
    """If the transcript holds a palinode_session_end call, the floor is
    redundant — skip (no curl POST)."""
    transcript = _NONTRIVIAL + (
        '{"type":"assistant","message":{"content":'
        '[{"type":"tool_use","name":"palinode_session_end","input":{}}]}}\n'
    )
    proc, _fallback, curl_called = _run_hook(tmp_path, transcript)
    assert proc.returncode == 0, proc.stderr
    assert not curl_called.exists(), "curl POST fired despite /wrap having run"


def test_hook_force_overrides_wrap_dedup(tmp_path):
    """PALINODE_HOOK_FORCE=1 captures even when /wrap ran."""
    transcript = _NONTRIVIAL + (
        '{"type":"assistant","message":{"content":'
        '[{"type":"tool_use","name":"palinode_session_end","input":{}}]}}\n'
    )
    proc, _fallback, curl_called = _run_hook(
        tmp_path, transcript, env={"PALINODE_HOOK_FORCE": "1"})
    assert proc.returncode == 0, proc.stderr
    assert curl_called.exists(), "FORCE=1 should capture regardless of /wrap"


def test_hook_dryrun_writes_nothing(tmp_path):
    """Dry-run prints the payload and never POSTs."""
    proc, _fallback, curl_called = _run_hook(
        tmp_path, _NONTRIVIAL, env={"PALINODE_HOOK_DRYRUN": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "DRYRUN" in proc.stdout
    assert not curl_called.exists(), "dry-run must not POST"


def test_hook_fallback_log_on_api_failure(tmp_path):
    """When the POST fails, the capture is appended to the fallback log so it
    isn't lost."""
    proc, fallback, curl_called = _run_hook(
        tmp_path, _NONTRIVIAL, env={"CURL_FAIL": "1"})
    assert proc.returncode == 0, proc.stderr
    assert curl_called.exists(), "curl should have been attempted"
    assert fallback.exists(), "failed POST must route to the fallback log"
    line = json.loads(fallback.read_text().strip())
    assert "summary" in line and "project" in line


def test_hook_happy_path_posts_once_no_fallback(tmp_path):
    """A non-trivial session with a healthy API POSTs once and writes no
    fallback."""
    proc, fallback, curl_called = _run_hook(tmp_path, _NONTRIVIAL)
    assert proc.returncode == 0, proc.stderr
    assert curl_called.exists()
    assert "/session-end" in curl_called.read_text()
    assert not fallback.exists()


# ---- Scaffolding flow ---------------------------------------------------


def test_init_creates_all_files(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "hooks" / "palinode-session-end.sh").exists()
    assert (tmp_path / ".claude" / "hooks" / "palinode-session-start.sh").exists()
    assert (tmp_path / ".claude" / "commands" / "wrap.md").exists()
    assert (tmp_path / ".mcp.json").exists()
    # /wrap is the sole scaffolded lifecycle command (save/ps deprecated)
    assert not (tmp_path / ".claude" / "commands" / "save.md").exists()
    assert not (tmp_path / ".claude" / "commands" / "ps.md").exists()


def test_init_settings_include_worktree_allow_rules(tmp_path: Path):
    """Scaffolded settings.json pre-approves the git-worktree cleanup commands so
    agents don't hit the auto-mode permission classifier reclaiming stale
    worktrees (#448)."""
    from palinode.cli.init import WORKTREE_ALLOW_RULES

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    allow = settings.get("permissions", {}).get("allow", [])
    for rule in WORKTREE_ALLOW_RULES:
        assert rule in allow, f"missing worktree allow-rule: {rule}"


def test_init_merge_adds_allow_rules_without_duplicating(tmp_path: Path):
    """Re-running init merges the allow-rules into an existing settings.json
    exactly once (idempotent), and preserves unrelated existing content."""
    from palinode.cli.init import WORKTREE_ALLOW_RULES

    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)", WORKTREE_ALLOW_RULES[0]]},
        "env": {"FOO": "bar"},
    }))

    runner = CliRunner()
    runner.invoke(main, ["init", "--dir", str(tmp_path)])
    runner.invoke(main, ["init", "--dir", str(tmp_path)])  # twice — must stay idempotent

    settings = json.loads(settings_path.read_text())
    allow = settings["permissions"]["allow"]
    assert allow.count(WORKTREE_ALLOW_RULES[0]) == 1, "allow-rule must not duplicate"
    for rule in WORKTREE_ALLOW_RULES:
        assert rule in allow
    assert "Bash(ls:*)" in allow, "existing allow-rules preserved"
    assert settings["env"] == {"FOO": "bar"}, "unrelated settings preserved"


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

    wrap_content = (tmp_path / ".claude" / "commands" / "wrap.md").read_text()
    settings_content = (tmp_path / ".claude" / "settings.json").read_text()

    second = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert second.exit_code == 0
    assert "skipped" in second.output

    # Files unchanged
    assert (tmp_path / ".claude" / "commands" / "wrap.md").read_text() == wrap_content
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
    assert "SessionStart" in merged["hooks"]
    assert len(merged["hooks"]["SessionStart"]) == 1


def test_init_registers_both_hooks_idempotently(tmp_path: Path):
    """A double init registers SessionStart + SessionEnd exactly once each
    (#261: the session-start hook rides the same settings merge as session-end)."""
    runner = CliRunner()
    runner.invoke(main, ["init", "--dir", str(tmp_path)])
    runner.invoke(main, ["init", "--dir", str(tmp_path)])

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    for event, script in (
        ("SessionStart", "palinode-session-start.sh"),
        ("SessionEnd", "palinode-session-end.sh"),
    ):
        cmds = [
            h["command"]
            for entry in settings["hooks"][event]
            for h in entry["hooks"]
        ]
        assert sum(script in c for c in cmds) == 1, f"{event} must register exactly once"


def test_init_upgrades_sessionend_only_settings(tmp_path: Path):
    """A settings.json scaffolded by a pre-#261 init (SessionEnd only) gains the
    SessionStart registration on re-run without duplicating SessionEnd."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {"SessionEnd": [{"hooks": [{
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/palinode-session-end.sh",
            "timeout": 35,
        }]}]},
    }, indent=2))

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    settings = json.loads(settings_path.read_text())
    assert len(settings["hooks"]["SessionEnd"]) == 1, "SessionEnd must not duplicate"
    assert len(settings["hooks"]["SessionStart"]) == 1, "SessionStart must be added"


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
    assert (tmp_path / ".claude" / "commands" / "wrap.md").exists()


# ---- /save //ps deprecation guards ---------------------------------------


def test_init_never_scaffolds_save_or_ps(tmp_path: Path):
    """#631: /wrap is the sole lifecycle command. Neither the command nor the
    skill form of /save //ps may be written, in any scope."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "init", "--dir", str(tmp_path), "--skills", "project",
    ])
    assert result.exit_code == 0, result.output

    for name in ("save", "ps"):
        assert not (tmp_path / ".claude" / "commands" / f"{name}.md").exists()
        assert not (tmp_path / ".claude" / "skills" / name).exists()
    assert (tmp_path / ".claude" / "commands" / "wrap.md").exists()
    assert (tmp_path / ".claude" / "skills" / "wrap" / "SKILL.md").exists()


def test_init_preserves_existing_save_ps_installs(tmp_path: Path):
    """Deprecation is forward-only: a re-run must not delete or rewrite
    save.md/ps.md that an older init already installed."""
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "save.md").write_text("legacy save body\n")
    (cmds / "ps.md").write_text("legacy ps body\n")

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert (cmds / "save.md").read_text() == "legacy save body\n"
    assert (cmds / "ps.md").read_text() == "legacy ps body\n"


# ---- ADR-012 Layer 1: AGENTS.md + .cursor/rules scaffolding -----------------


_FIXTURES = Path(__file__).parent / "fixtures"


def test_claude_md_block_byte_identical_to_golden():
    """The harness-neutral block split must not change CLAUDE.md output — the
    rendered block (light and heavy wrap policy) is pinned byte-for-byte
    against fixtures captured from the pre-split monolithic constant."""
    from palinode.cli.init import CLAUDE_MD_BLOCK, WRAP_POLICY_HEAVY_NOTE

    light = CLAUDE_MD_BLOCK.format(project_slug="sample-project", wrap_policy_note="")
    heavy = CLAUDE_MD_BLOCK.format(
        project_slug="sample-project", wrap_policy_note=WRAP_POLICY_HEAVY_NOTE
    )
    assert light == (_FIXTURES / "claude_md_memory_block_light.txt").read_text()
    assert heavy == (_FIXTURES / "claude_md_memory_block_heavy.txt").read_text()


def test_memory_block_core_is_harness_neutral():
    """The shared core must carry the full memory contract but none of the
    Claude-Code-only machinery — Codex/Antigravity/Cursor have no /clear,
    /wrap, or SessionEnd hook."""
    from palinode.cli.init import MEMORY_BLOCK_CORE

    core = MEMORY_BLOCK_CORE.format(project_slug="sample-project")
    for required in (
        "## Memory (Palinode)",
        "### At session start",
        "### During work",
        "### At session end",
        "### What NOT to save",
        "### Project slug",
        "palinode_session_end",
        "sample-project",
    ):
        assert required in core, f"core lost required section: {required!r}"
    for claude_only in ("/wrap", "/clear", "/save", "/ps", "hook", "SessionEnd"):
        assert claude_only not in core, f"Claude-ism leaked into core: {claude_only!r}"
    assert core.count("## Memory (Palinode)") == 1


def test_init_default_skips_agents_and_cursor(tmp_path: Path):
    """No harness footprint → no AGENTS.md, no .cursor/ (detection default off)."""
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".cursor").exists()


def test_init_detects_existing_agents_md(tmp_path: Path):
    """A pre-existing AGENTS.md turns the writer on; user content is preserved
    and the block is appended after it."""
    original = "# My agents\n\nProject-specific agent instructions.\n"
    (tmp_path / "AGENTS.md").write_text(original)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.startswith(original)
    assert text.count("## Memory (Palinode)") == 1


def test_init_detects_agent_dir(tmp_path: Path):
    """A .agent/ directory (Antigravity footprint) also turns the writer on."""
    (tmp_path / ".agent").mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "## Memory (Palinode)" in (tmp_path / "AGENTS.md").read_text()


def test_init_detects_cursor_dir(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    rules = tmp_path / ".cursor" / "rules" / "palinode.md"
    assert "## Memory (Palinode)" in rules.read_text()


def test_init_explicit_flags_force_without_detection(tmp_path: Path):
    """--agents / --cursor write even when no harness footprint exists."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["init", "--dir", str(tmp_path), "--agents", "--cursor"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".cursor" / "rules" / "palinode.md").exists()


def test_init_no_flags_skip_despite_detection(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("# agents\n")
    (tmp_path / ".cursor").mkdir()
    runner = CliRunner()
    result = runner.invoke(
        main, ["init", "--dir", str(tmp_path), "--no-agents", "--no-cursor"]
    )
    assert result.exit_code == 0, result.output
    assert "## Memory (Palinode)" not in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / ".cursor" / "rules" / "palinode.md").exists()


def test_init_agents_and_cursor_are_idempotent(tmp_path: Path):
    """Re-running init leaves exactly one memory section in each file."""
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(
            main, ["init", "--dir", str(tmp_path), "--agents", "--cursor"]
        )
        assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").read_text().count("## Memory (Palinode)") == 1
    rules = tmp_path / ".cursor" / "rules" / "palinode.md"
    assert rules.read_text().count("## Memory (Palinode)") == 1


def test_init_dry_run_lists_detected_harness_files(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("# agents\n")
    (tmp_path / ".cursor").mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "AGENTS.md" in result.output
    assert ".cursor/rules/palinode.md" in result.output
    # dry-run writes nothing
    assert "## Memory (Palinode)" not in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / ".cursor" / "rules").exists()


# ---- ADR-012 Layer 2: palinode-session skill scaffolding --------------------


def test_session_skill_constant_matches_canonical_file():
    """The embedded PALINODE_SESSION_SKILL must stay byte-for-byte identical to
    the canonical skill/palinode-session/SKILL.md (edit the canonical file
    first, then mirror — this guard catches one-sided edits)."""
    from palinode.cli.init import PALINODE_SESSION_SKILL

    canonical = (
        Path(__file__).parent.parent / "skill" / "palinode-session" / "SKILL.md"
    ).read_text()
    assert PALINODE_SESSION_SKILL == canonical


def test_init_installs_session_skill_by_default(tmp_path: Path):
    from palinode.cli.init import PALINODE_SESSION_SKILL

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    installed = tmp_path / ".claude" / "skills" / "palinode-session" / "SKILL.md"
    assert installed.read_text() == PALINODE_SESSION_SKILL


def test_init_session_skill_into_detected_harness_paths(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".agent").mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for root in (".claude", ".cursor", ".agent"):
        assert (tmp_path / root / "skills" / "palinode-session" / "SKILL.md").exists(), (
            f"missing session skill under {root}/skills/"
        )


def test_init_user_flag_installs_per_user_not_project(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "proj"
    project.mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(project), "--user"])
    assert result.exit_code == 0, result.output
    assert (home / ".claude" / "skills" / "palinode-session" / "SKILL.md").exists()
    assert not (project / ".claude" / "skills" / "palinode-session").exists()


def test_init_no_skill_skips(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-skill"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".claude" / "skills" / "palinode-session").exists()


def test_init_skill_path_overrides_detection(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()
    custom = tmp_path / "custom-skills"
    runner = CliRunner()
    result = runner.invoke(
        main, ["init", "--dir", str(tmp_path), "--skill-path", str(custom)]
    )
    assert result.exit_code == 0, result.output
    assert (custom / "palinode-session" / "SKILL.md").exists()
    assert not (tmp_path / ".claude" / "skills" / "palinode-session").exists()
    assert not (tmp_path / ".cursor" / "skills").exists()


def test_init_session_skill_preserves_customization(tmp_path: Path):
    """Re-running init never overwrites a customized skill without --force."""
    runner = CliRunner()
    assert runner.invoke(main, ["init", "--dir", str(tmp_path)]).exit_code == 0
    installed = tmp_path / ".claude" / "skills" / "palinode-session" / "SKILL.md"
    installed.write_text("# my customized skill\n")
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert installed.read_text() == "# my customized skill\n"
    assert "skipped" in result.output


def test_init_session_skill_never_clobbers_symlink(tmp_path: Path):
    """A symlinked SKILL.md is curated externally — untouched even by --force."""
    curated = tmp_path / "curated-source.md"
    curated.write_text("# curated by an external repo\n")
    skill_dir = tmp_path / ".claude" / "skills" / "palinode-session"
    skill_dir.mkdir(parents=True)
    link = skill_dir / "SKILL.md"
    link.symlink_to(curated)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--force"])
    assert result.exit_code == 0, result.output
    assert link.is_symlink()
    assert curated.read_text() == "# curated by an external repo\n"
    assert "symlink" in result.output


def test_init_dry_run_lists_session_skill(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "palinode-session" in result.output
    assert not (tmp_path / ".claude" / "skills").exists()

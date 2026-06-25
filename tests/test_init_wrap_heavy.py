"""Tests for Sprint 4 (#419): heavy `/wrap` variant as a per-repo init policy.

Coverage:
- WRAP_HEAVY_COMMAND_BODY encodes the four-step sequence (merge → push →
  triage → session_end) with halt-on-failure semantics
- `palinode init` default (light) scaffolds the light wrap.md unchanged, and
  does NOT add a wrap-policy line to CLAUDE.md
- `palinode init --wrap-policy heavy` scaffolds the heavy wrap.md AND records
  `wrap-policy: heavy` in the CLAUDE.md memory block
- dry-run reports the chosen policy
- --force / idempotency behave like the other slash commands
"""
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.init import (
    WRAP_COMMAND_BODY,
    WRAP_HEAVY_COMMAND_BODY,
    WRAP_POLICY_HEAVY_NOTE,
)


def run_init(tmp_path: Path, *extra_args: str):
    """Invoke ``palinode init --dir <tmp_path> <extra_args>`` and return result."""
    runner = CliRunner()
    return runner.invoke(main, ["init", "--dir", str(tmp_path), *extra_args])


# ---------------------------------------------------------------------------
# Template constant guards — the heavy body encodes the #419 contract
# ---------------------------------------------------------------------------

def test_heavy_body_declares_policy():
    """The heavy body must self-identify as wrap-policy: heavy."""
    assert "wrap-policy: heavy" in WRAP_HEAVY_COMMAND_BODY


def test_heavy_body_has_four_step_sequence():
    """Heavy body must encode merge → push → triage → session_end, in order."""
    body = WRAP_HEAVY_COMMAND_BODY
    i_merge = body.index("Step 1 — Merge")
    i_push = body.index("Step 2 — Push")
    i_triage = body.index("Step 3 — Triage")
    i_end = body.index("Step 4")
    assert i_merge < i_push < i_triage < i_end
    # session_end is the final action
    assert "palinode_session_end" in body


def test_heavy_body_has_halt_on_failure_semantics():
    """Heavy body must instruct halting on merge/push failure, not skipping."""
    assert "halt" in WRAP_HEAVY_COMMAND_BODY.lower()


def test_heavy_body_references_triage_hierarchy():
    """Step 3 must route into the four-destination hierarchy."""
    body = WRAP_HEAVY_COMMAND_BODY
    assert "papercut" in body
    assert "triage" in body.lower()
    assert "palinode_save" in body


def test_heavy_body_never_force_pushes_by_default():
    """Push step must explicitly forbid force-push by default."""
    assert "force-push" in WRAP_HEAVY_COMMAND_BODY.lower()


def test_heavy_body_handles_palinode_unreachable():
    """session_end step must degrade gracefully if Palinode is down."""
    assert "unreachable" in WRAP_HEAVY_COMMAND_BODY.lower()


def test_light_and_heavy_bodies_differ():
    """The two bodies must be distinct templates."""
    assert WRAP_HEAVY_COMMAND_BODY != WRAP_COMMAND_BODY
    # Light body does not auto-merge
    assert "Step 1 — Merge" not in WRAP_COMMAND_BODY


def test_heavy_merge_step_handles_non_github_remote():
    """#440: the merge step must gracefully skip (not halt) on a non-GitHub
    remote (e.g. Gitea), and must name the non-`gh` filing path."""
    body = WRAP_HEAVY_COMMAND_BODY
    # graceful-skip language for the gh-can't-see-this-host case
    assert "known GitHub host" in body
    assert "skip this step" in body.lower()
    # Gitea / non-gh tooling is named
    assert "Gitea" in body
    assert "tea" in body  # the Gitea CLI


def test_heavy_push_step_lists_and_can_stop_before_pushing():
    """#440: the push step must state the all-or-nothing assumption and
    stop-and-ask on not-ready commits rather than pushing blind."""
    body = WRAP_HEAVY_COMMAND_BODY
    assert "all-or-nothing" in body or "all** unpushed" in body
    assert "@{u}..HEAD" in body  # lists what would push
    assert "stop-and-ask" in body


# ---------------------------------------------------------------------------
# Default (light) scaffold — unchanged behaviour, no wrap-policy line
# ---------------------------------------------------------------------------

def test_default_scaffolds_light_wrap(tmp_path: Path):
    """Default init writes the light wrap.md (no merge step)."""
    result = run_init(tmp_path)
    assert result.exit_code == 0, result.output
    wrap = (tmp_path / ".claude" / "commands" / "wrap.md").read_text()
    assert "Step 1 — Merge" not in wrap
    assert "palinode_push" in wrap


def test_default_does_not_add_wrap_policy_to_claude_md(tmp_path: Path):
    """Light (default) must NOT add a wrap-policy line to CLAUDE.md."""
    run_init(tmp_path)
    claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert "wrap-policy" not in claude_md


# ---------------------------------------------------------------------------
# --wrap-policy heavy — heavy wrap.md + CLAUDE.md record
# ---------------------------------------------------------------------------

def test_heavy_scaffolds_heavy_wrap(tmp_path: Path):
    """--wrap-policy heavy writes the heavy wrap.md (merge step present)."""
    result = run_init(tmp_path, "--wrap-policy", "heavy")
    assert result.exit_code == 0, result.output
    wrap = (tmp_path / ".claude" / "commands" / "wrap.md").read_text()
    assert "Step 1 — Merge" in wrap
    assert "palinode_session_end" in wrap


def test_heavy_records_policy_in_claude_md(tmp_path: Path):
    """--wrap-policy heavy must record the policy in the CLAUDE.md block."""
    run_init(tmp_path, "--wrap-policy", "heavy")
    claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert "wrap-policy: heavy" in claude_md


def test_heavy_note_only_appears_when_heavy(tmp_path: Path):
    """The WRAP_POLICY_HEAVY_NOTE text appears only under heavy policy."""
    run_init(tmp_path, "--wrap-policy", "heavy")
    claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert WRAP_POLICY_HEAVY_NOTE.strip() in claude_md


# ---------------------------------------------------------------------------
# Output / dry-run reports the policy
# ---------------------------------------------------------------------------

def test_output_reports_heavy_policy(tmp_path: Path):
    """init output must surface which wrap policy was scaffolded."""
    result = run_init(tmp_path, "--wrap-policy", "heavy")
    assert "heavy" in result.output


def test_dry_run_reports_policy(tmp_path: Path):
    """--dry-run output must mention the wrap policy and write nothing."""
    result = run_init(tmp_path, "--wrap-policy", "heavy", "--dry-run")
    assert "heavy" in result.output
    assert not (tmp_path / ".claude" / "commands" / "wrap.md").exists()


def test_invalid_policy_rejected(tmp_path: Path):
    """An unknown --wrap-policy value is a usage error."""
    result = run_init(tmp_path, "--wrap-policy", "medium")
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Idempotency / --force, matching the other slash commands
# ---------------------------------------------------------------------------

def test_idempotent_wrap_md_skipped_on_second_run(tmp_path: Path):
    """A second heavy run must not clobber a user-edited wrap.md."""
    run_init(tmp_path, "--wrap-policy", "heavy")
    wrap = tmp_path / ".claude" / "commands" / "wrap.md"
    edited = wrap.read_text() + "\n# user edit\n"
    wrap.write_text(edited)
    run_init(tmp_path, "--wrap-policy", "heavy")
    assert wrap.read_text() == edited


def test_force_overwrites_wrap_md_with_heavy(tmp_path: Path):
    """--force re-scaffolds wrap.md with the chosen policy body."""
    run_init(tmp_path)  # light first
    wrap = tmp_path / ".claude" / "commands" / "wrap.md"
    assert "Step 1 — Merge" not in wrap.read_text()
    run_init(tmp_path, "--wrap-policy", "heavy", "--force")
    assert "Step 1 — Merge" in wrap.read_text()

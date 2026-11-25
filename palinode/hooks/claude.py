"""Claude Code's hook spec: the settings cascade (ADR-023)."""
from __future__ import annotations

from pathlib import Path

from palinode.hooks.spec import Command, ConfigFile, HarnessSpec, Layout, Registration

HOOKS_DIR = ".claude/hooks"
QUOTED_PROJECT_DIR = '"${CLAUDE_PROJECT_DIR}"'
CHANGE_QUOTED = "quoted ${CLAUDE_PROJECT_DIR}"

REGISTRATIONS = (
    Registration("SessionStart", "palinode-session-start.sh", 20, since="0.9.3"),
    # v0.7.1-v0.8.10 registered a 15s budget, and the deployment guide's
    # user-level example carried it.
    Registration("SessionEnd", "palinode-session-end.sh", 35, since="0.7.1",
                 legacy_fields=((15, False),)),
    # Per-turn implicit recall (ADR-019). additionalContext lands in the
    # conversation, not the system prompt, so it is cache-safe by construction.
    # The timeout must exceed the script's worst case: check-triggers + two
    # reads + search, each capped at 4s per curl.
    Registration("UserPromptSubmit", "palinode-user-prompt-submit.sh", 20, since="0.13.0"),
    # The compaction checkpoint. Synchronous on purpose: Claude Code cancels an
    # async hook, so an async registration almost never saved a checkpoint.
    # v0.24.0 registered it async.
    Registration("PostCompact", "palinode-post-compact.sh", 35, since="0.24.0",
                 legacy_fields=((35, True),)),
)


def _commands(reg: Registration, project_dir: Path) -> tuple[Command, ...]:
    current = f"{QUOTED_PROJECT_DIR}/{HOOKS_DIR}/{reg.script}"
    return (
        # What `palinode init --hook` writes.
        Command(current),
        # The documented user-level install, in ~/.claude/settings.json.
        Command(f"~/{HOOKS_DIR}/{reg.script}"),
        # What init wrote before 0.24.1: unquoted, so /bin/sh split it at a
        # space in the project path and the hook exited 127.
        Command(
            f"${{CLAUDE_PROJECT_DIR}}/{HOOKS_DIR}/{reg.script}",
            current=False, upgrade=current, change=CHANGE_QUOTED,
        ),
    )


def _home_dir(home: Path, project_dir: Path) -> Path:
    return home / ".claude" / "hooks"


def _project_dir(home: Path, project_dir: Path) -> Path:
    return project_dir / HOOKS_DIR


CLAUDE = HarnessSpec(
    name="claude",
    hooks_dir=HOOKS_DIR,
    registrations=REGISTRATIONS,
    layout=Layout("nested"),
    config_files=(
        # A registration added at user level uses the documented ~/ form.
        ConfigFile("user", lambda h, p: h / ".claude" / "settings.json", _home_dir, render_form=1),
        ConfigFile("project", lambda h, p: p / ".claude" / "settings.json", _project_dir,
                   render_form=0, init_writes=True),
        ConfigFile("project_local", lambda h, p: p / ".claude" / "settings.local.json", _project_dir,
                   render_form=0),
    ),
    command_forms=_commands,
    project_config=".claude/settings.json",
)

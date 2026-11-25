"""Cursor's hook spec: the project's ``.cursor/hooks.json`` (ADR-023).

The adapter runs the same session-start and per-turn recall scripts Claude
Code does, translating Cursor's payload and answer, so the recall logic is not
forked per harness. Cursor runs project hooks from the project root, so the
registered commands are relative to it.
"""
from __future__ import annotations

from pathlib import Path

from palinode.hooks.spec import Command, ConfigFile, HarnessSpec, Layout, Registration

HOOKS_DIR = ".cursor/hooks"
ADAPTER = "palinode-cursor-hook.sh"

REGISTRATIONS = (
    # Same budget as the Claude Code registrations of the same scripts.
    Registration("sessionStart", "palinode-session-start.sh", 20, since="0.24.0"),
    Registration("beforeSubmitPrompt", "palinode-user-prompt-submit.sh", 20, since="0.24.0"),
)


def _commands(reg: Registration, project_dir: Path) -> tuple[Command, ...]:
    return (Command(f"{HOOKS_DIR}/{ADAPTER} {HOOKS_DIR}/{reg.script}"),)


CURSOR = HarnessSpec(
    name="cursor",
    hooks_dir=HOOKS_DIR,
    registrations=REGISTRATIONS,
    layout=Layout("flat"),
    config_files=(
        # Not walked by `palinode hooks update` or doctor: init never writes
        # there, and its relative commands resolve from ~/.cursor/, not from the
        # project. `palinode controls status` still reports it.
        ConfigFile(
            "user", lambda h, p: h / ".cursor" / "hooks.json",
            lambda h, p: h / ".cursor" / "hooks", inspected=False,
        ),
        ConfigFile("project", lambda h, p: p / ".cursor" / "hooks.json", lambda h, p: p / HOOKS_DIR,
                   render_form=0, init_writes=True),
    ),
    command_forms=_commands,
    typed=False,
    adapter=ADAPTER,
    project_config=".cursor/hooks.json",
)

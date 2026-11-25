"""Antigravity's hook spec: the project's ``.agents/hooks.json`` (ADR-023).

Same approach as Cursor: an adapter runs the session-start and per-turn recall
scripts unchanged and translates Antigravity's payload and ``injectSteps``
answer. The file maps hook names to their events; palinode's handlers live
under :data:`HOOK_NAME`.

Commands use absolute paths, because Antigravity runs workspace hooks with the
cwd set to ``.agents/`` and does not document that, so a relative path would
depend on an unpromised detail. In a checkout that has moved, a command still
names the old path: it is not a form this spec renders for the new project,
and ``palinode init --agy-hooks`` re-registers it.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from palinode.hooks.spec import Command, ConfigFile, HarnessSpec, Layout, Registration

HOOKS_DIR = ".agents/hooks"
ADAPTER = "palinode-agy-hook.sh"
#: The hook name palinode's entries live under in .agents/hooks.json.
HOOK_NAME = "palinode"

REGISTRATIONS = (
    # Same budget as the Claude Code registrations of the same scripts.
    Registration("SessionStart", "palinode-session-start.sh", 20, since="0.24.0"),
    Registration("PreInvocation", "palinode-user-prompt-submit.sh", 20, since="0.24.0"),
)


def _commands(reg: Registration, project_dir: Path) -> tuple[Command, ...]:
    hooks_dir = project_dir / HOOKS_DIR
    adapter = shlex.quote(str(hooks_dir / ADAPTER))
    script = shlex.quote(str(hooks_dir / reg.script))
    return (Command(f"{adapter} {script}"),)


ANTIGRAVITY = HarnessSpec(
    name="agy",
    hooks_dir=HOOKS_DIR,
    registrations=REGISTRATIONS,
    layout=Layout("named", name=HOOK_NAME),
    config_files=(
        ConfigFile("project", lambda h, p: p / ".agents" / "hooks.json", lambda h, p: p / HOOKS_DIR,
                   render_form=0, init_writes=True),
    ),
    command_forms=_commands,
    adapter=ADAPTER,
    project_config=".agents/hooks.json",
)

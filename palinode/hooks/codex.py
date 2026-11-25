"""Codex's hook spec: ``hooks.json`` and ``config.toml``, user and project (ADR-023).

The scripts are the Claude Code ones, byte for byte: Codex's SessionStart and
UserPromptSubmit payloads carry the same fields and take the same
``hookSpecificOutput.additionalContext`` back. No matcher: on SessionStart the
script filters the source itself (so the PALINODE_HOOK_START_SOURCES knob
keeps working), and Codex ignores a matcher on UserPromptSubmit. SessionEnd
runs the session-end floor, which reads a Codex rollout itself; Codex caps a
SessionEnd hook at 3 seconds.

Codex ties a hook's trust to its ``hooks.json`` entry (command and timeout)
and records it by position, ``<file>:<event>:<group>:<handler>``. Refreshing
a script keeps the trust; writing a registration does not, so palinode only
ever appends after every existing group, and says so (:data:`TRUST_NOTE`).
"""
from __future__ import annotations

import os
from pathlib import Path

from palinode.hooks.spec import Command, ConfigFile, HarnessSpec, Layout, Registration

HOOKS_DIR = ".codex/hooks"

#: Where a Codex hook command finds the project. Codex has no counterpart of
#: ``${CLAUDE_PROJECT_DIR}``: it runs each command through ``$SHELL -lc`` in the
#: session's cwd, which can be a subdirectory, so the command asks git for the
#: checkout root and falls back to the cwd outside a repository.
PROJECT_ROOT = "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

#: Printed whenever Codex hooks are scaffolded. Codex skips a hook nobody has
#: reviewed, so without this the install looks done and silently does nothing.
TRUST_NOTE = (
    "Codex runs project hooks only in a trusted project, and only after each hook "
    "is reviewed: open Codex in this project and run /hooks to trust the palinode "
    "entries. Trust covers the hooks.json entry, not the script, so a later "
    "`palinode hooks update` keeps it."
)

REGISTRATIONS = (
    Registration("SessionStart", "palinode-session-start.sh", 20, since="0.24.0"),
    Registration("UserPromptSubmit", "palinode-user-prompt-submit.sh", 20, since="0.24.0"),
    Registration("SessionEnd", "palinode-session-end.sh", 3, since="0.24.0"),
)


def codex_home(home: Path) -> Path:
    """Codex's user config directory: ``$CODEX_HOME``, else ``~/.codex``."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else home / ".codex"


def command(script: str) -> str:
    return f'"{PROJECT_ROOT}/{HOOKS_DIR}/{script}"'


def _commands(reg: Registration, project_dir: Path) -> tuple[Command, ...]:
    return (Command(command(reg.script)),)


def _user_dir(home: Path, project_dir: Path) -> Path:
    return codex_home(home) / "hooks"


def _project_dir(home: Path, project_dir: Path) -> Path:
    return project_dir / HOOKS_DIR


CODEX = HarnessSpec(
    name="codex",
    hooks_dir=HOOKS_DIR,
    registrations=REGISTRATIONS,
    layout=Layout("nested"),
    config_files=(
        ConfigFile("user", lambda h, p: codex_home(h) / "hooks.json", _user_dir),
        ConfigFile("user", lambda h, p: codex_home(h) / "config.toml", _user_dir),
        ConfigFile("project", lambda h, p: p / ".codex" / "hooks.json", _project_dir,
                   render_form=0, init_writes=True),
        ConfigFile("project", lambda h, p: p / ".codex" / "config.toml", _project_dir),
    ),
    command_forms=_commands,
    project_config=".codex/hooks.json",
)

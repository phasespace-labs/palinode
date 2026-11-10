"""palinode hooks — keep installed Claude Code and Codex hook scripts current.

``palinode init`` copies the hook scripts into ``.claude/hooks/`` once and
skips them on every later run, and a manual install copies them once by hand.
Nothing refreshed a stale copy, so a release that fixed a hook never reached a
machine that already had one: the server moved on while the client kept
running the hook it was installed with.

This module is the single answer to "which palinode hooks does this machine
run, and do they match the packaged ones?" — used by ``palinode hooks update``
and by the ``hooks_current`` doctor check, so the diagnosis and the fix can
never disagree about what is installed.

Discovery follows the Claude Code settings cascade (user
``~/.claude/settings.json``, project ``.claude/settings.json`` and
``.claude/settings.local.json``), then Codex's hook layers (user
``$CODEX_HOME/hooks.json`` and ``config.toml``, default ``~/.codex``; project
``.codex/hooks.json`` and ``.codex/config.toml``), then the project's Cursor
``.cursor/hooks.json`` and Antigravity ``.agents/hooks.json``, and reads each
hook's command path. Codex loads every layer and adds them together, so all of
them are walked. Cursor's and Antigravity's entries register the adapter and
the script it runs on one command line, so both are checked. A hook is
palinode's when its script file name is one ``palinode init`` writes; every
other hook is ignored, never read past its command string and never written.

CLI-only by design: the hooks live on the machine running the agent harness,
which is often not the machine running the palinode server. An MCP or REST
twin would inspect the server's filesystem and report on hooks nobody runs.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import click

from palinode.cli._format import OutputFormat, console, get_default_format, print_result

#: The marker every packaged hook carries on its second line. It names the
#: palinode release in which the script last changed, so a stale install can
#: say how far behind it is. Copies installed before the stamp existed have no
#: such line and report "unstamped".
HOOK_VERSION_PREFIX = "# palinode-hook-version:"
_VERSION_RE = re.compile(r"^# palinode-hook-version:\s*(\S+)\s*$", re.MULTILINE)

#: The command that refreshes stale hooks; named by the doctor check.
UPDATE_COMMAND = "palinode hooks update"

#: Hook states. Only ``stale`` and ``missing`` are written by an update.
STATE_CURRENT = "current"
STATE_STALE = "stale"
STATE_MISSING = "missing"
STATE_SYMLINK = "symlink"
STATE_FOREIGN = "foreign"
STATE_UNREADABLE = "unreadable"

_REFRESHABLE = (STATE_STALE, STATE_MISSING)


def packaged_hooks() -> dict[str, str]:
    """Map each hook file name ``palinode init`` installs to its packaged content."""
    from palinode.cli.init import (
        ANTIGRAVITY_HOOK_SCRIPT,
        CURSOR_HOOK_SCRIPT,
        HOOK_SCRIPT,
        POST_COMPACT_HOOK_SCRIPT,
        SESSION_START_HOOK_SCRIPT,
        USER_PROMPT_SUBMIT_HOOK_SCRIPT,
    )

    return {
        "palinode-session-start.sh": SESSION_START_HOOK_SCRIPT,
        "palinode-session-end.sh": HOOK_SCRIPT,
        "palinode-user-prompt-submit.sh": USER_PROMPT_SUBMIT_HOOK_SCRIPT,
        "palinode-post-compact.sh": POST_COMPACT_HOOK_SCRIPT,
        "palinode-cursor-hook.sh": CURSOR_HOOK_SCRIPT,
        "palinode-agy-hook.sh": ANTIGRAVITY_HOOK_SCRIPT,
    }


def hook_version(text: str) -> str | None:
    """The ``palinode-hook-version`` stamp in *text*, or None when unstamped."""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def content_sha256(text: str) -> str:
    """sha256 of a hook's exact bytes — what init installed and doctor compares."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def codex_home(home: Path) -> Path:
    """Codex's user config directory: ``$CODEX_HOME``, else ``~/.codex``."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else home / ".codex"


def settings_files(home: Path, project_dir: Path) -> list[Path]:
    """Every file that can register a palinode hook, in cascade order.

    Claude Code's settings cascade, then Codex's user and project layers
    (``hooks.json`` and the ``[hooks]`` tables of ``config.toml``), then the
    project's Cursor ``hooks.json`` and Antigravity ``.agents/hooks.json``.
    Cursor's user-level ``~/.cursor/hooks.json`` is not walked: init never
    writes there, and its relative commands resolve from ``~/.cursor/``, not
    from the project. Nor is Antigravity's global ``~/.gemini/config/hooks.json``,
    which init never writes either.
    """
    codex = codex_home(home)
    return [
        home / ".claude" / "settings.json",
        project_dir / ".claude" / "settings.json",
        project_dir / ".claude" / "settings.local.json",
        codex / "hooks.json",
        codex / "config.toml",
        project_dir / ".codex" / "hooks.json",
        project_dir / ".codex" / "config.toml",
        project_dir / ".cursor" / "hooks.json",
        project_dir / ".agents" / "hooks.json",
    ]


def _hook_commands(settings_path: Path) -> list[str]:
    """Every hook ``command`` string registered in one settings file.

    JSON for the Claude Code settings and Codex ``hooks.json``; TOML for a
    Codex ``config.toml``, whose ``[[hooks.<Event>]]`` tables parse to the same
    event → groups → handlers shape (its ``[hooks.state]`` trust records are
    not a list and are skipped). An absent or unparseable file registers
    nothing: the harness would not run hooks from it either, and doctor has
    other checks for broken config.
    """
    try:
        text = settings_path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if settings_path.suffix == ".toml" else json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return []
    if settings_path.parent.name == ".agents" and isinstance(data, dict):
        # Antigravity maps hook names to event maps, with no "hooks" key.
        from palinode.cli.init import _agy_commands

        return _agy_commands(data)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return []
    commands: list[str] = []
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            # Cursor's hooks.json puts the command on the entry itself.
            direct = entry.get("command") if isinstance(entry, dict) else None
            if isinstance(direct, str):
                commands.append(direct)
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(inner, list):
                continue
            for hook in inner:
                command = hook.get("command") if isinstance(hook, dict) else None
                if isinstance(command, str):
                    commands.append(command)
    return commands


#: The command substitution a Codex hook uses to find its checkout
#: (``palinode.cli.init.CODEX_PROJECT_ROOT`` and hand-written variants of it).
#: Codex has no project-directory variable, so this is how its commands name a
#: script; it stands for the project being inspected.
_GIT_TOPLEVEL_RE = re.compile(r"\$\(\s*git\b[^)]*--show-toplevel[^)]*\)")


def _expand(token: str, home: Path, project_dir: Path) -> str:
    """Expand the variables Claude Code hook commands use to name a script."""
    for var in ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR"):
        token = token.replace(var, str(project_dir))
    for var in ("${HOME}", "$HOME"):
        token = token.replace(var, str(home))
    if token == "~" or token.startswith("~/"):
        token = str(home) + token[1:]
    return token


def _command_scripts(command: str, home: Path, project_dir: Path, names: set[str]) -> list[Path]:
    """Paths of palinode hook scripts a command runs; non-palinode tokens are ignored."""
    # Before tokenising: an unquoted substitution contains spaces.
    command = _GIT_TOPLEVEL_RE.sub(lambda _m: str(project_dir), command)
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    found: list[Path] = []
    for token in tokens:
        expanded = _expand(token, home, project_dir)
        path = Path(expanded)
        if path.name not in names:
            continue
        if not path.is_absolute():
            path = project_dir / path
        found.append(path)
    return found


@dataclass
class HookStatus:
    """One installed palinode hook compared against its packaged copy."""

    path: str
    name: str
    state: str
    packaged_version: str | None
    packaged_sha256: str
    installed_version: str | None = None
    installed_sha256: str | None = None
    settings: list[str] = field(default_factory=list)


def hook_statuses(home: Path, project_dir: Path) -> list[HookStatus]:
    """Every palinode hook registered across the settings cascade, with its state.

    One entry per script path: a script registered from several settings files
    or events is one file on disk and is reported (and refreshed) once.
    """
    packaged = packaged_hooks()
    names = set(packaged)
    by_path: dict[str, HookStatus] = {}
    for settings_path in settings_files(home, project_dir):
        for command in _hook_commands(settings_path):
            for path in _command_scripts(command, home, project_dir, names):
                key = os.path.normpath(str(path))
                if key in by_path:
                    if str(settings_path) not in by_path[key].settings:
                        by_path[key].settings.append(str(settings_path))
                    continue
                status = _inspect(Path(key), packaged[path.name])
                status.settings.append(str(settings_path))
                by_path[key] = status
    return list(by_path.values())


def _inspect(path: Path, packaged: str) -> HookStatus:
    status = HookStatus(
        path=str(path),
        name=path.name,
        state=STATE_CURRENT,
        packaged_version=hook_version(packaged),
        packaged_sha256=content_sha256(packaged),
    )
    if not path.exists() and not path.is_symlink():
        status.state = STATE_MISSING
        return status
    try:
        installed = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        status.state = STATE_UNREADABLE
        return status
    status.installed_version = hook_version(installed)
    status.installed_sha256 = content_sha256(installed)
    if installed == packaged:
        status.state = STATE_CURRENT
    elif "palinode" not in installed.lower():
        # A script that shares a palinode file name but is not palinode's.
        status.state = STATE_FOREIGN
    elif path.is_symlink():
        # Its content is managed wherever the link points; writing here would
        # either edit that target or silently replace the link with a copy.
        status.state = STATE_SYMLINK
    else:
        status.state = STATE_STALE
    return status


def describe_version(version: str | None) -> str:
    return version if version is not None else "unstamped"


def hook_diff(status: HookStatus) -> str:
    """Unified diff from the installed copy to the packaged one."""
    packaged = packaged_hooks()[status.name]
    try:
        installed = Path(status.path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        installed = ""
    return "".join(
        difflib.unified_diff(
            installed.splitlines(keepends=True),
            packaged.splitlines(keepends=True),
            fromfile=f"{status.path} (installed)",
            tofile=f"{status.name} (packaged {describe_version(status.packaged_version)})",
        )
    )


def _backup_path(path: Path, now: datetime) -> Path:
    stem = f"{path.name}.bak-{now.strftime('%Y%m%d-%H%M%S')}"
    candidate = path.with_name(stem)
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{stem}.{n}")
        n += 1
    return candidate


def refresh_hook(status: HookStatus, now: datetime | None = None) -> str | None:
    """Write the packaged copy over a stale or missing hook; return the backup path.

    The old copy is renamed aside first, then the packaged bytes are written
    to a temp file in the same directory and moved into place, so the hook
    path is never left half-written for a session firing it mid-refresh.
    """
    if status.state not in _REFRESHABLE:
        raise ValueError(f"refusing to write a hook in state {status.state!r}")
    path = Path(status.path)
    content = packaged_hooks()[status.name]
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if status.state == STATE_STALE:
        backup = _backup_path(path, now or datetime.now())
        os.replace(path, backup)
    tmp = path.with_name(f".{path.name}.palinode-tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, path)
    return str(backup) if backup else None


_SKIP_REASONS = {
    STATE_CURRENT: "already matches the packaged copy",
    STATE_SYMLINK: "symlink — refresh the file it points to",
    STATE_FOREIGN: "not a palinode script despite its name — left alone",
    STATE_UNREADABLE: "could not be read — left alone",
}


@click.group()
def hooks() -> None:
    """Manage the palinode hook scripts Claude Code, Codex, Cursor and Antigravity run on this machine."""


@hooks.command(name="update")
@click.option("--dry-run", is_flag=True, help="Show what would change, with a diff, and write nothing.")
@click.option(
    "--project-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Project whose .claude/settings*.json, .codex/ hooks, .cursor/hooks.json and .agents/hooks.json to read (default: current directory).",
)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None)
def hooks_update(dry_run: bool, project_dir: Path | None, fmt: str | None) -> None:
    """Refresh installed palinode hooks to the packaged copies.

    Finds every palinode hook registered in ~/.claude/settings.json,
    .claude/settings.json and .claude/settings.local.json, in Codex's
    hooks.json and config.toml (user $CODEX_HOME or ~/.codex, and the
    project's .codex/), and in the project's .cursor/hooks.json and
    .agents/hooks.json, and replaces each stale copy with the one this palinode
    release ships, keeping the old one as <name>.bak-<timestamp> beside it. A hook registered but absent is
    installed. Hooks that are not palinode's are never touched; symlinked
    hooks are reported, not rewritten. Re-running when everything is current
    changes nothing.
    """
    home = Path.home()
    project = (project_dir or Path.cwd()).resolve()
    statuses = hook_statuses(home, project)
    now = datetime.now()

    results: list[dict[str, object]] = []
    for status in statuses:
        entry: dict[str, object] = asdict(status)
        if status.state in _REFRESHABLE:
            entry["action"] = "would update" if dry_run else "updated"
            if status.state == STATE_MISSING:
                entry["action"] = "would install" if dry_run else "installed"
            if dry_run:
                entry["diff"] = hook_diff(status)
                entry["backup"] = None
            else:
                entry["backup"] = refresh_hook(status, now)
        else:
            entry["action"] = "skipped"
            entry["reason"] = _SKIP_REASONS[status.state]
        results.append(entry)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    checked = [str(p) for p in settings_files(home, project)]
    if output_fmt == OutputFormat.JSON:
        print_result(
            {"dry_run": dry_run, "settings_checked": checked, "hooks": results},
            fmt=output_fmt,
        )
        return

    if not results:
        console.print("No palinode hooks registered in:")
        for path in checked:
            console.print(f"  {path}", markup=False, highlight=False)
        return
    for entry in results:
        action = str(entry["action"])
        line = f"{action}: {entry['path']}"
        if entry["state"] in _REFRESHABLE:
            line += (
                f" ({describe_version(entry['installed_version'])} -> "
                f"{describe_version(entry['packaged_version'])})"
            )
            if entry.get("backup"):
                line += f", backup {entry['backup']}"
        else:
            line += f" — {entry['reason']}"
        console.print(line, markup=False, highlight=False)
        if dry_run and entry.get("diff"):
            click.echo(str(entry["diff"]))
    if dry_run and any(e["state"] in _REFRESHABLE for e in results):
        console.print(f"Dry run — nothing written. Run `{UPDATE_COMMAND}` to apply.", markup=False)

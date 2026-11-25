"""What palinode hooks this machine runs, and whether they match the package.

The single answer used by ``palinode hooks update`` and doctor's
``hooks_current`` check, so the diagnosis and the fix agree on what is
installed. Discovery walks each harness spec's config files (the Claude Code
settings cascade, Codex's user and project ``hooks.json`` and ``config.toml``,
the project's Cursor ``.cursor/hooks.json`` and Antigravity
``.agents/hooks.json``) and finds the palinode scripts each command runs with
:meth:`HarnessSpec.runs`. Only a handler :meth:`HarnessSpec.owns` recognises
is ever rewritten. Every other hook is ignored, never read past its command
string and never written.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from palinode.hooks import content_sha256, hook_version, packaged_scripts, shipped_hashes
from palinode.hooks.harnesses import HARNESSES
from palinode.hooks.spec import (
    STATE_CURRENT,
    STATE_EDITED,
    STATE_STALE,
    ConfigFile,
    HarnessSpec,
    Registration,
    read_config,
)

#: The command that refreshes stale hooks; named by the doctor check.
UPDATE_COMMAND = "palinode hooks update"

#: Hook script states (ADR-023 §3). ``current``, ``stale`` (a version some
#: release shipped) and ``edited`` (a palinode script matching no shipped
#: version) come from :mod:`palinode.hooks.spec`. An update writes ``stale``
#: and ``missing``, and ``edited`` only with ``--force``.
STATE_MISSING = "missing"
STATE_SYMLINK = "symlink"
STATE_FOREIGN = "foreign"
STATE_UNREADABLE = "unreadable"

REFRESHABLE = (STATE_STALE, STATE_MISSING)
FORCE_REFRESHABLE = (*REFRESHABLE, STATE_EDITED)

#: Claude Code backgrounds a hook registered with ``"async": true`` and cancels
#: it, so v0.24.0's compaction checkpoint was almost never saved: 1 save from
#: 179 compactions on one machine. Palinode registers no hook async.
CHANGE_SYNCHRONOUS = "made the PostCompact hook synchronous"


def settings_files(home: Path, project_dir: Path) -> list[Path]:
    """Every file that can register a palinode hook, in discovery order."""
    return [p for spec in HARNESSES for p in spec.config_paths(home, project_dir)]


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
    """Every palinode hook registered across every harness, with its state.

    One entry per script path: a script registered from several config files
    or events is one file on disk and is reported (and refreshed) once.
    """
    packaged = packaged_scripts()
    by_path: dict[str, HookStatus] = {}
    for spec in HARNESSES:
        for settings_path in spec.config_paths(home, project_dir):
            for _event, handler in spec.handlers(settings_path):
                command = handler.get("command")
                if not isinstance(command, str):
                    continue
                for path in spec.runs(command, home, project_dir):
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
    elif status.installed_sha256 in _shipped(path.name):
        status.state = STATE_STALE
    else:
        status.state = STATE_EDITED
    return status


def _shipped(name: str) -> dict[str, dict]:
    """Every content palinode shipped for one script, by sha256."""
    return {e["sha256"]: e for e in shipped_hashes()["scripts"].get(name, [])}


def describe_version(version: str | None) -> str:
    return version if version is not None else "unstamped"


def hook_diff(status: HookStatus) -> str:
    """Unified diff from the installed copy to the packaged one."""
    packaged = packaged_scripts()[status.name]
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


def backup_path(path: Path, now: datetime) -> Path:
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
    if status.state not in FORCE_REFRESHABLE:
        raise ValueError(f"refusing to write a hook in state {status.state!r}")
    path = Path(status.path)
    content = packaged_scripts()[status.name]
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if status.state in (STATE_STALE, STATE_EDITED):
        backup = backup_path(path, now or datetime.now())
        os.replace(path, backup)
    tmp = path.with_name(f".{path.name}.palinode-tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, path)
    return str(backup) if backup else None


# -- registrations -----------------------------------------------------------


def _fix_handler(reg: Registration, form, handler: dict) -> list[str]:
    """Make one owned handler the current render, in place; return what changed."""
    changes: list[str] = []
    if handler.get("async"):
        handler.pop("async")
        changes.append(f"made the {reg.event} hook synchronous")
    if not form.current and form.upgrade:
        handler["command"] = form.upgrade
        changes.append(form.change or f"rewrote the {reg.event} command")
    if handler.get("timeout") != reg.timeout:
        handler["timeout"] = reg.timeout
        changes.append(f"set the {reg.event} timeout to {reg.timeout}s")
    return changes


def repair_registrations(
    spec: HarnessSpec, data: object, project_dir: Path, *, force: bool = False,
) -> list[str]:
    """Repair palinode's stale handlers in one parsed config file, in place.

    Only a handler :meth:`HarnessSpec.classify` recognises changes. A stale
    one, a form an earlier release wrote, becomes the current render: the
    pre-0.24.1 unquoted ``${CLAUDE_PROJECT_DIR}`` is quoted, an async
    registration is made synchronous, and an old timeout is reset. An edited
    one, palinode's command with fields no release wrote, keeps its fields
    unless ``force``; a legacy command in it is still upgraded, because the
    command is palinode's text, not the user's, and the old form breaks.
    Returns what changed.
    """
    changes: list[str] = []
    for _event, handler in spec.layout.handlers(data):
        found = spec.classify(handler, project_dir)
        if found is None:
            continue
        reg, form, state = found
        if state == STATE_STALE or (state == STATE_EDITED and force):
            fixed = _fix_handler(reg, form, handler)
        elif not form.current and form.upgrade:
            handler["command"] = form.upgrade
            fixed = [form.change or f"rewrote the {reg.event} command"]
        else:
            fixed = []
        for change in fixed:
            if change not in changes:
                changes.append(change)
    return changes


def _writable(path: Path) -> bool:
    """A config file palinode may rewrite: JSON. Codex's config.toml is read only."""
    return path.suffix == ".json"


@dataclass
class EditedRegistration:
    event: str
    command: str
    timeout: object
    is_async: bool


@dataclass
class FilePlan:
    """What ``palinode hooks update`` would change in one config file."""

    spec: HarnessSpec
    path: Path
    hooks_dir: Path
    render_form: int | None
    init_writes: bool
    #: Repairs to stale handlers, as reported.
    repairs: list[str] = field(default_factory=list)
    #: Owned handlers whose fields someone changed (``--force`` resets them).
    edited: list[EditedRegistration] = field(default_factory=list)
    #: Missing registrations whose script is absent, in a file init writes:
    #: never offered, so added.
    new: list[Registration] = field(default_factory=list)
    #: Missing registrations ``--force`` adds: removed by the user (the script
    #: is still installed), or never in this hand-assembled file.
    removed: list[Registration] = field(default_factory=list)
    not_offered: list[Registration] = field(default_factory=list)

    def writes(self, force: bool) -> bool:
        optional = self.edited or self.removed or self.not_offered
        return bool(self.repairs or self.new or (force and optional))


def _scopes(spec: HarnessSpec, home: Path, project_dir: Path) -> dict[Path, list[ConfigFile]]:
    """The spec's inspected config files, grouped by the hooks folder they share.

    A file that is both a user and a project file (run from the home
    directory) is taken once, under its user entry (``distinct_config_files``).
    """
    groups: dict[Path, list[ConfigFile]] = {}
    for config in spec.distinct_config_files(home, project_dir):
        if config.inspected:
            groups.setdefault(config.hooks_dir(home, project_dir), []).append(config)
    return groups


def plan_registrations(home: Path, project_dir: Path) -> list[FilePlan]:
    """Every config file palinode's registrations need a change in, and which.

    A registration is missing from a scope (one hooks folder, and the config
    files that register into it) when that scope registers some palinode hook
    but no handler runs this one's script on its event. Whether it was ever
    offered is told by its script (ADR-023 §6): ``palinode init`` installs a
    script with its registration and ``hooks update`` refreshes only
    registered scripts, so an absent script was never offered and is added;
    a present one means the registration was removed.
    """
    plans: list[FilePlan] = []
    for spec in HARNESSES:
        for hooks_dir, configs in _scopes(spec, home, project_dir).items():
            by_path: dict[Path, FilePlan] = {}
            present: list[tuple[Path, str, dict]] = []
            for config in configs:
                path = config.path(home, project_dir)
                for event, handler in spec.handlers(path):
                    command = handler.get("command")
                    if isinstance(command, str) and spec.runs(command, home, project_dir):
                        present.append((path, event, handler))
                if not _writable(path):
                    continue
                plan = FilePlan(spec, path, hooks_dir, config.render_form, config.init_writes)
                data = read_config(path)
                if data is not None:
                    plan.repairs = repair_registrations(spec, json.loads(json.dumps(data)), project_dir)
                    for event, handler in spec.layout.handlers(data):
                        found = spec.classify(handler, project_dir)
                        if found and found[2] == STATE_EDITED:
                            plan.edited.append(EditedRegistration(
                                event, handler["command"], handler.get("timeout"), bool(handler.get("async")),
                            ))
                by_path[path] = plan
            if present:
                missing = [
                    reg for reg in spec.registrations
                    if not any(
                        event == reg.event and spec.runs_script(h, reg.script, home, project_dir)
                        for _p, event, h in present
                    )
                ]
                target = _target(by_path, present)
                for reg in missing:
                    if target is None:
                        continue
                    if (hooks_dir / reg.script).exists():
                        target.removed.append(reg)
                    elif target.init_writes:
                        target.new.append(reg)
                    else:
                        target.not_offered.append(reg)
            plans.extend(
                p for p in by_path.values()
                if p.repairs or p.edited or p.new or p.removed or p.not_offered
            )
    return plans


def _target(by_path: dict[Path, FilePlan], present: list[tuple[Path, str, dict]]) -> FilePlan | None:
    """The file a missing registration is added to: where palinode's live, else the first that takes one."""
    for path, _event, _handler in present:
        plan = by_path.get(path)
        if plan is not None and plan.render_form is not None:
            return plan
    return next((p for p in by_path.values() if p.render_form is not None), None)


def apply_plan(
    plan: FilePlan, project_dir: Path, *, force: bool = False, now: datetime | None = None,
) -> tuple[str | None, list[str]]:
    """Write one file's planned changes; return (backup, changes).

    Only palinode's own handlers change, and added ones go after every
    existing entry. The file is copied aside first and rewritten through a
    temp file in the same directory, keeping its mode. A script an added
    registration runs is installed if absent.
    """
    path = plan.path
    exists = path.exists()
    data = json.loads(path.read_text(encoding="utf-8")) if exists else {}
    changes = repair_registrations(plan.spec, data, project_dir, force=force)
    adding = plan.new + (plan.removed + plan.not_offered if force else [])
    form = plan.render_form if plan.render_form is not None else 0
    for reg in adding:
        plan.spec.layout.add(data, reg.event, plan.spec.render(reg, project_dir, form))
        changes.append(f"registered {reg.event}")
        script = plan.hooks_dir / reg.script
        if not script.exists():
            refresh_hook(_inspect(script, packaged_scripts()[reg.script]), now)
    if not changes:
        return None, []
    backup = None
    if exists:
        backup = backup_path(path, now or datetime.now())
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.palinode-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if exists:
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return (str(backup) if backup else None), changes


#: An expansion of the project directory outside double quotes.
_PROJECT_DIR_VAR = re.compile(r"\$\{?CLAUDE_PROJECT_DIR\}?")
_DOUBLE_QUOTED = re.compile(r'"[^"]*"')

FIX_ASYNC = "registered async, which Claude Code cancels"
FIX_UNQUOTED = "uses ${CLAUDE_PROJECT_DIR} unquoted, which the shell splits at a space"


@dataclass
class HandFix:
    """A palinode hook in a form palinode does not write, with a known defect."""

    path: Path
    command: str
    problem: str


def registrations_to_fix_by_hand(home: Path, project_dir: Path) -> list[HandFix]:
    """Hand-written registrations of a palinode script that show a known defect.

    They run a palinode script (:meth:`HarnessSpec.runs`) but are not a form
    palinode writes (:meth:`HarnessSpec.owns`), so they are reported, never
    rewritten.
    """
    found: list[HandFix] = []
    for spec in HARNESSES:
        for path in spec.config_paths(home, project_dir):
            for _event, handler in spec.handlers(path):
                command = handler.get("command")
                if not isinstance(command, str) or spec.owns(command, project_dir):
                    continue
                if not spec.runs(command, home, project_dir):
                    continue
                if handler.get("async"):
                    found.append(HandFix(path, command, FIX_ASYNC))
                elif _PROJECT_DIR_VAR.search(_DOUBLE_QUOTED.sub("", command)):
                    found.append(HandFix(path, command, FIX_UNQUOTED))
    return found

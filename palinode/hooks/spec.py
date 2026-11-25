"""What a palinode hook registration looks like, per harness (ADR-023).

A :class:`HarnessSpec` is one harness's hook spec: where its config files are,
which events it registers, which script each event runs, the command form it
writes, and the earlier forms a release wrote. Everything that writes, reads
or repairs a registration goes through it:

* :meth:`HarnessSpec.render` is the writer: the handler ``palinode init``
  registers for one :class:`Registration`.
* :meth:`HarnessSpec.owns` is the strict recogniser: the command is exactly a
  form the spec renders, either current or a listed legacy form. Only an owned
  handler is ever rewritten.
* :meth:`HarnessSpec.runs` is the tolerant one: the palinode scripts a command
  runs, after expanding the harness's path variables. It drives discovery:
  which scripts to inspect and refresh, and which events are reported as
  covered. Its consequences are read-only.

Every form :meth:`HarnessSpec.commands` lists is one :meth:`owns` recognises
and one :meth:`runs` maps to its script, so ``owns`` is a subset of ``runs``
by construction; ``tests/test_hook_specs.py`` checks it for every form.

The four harnesses differ in data, not in code: the file layout (nested
groups, flat entries, a named hook) is one of three :class:`Layout` values.
"""
from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


#: Registration and script states (ADR-023 §3, §5).
STATE_CURRENT = "current"
STATE_STALE = "stale"
STATE_EDITED = "edited"


@dataclass(frozen=True)
class Registration:
    """One event palinode registers, the script it runs, and its budget."""

    event: str
    script: str
    timeout: int
    #: The release that introduced this registration.
    since: str
    #: ``(timeout, async)`` pairs an earlier release wrote, beside the current
    #: ``(timeout, False)``. A handler with one of them is stale, not edited.
    legacy_fields: tuple[tuple[int, bool], ...] = ()


@dataclass(frozen=True)
class Command:
    """One command form a release wrote for a registration."""

    text: str
    #: False for a form only an earlier release wrote.
    current: bool = True
    #: For a legacy form: the current command it is rewritten to, and what
    #: the rewrite does, as reported to the user.
    upgrade: str | None = None
    change: str | None = None


@dataclass(frozen=True)
class ConfigFile:
    """A file that can register palinode hooks for a harness."""

    scope: str
    path: Callable[[Path, Path], Path]
    #: Where this scope's scripts live, for ``palinode controls status``.
    hooks_dir: Callable[[Path, Path], Path]
    #: Walked by ``palinode hooks update`` and doctor's ``hooks_current``.
    inspected: bool = True
    #: The command form (an index into :attr:`HarnessSpec.command_forms`) a
    #: registration added to this file is written in, or None when
    #: ``palinode hooks update`` never adds one here.
    render_form: int | None = None
    #: ``palinode init`` writes this file, so ``palinode hooks update`` adds a
    #: registration a newer release introduced to it unasked. Elsewhere (a
    #: user-level or hand-assembled file) it is added only with ``--force``.
    init_writes: bool = False


@dataclass(frozen=True)
class Layout:
    """How one harness's config file holds its handlers."""

    #: "nested": ``hooks.<event>[group].hooks[handler]`` (Claude Code, Codex).
    #: "flat": ``hooks.<event>[handler]`` (Cursor).
    #: "named": ``<hook name>.<event>[handler]`` (Antigravity).
    kind: str
    #: For "named": the hook name palinode's handlers live under.
    name: str = ""

    def handlers(self, data: object) -> Iterator[tuple[str, dict]]:
        """Every ``(event, handler)`` in a parsed config file, any owner."""
        if not isinstance(data, dict):
            return
        if self.kind == "named":
            tables = [d for d in data.values() if isinstance(d, dict)]
        else:
            hooks = data.get("hooks")
            tables = [hooks] if isinstance(hooks, dict) else []
        for table in tables:
            for event, entries in table.items():
                if not isinstance(event, str) or not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if self.kind == "flat" or isinstance(entry.get("command"), str):
                        yield event, entry
                    inner = entry.get("hooks")
                    for handler in inner if isinstance(inner, list) else []:
                        if isinstance(handler, dict):
                            yield event, handler

    def add(self, data: dict, event: str, handler: dict) -> None:
        if self.kind == "named":
            data.setdefault(self.name, {}).setdefault(event, []).append(handler)
        elif self.kind == "flat":
            data.setdefault("hooks", {}).setdefault(event, []).append(handler)
        else:
            data.setdefault("hooks", {}).setdefault(event, []).append({"hooks": [handler]})


#: The command substitution a Codex hook uses to find its checkout. Codex has
#: no project-directory variable, so this is how its commands name a script; it
#: stands for the project being inspected. Hand-written variants match too.
_GIT_TOPLEVEL_RE = re.compile(r"\$\(\s*git\b[^)]*--show-toplevel[^)]*\)")


def _expand(token: str, home: Path, project_dir: Path) -> str:
    """Expand the variables hook commands use to name a script."""
    for var in ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR"):
        token = token.replace(var, str(project_dir))
    for var in ("${HOME}", "$HOME"):
        token = token.replace(var, str(home))
    if token == "~" or token.startswith("~/"):
        token = str(home) + token[1:]
    return token


@dataclass(frozen=True)
class HarnessSpec:
    """One harness's hook spec."""

    name: str
    #: Where ``palinode init`` installs this harness's scripts, under the project.
    hooks_dir: str
    registrations: tuple[Registration, ...]
    layout: Layout
    config_files: tuple[ConfigFile, ...]
    #: Every command form for a registration in a project, current first. The
    #: first is the one :meth:`render` writes.
    command_forms: Callable[[Registration, Path], tuple[Command, ...]]
    #: Whether a handler names its kind (``"type": "command"``).
    typed: bool = True
    #: The adapter script every command runs first (Cursor, Antigravity).
    adapter: str | None = None
    #: The config file ``palinode init`` writes, under the project.
    project_config: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- the scripts ---------------------------------------------------------

    @property
    def scripts(self) -> tuple[str, ...]:
        """Every script this harness runs: the adapter, then each registration's."""
        names = [self.adapter] if self.adapter else []
        for reg in self.registrations:
            if reg.script not in names:
                names.append(reg.script)
        return tuple(names)

    def registration(self, event: str) -> Registration | None:
        return next((r for r in self.registrations if r.event == event), None)

    # -- writing -------------------------------------------------------------

    def commands(self, reg: Registration, project_dir: Path) -> tuple[Command, ...]:
        return self.command_forms(reg, project_dir)

    def render(self, reg: Registration, project_dir: Path, form: int = 0) -> dict:
        """The handler ``palinode init`` registers for ``reg``, in command form ``form``."""
        command = self.commands(reg, project_dir)[form]
        if not command.current:
            raise ValueError(f"{command.text!r} is a legacy form; palinode does not write it")
        handler: dict[str, Any] = {"type": "command"} if self.typed else {}
        handler["command"] = command.text
        handler["timeout"] = reg.timeout
        return handler

    def classify(self, handler: dict, project_dir: Path) -> tuple[Registration, Command, str] | None:
        """``(registration, form, state)`` for an owned handler, else None.

        The state compares the whole handler a release wrote, command, timeout
        and async (ADR-023 §5):

        * ``current``: the current command with the current fields.
        * ``stale``: a legacy command, or fields an earlier release wrote
          (:attr:`Registration.legacy_fields`). ``hooks update`` repairs it.
        * ``edited``: the command is palinode's, but its fields match no
          shipped form: someone changed them. Changed only with ``--force``.

        Keys palinode does not write are not compared.
        """
        command = handler.get("command")
        owned = self.owns(command, project_dir) if isinstance(command, str) else None
        if owned is None:
            return None
        reg, form = owned
        fields = (handler.get("timeout"), bool(handler.get("async")))
        current = (reg.timeout, False)
        if form.current and fields == current:
            return reg, form, STATE_CURRENT
        if fields == current or fields in reg.legacy_fields:
            return reg, form, STATE_STALE
        return reg, form, STATE_EDITED

    def registration_block(self, project_dir: Path) -> dict:
        """A config file holding exactly palinode's registrations."""
        data: dict = {}
        if self.layout.kind == "flat":
            data["version"] = 1
        for reg in self.registrations:
            self.layout.add(data, reg.event, self.render(reg, project_dir))
        return data

    # -- recognising ---------------------------------------------------------

    def owns(self, command: str, project_dir: Path) -> tuple[Registration, Command] | None:
        """The registration and form ``command`` is exactly, or None.

        Only the blanks the shell itself ignores around a word are trimmed.
        Nothing is unquoted or expanded first: a command palinode did not
        write in one of these forms is never palinode's to rewrite.
        """
        text = command.strip(" \t")
        for reg in self.registrations:
            for form in self.commands(reg, project_dir):
                if form.text == text:
                    return reg, form
        return None

    def runs(self, command: str, home: Path, project_dir: Path) -> list[Path]:
        """Paths of this harness's palinode scripts that ``command`` runs."""
        command = _GIT_TOPLEVEL_RE.sub(lambda _m: str(project_dir), command)
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        names = set(self.scripts)
        found: list[Path] = []
        for token in tokens:
            path = Path(_expand(token, home, project_dir))
            if path.name not in names:
                continue
            found.append(path if path.is_absolute() else project_dir / path)
        return found

    def runs_script(self, handler: dict, script: str, home: Path, project_dir: Path) -> bool:
        command = handler.get("command")
        return isinstance(command, str) and any(
            p.name == script for p in self.runs(command, home, project_dir)
        )

    # -- reading -------------------------------------------------------------

    def config_paths(self, home: Path, project_dir: Path, *, inspected_only: bool = True) -> list[Path]:
        return [
            c.path(home, project_dir) for c in self.config_files
            if c.inspected or not inspected_only
        ]

    def handlers(self, path: Path) -> list[tuple[str, dict]]:
        """Every ``(event, handler)`` registered in one config file.

        An absent or unparseable file registers nothing: the harness would not
        run hooks from it either, and doctor has other checks for broken config.
        """
        data = read_config(path)
        return list(self.layout.handlers(data)) if data is not None else []

    def palinode_events(self, path: Path, home: Path, project_dir: Path) -> set[str]:
        """Events in one config file with a handler whose command names a palinode script."""
        return {
            event for event, handler in self.handlers(path)
            if isinstance(handler.get("command"), str)
            and self.runs(handler["command"], home, project_dir)
        }

    # -- merging (palinode init) ---------------------------------------------

    def missing(self, data: dict, home: Path, project_dir: Path) -> list[Registration]:
        """Registrations no handler in ``data`` covers: none runs its script on its event."""
        present = list(self.layout.handlers(data))
        return [
            reg for reg in self.registrations
            if not any(
                event == reg.event and self.runs_script(h, reg.script, home, project_dir)
                for event, h in present
            )
        ]

    def add_missing(self, data: dict, home: Path, project_dir: Path) -> bool:
        """Append a handler for each missing registration, after every existing entry."""
        regs = self.missing(data, home, project_dir)
        for reg in regs:
            self.layout.add(data, reg.event, self.render(reg, project_dir))
        return bool(regs)

    def shape_problem(self, data: object) -> str | None:
        """Why ``data`` cannot be merged into safely, or None."""
        if not isinstance(data, dict):
            return "has an unexpected shape — left alone"
        if self.layout.kind == "named":
            ours = data.get(self.layout.name, {})
            if not isinstance(ours, dict):
                return f"{self.layout.name!r} is not a hook definition — left alone"
            for reg in self.registrations:
                if not isinstance(ours.get(reg.event, []), list):
                    return f"{self.layout.name}.{reg.event} is not a list — left alone"
            return None
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return "has an unexpected hooks shape — left alone"
        for reg in self.registrations:
            if not isinstance(hooks.get(reg.event, []), list):
                return f"{reg.event} is not a list — left alone"
        return None

    def merge(self, path: Path, home: Path, project_dir: Path, force: bool) -> str:
        """Register palinode's hooks in ``path``, keeping every other entry.

        Palinode's entries go after every existing one, so nothing already
        there moves: Codex records each hook's trust by its position.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            write_json(path, self.registration_block(project_dir))
            return "created"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if not force:
                return f"skipped (existing {path.name} is not valid JSON — re-run with --force to overwrite)"
            existing = {}
        problem = self.shape_problem(existing)
        if problem:
            return f"skipped (existing {path.name} {problem})"
        if self.layout.kind == "flat":
            existing.setdefault("version", 1)
        if not self.add_missing(existing, home, project_dir):
            return "skipped (palinode hooks already registered)"
        write_json(path, existing)
        return "merged"


def read_config(path: Path) -> Any | None:
    """A parsed hook config file (JSON, or TOML for ``.toml``), or None."""
    try:
        text = path.read_text(encoding="utf-8")
        return tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return None


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

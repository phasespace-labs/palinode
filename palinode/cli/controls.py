"""User-facing API client for Palinode capture, recall, and exclusion controls.

The API owns policy state.  This command deliberately contains no local pause
file or hook rewrite: a CLI-only switch would make a reassuring display while
other clients continued to use the API.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from palinode.cli._api import api_client
from palinode.cli._format import OutputFormat, console, get_default_format, print_result
from palinode.cli.hooks import codex_home
from palinode.cli.init import (
    _ANTIGRAVITY_HOOK_EVENTS,
    _CODEX_HOOK_EVENTS,
    _CURSOR_HOOK_EVENTS,
    _HOOK_EVENTS,
    ANTIGRAVITY_HOOKS_DIR,
    CURSOR_HOOKS_DIR,
    _agy_commands,
)
from palinode.core.disclosure import (
    EXCLUSION_SCOPE,
    PAUSE_SCOPE,
    UNVERIFIABLE_PATH_POLICY_UNREPORTED,
    redact_destination,
)


#: The hook scripts ``palinode init --hook`` installs, by Claude Code event.
_HOOK_SCRIPTS = dict(_HOOK_EVENTS)
#: The hook scripts ``palinode init --codex-hook`` installs, by Codex event.
_CODEX_HOOK_SCRIPTS = dict(_CODEX_HOOK_EVENTS)

#: What each automatic capture path sends to the API, for the disclosure.
OBSERVED_CAPTURE_RANGE = (
    "the SessionEnd hook sends a message count and a 200-character first-prompt topic hint, "
    "plus the last assistant reply capped at 2000 characters by default "
    "(PALINODE_HOOK_ENDING_MAX_CHARS; 0 disables the ending), from Claude Code and, "
    "with palinode init --codex-hook, from Codex; "
    "the PostCompact hook sends the summary Claude Code writes when it compacts a session, "
    "up to 32000 characters by default (PALINODE_HOOK_COMPACT_MAX_CHARS); "
    "neither is a full transcript import"
)


def _palinode_hook_events(settings: Path) -> set[str]:
    """The Claude Code events whose registration in ``settings`` names palinode."""
    events: set[str] = set()
    try:
        raw = json.loads(settings.read_text(encoding="utf-8"))
        hooks = raw.get("hooks", {}) if isinstance(raw, dict) else {}
        for event, entries in hooks.items() if isinstance(hooks, dict) else ():
            if not isinstance(event, str) or not isinstance(entries, list):
                continue
            if any(
                "palinode" in str(hook).lower()
                for entry in entries
                if isinstance(entry, dict)
                for hook in (
                    entry.get("hooks", [])
                    if isinstance(entry.get("hooks"), list)
                    else []
                )
            ):
                events.add(event)
    except (OSError, ValueError, AttributeError):
        pass
    return events


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cursor_hook_events(hooks_json: Path) -> set[str]:
    """The Cursor events whose ``hooks.json`` entries name a palinode script.

    Cursor's entries are flat ``{"command": ...}`` objects under each event,
    unlike Claude Code's nested ``hooks`` lists.
    """
    raw = _read_json(hooks_json)
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    events: set[str] = set()
    for event, entries in hooks.items() if isinstance(hooks, dict) else ():
        if isinstance(event, str) and isinstance(entries, list) and any(
            isinstance(entry, dict) and "palinode" in str(entry.get("command", "")).lower()
            for entry in entries
        ):
            events.add(event)
    return events


def _agy_hook_events(hooks_json: Path) -> set[str]:
    """The Antigravity events with a palinode handler, under any hook name."""
    raw = _read_json(hooks_json)
    events: set[str] = set()
    for definition in raw.values() if isinstance(raw, dict) else ():
        if not isinstance(definition, dict):
            continue
        for event, handlers in definition.items():
            if isinstance(event, str) and isinstance(handlers, list) and any(
                "palinode" in command.lower()
                for command in _agy_commands({"_": {event: handlers}})
            ):
                events.add(event)
    return events


def _observed_recall_hooks(
    scopes: list[tuple[str, Path, Path]],
    pairs: tuple[tuple[str, str], ...],
    read_events: Any,
) -> list[dict[str, Any]]:
    """Report each palinode hook found in ``(scope, hooks.json, hooks dir)``."""
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for scope, hooks_json, hooks_dir in scopes:
        resolved = hooks_json.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        registrations = read_events(hooks_json)
        for event, script in pairs:
            script_observed = (hooks_dir / script).is_file()
            if event in registrations or script_observed:
                found.append(
                    {
                        "event": event,
                        "scope": scope,
                        "settings_registration_observed": event in registrations,
                        "script_observed": script_observed,
                    }
                )
    return found


def _observed_project_setup(project_dir: Path, home: Path | None = None) -> dict[str, Any]:
    """Read generated files as evidence of setup, never of a live client.

    Claude Code merges hooks from the user's settings and the project's shared
    and local settings, so all three are read; each hook is reported with the
    scope it was found in. Codex hooks are read from the user's and the
    project's ``hooks.json`` the same way. Cursor (user and project
    ``hooks.json``) and Antigravity (project ``.agents/hooks.json``) hooks are
    recall only: ``palinode init`` registers no capture hook for either.
    """
    observed: dict[str, Any] = {
        "project_dir": str(project_dir),
        "claude_hooks": [],
        "codex_hooks": [],
        "cursor_hooks": [],
        "agy_hooks": [],
        "mcp_configured": False,
        "codex_instructions": False,
        "running_client_state": "unknown; Palinode cannot inspect client-managed traffic or processes",
    }
    user_dir = (home if home is not None else Path.home()) / ".claude"
    project_claude = project_dir / ".claude"
    scopes = [
        ("user", user_dir / "settings.json", user_dir / "hooks"),
        ("project", project_claude / "settings.json", project_claude / "hooks"),
        ("project_local", project_claude / "settings.local.json", project_claude / "hooks"),
    ]
    seen_settings: set[Path] = set()
    for scope, settings, hooks_dir in scopes:
        # Run from the home directory, the user and project files are the same.
        resolved = settings.resolve()
        if resolved in seen_settings:
            continue
        seen_settings.add(resolved)
        registrations = _palinode_hook_events(settings)
        for event, script in _HOOK_SCRIPTS.items():
            script_observed = (hooks_dir / script).is_file()
            # Local settings share the project's hooks folder; its scripts are
            # reported once, under the project scope.
            if event in registrations or (script_observed and scope != "project_local"):
                observed["claude_hooks"].append(
                    {
                        "event": event,
                        "scope": scope,
                        "settings_registration_observed": event in registrations,
                        "script_observed": script_observed,
                    }
                )
    # Codex adds every layer's hooks together rather than overriding, so the
    # user and project hooks.json are both read the same way.
    codex_user = codex_home(home if home is not None else Path.home())
    codex_project = project_dir / ".codex"
    seen_codex: set[Path] = set()
    for scope, codex_dir in (("user", codex_user), ("project", codex_project)):
        resolved = codex_dir.resolve()
        if resolved in seen_codex:
            continue
        seen_codex.add(resolved)
        registrations = _palinode_hook_events(codex_dir / "hooks.json")
        for event, script in _CODEX_HOOK_SCRIPTS.items():
            script_observed = (codex_dir / "hooks" / script).is_file()
            if event in registrations or script_observed:
                observed["codex_hooks"].append(
                    {
                        "event": event,
                        "scope": scope,
                        "settings_registration_observed": event in registrations,
                        "script_observed": script_observed,
                    }
                )
    user_home = home if home is not None else Path.home()
    observed["cursor_hooks"] = _observed_recall_hooks(
        [
            ("user", user_home / ".cursor" / "hooks.json", user_home / ".cursor" / "hooks"),
            ("project", project_dir / ".cursor" / "hooks.json", project_dir / CURSOR_HOOKS_DIR),
        ],
        _CURSOR_HOOK_EVENTS,
        _cursor_hook_events,
    )
    observed["agy_hooks"] = _observed_recall_hooks(
        [("project", project_dir / ".agents" / "hooks.json", project_dir / ANTIGRAVITY_HOOKS_DIR)],
        _ANTIGRAVITY_HOOK_EVENTS,
        _agy_hook_events,
    )
    try:
        mcp = json.loads((project_dir / ".mcp.json").read_text(encoding="utf-8"))
        observed["mcp_configured"] = "palinode" in mcp.get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        pass
    try:
        observed["codex_instructions"] = "palinode" in (project_dir / "AGENTS.md").read_text(
            encoding="utf-8"
        ).lower()
    except OSError:
        pass
    return observed


def _redact_remote_line(line: str) -> str:
    """Redact the URL field of ``git remote -v`` output, not its label."""
    fields = line.split()
    if len(fields) < 2:
        return redact_destination(line)
    return " ".join([fields[0], redact_destination(fields[1]), *fields[2:]])


def _redact_destinations(values: object) -> list[str]:
    """Defensively redact configured destination strings received from status."""
    if not isinstance(values, list):
        return []
    return [redact_destination(value) for value in values if isinstance(value, str)]


def _enabled_label(value: object) -> str:
    """Keep an absent enabled flag distinct from a configured false value."""
    if value is True:
        return "enabled"
    if value is False:
        return "disabled"
    return "unknown"


def build_disclosure(
    status: dict[str, Any],
    controls: dict[str, Any],
    preflight: dict[str, Any] | None,
    project_dir: Path,
    *,
    remotes: list[str] | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Produce truthful, serializable first-use disclosure data.

    API status is the runtime authority.  Files in the project are labelled as
    observations because their presence cannot prove a hook/client is running.
    """
    consolidation = status.get("consolidation") if isinstance(status.get("consolidation"), dict) else {}
    project = (preflight or {}).get("project") or "unknown (policy preflight did not resolve a project)"
    store = status.get("memory_dir")
    return {
        "controls": controls,
        "effective_project": project,
        "effective_store": store or "unknown (not reported by API)",
        "policy_preflight": preflight or {"allowed": "unknown", "reason": "policy preflight unavailable"},
        "observed_setup": _observed_project_setup(project_dir, home),
        "transcript_capture": {
            "opt_in": "not inferred from MCP installation; selected harness setup must opt in",
            "observed_source": (
                "Claude Code SessionEnd and PostCompact hooks and the Codex SessionEnd hook, "
                "only when listed under observed hooks; Cursor and Antigravity hooks are "
                "recall only and capture nothing"
            ),
            "observed_range": OBSERVED_CAPTURE_RANGE,
            # The correction miner is a second, separate transcript reader with
            # its own switch. Reported from API status, which is the runtime
            # authority: config on this machine is not proof of what the server
            # reads.
            "correction_mining": status.get("transcript_correction_capture")
            or {"enabled": "unknown (not reported by API)"},
        },
        "capture_and_recall": {
            "automatic": "observed Claude Code, Codex, Cursor and Antigravity hooks may attempt automatic lifecycle work; MCP tool use is explicit unless that client follows project instructions",
            "explicit": "CLI and MCP calls routed through this API use the server policy",
            "pause_scope": PAUSE_SCOPE,
            "exclusion_scope": EXCLUSION_SCOPE,
        },
        "destinations": {
            "palinode_api": redact_destination(status.get("api_url") or status.get("api") or "unknown"),
            "embedding": redact_destination(status.get("embedding_url") or "unknown"),
            "auto_summary": {
                "enabled": status.get("auto_summary_enabled"),
                "primary": redact_destination(status.get("auto_summary_primary_url") or "unknown"),
                "llm_fallbacks": _redact_destinations(status.get("auto_summary_llm_fallbacks")),
            },
            "consolidation": {
                "enabled": status.get("consolidation_enabled"),
                "primary": redact_destination(consolidation.get("llm_url") or status.get("consolidation_url") or "unknown"),
                "llm_fallbacks": _redact_destinations(status.get("consolidation_llm_fallbacks")),
            },
            "transcriptor": redact_destination(status.get("transcriptor_url") or "unknown"),
            "git_remotes": [
                _redact_remote_line(remote)
                for remote in (remotes if remotes is not None else status.get("git_remotes") or [])
                if isinstance(remote, str)
            ],
            "push_policy": status.get("git_push_policy") or "not reported by API; a configured remote is not proof of an automatic push",
        },
        "limits": {
            "client_traffic": "Palinode cannot inspect or control client-managed model traffic.",
            "network": "Local storage is not a no-network guarantee: configured embedding, consolidation, git, and client providers may receive data.",
            "secret_detection": "No secret-scanner guarantee is made. Exclusions protect only their tested automatic paths; do not submit secrets explicitly.",
            "visibility": "Use the local inspector/history to review stored data. They are visibility tools, not an authorization boundary.",
            # A server-owned rule, so the API's own statement is the authority.
            "unverifiable_paths": status.get("unverifiable_path_policy")
            or UNVERIFIABLE_PATH_POLICY_UNREPORTED,
        },
    }


def _emit(data: dict[str, Any], fmt: str | None) -> None:
    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        print_result(data, fmt=output_fmt)
        return
    controls = data.get("controls", data)
    console.print("Palinode controls")
    console.print(f"  Capture: {'paused' if controls.get('capture_paused') else 'active'}")
    console.print(f"  Recall: {'paused' if controls.get('recall_paused') else 'active'}")
    console.print(f"  Excluded projects: {len(controls.get('excluded_projects') or [])}")
    console.print(f"  Excluded paths: {len(controls.get('excluded_paths') or [])}")
    if "effective_project" in data:
        console.print(f"  Effective project: {data['effective_project']}")
        console.print(f"  Store: {data['effective_store']}")
        destinations = data["destinations"]
        auto_summary = destinations["auto_summary"]
        consolidation = destinations["consolidation"]
        auto_fallbacks = ", ".join(auto_summary["llm_fallbacks"]) or "none"
        consolidation_fallbacks = ", ".join(consolidation["llm_fallbacks"]) or "none"
        console.print(f"  Destinations: API {destinations['palinode_api']}; embedding {destinations['embedding']}")
        console.print(
            f"  Auto-summary: {_enabled_label(auto_summary['enabled'])}; "
            f"primary {auto_summary['primary']}; fallbacks {auto_fallbacks}"
        )
        console.print(
            f"  Consolidation: {_enabled_label(consolidation['enabled'])}; "
            f"primary {consolidation['primary']}; fallbacks {consolidation_fallbacks}; "
            f"transcriptor {destinations['transcriptor']}"
        )
        remotes = destinations["git_remotes"]
        console.print(f"  Git: {destinations['push_policy']}; remotes: {'; '.join(remotes) if remotes else 'none configured'}")
        transcript = data["transcript_capture"]
        hooks = data["observed_setup"]["claude_hooks"]
        console.print(
            "  Claude Code hooks observed: "
            + (
                ", ".join(
                    f"{hook['event']} ({hook['scope']}"
                    + ("" if hook["script_observed"] else ", script not found")
                    + ("" if hook["settings_registration_observed"] else ", not registered")
                    + ")"
                    for hook in hooks
                )
                or "none in user or project settings"
            )
        )
        codex_hooks = data["observed_setup"]["codex_hooks"]
        console.print(
            "  Codex hooks observed: "
            + (
                ", ".join(
                    f"{hook['event']} ({hook['scope']}"
                    + ("" if hook["script_observed"] else ", script not found")
                    + ("" if hook["settings_registration_observed"] else ", not registered")
                    + ")"
                    for hook in codex_hooks
                )
                or "none in user or project hooks.json"
            )
        )
        for label, key, absent in (
            ("Cursor", "cursor_hooks", "none in user or project .cursor/hooks.json"),
            ("Antigravity", "agy_hooks", "none in project .agents/hooks.json"),
        ):
            recall_hooks = data["observed_setup"].get(key) or []
            console.print(
                f"  {label} hooks observed (recall only): "
                + (
                    ", ".join(
                        f"{hook['event']} ({hook['scope']}"
                        + ("" if hook["script_observed"] else ", script not found")
                        + ("" if hook["settings_registration_observed"] else ", not registered")
                        + ")"
                        for hook in recall_hooks
                    )
                    or absent
                )
            )
        console.print(f"  Transcript capture: opt-in; {transcript['observed_source']}")
        console.print(f"  Automatic capture sends: {transcript['observed_range']}")
        mining = transcript.get("correction_mining") or {}
        if mining.get("enabled") is True:
            console.print(
                f"  Correction mining: enabled for {', '.join(mining.get('harnesses') or []) or 'no harness'}; "
                f"reads {mining.get('reads')}; stores {mining.get('stores')}"
            )
            # Reading and classifying are separate opt-ins, so they are reported
            # as separate facts: the destination is the part a reader cannot
            # infer, and "enabled" alone must not imply anything is transmitted.
            console.print(
                f"  Correction mining classification: {_enabled_label(mining.get('classify_enabled'))}"
            )
            # Parentheses, not brackets: ``console.print`` reads ``[...]`` as
            # rich markup and silently consumes it, which is exactly how a
            # destination disclosure would come to be printed as nothing.
            console.print(
                f"  Correction mining sends: {mining.get('sends')} "
                f"(destination: {mining.get('sends_to') or 'none'})"
            )
        else:
            console.print(
                f"  Correction mining: {_enabled_label(mining.get('enabled'))}; "
                "no transcript path is read and nothing is sent"
            )
        console.print(
            f"  Scope: pauses affect {PAUSE_SCOPE}; exclusions: {EXCLUSION_SCOPE}"
        )
        console.print(f"  Unverifiable paths: {data['limits']['unverifiable_paths']}")
        console.print("  Client runtime state: unknown unless a client reports it directly")


def _update_pause(capture: bool, recall: bool, paused: bool, fmt: str | None) -> None:
    if not capture and not recall:
        raise click.UsageError("Select --capture and/or --recall.")
    data = api_client.set_controls(
        capture_paused=paused if capture else None,
        recall_paused=paused if recall else None,
    )
    _emit(data, fmt)


@click.group()
def controls() -> None:
    """Inspect or change server-owned capture, recall, and automatic exclusions."""


@controls.command()
@click.option("--format", "fmt", type=click.Choice(["json", "text"]))
@click.option("--cwd", type=click.Path(file_okay=False, path_type=Path), default=Path.cwd)
@click.option("--project")
def status(fmt: str | None, cwd: Path, project: str | None) -> None:
    """Show controls plus a bounded first-use disclosure."""
    try:
        controls_data = api_client.get_controls()
        runtime = api_client.get_status()
        preflight = api_client.check_controls("capture", cwd=str(cwd), project=project, automatic=True)
        _emit(build_disclosure(runtime, controls_data, preflight, cwd), fmt)
    except Exception as exc:
        raise click.ClickException("Unable to read controls from the API; check server reachability and authorization.") from exc


@controls.command()
@click.option("--capture/--no-capture", default=True, help="Pause capture requests.")
@click.option("--recall/--no-recall", default=True, help="Pause recall/injection requests.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]))
def pause(capture: bool, recall: bool, fmt: str | None) -> None:
    """Pause future API capture and/or recall."""
    try:
        _update_pause(capture, recall, True, fmt)
    except Exception as exc:
        raise click.ClickException("Unable to pause controls through the API; check server reachability and authorization.") from exc


@controls.command()
@click.option("--capture/--no-capture", default=True, help="Resume capture requests.")
@click.option("--recall/--no-recall", default=True, help="Resume recall/injection requests.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]))
def resume(capture: bool, recall: bool, fmt: str | None) -> None:
    """Resume future API capture and/or recall."""
    try:
        _update_pause(capture, recall, False, fmt)
    except Exception as exc:
        raise click.ClickException("Unable to resume controls through the API; check server reachability and authorization.") from exc


def _change_exclusion(field: str, value: str, remove: bool, fmt: str | None) -> None:
    if not value.strip():
        raise click.UsageError("Exclusion value must not be empty.")
    current = api_client.get_controls()
    values = list(current.get(field) or [])
    if remove:
        values = [item for item in values if item != value]
    elif value not in values:
        values.append(value)
    _emit(api_client.set_controls(**{field: values}), fmt)


@controls.command(name="exclude-project")
@click.argument("project")
@click.option("--remove", is_flag=True, help="Remove this project from automatic capture/recall exclusions.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]))
def exclude_project(project: str, remove: bool, fmt: str | None) -> None:
    """Exclude one project from automatic capture and recall, or remove that exclusion."""
    try:
        _change_exclusion("excluded_projects", project, remove, fmt)
    except Exception as exc:
        raise click.ClickException("Unable to update project exclusions through the API; check server reachability and authorization.") from exc


@controls.command(name="exclude-path")
@click.argument("path")
@click.option("--remove", is_flag=True, help="Remove this path from automatic capture/recall exclusions.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]))
def exclude_path(path: str, remove: bool, fmt: str | None) -> None:
    """Exclude one source path from automatic capture and recall, or remove that exclusion."""
    try:
        _change_exclusion("excluded_paths", path, remove, fmt)
    except Exception as exc:
        raise click.ClickException("Unable to update path exclusions through the API; check server reachability and authorization.") from exc

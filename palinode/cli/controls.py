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
from palinode.core.disclosure import EXCLUSION_SCOPE, PAUSE_SCOPE, redact_destination


def _observed_project_setup(project_dir: Path) -> dict[str, Any]:
    """Read generated files as evidence of setup, never of a live client."""
    observed: dict[str, Any] = {
        "project_dir": str(project_dir),
        "claude_hooks": [],
        "mcp_configured": False,
        "codex_instructions": False,
        "running_client_state": "unknown; Palinode cannot inspect client-managed traffic or processes",
    }
    settings = project_dir / ".claude" / "settings.json"
    scripts = {
        "SessionStart": project_dir / ".claude" / "hooks" / "palinode-session-start.sh",
        "SessionEnd": project_dir / ".claude" / "hooks" / "palinode-session-end.sh",
        "UserPromptSubmit": project_dir / ".claude" / "hooks" / "palinode-user-prompt-submit.sh",
    }
    registrations: set[str] = set()
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
                registrations.add(event)
    except (OSError, ValueError, AttributeError):
        pass
    for event, path in scripts.items():
        if event in registrations or path.is_file():
            observed["claude_hooks"].append(
                {
                    "event": event,
                    "settings_registration_observed": event in registrations,
                    "script_observed": path.is_file(),
                }
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
        "observed_setup": _observed_project_setup(project_dir),
        "transcript_capture": {
            "opt_in": "not inferred from MCP installation; selected harness setup must opt in",
            "observed_source": "Claude Code SessionEnd hook only when shown above as observed generated setup",
            "observed_range": "eligible floor capture sends a message count and a 200-character first-prompt topic hint; it is not a full transcript import",
            # The correction miner is a second, separate transcript reader with
            # its own switch. Reported from API status, which is the runtime
            # authority: config on this machine is not proof of what the server
            # reads.
            "correction_mining": status.get("transcript_correction_capture")
            or {"enabled": "unknown (not reported by API)"},
        },
        "capture_and_recall": {
            "automatic": "observed Claude hooks may attempt automatic lifecycle work; MCP/Codex tool use is explicit unless that client follows project instructions",
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
        console.print(f"  Transcript capture: opt-in; {transcript['observed_source']}")
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

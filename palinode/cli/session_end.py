import click
import httpx
from palinode.cli._api import HTTPStatusError, RequestError, api_client
from palinode.cli._format import print_result, get_default_format, OutputFormat


@click.command("session-end")
@click.argument("summary")
@click.option("--decision", "-d", multiple=True, help="Key decision made (repeatable)")
@click.option("--blocker", "-b", multiple=True, help="Open blocker or next step (repeatable)")
@click.option("--project", "-p", default=None, help="Project slug to append status to (e.g., 'palinode')")
@click.option("--source", help="Source surface (e.g., claude-code, cursor, api)")
@click.option("--harness", help="Harness identifier (e.g., claude-code, cursor) — #145")
@click.option("--cwd", help="Working directory the session ran in — #145")
@click.option("--model", help="Model name (e.g., claude-opus-4-7) — #145")
@click.option("--trigger", help="What triggered this save (manual, wrap-slash, hook, …) — #145")
@click.option("--session-id", "session_id", help="Opaque session id from the harness — #145")
@click.option("--duration-seconds", "duration_seconds", type=int, help="Session duration in seconds — #145")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None, help="Output format")
def session_end(summary, decision, blocker, project, source, harness, cwd, model,
                trigger, session_id, duration_seconds, fmt):
    """Capture session outcomes to daily notes and project status.

    Call at the end of a coding or chat session to persist what was accomplished.

    Examples:

        palinode session-end "Implemented CLI wrapper with 22 commands"

        palinode session-end "Fixed embedding timeout" -d "Increase batch size" -b "Test under load" -p palinode
    """
    decisions = list(decision)
    blockers = list(blocker)

    try:
        result = api_client.session_end(
            summary=summary,
            decisions=decisions,
            blockers=blockers,
            project=project,
            source=source,
            harness=harness,
            cwd=cwd,
            model=model,
            trigger=trigger,
            session_id=session_id,
            duration_seconds=duration_seconds,
        )
    except HTTPStatusError as e:
        raise click.ClickException(f"Session-end failed: {e.response.text}") from e
    except httpx.ReadTimeout as e:
        # Distinguish a slow-server timeout from a connection failure — the old
        # message ("is palinode running?") was misleading when the API was up but
        # the embedding + git commit path exceeded the request budget (#377).
        from palinode.core.defaults import SESSION_END_TIMEOUT_SECONDS
        raise click.ClickException(
            f"Session-end timed out after {SESSION_END_TIMEOUT_SECONDS:.0f}s — "
            "palinode-api is reachable but the embed+commit took too long. "
            "Raise PALINODE_SESSION_END_TIMEOUT or check Ollama load."
        ) from e
    except RequestError as e:
        raise click.ClickException(f"Cannot reach API — is palinode running? ({e})") from e

    daily_file = result.get("daily_file")
    status_file = result.get("status_file")
    individual_file = result.get("individual_file")
    entry = result.get("entry", "")

    effective_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if effective_fmt == OutputFormat.JSON:
        # Preserve the prior CLI JSON shape: callers that script against
        # `palinode session-end --format json` should still see summary/
        # decisions/blockers and `project_status` (alias for `status_file`).
        out = {
            "daily_file": daily_file,
            "individual_file": individual_file,
            "project_status": status_file,
            "summary": summary,
            "decisions": decisions,
            "blockers": blockers,
        }
        print_result(out, OutputFormat.JSON)
    else:
        status_msg = f" + status → {status_file.split('/')[-1]}" if status_file else ""
        click.echo(f"Session captured → {daily_file}{status_msg}\n\n{entry}")

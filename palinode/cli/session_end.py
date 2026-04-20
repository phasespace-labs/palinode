import click
import hashlib
import json
import os
from datetime import datetime, timezone
from palinode.core.config import config
from palinode.cli._format import print_result, get_default_format, OutputFormat


@click.command("session-end")
@click.argument("summary")
@click.option("--decision", "-d", multiple=True, help="Key decision made (repeatable)")
@click.option("--blocker", "-b", multiple=True, help="Open blocker or next step (repeatable)")
@click.option("--project", "-p", default=None, help="Project slug to append status to (e.g., 'palinode')")
@click.option("--source", help="Source surface (e.g., claude-code, cursor, api)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None, help="Output format")
def session_end(summary, decision, blocker, project, source, fmt):
    """Capture session outcomes to daily notes and project status.

    Call at the end of a coding or chat session to persist what was accomplished.

    Examples:

        palinode session-end "Implemented CLI wrapper with 22 commands"

        palinode session-end "Fixed embedding timeout" -d "Increase batch size" -b "Test under load" -p palinode
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()

    # Build session entry
    source_val = source or "cli"
    parts = [f"## Session End — {now_iso}\n"]
    parts.append(f"**Source:** {source_val}\n")
    parts.append(f"**Summary:** {summary}\n")

    decisions = list(decision)
    blockers = list(blocker)

    if decisions:
        parts.append("**Decisions:**")
        for d in decisions:
            parts.append(f"- {d}")
        parts.append("")
    if blockers:
        parts.append("**Blockers/Next:**")
        for b in blockers:
            parts.append(f"- {b}")
        parts.append("")

    session_entry = "\n".join(parts)

    # Write to daily notes
    daily_dir = os.path.join(config.memory_dir, "daily")
    os.makedirs(daily_dir, exist_ok=True)
    daily_path = os.path.join(daily_dir, f"{today}.md")
    with open(daily_path, "a") as f:
        f.write(f"\n{session_entry}\n")

    # Append status to project -status.md if project specified
    status_msg = ""
    if project:
        status_path = os.path.join(config.memory_dir, "projects", f"{project}-status.md")
        if os.path.exists(status_path):
            one_liner = summary.replace("\n", " ").strip()[:200]
            with open(status_path, "a") as f:
                f.write(f"\n- [{today}] {one_liner}\n")
            status_msg = f" + status → {project}-status.md"

    # Also save as an individual indexed memory file (M0: dual-write)
    individual_file = None
    try:
        import httpx
        short_hash = hashlib.sha256(summary.encode()).hexdigest()[:8]
        api_url = f"http://localhost:{config.services.api.port}/save"
        save_payload = {
            "content": session_entry,
            "type": "ProjectSnapshot" if project else "Insight",
            "slug": f"session-end-{today}-{project}-{short_hash}" if project else f"session-end-{today}-{short_hash}",
            "entities": [f"project/{project}"] if project else [],
            "source": source_val,
        }
        resp = httpx.post(api_url, json=save_payload, timeout=10.0)
        if resp.status_code == 200:
            individual_file = resp.json().get("file_path")
    except Exception:
        pass  # Non-fatal — daily append is the primary path

    # Git commit
    try:
        import subprocess
        files_to_add = [daily_path]
        if project and status_msg:
            files_to_add.append(os.path.join(config.memory_dir, "projects", f"{project}-status.md"))
        subprocess.run(
            ["git", "-C", config.memory_dir, "add"] + files_to_add,
            capture_output=True, check=False
        )
        subprocess.run(
            ["git", "-C", config.memory_dir, "commit", "-m", f"session-end: {summary[:72]}"],
            capture_output=True, check=False
        )
    except Exception:
        pass  # Non-fatal if git fails

    result = {
        "daily_file": f"daily/{today}.md",
        "individual_file": individual_file,
        "project_status": f"projects/{project}-status.md" if status_msg else None,
        "summary": summary,
        "decisions": decisions,
        "blockers": blockers,
    }

    effective_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if effective_fmt == OutputFormat.JSON:
        print_result(result, OutputFormat.JSON)
    else:
        click.echo(f"Session captured → daily/{today}.md{status_msg}\n\n{session_entry}")

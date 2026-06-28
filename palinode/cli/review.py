import json

import click
from rich.console import Console

from palinode.cli._api import HTTPStatusError, RequestError, api_client

console = Console()


@click.command()
@click.argument("project", required=False)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text", help="Output format")
def review(project, fmt):
    """Advisory project-memory review.

    Composes the deterministic health signals scoped to PROJECT (a slug like
    'palinode' or a typed ref 'project/palinode') and proposes corrective ops —
    read-only, applies nothing. Omit PROJECT to review the whole store.
    """
    try:
        data = api_client.review(project)
    except HTTPStatusError as e:
        console.print(f"[red]Error: API returned {e.response.status_code}[/red]")
        return
    except RequestError:
        # Fallback to local in-process review if the API is down.
        from palinode.core.review import run_review
        data = run_review(project=project)

    if fmt == "json":
        console.print(json.dumps(data, indent=2))
        return

    scope = data.get("project") or "whole store"
    summary = data.get("summary", {})
    console.print(f"\n[bold green]Palinode Memory Review[/bold green] — {scope}\n")
    console.print(
        f"[dim]{summary.get('scope_file_count', 0)} memories in scope · "
        f"{summary.get('finding_count', 0)} findings · "
        f"{summary.get('proposed_op_count', 0)} proposed ops[/dim]\n"
    )

    findings = data.get("findings", {})
    _section(findings.get("stale", []), "Stale active files", lambda x: f"{x['file']} ({x['days_old']} days)")
    _section(findings.get("open_questions", []), "Long-unresolved open questions", lambda x: f"{x['file']} ({x['days_old']} days)")
    _section(findings.get("contradictions", []), "Open contradictions", lambda x: f"{x['file']} → {', '.join(x.get('contradicts', []))}")
    _section(findings.get("orphaned", []), "Orphaned memories", lambda x: x)
    _section(findings.get("missing_descriptions", []), "Missing descriptions", lambda x: x)
    _section(findings.get("wiki_drift", []), "Wiki drift", lambda x: x["file"])

    ops = data.get("proposed_ops", [])
    if ops:
        console.print(f"[bold cyan]Proposed ops ({len(ops)})[/bold cyan] [dim](advisory — none applied)[/dim]")
        for op in ops:
            console.print(f"  [cyan]{op['op']}[/cyan] {op.get('file', '')}\n    [dim]{op.get('reason', '')}[/dim]")
    else:
        console.print("[green]✓ No corrective ops proposed[/green]")

    for hint in data.get("hints", []):
        console.print(f"[dim]· {hint}[/dim]")


def _section(items, title, render):
    if not items:
        return
    console.print(f"[bold yellow]{title} ({len(items)})[/bold yellow]")
    for it in items:
        try:
            console.print(f"  - {render(it)}")
        except Exception:
            console.print(f"  - {it}")
    console.print("")

import click
import json
from rich.console import Console

from palinode.cli._api import HTTPStatusError, RequestError, api_client
from palinode.lint.contradictions import (
    DEFAULT_MAX_LLM_CALLS,
    DEFAULT_SIMILARITY_THRESHOLD,
)

console = Console()

@click.command()
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text", help="Output format")
@click.option(
    "--deep-contradictions",
    "deep_contradictions",
    is_flag=True,
    default=False,
    help=(
        "Run LLM-confirmed semantic contradiction check across Decision memories. "
        "Requires the configured LLM endpoint to be reachable."
    ),
)
@click.option(
    "--max-llm-calls",
    "max_llm_calls",
    type=int,
    default=DEFAULT_MAX_LLM_CALLS,
    show_default=True,
    help="Hard cap on LLM calls during --deep-contradictions (per run).",
)
@click.option(
    "--similarity-threshold",
    "similarity_threshold",
    type=float,
    default=DEFAULT_SIMILARITY_THRESHOLD,
    show_default=True,
    help="Cosine similarity floor for candidate pairs in --deep-contradictions (0–1).",
)
def lint(fmt, deep_contradictions, max_llm_calls, similarity_threshold):
    """Scan memory and report orphans, stale files, and contradictions."""
    try:
        data = api_client.lint()
    except HTTPStatusError as e:
        console.print(f"[red]Error: API returned {e.response.status_code}[/red]")
        return
    except RequestError:
        # Fallback to local import if API is down
        from palinode.core.lint import run_lint_pass
        data = run_lint_pass()

    if fmt == "json" and not deep_contradictions:
        console.print(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold green]Palinode Memory Lint Report[/bold green]\n")

    if data["missing_fields"]:
        console.print(f"[bold yellow]Missing Frontmatter ({len(data['missing_fields'])})[/bold yellow]")
        for mf in data["missing_fields"]:
             console.print(f"  - {mf['file']}: missing {', '.join(mf['missing'])}")
    else:
        console.print("[green]✓ No files missing frontmatter[/green]")

    console.print("")

    if data["orphaned_files"]:
        console.print(f"[bold yellow]Orphaned Files ({len(data['orphaned_files'])})[/bold yellow]")
        for of in data["orphaned_files"]:
             console.print(f"  - {of}")
    else:
        console.print("[green]✓ No orphaned files[/green]")

    console.print("")

    if data["stale_files"]:
        console.print(f"[bold yellow]Stale Active Files ({len(data['stale_files'])})[/bold yellow]")
        for sf in data["stale_files"]:
             console.print(f"  - {sf['file']} ({sf['days_old']} days old)")
    else:
        console.print("[green]✓ No stale active files (>90 days)[/green]")

    console.print("")

    if data["contradictions"]:
        console.print(f"[bold yellow]Potential Contradictions ({len(data['contradictions'])})[/bold yellow]")
        for ct in data["contradictions"]:
             console.print(f"  - {ct['entity']}: {ct['issue']}")
    else:
        console.print("[green]✓ No contradictions detected[/green]")

    console.print("")

    # M0: new checks
    missing_ent = data.get("missing_entities", [])
    if missing_ent:
        console.print(f"[bold yellow]Missing Entities ({len(missing_ent)})[/bold yellow]")
        for me in missing_ent:
            console.print(f"  - {me}")
    else:
        console.print("[green]✓ All files have entity refs[/green]")

    console.print("")

    missing_desc = data.get("missing_descriptions", [])
    if missing_desc:
        console.print(f"[bold yellow]Missing Descriptions ({len(missing_desc)})[/bold yellow]")
        for md in missing_desc:
            console.print(f"  - {md}")
    else:
        console.print("[green]✓ All files have descriptions[/green]")

    console.print("")

    # #459: source-citation anchor integrity (drifted / missing / tampered).
    source_issues = data.get("source_anchor_issues", [])
    if source_issues:
        console.print(f"[bold yellow]Source Anchor Issues ({len(source_issues)})[/bold yellow]")
        for si in source_issues:
            for anchor in si["anchors"]:
                console.print(
                    f"  - {si['file']}: [{anchor['status']}] {anchor['ref']} — {anchor['detail']}"
                )
    else:
        console.print("[green]✓ No source-anchor issues[/green]")

    console.print("")

    # #72: long-lived unresolved open questions (epistemic: open_question).
    stale_oq = data.get("stale_open_questions", [])
    if stale_oq:
        console.print(f"[bold yellow]Stale Open Questions ({len(stale_oq)})[/bold yellow]")
        for oq in stale_oq:
            console.print(f"  - {oq['file']} ({oq['days_old']} days old)")
    else:
        console.print("[green]✓ No stale open questions (>90 days)[/green]")
    # #533 (G4): unresolved typed contradiction links (neither side won yet).
    open_contradictions = data.get("open_contradictions", [])
    if open_contradictions:
        console.print(
            f"[bold yellow]Open Contradictions ({len(open_contradictions)})[/bold yellow]"
        )
        for oc in open_contradictions:
            refs = ", ".join(oc.get("contradicts", []))
            console.print(f"  - {oc['file']} contradicts: {refs}")
    else:
        console.print("[green]✓ No open contradictions[/green]")

    console.print("")

    core_count = data.get("core_count", 0)
    if core_count > 10:
        console.print(f"[bold red]Core Files: {core_count}[/bold red] (recommended: ≤10 — prune with `palinode list --core-only`)")
    elif core_count > 0:
        console.print(f"[green]Core Files: {core_count}[/green]")
    else:
        console.print("[dim]No core files found[/dim]")

    console.print("")

    # --deep-contradictions: LLM-confirmed semantic check (opt-in only)
    if deep_contradictions:
        _run_deep_contradictions_output(
            fmt=fmt,
            similarity_threshold=similarity_threshold,
            max_llm_calls=max_llm_calls,
        )


def _run_deep_contradictions_output(
    fmt: str,
    similarity_threshold: float,
    max_llm_calls: int,
) -> None:
    """Execute deep contradiction check and render results."""
    from palinode.lint.contradictions import run_deep_contradiction_check

    console.print("[bold cyan]Running deep contradiction check (LLM-confirmed)...[/bold cyan]")
    try:
        result = run_deep_contradiction_check(
            similarity_threshold=similarity_threshold,
            max_llm_calls=max_llm_calls,
        )
    except Exception as exc:
        console.print(f"[red]Deep contradiction check failed: {exc}[/red]")
        return

    decisions = result["decisions_found"]
    candidates = result["candidate_pairs"]
    calls = result["llm_calls"]
    budget = result["llm_budget"]
    contradictions = result["contradictions"]

    if fmt == "json":
        console.print(json.dumps(result, indent=2))
        return

    console.print(
        f"  Compared {candidates} candidate pair(s) across {decisions} Decision memories."
    )
    console.print(f"  LLM calls: {calls} / {budget} budget.\n")

    if contradictions:
        for ct in contradictions:
            console.print("[bold yellow]⚠ Possible contradiction:[/bold yellow]")
            console.print(f"  {ct['file_a']}")
            console.print(f"  {ct['file_b']}")
            console.print(f"  Similarity: {ct['similarity']}")
            if ct["llm_explanation"]:
                console.print(f"  LLM: \"{ct['llm_explanation']}\"")
            console.print("")
    else:
        console.print("[green]✓ No semantic contradictions detected.[/green]")
    console.print("")

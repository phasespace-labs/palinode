import click
from rich.console import Console

from palinode.cli._api import HTTPStatusError, RequestError, api_client
from palinode.cli._format import emit_json
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
    """Scan memory and report every deterministic memory-health check."""
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
        emit_json(data)
        return

    console.print("\n[bold green]Palinode Memory Lint Report[/bold green]\n")
    console.print(f"[dim]Files scanned: {data.get('total_files', 0)}[/dim]\n")

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

    relative_dates = data.get("relative_dates", [])
    if relative_dates:
        match_count = sum(len(item.get("matches", [])) for item in relative_dates)
        console.print(f"[bold yellow]Relative Dates ({match_count})[/bold yellow]")
        for item in relative_dates:
            for match in item.get("matches", []):
                console.print(
                    f"  - {item['file']}:{match['line']}: {match['expression']}"
                )
    else:
        console.print("[green]✓ No relative dates[/green]")

    console.print("")

    missing_priority = data.get("missing_priority", [])
    if missing_priority:
        console.print(
            f"[bold yellow]Missing Priority ({len(missing_priority)})[/bold yellow]"
        )
        for path in missing_priority:
            console.print(f"  - {path}", markup=False)
    else:
        console.print("[green]✓ All core and Decision memories have priority[/green]")

    console.print("")

    wiki_drift = data.get("wiki_drift", [])
    if wiki_drift:
        console.print(f"[bold yellow]Wiki Drift ({len(wiki_drift)})[/bold yellow]")
        for drift in wiki_drift:
            for warning in drift["warnings"]:
                console.print(
                    f"  - {drift['file']}: [{warning['kind']}] {warning['detail']}",
                    markup=False,
                )
    else:
        console.print("[green]✓ No wiki drift[/green]")

    console.print("")

    # source-citation anchor integrity (drifted / missing / tampered).
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

    # claim-level anchor issues (span integrity / id mismatch / undeclared source).
    claim_issues = data.get("claim_anchor_issues", [])
    if claim_issues:
        console.print(f"[bold yellow]Claim Anchor Issues ({len(claim_issues)})[/bold yellow]")
        for ci in claim_issues:
            for claim in ci["claims"]:
                issues = ", ".join(claim.get("issues", []))
                console.print(
                    f"  - {ci['file']}: [{issues}] {claim['claim_id']} → {claim['source_id']}"
                )
    else:
        console.print("[green]✓ No claim-anchor issues[/green]")

    console.print("")

    # long-lived unresolved open questions (epistemic: open_question).
    stale_oq = data.get("stale_open_questions", [])
    if stale_oq:
        console.print(f"[bold yellow]Stale Open Questions ({len(stale_oq)})[/bold yellow]")
        for oq in stale_oq:
            console.print(f"  - {oq['file']} ({oq['days_old']} days old)")
    else:
        console.print("[green]✓ No stale open questions (>90 days)[/green]")
    # (G4): unresolved typed contradiction links (neither side won yet).
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

    # Refs that look like aliases of one another. A split entity makes every
    # lookup return a plausible, non-empty, INCOMPLETE result — under-recall that
    # never announces itself. Reported as a question, never auto-merged: a short
    # form and a longer one may be two different people, and a wrong join is
    # unrecoverable while a split is merely invisible.
    entity_aliases = data.get("entity_aliases", [])
    if entity_aliases:
        console.print(
            f"[bold yellow]Possible Entity Aliases ({len(entity_aliases)})[/bold yellow]"
        )
        hi = [c for c in entity_aliases if c.get("confidence") != "low"]
        lo = [c for c in entity_aliases if c.get("confidence") == "low"]

        def _emit(cluster):
            members = " | ".join(
                f"{r['ref']} ({r['files']} files)" for r in cluster["refs"]
            )
            console.print(f"  - [{cluster['kind']}] {members}")
            console.print(f"    [dim]{cluster['detail']}[/dim]")

        for cluster in hi:
            _emit(cluster)
        if lo:
            console.print(
                f"  [dim]— {len(lo)} lower-confidence candidate(s): the store's own "
                f"usage says these are probably two subjects, not one split. Each "
                f"cluster's detail says why —[/dim]"
            )
            for cluster in lo:
                _emit(cluster)
        console.print(
            "  [dim]Review before acting — these are candidates, not confirmed "
            "duplicates.[/dim]"
        )
    else:
        console.print("[green]✓ No entity-alias candidates[/green]")

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
        emit_json(result)
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

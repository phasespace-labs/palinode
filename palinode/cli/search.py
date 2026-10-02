import click
from rich.markup import escape
from palinode.cli._api import HTTPStatusError, api_client
from palinode.cli._format import print_result, console, OutputFormat, get_default_format
from palinode.core.parity import CATEGORIES, MEMORY_TYPES, RESOLVE_MODES, TIERS
from palinode.core.scoring import describe_match

#: How one evidence record reads, by (relation, direction) — same wording as
#: the MCP renderer, per-surface formatting (ADR-010).
_EVIDENCE_LABELS: dict[tuple[str, str], str] = {
    ("superseded_by", "forward"): "replaced by",
    ("superseded_by", "reverse"): "replaces",
    ("contradicts", "forward"): "contradicts",
    ("contradicts", "reverse"): "contradicted by",
    ("backed_by", "forward"): "backed by",
    ("backed_by", "reverse"): "backs",
}


def _evidence_lines(evidence: object) -> list[str]:
    """Rich-markup lines for a hit's ``evidence`` block (``--resolve``), or ``[]``."""
    if not isinstance(evidence, dict):
        return []
    lines: list[str] = []
    for bucket in ("replacements", "conflicts", "support", "discovered"):
        for rec in evidence.get(bucket) or []:
            if not isinstance(rec, dict):
                continue
            relation = str(rec.get("relation") or "linked")
            if rec.get("direction") == "discovered":
                label = f"discovered via {relation}"
            else:
                label = _EVIDENCE_LABELS.get((relation, str(rec.get("direction"))), relation)
            currency = str(rec.get("currency") or "")
            colour = "yellow" if currency in ("retired", "contested") else "dim"
            flags = [currency] if currency else []
            if rec.get("freshness") == "stale":
                flags.append("index stale")
            if rec.get("effective_at"):
                flags.append(str(rec["effective_at"])[:10])
            excerpt = str(rec.get("excerpt") or "").strip()
            if len(excerpt) > 160:
                excerpt = excerpt[:160].rstrip() + "…"
            head = escape(f"  ↳ {label}: {rec.get('ref')} [{', '.join(flags)}]")
            lines.append(f"[{colour}]{head}[/{colour}]" + (f" {escape(excerpt)}" if excerpt else ""))
    coverage = evidence.get("coverage") or {}
    if isinstance(coverage, dict) and coverage.get("status") == "partial":
        reasons = ", ".join(str(x) for x in coverage.get("reasons") or [])
        lines.append("[dim]" + escape(f"  ↳ coverage: partial ({reasons})") + "[/dim]")
    return lines


#: How one resolution outcome reads, and in what colour. Same three states as
#: the MCP renderer, per-surface formatting (ADR-010).
_OUTCOME_LABELS: dict[str, tuple[str, str]] = {
    "supported_current": ("current", "green"),
    "unresolved_conflict": ("unresolved conflict", "yellow"),
    "insufficient_evidence": ("insufficient evidence", "yellow"),
}


def _side_text(side: dict) -> str:
    bits = [str(side.get("kind") or "unknown"), str(side.get("currency") or "")]
    bits.extend(str(q) for q in side.get("qualifiers") or [])
    return f"{side.get('ref')} [{', '.join(b for b in bits if b)}]"


def _resolution_lines(resolution: object) -> list[str]:
    """Rich-markup lines for a hit's ``resolution`` block (``--resolve``), or ``[]``.

    The outcome and its reasons are decided server-side and only rendered
    here, so the CLI reading of a hit matches the MCP and REST ones exactly.
    """
    if not isinstance(resolution, dict):
        return []
    outcome = str(resolution.get("outcome") or "")
    label, colour = _OUTCOME_LABELS.get(outcome, (outcome, "dim"))
    reasons = ", ".join(str(r) for r in resolution.get("reasons") or [])
    head = f"  ⇒ {label}"
    current = resolution.get("current")
    if isinstance(current, dict):
        head += f": {_side_text(current)}"
    if reasons:
        head += f" — {reasons}"
    lines = [f"[{colour}]" + escape(head) + f"[/{colour}]"]
    sides = [s for s in resolution.get("sides") or [] if isinstance(s, dict)]
    if outcome != "supported_current" or len(sides) > 1:
        lines.extend(
            "[dim]" + escape(f"    · side: {_side_text(s)}") + "[/dim]" for s in sides
        )
    for group in resolution.get("support") or []:
        if not isinstance(group, dict):
            continue
        members = [str(m.get("ref")) for m in group.get("members") or [] if isinstance(m, dict)]
        if len(members) > 1:
            text = (
                f"    · support origin {group.get('origin_kind')}:{group.get('origin')} "
                f"— {len(members)} records ({', '.join(members)}) count once"
            )
            lines.append("[dim]" + escape(text) + "[/dim]")
    return lines


def _cli_resolve_scope():
    """This shell's project scope and the source that decided it.

    The same shared resolver every other surface calls: an explicit setting
    (``PALINODE_PROJECT``) before git/cwd inference, resolved per invocation.
    """
    from palinode.core.context_prime import ambient_cwd, resolve_context

    return resolve_context(cwd=ambient_cwd())


def _cli_resolve_context() -> list[str] | None:
    """List view of the common ADR-008 resolver for ambient search."""
    return _cli_resolve_scope().context


def _status_labels(res: dict) -> str:
    """Rich-markup labels for a hit's three provenance answers, or ``""``.

    ``freshness`` is index/source agreement only — the stored hash against the
    file — so it is labelled as such and never as the assertion being verified.
    ``span_integrity`` is whether the record's cited quotes are still in their
    sources. ``currency`` is whether the assertion is still in force; only the
    states a reader must not miss (``retired``, ``contested``) are labelled.
    Same vocabulary as the MCP renderer, per-surface formatting (ADR-010).
    """
    bits: list[str] = []
    freshness = res.get("freshness")
    if freshness == "valid":
        bits.append("[dim]" + escape("[index matches source]") + "[/dim]")
    elif freshness == "stale":
        bits.append("[yellow]" + escape("[⚠ index stale]") + "[/yellow]")
    span = res.get("span_integrity")
    if span == "ok":
        bits.append("[dim]" + escape("[cited quote found in source]") + "[/dim]")
    elif span and span != "unanchored":
        bits.append("[yellow]" + escape(f"[⚠ cited quote: {span}]") + "[/yellow]")
    currency = res.get("currency")
    if currency == "retired":
        reason = res.get("currency_reason")
        bits.append("[red]" + escape(f"[⚠ retired{': ' + reason if reason else ''}]") + "[/red]")
    elif currency == "contested":
        bits.append("[yellow]" + escape("[⚠ contested]") + "[/yellow]")
    other = res.get("other_project")
    if other:
        bits.append("[yellow]" + escape(f"[other project: {', '.join(other)}]") + "[/yellow]")
    return " ".join(bits)


@click.command()
@click.argument("query")
@click.option("--limit", default=3, help="Number of results (default: 3)")
@click.option(
    "--category",
    type=click.Choice(CATEGORIES),
    help="Filter by memory directory (people, projects, decisions, insights, research)",
)
@click.option(
    "--threshold",
    type=float,
    help="Vector similarity floor (0.0–1.0); ignored in lexical mode. Default from config.",
)
@click.option(
    "--since-days",
    type=int,
    help="Only return memories created/updated in the last N days.",
)
@click.option(
    "--types",
    "types",
    type=click.Choice(MEMORY_TYPES),
    multiple=True,
    help=(
        "Filter by memory type (PersonMemory, Decision, ProjectSnapshot, Insight, "
        "ResearchRef, ActionItem).  Repeat to allow multiple."
    ),
)
@click.option(
    "--min-priority",
    type=click.IntRange(1, 5),
    help="Only return memories with human-assigned priority at least N (missing defaults to 3).",
)
@click.option(
    "--date-after",
    help="Only return memories created/updated after this ISO date (e.g. 2026-01-01).",
)
@click.option(
    "--date-before",
    help="Only return memories created/updated before this ISO date.",
)
@click.option(
    "--include-daily",
    is_flag=True,
    help="Include daily/ session notes at full rank (default: penalized).",
)
@click.option(
    "--include-telemetry",
    is_flag=True,
    help=(
        "Include machine/monitor telemetry writes (metadata.kind: telemetry). "
        "Default: hard-excluded from recall (ADR-015)."
    ),
)
@click.option(
    "--tier",
    type=click.Choice(list(TIERS)),
    default=None,
    help=(
        "How much of each hit to return: abstract (~300 chars, summary first), "
        "overview (frontmatter + head of body), or full. Default: snippet view."
    ),
)
@click.option(
    "--resolve",
    type=click.Choice(list(RESOLVE_MODES)),
    default=None,
    help=(
        "Attach evidence around each hit: linked (follow superseded_by / "
        "contradicts / backed_by both ways under fixed budgets) or full (also "
        "bounded unlinked discovery). Each hit reports coverage and a "
        "resolution: a current answer, an unresolved conflict with both sides, "
        "or insufficient evidence. Default: none."
    ),
)
@click.option(
    "--include-retired",
    is_flag=True,
    default=False,
    help=(
        "With --resolve: also show retired records (archived, superseded, "
        "expired) in each hit's evidence, labelled as history."
    ),
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
@click.option("--score/--no-score", default=False, help="Show relevance scores")
@click.option("--no-context", is_flag=True, help="Disable ambient context boost")
@click.option(
    "--include-other-projects",
    is_flag=True,
    help=(
        "Also return records tagged to other projects, labelled with their "
        "project. Default: a project-scoped search leaves them out."
    ),
)
@click.option("--diagnostics", is_flag=True, help="Include retrieval diagnostics and receipt in JSON output")
def search(
    query,
    limit,
    category,
    threshold,
    since_days,
    types,
    min_priority,
    date_after,
    date_before,
    include_daily,
    include_telemetry,
    tier,
    resolve,
    include_retired,
    fmt,
    score,
    no_context,
    include_other_projects,
    diagnostics,
):
    """Search memory by meaning or keyword."""
    try:
        from palinode.core.context_prime import ProjectResolution

        scope = ProjectResolution(None, "none") if no_context else _cli_resolve_scope()
        # Always stated, empty included — see the MCP surface: an absent
        # context is the API's cue to apply its own pinned project, so
        # --no-context (and a shell where nothing resolved) has to say so.
        context = scope.context or []
        results, receipt = api_client.search(
            query,
            limit=limit,
            category=category,
            context=context,
            threshold=threshold,
            since_days=since_days,
            types=list(types) if types else None,
            min_priority=min_priority,
            date_after=date_after,
            date_before=date_before,
            include_daily=include_daily or None,
            include_telemetry=include_telemetry or None,
            tier=tier,
            resolve=resolve,
            receipt=True,
            include_other_projects=include_other_projects or None,
            include_retired=include_retired,
        )

        output_fmt = OutputFormat(fmt) if fmt else get_default_format()

        if output_fmt == OutputFormat.JSON:
            # The piped shape is the results array, unchanged — a script that
            # parses `palinode search --format json` keeps parsing it. The
            # receipt is rendered in text mode and returned in full by the
            # REST surface.
            payload = (
                {"results": results, "receipt": receipt, **scope.fields()}
                if diagnostics else results
            )
            print_result(payload, fmt=output_fmt)
        else:
            # Which project this search was scoped to, and why. First line:
            # a search that found nothing in the wrong project is exactly when
            # the reader needs it.
            console.print("[dim]" + escape(scope.describe()) + "[/dim]")
            if receipt and receipt.get("retrieval"):
                from palinode.core.scoring import (
                    describe_diagnostics,
                    describe_other_projects_withheld,
                )
                console.print(escape(describe_diagnostics(receipt["retrieval"])))
                other = describe_other_projects_withheld(
                    receipt["retrieval"].get("other_projects_withheld"),
                    delivered=len(results), project=scope.project, human=True,
                )
                if other:
                    console.print("[yellow]" + escape(other) + "[/yellow]")
            if not results:
                console.print("[yellow]No results found.[/yellow]")
                return
            
            for res in results:
                score_str = f"[{describe_match(res)}] " if score else ""
                title = res.get("file", "Untitled")
                # prefer the API-provided snippet — already match-windowed
                # and bounded. Fall back to the legacy blind-truncation path
                # when talking to an older API server that doesn't populate it.
                snippet = res.get("snippet")
                if snippet is not None:
                    body = snippet.strip()
                else:
                    raw = res.get("content", "")
                    body = raw.strip()[:200] + "..." if len(raw) > 200 else raw

                labels = _status_labels(res)
                console.print(f"[bold blue]{score_str}{title}[/bold blue]" + (f" {labels}" if labels else ""))
                console.print(f"  {body}")
                for line in _evidence_lines(res.get("evidence")):
                    console.print(line)
                for line in _resolution_lines(res.get("resolution")):
                    console.print(line)
                console.print()

            blocks = [r.get("evidence") for r in results if isinstance(r.get("evidence"), dict)]
            if blocks:
                from palinode.core.evidence import fold_coverage

                cov = fold_coverage(blocks)
                summary = f"Evidence coverage: {cov['status']}"
                if cov["reasons"]:
                    summary += " (" + ", ".join(cov["reasons"]) + ")"
                console.print("[dim]" + escape(summary) + "[/dim]")

            # The delivery receipt's identity. One line: the bundle id is the
            # handle the response, the retrieval log and any later bundle all
            # share, so it is what a human needs to carry between them.
            if receipt and receipt.get("bundle_id"):
                console.print(
                    "[dim]" + escape(f"Receipt: {receipt['bundle_id']}") + "[/dim]"
                )

    except HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        console.print(f"[red]Error searching memory:[/red] {detail or str(e)}")
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Error searching memory: {str(e)}[/red]")
        raise click.Abort()

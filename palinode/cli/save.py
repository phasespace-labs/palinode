import json as _json

import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.argument("content", required=False)
@click.option("--type", "memory_type", required=False, help="Memory type (e.g. PersonMemory, Decision, Insight, ProjectSnapshot)")
@click.option("--ps", "is_ps", is_flag=True, help="Shorthand for --type ProjectSnapshot (Palinode Save a mid-session snapshot)")
@click.option("--entity", "entities", multiple=True, help="Entity tag (e.g. person/X, project/X)")
@click.option(
    "-p",
    "--project",
    help="Project slug shorthand — e.g. 'palinode' becomes entity 'project/palinode'. "
         "Pairs with `palinode session-end -p` for consistent project tagging.",
)
@click.option("--file", "file_path", type=click.Path(exists=True), help="Read content from file instead of argument")
@click.option("--title", help="Optional title override")
@click.option(
    "--slug",
    help="URL-safe filename slug.  Auto-generated from content if omitted.",
)
@click.option(
    "--core/--no-core",
    "core",
    default=None,
    help=(
        "Mark this memory as core (always injected at session start).  "
        "Defaults to unset (regular memory)."
    ),
)
@click.option(
    "--confidence",
    type=float,
    help=(
        "Confidence in this memory's accuracy (0.0–1.0).  Stored as "
        "frontmatter; consumed by consolidation."
    ),
)
@click.option(
    "--importance",
    "priority",
    type=click.IntRange(1, 5),
    help="Human-assigned memory priority (1–5). Stored as `priority` frontmatter.",
)
@click.option("--important", is_flag=True, help="Shortcut for --importance 4.")
@click.option("--critical", is_flag=True, help="Shortcut for --importance 5.")
@click.option(
    "--metadata-json",
    "metadata",
    help=(
        "Extra frontmatter fields as a JSON object string, "
        "e.g. --metadata-json '{\"topic\": \"deployment\"}'.  "
        "Parsed before sending — matches the API's `metadata` field."
    ),
)
@click.option(
    "--external-ref",
    "external_ref_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "SDLC object reference in KEY=VALUE form. Repeatable. "
        "e.g. --external-ref gitlab_mr=myorg/myrepo!42 "
        "--external-ref linear_issue=PAL-1"
    ),
)
@click.option("--source", help="Source surface (e.g., claude-code, cursor, api)")
@click.option(
    "--cite",
    "sources",
    multiple=True,
    metavar="REF::QUOTE",
    help=(
        "Source-citation anchor in REF::QUOTE form (split on the first '::'). "
        "Repeatable. REF is a path under the memory dir; QUOTE is the exact "
        "cited passage. The integrity hash is computed on save. "
        "e.g. --cite 'research/paper.md::the exact cited passage'"
    ),
)
@click.option(
    "--update-policy",
    "update_policy",
    type=click.Choice(["append", "replace"]),
    default=None,
    help=(
        "Write-semantics axis (ADR-015). 'append' (default) is episodic; "
        "'replace' marks a living/current-state document — re-saving the same "
        "slug updates it in place and consolidation never supersedes/archives "
        "it. Persisted as sticky frontmatter."
    ),
)
@click.option(
    "--sync/--no-sync",
    default=False,
    help="Run the write-time contradiction check inline and include its result "
         "in the response. Default is async (fire-and-forget). Requires "
         "consolidation.write_time.enabled in config.",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def save(
    content,
    memory_type,
    is_ps,
    entities,
    project,
    file_path,
    title,
    slug,
    core,
    confidence,
    priority,
    important,
    critical,
    metadata,
    external_ref_pairs,
    source,
    sources,
    update_policy,
    sync,
    fmt,
):
    """Store a new memory.

    Use --ps as shorthand for --type ProjectSnapshot when dropping a quick
    mid-session note ("Palinode Save"). For structured session wrap-ups with
    decisions and blockers, use `palinode session-end` instead.
    """
    if file_path:
        with open(file_path, "r") as f:
            content = f.read()

    if not content:
        console.print("[red]Error: Must provide content or a file.[/red]")
        click.Abort()
        return

    # Resolve memory type: --ps is shorthand for ProjectSnapshot
    if is_ps and memory_type and memory_type != "ProjectSnapshot":
        console.print(f"[red]Error: --ps conflicts with --type {memory_type}. Pick one.[/red]")
        click.Abort()
        return
    if is_ps:
        memory_type = "ProjectSnapshot"
    if not memory_type:
        console.print("[red]Error: Must provide --type or --ps.[/red]")
        click.Abort()
        return

    priority_flags = int(priority is not None) + int(important) + int(critical)
    if priority_flags > 1:
        console.print("[red]Error: --importance, --important, and --critical conflict. Pick one.[/red]")
        raise click.Abort()
    if important:
        priority = 4
    elif critical:
        priority = 5

    # Parse --metadata-json into a dict.  Reject non-object payloads to
    # keep the API contract clear (metadata merges into a dict frontmatter).
    if metadata:
        raw = metadata
        try:
            metadata = _json.loads(raw)
        except _json.JSONDecodeError as e:
            console.print(f"[red]Error: --metadata-json is not valid JSON: {e}[/red]")
            click.Abort()
            return
        if not isinstance(metadata, dict):
            console.print("[red]Error: --metadata-json must be a JSON object.[/red]")
            click.Abort()
            return
    else:
        metadata = None

    # Parse --external-ref KEY=VALUE pairs into a dict.
    external_refs: dict | None = None
    if external_ref_pairs:
        external_refs = {}
        for pair in external_ref_pairs:
            if "=" not in pair:
                console.print(
                    f"[red]Error: --external-ref must be KEY=VALUE, got: {pair!r}[/red]"
                )
                click.Abort()
                return
            key, _, value = pair.partition("=")
            external_refs[key.strip()] = value

    # Parse --cite REF::QUOTE pairs into source-citation anchors (#459).
    # Split on the FIRST '::' so quotes may themselves contain '::'.
    source_anchors: list[dict] | None = None
    if sources:
        source_anchors = []
        for raw in sources:
            if "::" not in raw:
                console.print(
                    f"[red]Error: --cite must be REF::QUOTE, got: {raw!r}[/red]"
                )
                raise click.Abort()
            ref, _, quote = raw.partition("::")
            source_anchors.append({"ref": ref.strip(), "quote": quote})

    try:
        # ADR-010 / #167: do not default source here.  The HTTP client sets
        # X-Palinode-Source: cli on every request; only forward `source` in
        # the body when the user explicitly passed --source.
        result = api_client.save(
            content,
            memory_type,
            entities=list(entities),
            title=title,
            source=source,
            sync=sync,
            project=project,
            slug=slug,
            core=core,
            confidence=confidence,
            priority=priority,
            metadata=metadata,
            external_refs=external_refs,
            update_policy=update_policy,
            sources=source_anchors,
        )

        output_fmt = OutputFormat(fmt) if fmt else get_default_format()

        if output_fmt == OutputFormat.JSON:
            print_result(result, fmt=output_fmt)
        else:
            filename = result.get("file_path", result.get("file", "unknown"))
            id_str = result.get("id", "unknown")
            console.print(f"[green]Saved:[/green] {filename} (id: {id_str})")
            if sync and "write_time_check" in result:
                check = result["write_time_check"]
                ops = check.get("operations", [])
                applied = check.get("applied_stats", {})
                ms = check.get("llm_latency_ms", 0)
                console.print(
                    f"[dim]Write-time check:[/dim] {len(ops)} ops proposed, "
                    f"applied={applied}, llm={ms}ms"
                )

    except Exception as e:
        console.print(f"[red]Error saving memory: {str(e)}[/red]")
        click.Abort()

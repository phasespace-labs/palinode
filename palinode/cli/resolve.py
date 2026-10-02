import click
from rich.console import Console

from palinode.cli._api import HTTPStatusError, RequestError, api_client
from palinode.cli._format import OutputFormat, emit_json, get_default_format
from palinode.core.parity import RESOLVE_INTENTS

console = Console()


@click.command()
@click.argument("query", required=False)
@click.option("--ref", help="Exact memory ref (path without .md), e.g. decisions/db.")
@click.option(
    "--context",
    multiple=True,
    help="A ref you already hold; repeatable. Each is checked, not assumed current.",
)
@click.option(
    "--intent",
    type=click.Choice(list(RESOLVE_INTENTS)),
    default=None,
    help="What to answer. Only current state is supported.",
)
@click.option("--max-items", type=int, default=None, help="Max units in the answer.")
@click.option("--max-chars", type=int, default=None, help="Max characters in the answer.")
@click.option(
    "--include-retired",
    is_flag=True,
    default=False,
    help="Also show retired records (archived, superseded, expired), labelled as history.",
)
@click.option(
    "--include-other-projects",
    is_flag=True,
    default=False,
    help="Also show records tagged to other projects, labelled with their project.",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def resolve(
    query, ref, context, intent, max_items, max_chars, include_retired,
    include_other_projects, fmt,
):
    """Ask what memory currently holds about QUERY (or about one record).

    Returns the assertions that stand, what replaced what, conflicts with every
    side intact, and what is explicitly unknown — with coverage and source
    revisions. Read-only. Needs no model: with no embedder available it seeds
    from keyword search and says so in the coverage line.
    """
    if not query and not ref:
        raise click.UsageError("give a QUERY or --ref")
    from palinode.cli.search import _cli_resolve_scope
    from palinode.core.context_prime import InvalidProjectScope

    # This shell's project, resolved here: the API may be on another machine,
    # and its own directory says nothing about which project the caller is in.
    try:
        project = _cli_resolve_scope().project
    except InvalidProjectScope as e:
        raise click.ClickException(str(e))
    try:
        data = api_client.resolve(
            query=query,
            ref=ref,
            context=list(context) or None,
            intent=intent,
            max_items=max_items,
            max_chars=max_chars,
            include_retired=include_retired,
            project=project,
            include_other_projects=include_other_projects,
        )
    except HTTPStatusError as e:
        console.print(f"[red]Error: API returned {e.response.status_code}[/red]")
        raise SystemExit(1)
    except RequestError:
        # The API is down; resolve in-process. Nothing in this operation needs
        # the server — it is deterministic over the local store.
        from palinode.core.bundle import (
            DEFAULT_MAX_CHARS,
            DEFAULT_MAX_ITEMS,
            BundleBudget,
            BundleRequest,
            build_bundle,
        )

        request = BundleRequest(
            query=query,
            ref=ref,
            context=tuple(context),
            intent=intent or "current_state",
            budget=BundleBudget(
                max_items=DEFAULT_MAX_ITEMS if max_items is None else max_items,
                max_chars=DEFAULT_MAX_CHARS if max_chars is None else max_chars,
            ),
            include_other_projects=include_other_projects,
            include_retired=include_retired,
        )
        from palinode.core.config import config
        from palinode.core.scope import resolve_scope_chain

        chain = resolve_scope_chain(
            config, project=project.removeprefix("project/") if project else None,
        )
        data = build_bundle(request, chain=chain if chain.has_identity() else None).to_dict()

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        emit_json(data)
        return
    # The rendered text is the server's, not this command's: one template in
    # palinode.core.bundle, so CLI, MCP and REST read identically.
    click.echo(data.get("text", ""))

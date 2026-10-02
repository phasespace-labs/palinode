import json

import click

from palinode.cli._api import api_client
from palinode.cli._format import console, get_default_format, OutputFormat
from palinode.core.explain import DEFAULT_MAX_RECORDS


@click.command()
@click.argument("bundle_id")
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_MAX_RECORDS,
    show_default=True,
    help="Maximum supplied records to show. The rest are counted, not hidden.",
)
@click.option(
    "--diagnostics",
    is_flag=True,
    default=False,
    help=(
        "Also show the delivery's query prose and session id. Served only when the "
        "API is bound to loopback, or when --session-id matches the session that "
        "made the delivery; otherwise both come back marked withheld, with the reason."
    ),
)
@click.option(
    "--session-id",
    default=None,
    help=(
        "Resolve this session's scope chain; records it may not see stay redacted. "
        "Also unlocks --diagnostics against a remote API for that session's own "
        "deliveries."
    ),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default=None,
    help="Output format. Defaults to text on a TTY, JSON when piped.",
)
def explain(bundle_id, limit, diagnostics, session_id, fmt):
    """Explain one delivery: what context an agent received, and why.

    Takes the delivery reference a response handed back — the receipt's
    `bundle_id` — and reads the rows that delivery wrote to the retrieval log:
    the records supplied, the exact revision each was supplied at (and whether
    the source has changed since), the server-resolved scope, the calling
    surface, each record's disposition, and the delivery's coverage qualifiers.

    Fields the log never recorded are shown as `unavailable` with the reason,
    never guessed. A search that delivered nothing is explained as exactly that.
    Supplied context is not evidence an agent acted on it — Palinode records no
    such evidence, and this command says so rather than implying otherwise.

    `--diagnostics` asks the server for the delivery's query prose and session
    id. Against your own loopback-bound API that is served; against a remote
    API it is refused unless `--session-id` names the session that made the
    delivery, and the two fields come back marked withheld with the reason
    printed — the command never quietly shows less than you asked for.
    """
    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    try:
        data = api_client.explain(
            bundle_id,
            view="diagnostics" if diagnostics else "public",
            limit=limit,
            session_id=session_id,
        )
    except Exception as e:
        console.print(f"[red]Error explaining delivery: {str(e)}[/red]")
        raise SystemExit(1)

    if output_fmt == OutputFormat.JSON:
        # click.echo (not console.print): machine-readable JSON must not pass
        # through Rich's highlighter, which would inject ANSI colour codes.
        click.echo(json.dumps(data, indent=2))
        return

    from palinode.core.explain import format_explanation_text

    # markup=False: the render carries [disposition] brackets that Rich would
    # otherwise consume as style tags (the same guard trace and blame need).
    console.print(format_explanation_text(data), markup=False)

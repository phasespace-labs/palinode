import click
from palinode.cli._api import HTTPStatusError, ReadTimeout, RequestError, api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

#: Where a pass that outlived its client ends up. The API logs the run (under
#: systemd: ``journalctl -u palinode-api``); the consolidation logger also
#: writes here when file logging is configured.
CONSOLIDATION_LOG = "logs/consolidation.log"


def _timeout_report(seconds: float) -> dict:
    """What to tell an operator whose client stopped waiting.

    The request timing out does not cancel the pass: the server holds the
    store's run lock until it finishes, so the next invocation gets a 409 and
    the result of this one is only in the log.
    """
    # Lazy: the lock path is the run lock's own constant, and only the timeout
    # path needs it.
    from palinode.consolidation.run_lock import LOCK_RELATIVE_PATH

    lock = str(LOCK_RELATIVE_PATH)
    return {
        "status": "timeout",
        "timeout_seconds": seconds,
        "server_still_running": True,
        "lock": lock,
        "log": CONSOLIDATION_LOG,
        "message": (
            f"Stopped waiting after {seconds:.0f}s. The consolidation was not "
            f"cancelled — the server is still running it and holds {lock}, so "
            "another run returns 409 until it finishes. Results land in the API "
            f"log and {CONSOLIDATION_LOG}. Raise PALINODE_CONSOLIDATE_TIMEOUT to "
            "wait longer."
        ),
    }


@click.command()
@click.option("--nightly", is_flag=True, help="Run lightweight nightly pass (today only, UPDATE/SUPERSEDE)")
@click.option("--dry-run", is_flag=True, help="Preview changes without applying")
@click.option(
    "--source",
    "sources",
    multiple=True,
    metavar="DIR",
    help="Memory directory to consolidate; repeatable. Defaults to daily/.",
)
@click.option(
    "--respect-gate",
    "respect_gate",
    is_flag=True,
    help="Apply the activity gate (consolidation.auto_gate) to this run, as the "
         "cron path does; skip and report when a pass is not yet due.",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def consolidate(nightly, dry_run, sources, respect_gate, fmt):
    """Run or preview memory compaction (weekly full or --nightly lightweight).

    Runs unconditionally: the activity gate governs the automatic cron path,
    not an operator who has asked for a pass. ``--respect-gate`` opts this run
    into the same policy.

    A pass that reaches the LLM can run for minutes; the client waits
    ``PALINODE_CONSOLIDATE_TIMEOUT`` seconds (default 900) and, if that is not
    enough, reports that the server is still running rather than aborting
    silently.

    ``palinode dream`` is an alias; ``palinode consolidate`` is the canonical name.
    """
    output_fmt = OutputFormat(fmt) if fmt else get_default_format()

    try:
        data = api_client.consolidate(
            dry_run=dry_run,
            nightly=nightly,
            sources=list(sources) or None,
            respect_gate=respect_gate,
        )

        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
        else:
            if data.get("status") == "deferred":
                console.print(
                    f"[yellow]Consolidation skipped — {data['gate']['reason']}.[/yellow]"
                )
            elif dry_run:
                console.print("[cyan]Previewing consolidation...[/cyan]")
                for change in data.get("proposed_changes", []):
                    console.print(f"  [{change['type']}] {change['file']}")
            else:
                console.print("[green]Consolidation complete.[/green]")
                console.print(f"Stats: {data.get('stats', 'none')}")

    except ReadTimeout as e:
        # Catch before RequestError (its superclass) and before the blanket
        # handler below, whose click.Abort printed a bare "Aborted!" for the
        # one outcome an operator most needs explained.
        # Imported here, not at module scope: the budget is read at failure
        # time so an override is reflected in what we print.
        from palinode.cli import _api

        report = _timeout_report(_api.CONSOLIDATION_TIMEOUT_SECONDS)
        if output_fmt == OutputFormat.JSON:
            print_result(report, fmt=output_fmt)
        else:
            console.print(
                f"[yellow]Stopped waiting after "
                f"{report['timeout_seconds']:.0f}s — consolidation is still "
                "running on the server.[/yellow]"
            )
            console.print(
                f"The pass was not cancelled: the API holds {report['lock']} "
                "until it finishes, so another run returns 409 until then."
            )
            console.print(
                f"Results land in the API log and {report['log']}. "
                "Raise PALINODE_CONSOLIDATE_TIMEOUT to wait longer."
            )
        raise SystemExit(1) from e
    except HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        raise click.ClickException(
            detail or f"Consolidation request failed ({e.response.status_code})"
        ) from e
    except RequestError as e:
        raise click.ClickException(
            f"Cannot reach API — is palinode running? ({e})"
        ) from e
    except Exception as e:
        console.print(f"[red]Error consolidating: {str(e)}[/red]")
        raise click.Abort()

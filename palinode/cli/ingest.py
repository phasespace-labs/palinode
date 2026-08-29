import click
from palinode.cli._api import HTTPStatusError, RequestError, api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat


@click.command()
@click.option("--url", help="URL to fetch and save as a research reference")
@click.option("--name", help="Optional title for the reference")
@click.option("--inbox", is_flag=True, help="Process files in the inbox directory")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]))
def ingest(url, name, inbox, fmt):
    """Ingest a URL or process the inbox directory."""
    if not url and not inbox:
        raise click.UsageError("Provide --url <URL> or --inbox")

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()

    try:
        if inbox:
            data = api_client.ingest_inbox()
            if output_fmt == OutputFormat.JSON:
                print_result(data, fmt=output_fmt)
            else:
                console.print("[green]✓[/green] Inbox processed.")
        else:
            data = api_client.ingest_url(url=url, name=name)
            if output_fmt == OutputFormat.JSON:
                print_result(data, fmt=output_fmt)
            else:
                if data.get("status") == "success":
                    # Prefer the server-computed relative path (ADR-010
                    # parity with MCP/API) so the console message never
                    # leaks an absolute filesystem path.
                    shown = data.get("rel_path") or data.get("file_path", "research/")
                    console.print(f"[green]✓[/green] Saved to {shown}")
                else:
                    console.print("[yellow]No content extracted from URL.[/yellow]")
    except HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        console.print(f"[red]Error:[/red] {detail or e.response.status_code}")
        raise SystemExit(1)
    except RequestError as e:
        console.print(f"[red]Error:[/red] Cannot reach API — is palinode running? ({e})")
        raise SystemExit(1)

"""``palinode corrections`` — the candidate queue, and the review flow over it.

Bare ``palinode corrections`` is what it always was: a listing of candidates
proposed from harness session transcripts, applying nothing. The subcommands
are the review flow the listing used to say did not exist yet —
``preview`` (reads), ``apply`` (the only writer, and never the default),
``dismiss`` (records a decision against a candidate) and ``undo``
(recovery, with its own preview).

Every subcommand is TTY-aware like the rest of the CLI: human-readable when
interactive, JSON when piped.
"""
from __future__ import annotations

from typing import Any, Callable

import click

from palinode.cli._api import HTTPStatusError, RequestError, api_client
from palinode.cli._format import OutputFormat, console, emit_json, get_default_format
from palinode.core.parity import CORRECTION_ACTIONS, CORRECTION_APPLIED_PARTIAL


def _listing_options(command: Callable[..., Any]) -> Callable[..., Any]:
    """The four flags the listing takes, shared by the group and ``list``."""
    for decorator in reversed(
        [
            click.option("--project", help="Only candidates scoped to this project."),
            click.option(
                "--since", "since_days", type=int,
                help="Only candidates from the last N days; also narrows a --scan lookback.",
            ),
            click.option(
                "--scan", is_flag=True,
                help=(
                    "Run a detection pass over the configured transcript paths first. "
                    "Does nothing unless capture.transcripts is enabled in config."
                ),
            ),
            click.option(
                "--format", "fmt", type=click.Choice(["json", "text"]),
                help="Output format",
            ),
        ]
    ):
        command = decorator(command)
    return command


@click.group(invoke_without_command=True)
@_listing_options
@click.pass_context
def corrections(
    ctx: click.Context,
    project: str | None,
    since_days: int | None,
    scan: bool,
    fmt: str | None,
) -> None:
    """Review correction candidates mined from harness session transcripts.

    Candidates are proposals, never operations: each one quotes a bounded span
    of the user's own words with the session and turn it came from. Off by
    default — set `capture.transcripts.enabled` and name the transcript paths in
    `palinode.config.yaml` before `--scan` can read anything.

    With no subcommand this lists the queue. `preview` shows exactly what a
    correction would change; `apply` is the only thing that writes, and it needs
    the revision `preview` showed plus `--confirm`.
    """
    if ctx.invoked_subcommand is None:
        _run_listing(project, since_days, scan, fmt)


@corrections.command("list")
@_listing_options
def corrections_list(
    project: str | None, since_days: int | None, scan: bool, fmt: str | None
) -> None:
    """List correction candidates. The same listing bare `corrections` prints."""
    _run_listing(project, since_days, scan, fmt)


def _run_listing(
    project: str | None, since_days: int | None, scan: bool, fmt: str | None
) -> None:
    try:
        data = api_client.corrections(project=project, since_days=since_days, scan=scan)
    except HTTPStatusError as error:
        console.print(f"[red]Error: API returned {error.response.status_code}[/red]")
        raise SystemExit(1)
    except RequestError:
        # Local fallback: the queue is a file in this store, so a listing does
        # not need the server. A scan does not either, and both stay read-only.
        from palinode.corrections.scan import corrections_report
        data = corrections_report(project=project, since_days=since_days, scan=scan)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        emit_json(data)
        return
    _render(data)


def _render(data: dict[str, Any]) -> None:
    console.print("\n[bold green]Correction candidates[/bold green] [dim](proposals — nothing applied)[/dim]\n")
    if not data.get("enabled"):
        console.print(
            "[yellow]Transcript correction capture is disabled.[/yellow] "
            "Set [cyan]capture.transcripts.enabled[/cyan] and list transcript "
            "paths under [cyan]capture.transcripts.harness_paths[/cyan] to turn it on. "
            "Classification is a separate opt-in "
            "([cyan]capture.transcripts.classify[/cyan]) and is what sends text "
            "to a model.\n"
        )

    console.print(f"[dim]Classification: {data.get('classification', 'unknown')}[/dim]\n")

    scan = data.get("scan")
    if scan:
        if scan.get("blocked_reason"):
            console.print(f"[yellow]Scan did not run: {scan['blocked_reason']}[/yellow]")
        else:
            console.print(
                f"[dim]Scanned {scan.get('transcripts_read', 0)} transcript(s), "
                f"{scan.get('turns_scanned', 0)} eligible turn(s) → "
                f"{scan.get('candidates_detected', 0)} detected, "
                f"{scan.get('candidates_added', 0)} new, "
                f"{scan.get('duplicates', 0)} already known[/dim]"
            )
            classifier = scan.get("classifier") or {}
            if classifier.get("summary"):
                console.print(f"[dim]This pass: {classifier['summary']}[/dim]")
        for reason, count in sorted((scan.get("skipped") or {}).items()):
            console.print(f"[dim]  skipped {count} · {reason}[/dim]")
        console.print("")

    candidates = data.get("candidates") or []
    if not candidates:
        console.print("[dim]No candidates queued.[/dim]")
        return

    for candidate in candidates:
        scope = candidate.get("project") or "unscoped"
        status = candidate.get("status") or "proposed"
        state = "" if status == "proposed" else f" [yellow]{status}[/yellow]"
        console.print(
            f"[cyan]{candidate.get('classification')}[/cyan]{state} "
            f"[dim]{scope} · session {str(candidate.get('session_id'))[:8]} · "
            f"turn {candidate.get('turn_index')} · {candidate.get('occurred_at') or 'undated'}[/dim]"
        )
        console.print(f"  “{candidate.get('span', '')}”")
        console.print(f"  [dim]id: {candidate.get('candidate_id')}[/dim]")
        if candidate.get("rationale"):
            console.print(f"  [dim]reason: {candidate['rationale']}[/dim]")
        relation = candidate.get("relation")
        if relation:
            console.print(
                f"  [dim]replaces: {relation.get('replaced')} → {relation.get('replacement')}[/dim]"
            )
        resolution = candidate.get("resolution")
        if resolution:
            console.print(
                f"  [dim]{resolution.get('status')} {resolution.get('resolved_at')}"
                f" — {resolution.get('reason')}[/dim]"
            )
        console.print("")

    console.print(f"[dim]{len(candidates)} candidate(s). {data.get('applied', '')}[/dim]")


# ── review flow ─────────────────────────────────────────────────────────────
def _call(phase: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call the API, falling back to the in-process contract when it is down.

    Same shape as the listing's fallback and for the same reason: the store is
    a directory on this machine, so a review does not need a server. The
    fallback calls the identical functions the route calls, so there is one
    contract, not two.
    """
    try:
        return api_client.correction_review(phase, payload)
    except HTTPStatusError as error:
        try:
            detail = error.response.json().get("detail")
        except ValueError:
            detail = error.response.text
        if isinstance(detail, dict):
            if detail.get("applied") == CORRECTION_APPLIED_PARTIAL:
                # Not a refusal: half of it landed. The payload is the report.
                return detail
            return {"refused": detail.get("detail"), **detail}
        console.print(f"[red]Error: API returned {error.response.status_code}: {detail}[/red]")
        raise SystemExit(1)
    except RequestError:
        from palinode.corrections import review as review_module

        try:
            if phase == "preview":
                return review_module.preview_correction(**payload)
            if phase == "apply":
                return review_module.apply_correction(**payload)
            if phase == "dismiss":
                return review_module.dismiss_candidate(
                    payload["candidate_id"], reason=payload["reason"]
                )
            if payload.get("confirm"):
                return review_module.apply_undo(**payload)
            return review_module.preview_undo(target=payload["target"])
        except review_module.CorrectionError as refusal:
            return {"refused": str(refusal), **refusal.as_dict()}


def _emit(data: dict[str, Any], fmt: str | None, renderer: Callable[[dict[str, Any]], None]) -> None:
    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        emit_json(data)
    else:
        renderer(data)
    if (
        data.get("refused")
        or data.get("error")
        or data.get("applied") == CORRECTION_APPLIED_PARTIAL
    ):
        raise SystemExit(1)


def _render_refusal(data: dict[str, Any]) -> bool:
    message = data.get("refused") or data.get("detail")
    if not message:
        return False
    console.print(f"\n[red]Refused:[/red] {message}")
    for candidate in data.get("candidates") or []:
        console.print(f"  [dim]· {candidate}[/dim]")
    if data.get("expected"):
        console.print(
            f"  [dim]previewed revision {data['expected'][:12]} · "
            f"on disk now {str(data.get('actual'))[:12]}[/dim]"
        )
    console.print("")
    return True


def _render_preview(data: dict[str, Any]) -> None:
    if _render_refusal(data) and not data.get("target"):
        return
    target = data.get("target") or {}
    console.print("\n[bold green]Correction preview[/bold green] [dim](nothing written)[/dim]\n")
    console.print(f"[bold]{data.get('action')}[/bold] · {data.get('level')} level")
    console.print(f"  target      {target.get('file')}")
    console.print(f"  revision    [dim]{target.get('revision')}[/dim] ({target.get('revision_basis')})")
    console.print(f"  visibility  {(target.get('visibility') or {}).get('label')}")
    console.print(f"  policies    update_policy={target.get('update_policy')} · "
                  f"retirement={target.get('retirement_policy')}")
    console.print(f"  scope       {(data.get('scope') or {}).get('project') or 'unscoped'}")
    console.print(f"\n[dim]old[/dim]\n  {data.get('old_text')}")
    if data.get("new_text"):
        console.print(f"\n[dim]new[/dim]\n  {data.get('new_text')}")
    if (data.get("content_loss") or {}).get("removed_text"):
        console.print("\nOriginal text that would stop being delivered from this record:")
        for text in data["content_loss"]["removed_text"]:
            console.print(text, markup=False)
    console.print(f"\n[dim]rationale[/dim]  {data.get('rationale') or '(none given)'}")

    source = data.get("source") or {}
    console.print(f"[dim]source[/dim]     {source.get('kind')}")
    if source.get("span"):
        console.print(f"  “{source['span']}”")
        console.print(
            f"  [dim]session {str(source.get('session_id'))[:8]} · "
            f"turn {source.get('turn_index')} · {source.get('occurred_at')}[/dim]"
        )
    if source.get("unavailable_reason"):
        console.print(f"  [yellow]{source['unavailable_reason']}[/yellow]")

    relation = data.get("relation") or {}
    console.print(f"\n[dim]relation[/dim]   {relation.get('recorded_as')}")

    referencing = data.get("referencing_records") or []
    if referencing:
        console.print("\n[yellow]Other records referencing this one (reported, not changed):[/yellow]")
        for row in referencing:
            console.print(f"  · {row['file']} [dim]({row['relation']})[/dim]")

    for warning in data.get("warnings") or []:
        console.print(f"\n[yellow]{warning}[/yellow]")
    for note in data.get("notes") or []:
        console.print(f"\n[dim]{note}[/dim]")

    policy = data.get("capture_policy") or {}
    if not policy.get("apply_allowed", True):
        console.print(f"\n[yellow]{policy.get('note')}[/yellow]")

    recovery = data.get("recovery") or {}
    console.print(f"\n[dim]recovery[/dim]   {recovery.get('undo_preview_command')}")
    confirm = data.get("confirm") or {}
    if confirm.get("command"):
        console.print(f"\n[bold]To apply:[/bold]\n  {confirm['command']}\n")


def _render_partial(data: dict[str, Any]) -> None:
    """A half-finished apply, led by the half that is missing."""
    partial = data.get("partial") or {}
    console.print("\n[bold yellow]Correction partially applied[/bold yellow]\n")
    console.print(f"  [green]written[/green]      {partial.get('written')}")
    console.print(f"  [red]not written[/red]  {partial.get('not_written')}")
    console.print(f"  [dim]failed at[/dim]    {partial.get('failed_step')} — {partial.get('error')}")
    if partial.get("candidate"):
        console.print(f"  [dim]candidate[/dim]    {partial['candidate']}")
    console.print(f"\n[bold]To complete it:[/bold]\n  {partial.get('complete_command')}")
    console.print(f"\n[dim]to unwind instead:[/dim] {partial.get('unwind_command')}")
    console.print(f"[dim]{partial.get('unwind_note')}[/dim]")
    console.print(f"[yellow]{partial.get('undo')}[/yellow]\n")


def _render_apply(data: dict[str, Any]) -> None:
    if _render_refusal(data):
        return
    if data.get("applied") == CORRECTION_APPLIED_PARTIAL:
        _render_partial(data)
        return
    target = data.get("target") or {}
    console.print("\n[bold green]Correction applied[/bold green]\n")
    console.print(f"  {data.get('action')} · {target.get('file')}")
    replacement = data.get("replacement") or {}
    if replacement.get("rel_path"):
        console.print(f"  replacement  {replacement['rel_path']}")
    console.print(f"  actor        {data.get('actor')}")
    console.print(f"  committed    {data.get('committed')}")
    recovery = data.get("recovery") or {}
    console.print(f"\n[dim]undo preview:[/dim] {recovery.get('undo_preview_command')}")
    console.print(f"[dim]history:[/dim]      {recovery.get('history_command')}\n")


def _render_dismiss(data: dict[str, Any]) -> None:
    if _render_refusal(data):
        return
    candidate = data.get("candidate") or {}
    console.print("\n[bold green]Candidate dismissed[/bold green]\n")
    console.print(f"  {candidate.get('candidate_id')} — {(candidate.get('resolution') or {}).get('reason')}")
    if candidate.get("already_resolved"):
        console.print("  [yellow]already resolved; nothing was written[/yellow]")
    console.print(f"\n[dim]{data.get('note')}[/dim]\n")


def _render_undo(data: dict[str, Any]) -> None:
    if _render_refusal(data) and not data.get("target"):
        return
    target = data.get("target") or {}
    heading = "Undo applied" if data.get("applied") else "Undo preview"
    console.print(f"\n[bold green]{heading}[/bold green]\n")
    console.print(f"  target       {target.get('file')} [dim]({target.get('status')})[/dim]")
    if target.get("superseded_by"):
        console.print(f"  superseded by {target['superseded_by']}")
    console.print(f"\n  [green]does[/green]      {data.get('restores')}")
    console.print(f"  [yellow]does not[/yellow]  {data.get('does_not_delete_history')}")
    console.print(f"  [red]cannot[/red]    {data.get('cannot_reach_external_actions')}")
    for line in data.get("also_true") or []:
        console.print(f"  [dim]also[/dim]      {line}")
    confirm = data.get("confirm") or {}
    if confirm.get("command"):
        console.print(f"\n[bold]To undo:[/bold]\n  {confirm['command']}\n")
    else:
        console.print("")


_TARGET_OPTIONS = [
    click.option("--target", help="Memory to correct: 'decisions/x.md', 'decisions/x', or a slug."),
    click.option("--claim", "claim_id", help="Narrow the correction to one <!-- fact:id --> claim."),
    click.option("--allow-content-loss", is_flag=True,
                 help="Explicitly allow dropping the original text listed by preview."),
    click.option("--replacement", help="The text that stands instead of the old statement."),
    click.option(
        "--action", type=click.Choice(list(CORRECTION_ACTIONS)),
        help="supersede (a replacement stands) or retire (nothing does). "
             "Derived from --replacement when omitted.",
    ),
    click.option("--reason", help="Why. Recorded in the history sibling and the commit."),
    click.option("--candidate", "candidate_id", help="The queued candidate this correction came from."),
    click.option("--project", help="Project scope. Inferred from the target when omitted."),
    click.option(
        "--backed-by", "backed_by", multiple=True,
        help="A record supporting the REPLACEMENT. Repeatable. The superseded "
             "original is never cited as support for its replacement.",
    ),
    click.option("--type", "type_", help="Memory type for the replacement. Inherits the target's."),
    click.option("--slug", help="Slug for the replacement. Derived from the target's when omitted."),
    click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format"),
]


def _target_options(command: Callable[..., Any]) -> Callable[..., Any]:
    for decorator in reversed(_TARGET_OPTIONS):
        command = decorator(command)
    return command


def _payload(
    target: str | None, claim_id: str | None, replacement: str | None, action: str | None,
    reason: str | None, candidate_id: str | None, project: str | None,
    backed_by: tuple[str, ...], type_: str | None, slug: str | None,
    allow_content_loss: bool = False,
) -> dict[str, Any]:
    return {
        "target": target,
        "claim_id": claim_id,
        "replacement": replacement,
        "allow_content_loss": allow_content_loss,
        "action": action,
        "reason": reason,
        "candidate_id": candidate_id,
        "project": project,
        "backed_by": list(backed_by) or None,
        "type": type_,
        "slug": slug,
    }


@corrections.command("preview")
@_target_options
def corrections_preview(
    target: str | None, claim_id: str | None, replacement: str | None, action: str | None,
    reason: str | None, candidate_id: str | None, project: str | None,
    backed_by: tuple[str, ...], type_: str | None, slug: str | None, fmt: str | None,
    allow_content_loss: bool,
) -> None:
    """Show exactly what a correction would change. Writes nothing.

    Prints the old text, the proposed new text, the target's exact revision,
    the rationale, where the correction came from, the scope, the relation that
    would be recorded, the other records that reference the target (reported,
    never changed) and the recovery command — plus the `apply` invocation,
    with the revision baked in.
    """
    payload = _payload(
        target, claim_id, replacement, action, reason, candidate_id, project,
        backed_by, type_, slug, allow_content_loss,
    )
    _emit(_call("preview", payload), fmt, _render_preview)


@corrections.command("apply")
@_target_options
@click.option("--expect-revision", required=True, help="The revision `preview` showed.")
@click.option("--confirm", is_flag=True, help="Required. Apply is never the default.")
def corrections_apply(
    target: str | None, claim_id: str | None, replacement: str | None, action: str | None,
    reason: str | None, candidate_id: str | None, project: str | None,
    backed_by: tuple[str, ...], type_: str | None, slug: str | None, fmt: str | None,
    allow_content_loss: bool,
    expect_revision: str, confirm: bool,
) -> None:
    """Apply a previewed correction. Needs `--expect-revision` and `--confirm`.

    Refuses a stale revision (the file changed since the preview) and an
    ambiguous target, returning what it found rather than choosing. Writes only
    through the existing save / archive / executor path, with git provenance.

    If the replacement is saved and the original then fails to retire, this
    exits non-zero with `applied: partial`: what was written, what was not, and
    the `palinode archive --superseded-by` command that completes it.
    """
    payload = _payload(
        target, claim_id, replacement, action, reason, candidate_id, project,
        backed_by, type_, slug, allow_content_loss,
    )
    payload.update({"expect_revision": expect_revision, "confirm": confirm})
    _emit(_call("apply", payload), fmt, _render_apply)


@corrections.command("dismiss")
@click.option("--candidate", "candidate_id", required=True, help="The candidate to decline.")
@click.option("--reason", required=True, help="Why it was declined. This is the record.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def corrections_dismiss(candidate_id: str, reason: str, fmt: str | None) -> None:
    """Record that a reviewer looked at a candidate and declined it.

    The row is marked, never deleted — which is what stops the next scan
    proposing the same span again.
    """
    _emit(
        _call("dismiss", {"candidate_id": candidate_id, "reason": reason}),
        fmt,
        _render_dismiss,
    )


@corrections.command("undo")
@click.option("--target", required=True, help="The archived memory to restore.")
@click.option("--expect-revision", default="", help="The revision the undo preview showed.")
@click.option("--confirm", is_flag=True, help="Required to write. Preview is the default.")
@click.option("--reason", help="Why the correction is being undone.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def corrections_undo(
    target: str, expect_revision: str, confirm: bool, reason: str | None, fmt: str | None
) -> None:
    """Preview (default) or apply the undo of a correction.

    Restoring a previous assertion, deleting history and undoing an agent's
    external actions are three different things: the output names all three,
    does the first, refuses the second and cannot reach the third.
    """
    payload: dict[str, Any] = {
        "target": target,
        "expect_revision": expect_revision,
        "confirm": confirm,
        "reason": reason,
    }
    _emit(_call("undo", payload), fmt, _render_undo)

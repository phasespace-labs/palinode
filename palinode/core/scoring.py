"""How a search hit's score, and a delivery's confidence, are described.

The fused score is a rank, not a similarity. RRF gives the top-ranked hit
1.0 whether it is an excellent match or the least bad of a weak field, so no
surface presents it as confidence. Cosine is presented that way, when the
vector arm produced one.

The same reason the fused score cannot be presented as confidence is why the
delivery-level verdict exists: :mod:`palinode.core.confidence` decides it from
the pre-fusion arm scores, and the renderers here are what every surface uses
to say it, so the MCP, CLI and inspector readings of one delivery cannot
disagree about whether the store had an answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_RAW_SCORE = "raw_score"


def _percent(raw: float) -> int:
    # Half-up, matching the jq hook and both TypeScript renderers
    # (Math.round); Python's round() does banker's rounding instead.
    return int(raw * 100 + 0.5)


def describe_match(result: Mapping[str, Any]) -> str:
    """A short phrase for how well ``result`` matched.

    Three cases, and the middle one is the reason this exists:

    - a cosine similarity is present, so say so as a percentage
    - ``raw_score`` is present and ``None``: a BM25-only hit, which the
      ranker marks explicitly. There is no similarity to report, so none is
      claimed, and the fused value is shown labelled as rank
    - ``raw_score`` is absent: a pre-0.12 server that never sent the field.
      The arm is unknown, so the rank is all that can be said
    """
    fused = result.get("score") or 0.0
    if _RAW_SCORE not in result:
        return f"rank {fused:.2f}"
    raw = result.get(_RAW_SCORE)
    if raw is None:
        return f"keyword match, rank {fused:.2f}"
    return f"{_percent(raw)}% match"


def describe_arms(value: Mapping[str, Any]) -> str:
    """The arm evidence behind a verdict: each arm's best score and its mark.

    Only arms that produced evidence in this delivery are named — an arm that
    did not run, or whose candidates never reached the slate, has nothing to
    report and says nothing rather than reporting a zero.
    """
    arms = value.get("arms")
    if not isinstance(arms, Mapping):
        return ""
    bits = []
    for name in ("vector", "keyword"):
        arm = arms.get(name)
        if not isinstance(arm, Mapping) or arm.get("best") is None:
            continue
        bits.append(
            f"best {name} {float(arm['best']):.2f} "
            f"(confident at {float(arm['confident_at']):.2f})"
        )
    return ", ".join(bits)


def describe_diagnostics(value: dict[str, Any]) -> str:
    line = (f"Retrieval: {value['active_mode']} · index: {value['index_state']}"
            + (f" · {value['outcome']}" if value.get("outcome") else ""))
    verdict = value.get("confidence")
    if verdict:
        evidence = describe_arms(value)
        line += f" · match confidence: {verdict}"
        line += f" ({evidence})" if evidence else " (no arm evidence)"
        if value.get("corroboration") == "missing":
            # Say why the verdict is below what an arm on its own claimed, or
            # the arm evidence beside it reads as a contradiction.
            line += " · uncorroborated: the keyword arm does not reach this query"
        if value.get("co_occurrence") == "chance":
            line += " · keyword match is what chance gives this index"
        coverage = value.get("subject_coverage")
        if coverage == "no_subject":
            line += " · the prompt names nothing a memory could be about"
        elif coverage == "working_state_only":
            line += " · keyword match covers none of the prompt's subject"
    return line


def describe_no_confident_match(
    value: Mapping[str, Any], *, delivered: bool, withheld: int = 0
) -> str:
    """The line a surface leads with when nothing delivered is worth trusting.

    Empty unless the verdict is ``none``: the other two verdicts are carried by
    :func:`describe_diagnostics` and do not warrant a banner.

    It leads rather than trails because it has to be read *before* the results
    it qualifies. Three endings, one per reason the slate looks the way it
    does — rows delivered underneath (the default), rows withheld by the
    opt-in switch, or nothing retrieved at all. Collapsing the last two would
    make a configured abstention indistinguishable from an empty store, which
    is the reason the switch is off by default. An empty slate that a ``where``
    or ``types`` filter emptied ends with :func:`describe_filter_exclusion`
    instead, so the filter is not read as the query having found nothing.
    """
    if value.get("confidence") != "none":
        return ""
    evidence = describe_arms(value)
    line = "No confident match."
    if evidence:
        line += f" Nothing delivered reaches a confident mark — {evidence}."
    if withheld:
        line += (
            f" {withheld} weak result{'s' if withheld != 1 else ''} withheld by"
            " search.abstain_on_no_confident_match; the store was searched and"
            " is not empty."
        )
    elif delivered:
        line += (
            " The results below are the closest weak matches; say the memory"
            " does not settle this rather than presenting them as the answer."
        )
    else:
        line += " " + (
            describe_filter_exclusion(value)
            or "Nothing in the visible indexed corpus matched this query."
        )
    return line


def describe_filter_exclusion(value: Mapping[str, Any]) -> str:
    """The line an empty delivery carries when ``where`` or ``types`` emptied it.

    Empty unless the delivery's diagnostics hold a ``filter_exclusion`` block.
    Without it, an empty slate reads as "nothing matched this query", and an
    agent that searched with a filter no record satisfies concludes nothing was
    saved. So the line names the filter and, where the server found one, the
    key no record in scope has. Every count in it is of records the caller may
    see.
    """
    exclusion = value.get("filter_exclusion")
    if not isinstance(exclusion, Mapping):
        return ""
    by = [str(name) for name in exclusion.get("by") or []]
    named = []
    if "where" in by:
        named.append("where: " + ", ".join(str(e) for e in exclusion.get("where") or []))
    if "types" in by:
        named.append("types: " + ", ".join(str(t) for t in exclusion.get("types") or []))
    if not named:
        return ""
    filters = " and ".join(named)
    matches = exclusion.get("matches")
    if matches is None:
        line = f"No record in scope matches {filters}"
        missing = [f"`{key}`" for key in exclusion.get("missing_keys") or []]
        if len(missing) == 1:
            line += f"; no record in scope has the key {missing[0]}"
        elif missing:
            line += f"; no record in scope has any of the keys {', '.join(missing)}"
        line += "."
    else:
        count = int(matches)
        at_least = bool(exclusion.get("matches_at_least"))
        one = count == 1 and not at_least
        line = (
            f"{'at least ' if at_least else ''}{count} "
            f"{'match' if one else 'matches'} for this search "
            f"{'was' if one else 'were'} excluded by {filters}."
        )
        line = line[0].upper() + line[1:]
    subject = "The filters" if len(named) > 1 else "The filter"
    return f"{line} {subject}, not the query, emptied this result."


def _as_project_ref(project: str) -> str:
    """``project/<slug>`` for a bare slug or a ref, as every label writes it."""
    return project if project.lower().startswith("project/") else f"project/{project}"


def other_project_label(refs: Iterable[str], project: str | None) -> str:
    """The label that opens an opted-in record from another project.

    Names the record's own project **and** the one the request is scoped to,
    side by side, at the start of the row. An inline tag after the match score
    was not enough: an agent read ``[other project: project/ledger-9c3e57]``
    on a decision, in a session scoped to ``project/ledger-4a1f20``, and wrote
    that decision's value into its own config. Two slugs that share a base
    name are easy to read as one when only one of them is printed.
    """
    own = ", ".join(refs)
    if not project:
        return f"[other project: {own}]"
    return f"[other project: {own} — not {_as_project_ref(project)}]"


def other_project_read_notice(refs: Iterable[str], project: str | None) -> str:
    """The line an explicit read of another project's record opens with.

    A known path stays readable across projects, so a read is how an agent
    that browsed its way to another project's file ends up holding it. The
    record then opens with the label search puts on an opted-in row, and says
    outright that it is not this project's decision.
    """
    return f"{other_project_label(refs, project)} Context only, not this project's decision."


def other_projects_heading(project: str | None) -> str:
    """The heading over opted-in records from other projects.

    They are rendered after the request's own results, never interleaved, so
    no other project's decision reads as one of this project's.
    """
    scope = f" (this project: {_as_project_ref(project)})" if project else ""
    return f"Other projects — context only, not this project's decisions{scope}"


def split_other_projects(
    results: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """This project's rows and other projects' rows, each in delivered order.

    A row is another project's when the API labelled it ``other_project``,
    which it does only for a request that opted in. Rendering only: the
    delivered order and the payload are unchanged.
    """
    own: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for row in results:
        (other if row.get("other_project") else own).append(row)
    return own, other


def describe_other_projects_withheld(
    withheld: int | None, *, delivered: int, project: str | None,
    human: bool = False,
) -> str:
    """The line a scoped delivery adds when isolation left it (nearly) empty.

    Only when project isolation withheld at least one record **and** fewer
    than two items were delivered: a thin scoped result should say that other
    projects' records exist rather than read as "nothing in memory". A full
    result stays quiet; the count is in the payload/receipt either way.

    Two audiences, deliberately different. The agent-facing line (MCP text,
    the resolve bundle and so the hook, the plugin tool) names no way to see
    the withheld records: an agent told how will fetch another project's
    decision and answer with it, which is the failure isolation exists to
    stop. It says instead that they are about other projects. The
    human-facing line (``human=True``, the CLI) names the flag.
    """
    if not withheld or withheld < 1 or delivered >= 2:
        return ""
    ref = project or "none"
    if not ref.lower().startswith("project/"):
        ref = f"project/{ref}"
    noun = "record" if withheld == 1 else "records"
    head = f"{withheld} {noun} from other projects withheld (scope: {ref})"
    if human:
        return f"{head}; --include-other-projects to see them"
    return f"{head}. They are about other projects, not this one."

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

from collections.abc import Mapping
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
    is the reason the switch is off by default.
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
        line += " Nothing in the visible indexed corpus matched this query."
    return line


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

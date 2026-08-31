"""How a search hit's score is described to a person.

The fused score is a rank, not a similarity. RRF gives the top-ranked hit
1.0 whether it is an excellent match or the least bad of a weak field, so no
surface presents it as confidence. Cosine is presented that way, when the
vector arm produced one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_RAW_SCORE = "raw_score"


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
    return f"{round(raw * 100)}% match"

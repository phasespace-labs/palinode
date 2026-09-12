"""A fused rank is never presented as match confidence."""

import pytest
from click.testing import CliRunner
from unittest.mock import patch

from palinode.core.scoring import describe_match
from palinode.mcp import _format_results
from palinode.cli.search import search


# The measured case from the issue: 32 searches whose answer was absent from
# the corpus all reported a fused 1.0, while cosine ranged 0.402 to 0.459.
ABSENT_ANSWER_COSINE = 0.421


def test_a_top_ranked_weak_match_is_not_reported_as_certain() -> None:
    assert describe_match({"score": 1.0, "raw_score": ABSENT_ANSWER_COSINE}) == "42% match"


def test_a_keyword_only_hit_claims_no_similarity() -> None:
    # The ranker sets raw_score=None for BM25-only results. There is no cosine
    # to report, so the fused value is shown as rank and nothing calls it a match.
    described = describe_match({"score": 1.0, "raw_score": None})
    assert described == "keyword match, rank 1.00"
    assert "%" not in described


def test_an_absent_raw_score_is_not_the_same_as_a_null_one() -> None:
    # A pre-0.12 server never sent the field, so which arm hit is unknown.
    assert describe_match({"score": 1.0}) == "rank 1.00"
    assert describe_match({"score": 1.0, "raw_score": None}) != describe_match({"score": 1.0})


@pytest.mark.parametrize("raw_score", [0.402, 0.421, 0.459])
def test_the_whole_measured_range_renders_below_half(raw_score: float) -> None:
    percent = int(describe_match({"score": 1.0, "raw_score": raw_score}).split("%")[0])
    assert percent < 50


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    [
        (0.005, "1% match"),  # 0.5 rounds up; round() would give 0
        (0.025, "3% match"),  # 2.5 rounds up; round() would give 2
        (0.125, "13% match"),  # 12.5 rounds up; round() would give 12
        (0.425, "43% match"),  # the half in the issue's measured band
        (0.421, "42% match"),  # below the half, unchanged
    ],
)
def test_match_percent_rounds_half_up(raw_score: float, expected: str) -> None:
    assert describe_match({"score": 1.0, "raw_score": raw_score}) == expected


def test_the_mcp_surface_stops_calling_a_fused_rank_a_match() -> None:
    rendered = _format_results(
        [{"file": "notes/a.md", "score": 1.0, "raw_score": ABSENT_ANSWER_COSINE, "snippet": "body"}]
    )
    assert "(42% match)" in rendered
    assert "100% match" not in rendered


def test_the_mcp_surface_claims_no_similarity_for_a_keyword_only_hit() -> None:
    rendered = _format_results(
        [{"file": "notes/a.md", "score": 1.0, "raw_score": None, "snippet": "body"}]
    )
    assert "(keyword match, rank 1.00)" in rendered
    assert "% match" not in rendered


def test_the_cli_score_flag_shows_similarity_not_rank() -> None:
    hit = {"file": "notes/a.md", "score": 1.0, "raw_score": ABSENT_ANSWER_COSINE, "snippet": "body"}
    with patch("palinode.cli.search.api_client.search", return_value=[hit]):
        result = CliRunner().invoke(search, ["anything", "--score", "--format", "text"])
    assert "[42% match]" in result.output
    assert "[1.00]" not in result.output

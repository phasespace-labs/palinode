"""Unit tests for `palinode read` frontmatter handling in `cli/read.py`.

Issue #100: `palinode read` was mangling the body when frontmatter values contained `---`.
Replacing the hand-rolled `_strip_frontmatter` helper with `parser.split_frontmatter`
ensures the frontmatter block is cleanly stripped based on YAML boundary regex rather than
naively splitting on the first two `---` occurrences anywhere in the content string.
"""
from __future__ import annotations

from unittest.mock import patch
from click.testing import CliRunner

from palinode.cli.read import _format_with_meta, read


def test_format_with_meta_preserves_body_when_frontmatter_contains_dashes():
    """When a frontmatter field contains `---`, _format_with_meta must preserve the full body."""
    raw_content = (
        "---\n"
        "title: 'Design --- Architecture ---\'\n"
        "description: 'A quote anchor containing --- sequence'\n"
        "---\n"
        "This is the actual document body.\n"
        "It also has a --- line inside the body.\n"
    )
    result = {
        "frontmatter": {
            "title": "Design --- Architecture ---",
            "description": "A quote anchor containing --- sequence",
        },
        "content": raw_content,
    }

    formatted = _format_with_meta(result)

    assert "── Frontmatter ──" in formatted
    assert "title: Design --- Architecture ---" in formatted
    assert "── Content ──" in formatted
    assert "This is the actual document body." in formatted
    assert "It also has a --- line inside the body." in formatted

    # The body after "── Content ──" must be the intact body content,
    # not a truncated/mangled piece of the frontmatter.
    content_part = formatted.split("── Content ──\n")[1]
    assert content_part == "This is the actual document body.\nIt also has a --- line inside the body.\n"


def test_cli_read_meta_with_dashes_in_frontmatter():
    """`palinode read file.md --meta` CLI invocation correctly handles `---` inside frontmatter."""
    raw_content = (
        "---\n"
        "title: 'Title --- test'\n"
        "---\n"
        "Document body line 1.\n"
        "Document body line 2.\n"
    )
    mock_payload = {
        "frontmatter": {"title": "Title --- test"},
        "content": raw_content,
    }

    runner = CliRunner()
    with patch("palinode.cli.read.api_client.read", return_value=mock_payload) as mock_read:
        res = runner.invoke(read, ["projects/test.md", "--meta"])
        assert res.exit_code == 0
        assert "Title --- test" in res.output
        assert "Document body line 1." in res.output
        assert "Document body line 2." in res.output
        mock_read.assert_called_once_with("projects/test.md", meta=True)

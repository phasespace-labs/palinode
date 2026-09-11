"""Relative dates: detected, resolved, and rewritten on the way in.

``PROGRAM.md`` § "Resolve dates" has required absolute dates since v0.16.0 and
nothing enforced it. These tests cover the three halves of the enforcement —
the detector's precision, the resolution arithmetic, and what the write-time
normaliser refuses to touch.

Real files under ``tmp_path``; the session-end tests drive the real API handler
against a real memory dir (repo rule: never mock the database).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest

from palinode.core.config import config
from palinode.core.lint import check_relative_dates, run_lint_pass
from palinode.core.relative_dates import (
    NO_ANCHOR,
    UNRESOLVABLE,
    anchor_for,
    find_relative_dates,
    normalize_text,
    resolve,
)

# A Thursday, chosen so weekday arithmetic crosses into the previous month.
ANCHOR = date(2026, 9, 3)


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Keep session-end's dedup probe off the real store."""
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))


# ── the detector ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    ["yesterday", "today", "tomorrow", "3 days ago", "two days ago",
     "last Tuesday", "next Friday", "last week", "next month", "recently"],
)
def test_detector_finds_every_relative_phrase_class(phrase):
    assert [m.expression for m in find_relative_dates(f"We shipped {phrase}.")] == [phrase]


@pytest.mark.parametrize(
    "body",
    [
        "The decision was recorded on 2026-08-03.",
        "- [2026-09-10] Shipped the release.",
        "The launch month is August.",
        "TodaysStatus is a legacy identifier.",
        "Deadline 2026-09-10 stands.",
        "yesterday (2026-09-02) we shipped",
        "yesterday [2026-09-02] we shipped",
    ],
)
def test_detector_leaves_absolute_and_already_anchored_text_alone(body):
    assert find_relative_dates(body, ANCHOR) == []


def test_detector_ignores_phrases_inside_a_fenced_block():
    body = (
        "Today we shipped.\n"
        "```bash\n"
        "palinode search --since yesterday   # tomorrow too\n"
        "```\n"
        "~~~\n"
        "last Tuesday\n"
        "~~~\n"
        "And next Friday we cut the release.\n"
    )

    assert [m.expression for m in find_relative_dates(body, ANCHOR)] == [
        "Today", "next Friday",
    ]


def test_detector_ignores_an_inline_code_span():
    body = "Run `palinode log --since yesterday` before tomorrow."

    assert [m.expression for m in find_relative_dates(body, ANCHOR)] == ["tomorrow"]


def test_detector_reports_quoted_text_even_though_the_rewriter_refuses_it():
    # The two layers disagree on purpose: a quoted relative date rots like any
    # other, and reporting it costs a line; rewriting it changes what was said.
    body = 'She said, "yesterday it broke".'

    assert [m.expression for m in find_relative_dates(body, ANCHOR)] == ["yesterday"]
    assert normalize_text(body, ANCHOR) == (body, 0)


# ── the arithmetic ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("yesterday", "2026-09-02"),
        ("today", "2026-09-03"),
        ("tomorrow", "2026-09-04"),
        ("4 days ago", "2026-08-30"),        # crosses the month boundary
        ("ten days ago", "2026-08-24"),
        ("last Tuesday", "2026-09-01"),
        ("last Thursday", "2026-08-27"),     # the anchor's own weekday goes back a week
        ("last Friday", "2026-08-28"),       # crosses the month boundary
        ("next Friday", "2026-09-04"),
        ("next Thursday", "2026-09-10"),
    ],
)
def test_resolution_arithmetic(phrase, expected):
    resolved, reason = resolve(phrase, ANCHOR)

    assert resolved is not None and resolved.isoformat() == expected
    assert reason == ""


@pytest.mark.parametrize(
    ("phrase", "reason_fragment"),
    [
        ("recently", "vague"),
        ("lately", "vague"),
        ("currently", "vague"),
        ("last week", "interval"),
        ("next month", "interval"),
        ("three weeks ago", "interval"),
        ("this Tuesday", "ambiguous"),
    ],
)
def test_phrases_that_name_no_day_never_resolve(phrase, reason_fragment):
    resolved, reason = resolve(phrase, ANCHOR)

    assert resolved is None
    assert reason_fragment in reason


def test_without_an_anchor_even_a_day_precise_phrase_is_unresolvable():
    resolved, reason = resolve("yesterday", None)

    assert resolved is None
    assert "no anchor" in reason


def test_anchor_comes_from_created_at_then_from_a_dated_filename():
    assert anchor_for({"created_at": "2026-09-03T10:00:00Z"}, "insights/x.md") == ANCHOR
    assert anchor_for({}, "daily/2026-09-03.md") == ANCHOR
    assert anchor_for({}, "insights/session-end-2026-09-03-alpha-ab12.md") == ANCHOR
    # last_updated is deliberately not an anchor: an edit months later would
    # resolve the original "yesterday" to a confidently wrong date.
    assert anchor_for({"last_updated": "2026-09-03T10:00:00Z"}, "insights/x.md") is None


# ── the rewriter ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yesterday we chose SQLite.", "On 2026-09-02 we chose SQLite."),
        ("We chose SQLite yesterday.", "We chose SQLite on 2026-09-02."),
        ("Since yesterday it has held.", "Since 2026-09-02 it has held."),
        ("Shipped 4 days ago.", "Shipped on 2026-08-30."),
        ("We met last Tuesday and meet next Friday.",
         "We met on 2026-09-01 and meet on 2026-09-04."),
    ],
)
def test_normaliser_rewrites_resolvable_phrases(text, expected):
    assert normalize_text(text, ANCHOR) == (expected, 1 if "and" not in text else 2)


@pytest.mark.parametrize(
    "text",
    [
        "We shipped it last week.",                 # an interval
        "We shipped it recently.",                  # vague
        "> yesterday we shipped",                   # a blockquote
        'He wrote "we shipped yesterday".',         # quoted
        "Run `palinode log --since yesterday`.",    # inline code
        "yesterday's build failed",                 # possessive: needs rewording
    ],
)
def test_normaliser_refuses_what_it_cannot_rewrite_honestly(text):
    assert normalize_text(text, ANCHOR) == (text, 0)


def test_normaliser_leaves_a_fenced_block_byte_identical():
    text = "Today we shipped.\n```\nsince yesterday\n```\n"

    rewritten, count = normalize_text(text, ANCHOR)

    assert count == 1
    assert rewritten == "On 2026-09-03 we shipped.\n```\nsince yesterday\n```\n"


def test_normaliser_without_an_anchor_changes_nothing():
    assert normalize_text("Yesterday we shipped.", None) == ("Yesterday we shipped.", 0)


# ── session-end: normalisation at write time ─────────────────────────────────


def _session_end(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    from palinode.api.routers.session import SessionEndRequest, session_end_api

    payload = {
        "summary": "Shipped the executor fix.",
        "decisions": ["Chose SQLite."],
        "blockers": [],
        "source": "test",
    }
    payload.update(kwargs)
    result = session_end_api(SessionEndRequest(**payload))
    daily = os.path.join(str(tmp_path), result["daily_file"])
    return result, open(daily, encoding="utf-8").read()


def _yesterday() -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def test_session_end_normalises_summary_and_decisions(tmp_path, monkeypatch):
    _, daily = _session_end(
        tmp_path, monkeypatch,
        summary="Yesterday we chose SQLite.",
        decisions=["Agreed yesterday to keep the executor deterministic."],
        blockers=["Ollama was cold yesterday."],
    )

    yesterday = _yesterday()
    assert f"**Summary:** On {yesterday} we chose SQLite." in daily
    assert f"- Agreed on {yesterday} to keep the executor deterministic." in daily
    assert f"- Ollama was cold on {yesterday}." in daily
    assert "yesterday" not in daily.lower().replace("yesterday's", "")


def test_session_end_leaves_quoted_text_and_code_alone(tmp_path, monkeypatch):
    quoted = 'Paul said "we shipped yesterday" and `--since yesterday` still works.'
    _, daily = _session_end(tmp_path, monkeypatch, summary=quoted)

    assert quoted in daily


def test_session_end_normalisation_is_off_when_configured_off(tmp_path, monkeypatch):
    monkeypatch.setattr(config.write, "normalize_relative_dates", False)

    _, daily = _session_end(tmp_path, monkeypatch, summary="Yesterday we chose SQLite.")

    assert "**Summary:** Yesterday we chose SQLite." in daily


def test_session_end_dry_run_renders_what_it_would_write(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    from palinode.api.routers.session import SessionEndRequest, session_end_api

    result = session_end_api(SessionEndRequest(
        summary="Yesterday we chose SQLite.",
        decisions=[],
        blockers=[],
        dry_run=True,
    ))

    assert f"On {_yesterday()} we chose SQLite." in result["entry"]


# ── the lint surface ─────────────────────────────────────────────────────────


def _memory(tmp_path, body: str, *, created_at: str = "2026-09-03T09:00:00Z"):
    insights = tmp_path / "insights"
    insights.mkdir(exist_ok=True)
    (insights / "drifting.md").write_text(
        "---\nid: insights-drifting\ncategory: insights\ntype: Insight\n"
        f"description: a memory\ncreated_at: {created_at}\n"
        "entities:\n- project/alpha\n---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_lint_reports_the_resolution_and_the_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _memory(tmp_path, "[[project/alpha]] shipped yesterday.")

    matches = run_lint_pass()["relative_dates"][0]["matches"]

    assert matches == [{
        "line": "1",
        "expression": "yesterday",
        "resolved": "2026-09-02",
        "anchor": "2026-09-03",
        "reason": "",
    }]


def test_lint_says_unresolvable_with_the_reason_when_nothing_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _memory(tmp_path, "[[project/alpha]] shipped recently.", created_at="")

    match = run_lint_pass()["relative_dates"][0]["matches"][0]

    assert match["resolved"] == UNRESOLVABLE
    assert match["anchor"] == NO_ANCHOR
    assert "vague" in match["reason"]


def test_lint_text_output_carries_the_resolution(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from palinode.cli.lint import api_client, lint as lint_command

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _memory(tmp_path, "[[project/alpha]] shipped yesterday.")
    monkeypatch.setattr(api_client, "lint", lambda **_kwargs: run_lint_pass())

    result = CliRunner().invoke(lint_command, ["--format", "text"])

    assert result.exit_code == 0, result.output
    assert "insights/drifting.md:1: yesterday → 2026-09-02" in result.output


def test_lint_json_output_carries_the_resolution(tmp_path, monkeypatch):
    import json

    from click.testing import CliRunner

    from palinode.cli.lint import api_client, lint as lint_command

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _memory(tmp_path, "[[project/alpha]] shipped yesterday.")
    monkeypatch.setattr(api_client, "lint", lambda **_kwargs: run_lint_pass())

    result = CliRunner().invoke(lint_command, ["--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["relative_dates"][0]["matches"][0]["resolved"] == "2026-09-02"


def test_check_relative_dates_anchor_is_optional(tmp_path):
    # The public helper keeps working with no anchor — it just cannot resolve.
    assert check_relative_dates("shipped yesterday")[0]["resolved"] == UNRESOLVABLE

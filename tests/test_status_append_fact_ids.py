"""The session-end status append mints the fact id the runner needs.

Consolidation addresses facts by id and harvests only bullets carrying
``<!-- fact:id -->``. Session-end appended plain ``- [YYYY-MM-DD] …`` lines, so
a store fed only through the supported write path accumulated bullets no
operation could ever name.

The contract pinned here:
  1. every appended line carries a marker, and the *same* id a later
     ``bootstrap-ids`` walk would mint for that text — the two writers cannot
     disagree about what a line's id is;
  2. re-running the backfill over an already-minted file is a no-op (no
     double-stamping, in either direction);
  3. the minted line is harvestable by the runner's own extractor;
  4. the human-readable part of the line is unchanged.
"""
from __future__ import annotations

import os
import re
from unittest import mock

import pytest

from palinode.consolidation.fact_ids import (
    add_fact_ids_to_file,
    generate_fact_id,
    stamp_fact_id,
)
from palinode.consolidation.runner import _tagged_fact_count
from palinode.core.config import config

_MARKER_RE = re.compile(r"<!-- fact:(\S+) -->")


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Per-test SQLite DB so the dedup probe never reaches the real store."""
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))


def _run_session_end(tmp_path, monkeypatch, **kwargs) -> tuple[str, str]:
    """Drive the real handler against a tmp store. Returns (status_path, text)."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    projects_dir = os.path.join(str(tmp_path), "projects")
    os.makedirs(projects_dir, exist_ok=True)
    status_path = os.path.join(projects_dir, "palinode-status.md")
    if not os.path.exists(status_path):
        with open(status_path, "w", encoding="utf-8") as f:
            f.write("---\nid: projects-palinode-status\n---\n\n# palinode status\n")

    with mock.patch(
        "palinode.api.routers.session._check_session_end_dedup",
        return_value=(None, None),
    ):
        from palinode.api.server import SessionEndRequest, session_end_api

        kwargs.setdefault("project", "palinode")
        kwargs.setdefault("source", "test")
        session_end_api(SessionEndRequest(**kwargs))

    return status_path, open(status_path, encoding="utf-8").read()


def _appended_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- [")]


# ── 1. Every appended line is minted ─────────────────────────────────────────


def test_the_appended_line_carries_a_fact_marker(tmp_path, monkeypatch):
    _, text = _run_session_end(tmp_path, monkeypatch, summary="Shipped the thing.")

    line = _appended_lines(text)[-1]
    assert _MARKER_RE.search(line), f"no fact marker on the appended line: {line!r}"
    assert "Shipped the thing." in line


def test_the_id_is_the_one_bootstrap_ids_would_mint(tmp_path, monkeypatch):
    """Both writers go through ``stamp_fact_id``, so this cannot drift."""
    status_path, text = _run_session_end(
        tmp_path, monkeypatch, summary="Shipped the thing."
    )

    line = _appended_lines(text)[-1]
    body, marker = line.rsplit(" <!-- fact:", 1)

    assert marker.removesuffix(" -->") == generate_fact_id(status_path, body)


def test_the_marker_is_the_only_change_to_the_line(tmp_path, monkeypatch):
    """The one-liner a human reads is untouched; the marker is a suffix."""
    from palinode.api.routers.session import _status_line

    status_path, text = _run_session_end(
        tmp_path, monkeypatch, summary="Shipped the thing.", decisions=["Chose X."]
    )

    line = _appended_lines(text)[-1]
    rendered = _MARKER_RE.sub("", line).rstrip()
    assert rendered.startswith("- [")
    assert rendered.endswith(")")  # the "(1 decision → pointer)" suffix survives
    assert _status_line.__doc__  # the renderer is still the one producing it


# ── 2. Idempotent in both directions ─────────────────────────────────────────


def test_bootstrap_ids_is_a_no_op_over_a_minted_file(tmp_path, monkeypatch):
    status_path, _ = _run_session_end(tmp_path, monkeypatch, summary="One session.")

    assert add_fact_ids_to_file(status_path) == 0

    with open(status_path, encoding="utf-8") as f:
        text = f.read()
    for line in _appended_lines(text):
        assert len(_MARKER_RE.findall(line)) == 1, f"double-stamped: {line!r}"


def test_stamping_an_already_marked_line_changes_nothing():
    line = "- [2026-09-11] Already done. <!-- fact:palinode-status-abc123 -->"

    assert stamp_fact_id("/store/projects/palinode-status.md", line) == line


def test_two_session_ends_produce_two_distinct_marked_lines(tmp_path, monkeypatch):
    _run_session_end(tmp_path, monkeypatch, summary="First session.")
    status_path, text = _run_session_end(tmp_path, monkeypatch, summary="Second session.")

    lines = _appended_lines(text)
    assert len(lines) == 2
    ids = [_MARKER_RE.search(line).group(1) for line in lines]
    assert len(set(ids)) == 2, "distinct text must mint distinct ids"


# ── 3. The runner can actually harvest what was written ──────────────────────


def test_the_runner_harvests_the_minted_line(tmp_path, monkeypatch):
    status_path, _ = _run_session_end(tmp_path, monkeypatch, summary="Shipped the thing.")

    assert _tagged_fact_count(status_path) == 1


def test_a_store_fed_only_by_session_end_is_consolidatable(tmp_path, monkeypatch):
    """The regression in one line: three session-ends, three addressable facts."""
    for n in range(3):
        status_path, _ = _run_session_end(
            tmp_path, monkeypatch, summary=f"Session number {n}."
        )

    assert _tagged_fact_count(status_path) == 3

"""A proposal that could not be read is a failed project, not a quiet week.

Measured on the dogfood store (2026-09-11): with a 449-fact status
document tagged, the consolidation LLM ran for 60 s, emitted 4754 chars of
per-fact ``KEEP`` operations, hit ``consolidation.llm_max_tokens`` mid-id, and
stopped with no closing ``]``. ``parse_operations`` found no array and returned
``[]``; ``_consolidate_project`` returned ``([], "primary")``; the run loop read
that as "no operations" and the summary said
``status: success, projects_failed: 0, proposed_changes: []`` — byte-identical
to a week with nothing to compact.

``LLM_FAILED`` covered transport failure only. These tests drive the real
runner→executor path on a real ``tmp_path`` store with a fake at the propose
seam, and assert on the three outcomes that used to be one: truncated,
unparseable, and legitimately empty.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from palinode.consolidation import runner
from palinode.core.config import config
from palinode.core.ollama_client import ChatCompletionText


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@pytest.fixture
def store(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """One project with a tagged fact, one dated note that mentions it."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for sub in ("projects", "specs/prompts", "daily"):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "specs" / "prompts" / "compaction.md").write_text(
        "Return consolidation operations as a JSON array.\n", encoding="utf-8"
    )
    (tmp_path / "specs" / "prompts" / "nightly-consolidation.md").write_text(
        "Return consolidation operations as a JSON array.\n", encoding="utf-8"
    )
    target = tmp_path / "projects" / "alpha.md"
    target.write_text(
        "---\nid: projects-alpha\ncategory: project\n---\n\n# alpha\n\n"
        "- [2026-06-01] Old alpha fact. <!-- fact:a1 -->\n",
        encoding="utf-8",
    )
    note = tmp_path / "daily" / f"{_today()}-alpha.md"
    note.write_text(
        "---\nid: alpha\ncategory: daily\n---\n\nWorked on project/alpha today.\n",
        encoding="utf-8",
    )
    return tmp_path, note


def _returning(text: str):
    """A fake propose seam returning fixed text, regardless of the prompts."""
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return text, "fake-model"
    return _fn


#: The dogfood shape: an opening bracket, some ops, and an id that stops mid-word.
TRUNCATED = (
    '```json\n[\n  {"op": "KEEP", "id": "a1"},\n  {"op": "KEEP", "id": "alpha-65b9c'
)

#: A model that answered in prose — a refusal, a preamble it never finished.
UNPARSEABLE = "I need more context before I can decide what to do with these facts."

#: A model that looked at the facts and proposed nothing. Not a failure.
EMPTY = "[]"


def _truncated_at_the_transport(text: str = '[{"op": "KEEP", "id": "a1"}]'):
    """A seam whose response *parses* but is flagged truncated by the server.

    The second half of the detection: a response can close its array by luck
    (the cut landed after a nested ``]``) and still be a partial proposal. Only
    ``finish_reason`` catches that, so it must be enough on its own.
    """
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return ChatCompletionText(text, finish_reason="length"), "fake-model"
    return _fn


# ---------------------------------------------------------------------------
# Truncated → failed
# ---------------------------------------------------------------------------

def test_truncated_response_fails_the_project(store):
    memory_dir, note = store

    result = runner.run_consolidation(llm_fn=_returning(TRUNCATED))

    assert result["status"] == "partial"
    assert result["projects_failed"] == 1
    assert result["failed_projects"] == ["alpha"]
    assert result["projects_compacted"] == 0


def test_truncated_response_leaves_the_notes_in_place(store):
    """The archival guarantee, on the new failure class: a group that produced
    nothing usable has not been consolidated, so its notes must survive."""
    memory_dir, note = store

    result = runner.run_consolidation(llm_fn=_returning(TRUNCATED))

    assert note.exists(), "the note was archived after a failed proposal"
    assert not (memory_dir / "archive").exists()
    assert result["notes_archived"] == 0
    assert result["notes_left_in_place"] == 1


def test_truncated_response_does_not_touch_the_project_document(store):
    memory_dir, _ = store

    runner.run_consolidation(llm_fn=_returning(TRUNCATED))

    assert "Old alpha fact." in (memory_dir / "projects" / "alpha.md").read_text()


def test_truncation_reason_is_logged_at_warning(store, caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
        runner.run_consolidation(llm_fn=_returning(TRUNCATED))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("alpha" in m and "truncated" in m for m in warnings), warnings


def test_finish_reason_alone_fails_the_project(store):
    """Parse success is not enough when the server says it cut the model off."""
    result = runner.run_consolidation(llm_fn=_truncated_at_the_transport())

    assert result["status"] == "partial"
    assert result["failed_projects"] == ["alpha"]
    assert "Old alpha fact." in (store[0] / "projects" / "alpha.md").read_text()


def test_finish_reason_truncation_names_the_token_cap(store, caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
        runner.run_consolidation(llm_fn=_truncated_at_the_transport())

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("llm_max_tokens" in m and "finish_reason=length" in m for m in messages), messages


# ---------------------------------------------------------------------------
# Unparseable → failed
# ---------------------------------------------------------------------------

def test_unparseable_response_fails_the_project(store):
    memory_dir, note = store

    result = runner.run_consolidation(llm_fn=_returning(UNPARSEABLE))

    assert result["status"] == "partial"
    assert result["projects_failed"] == 1
    assert result["failed_projects"] == ["alpha"]
    assert note.exists()
    assert result["notes_archived"] == 0


def test_unparseable_reason_is_logged_at_warning(store, caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
        runner.run_consolidation(llm_fn=_returning(UNPARSEABLE))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("alpha" in m and "no JSON array" in m for m in warnings), warnings


# ---------------------------------------------------------------------------
# Legitimately empty → still a clean no-op
# ---------------------------------------------------------------------------

def test_empty_array_is_a_successful_no_op(store):
    """The regression this change must not cause: a quiet week stays quiet."""
    memory_dir, note = store

    result = runner.run_consolidation(llm_fn=_returning(EMPTY))

    assert result["status"] == "success"
    assert result["projects_failed"] == 0
    assert result["projects_compacted"] == 0
    assert "failed_projects" not in result


def test_empty_array_still_leaves_unconsolidated_notes_alone(store):
    """Nothing compacted → nothing archived, which is pre-existing behaviour."""
    memory_dir, note = store

    result = runner.run_consolidation(llm_fn=_returning(EMPTY))

    assert note.exists()
    assert result["notes_archived"] == 0


def test_real_operations_still_apply(store):
    """The path that must stay working: a readable proposal is applied."""
    memory_dir, note = store
    ops = json.dumps([{"op": "UPDATE", "id": "a1", "new_text": "Updated alpha fact."}])

    result = runner.run_consolidation(llm_fn=_returning(ops))

    assert result["status"] == "success"
    assert result["projects_compacted"] == 1
    assert "Updated alpha fact." in (memory_dir / "projects" / "alpha.md").read_text()


# ---------------------------------------------------------------------------
# Dry run and nightly report it the same way
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response", [TRUNCATED, UNPARSEABLE])
def test_dry_run_reports_the_failure(store, response):
    """`POST /consolidate {"dry_run": true}` is where this was first seen."""
    memory_dir, _ = store

    result = runner.run_consolidation(dry_run=True, llm_fn=_returning(response))

    assert result["dry_run"] is True
    assert result["status"] == "partial"
    assert result["projects_failed"] == 1
    assert result["failed_projects"] == ["alpha"]
    assert result["proposed_changes"] == []


@pytest.mark.parametrize("response", [TRUNCATED, UNPARSEABLE])
def test_nightly_reports_the_failure(store, response):
    result = runner.run_nightly(llm_fn=_returning(response))

    assert result["status"] == "partial"
    assert result["projects_failed"] == 1
    assert result["failed_projects"] == ["alpha"]


def test_nightly_empty_array_is_still_success(store):
    result = runner.run_nightly(llm_fn=_returning(EMPTY))

    assert result["status"] == "success"
    assert result["projects_failed"] == 0


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response", [TRUNCATED, UNPARSEABLE])
def test_consolidate_project_returns_the_failed_sentinel(store, response):
    ops, model = runner._consolidate_project(
        "alpha", [{"date": _today(), "content": "n"}], llm_fn=_returning(response)
    )

    assert ops == []
    assert model == runner.LLM_FAILED


def test_consolidate_project_empty_array_keeps_the_model_name(store):
    """The distinction in one assertion: an empty proposal reports the model
    that made it, so the caller counts a no-op rather than a failure."""
    ops, model = runner._consolidate_project(
        "alpha", [{"date": _today(), "content": "n"}], llm_fn=_returning(EMPTY)
    )

    assert ops == []
    assert model == "fake-model"

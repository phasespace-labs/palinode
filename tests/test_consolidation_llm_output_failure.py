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
import random
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from palinode.consolidation import runner
from palinode.core.config import config
from palinode.core.ollama_client import ChatCompletionText, OllamaClient, RetryPolicy


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


def test_empty_array_retires_the_notes_the_model_saw(store):
    """A group that reached the model and proposed nothing is a decision: its
    notes retire, and the group is named in the summary rather than vanishing.
    (Previously nothing compacted meant nothing archived — a quiet store never
    drained ``daily/``.)"""
    memory_dir, note = store

    result = runner.run_consolidation(llm_fn=_returning(EMPTY))

    assert not note.exists()
    assert result["notes_archived"] == 1
    assert result["projects_no_ops"] == ["alpha"]


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


@pytest.mark.parametrize("completion_tokens", [None, 75])
def test_completion_tokens_at_threshold_or_missing_do_not_warn(
    monkeypatch, caplog, completion_tokens,
):
    class Client:
        def chat_completions(self, *args, **kwargs):
            return ChatCompletionText("[]", completion_tokens=completion_tokens)

    monkeypatch.setattr(runner, "get_ollama_client", lambda: Client())
    monkeypatch.setattr(config.consolidation, "llm_max_tokens", 100)
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
        result, model = runner._call_llm_with_fallback("system", "user")

    assert result == "[]"
    assert model == config.consolidation.llm_model
    assert "completion tokens" not in caplog.text


def test_completion_tokens_above_threshold_warn_for_fallback(monkeypatch, caplog):
    class Client:
        calls = 0

        def chat_completions(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise runner.OllamaError("primary failed")
            return ChatCompletionText("[]", completion_tokens=76)

    monkeypatch.setattr(runner, "get_ollama_client", lambda: Client())
    monkeypatch.setattr(config.consolidation, "llm_max_tokens", 100)
    monkeypatch.setattr(
        config.consolidation, "llm_fallbacks",
        [{"model": "fallback-model", "url": "http://fallback"}],
    )
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
        result, model = runner._call_llm_with_fallback("system", "user")

    assert result == "[]"
    assert model == "fallback-model"
    assert "fallback-model" in caplog.text
    assert "76 completion tokens" in caplog.text
    assert "maximum 100" in caplog.text
    assert "76.0% utilization" in caplog.text


def test_fallback_success_logs_size_tokens_and_latency(monkeypatch, caplog):
    def handler(request):
        if request.url.host == "primary":
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "[]"}}],
            "usage": {"completion_tokens": 12},
        })

    client = OllamaClient(
        retry_policy=RetryPolicy(retries=0),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        rng=random.Random(0),
    )
    monkeypatch.setattr(runner, "get_ollama_client", lambda: client)
    monkeypatch.setattr(config.consolidation, "llm_url", "http://primary")
    monkeypatch.setattr(
        config.consolidation, "llm_fallbacks",
        [{"model": "fallback-model", "url": "http://fallback"}],
    )
    caplog.set_level(logging.INFO, logger="palinode.ollama.events")

    result, model = runner._call_llm_with_fallback("system", "user")

    success = next(
        json.loads(record.message)
        for record in caplog.records
        if record.name == "palinode.ollama.events"
        and json.loads(record.message)["outcome"] == "ok"
    )
    assert result == "[]"
    assert model == "fallback-model"
    assert success["model"] == "fallback-model"
    assert success["response_chars"] == 2
    assert success["completion_tokens"] == 12
    assert success["latency_ms"] >= 0

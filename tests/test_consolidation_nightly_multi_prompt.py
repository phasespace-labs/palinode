"""One nightly pass may send a project several resumed prompts.

The single-prompt pass (``test_consolidation_watermark_coverage.py``) shows
each project one ~6,000-character contiguous excerpt and records where it
stopped. Real daily volume is several times that, so one prompt a night falls
behind until the catch-up bound clamps the backlog away. Here a pass sends up
to ``consolidation.nightly.max_prompts_per_project`` prompts per project, each
planned exactly as a single pass plans one and resumed where the previous one
stopped. It stops early when nothing is pending or a prompt fails, and the
mark then stays at the end of the last prompt that resolved.

Real runner, real ``tmp_path`` store, a fake at the propose seam
(``llm_fn``). ``memory_dir``, ``db_path`` and the audit log path all point
into ``tmp_path``; nothing here touches the developer's store.
"""
from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import pytest

from palinode.consolidation import activity_gate, runner, watermark
from palinode.core.config import config
from palinode.core.ollama_client import ChatCompletionText
from tests.test_consolidation_watermark_coverage import (
    NOW,
    WRITTEN,
    _body,
    _gate_clock,
    _hashes,
    _marks,
    _model,
    _note,
    _presented,
    _reassemble,
    _reproduction,
)

#: The reproduction's overflow project needs this many prompts in total.
PROMPTS_NEEDED = 3
PASS_START = watermark.stamp(NOW)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(activity_gate, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(runner, "_utc_now", lambda: state["now"])
    return state


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """Projects ``control``, ``overflow`` and ``shared``, one tagged fact each."""
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.audit, "log_path", str(tmp_path / ".audit" / "mcp-calls.jsonl"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for sub in ("projects", "daily", "specs/prompts"):
        (tmp_path / sub).mkdir(parents=True)
    for prompt in ("compaction.md", "nightly-consolidation.md"):
        (tmp_path / "specs" / "prompts" / prompt).write_text(
            "Return consolidation operations as a JSON array.\n", encoding="utf-8"
        )
    for project in ("control", "overflow", "shared"):
        (tmp_path / "projects" / f"{project}.md").write_text(
            f"# {project}\n\n- Existing fixture fact. <!-- fact:{project}1 -->\n",
            encoding="utf-8",
        )
    return tmp_path


def _k(monkeypatch, k: int) -> None:
    monkeypatch.setattr(config.consolidation.nightly, "max_prompts_per_project", k)


def _notes_chars(prompt: str) -> int:
    """Characters of note entries a prompt carries, counted as the plan counts
    them: each entry's heading, its newline and its text, without the blank
    lines that separate entries."""
    section = prompt.split("## RECENT_NOTES", 1)[1].split("\n\nReturn the operations", 1)[0]
    entries = section[section.index("### "):]
    return len(entries) - 2 * (len(_presented(prompt)) - 1)


def _run_passes(limit: int = 10, **model_kwargs) -> tuple[list[dict], dict[str, list[str]]]:
    """Run passes until one is idle; returns the non-idle results and every prompt."""
    prompts: dict[str, list[str]] = {}
    results = []
    for _ in range(limit):
        result = runner.run_nightly(llm_fn=_model(prompts, **model_kwargs))
        if result["status"] == "no_new_notes":
            return results, prompts
        results.append(result)
    raise AssertionError(f"nightly never went idle in {limit} passes: {results[-1]}")


# ---------------------------------------------------------------------------
# A backlog needing three prompts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 2, 3, 4, 5])
def test_a_three_prompt_backlog_takes_ceil_3_over_k_passes(store, clock, monkeypatch, k) -> None:
    _k(monkeypatch, k)
    _reproduction(store)
    before = _hashes(store)

    results, prompts = _run_passes()

    assert len(results) == math.ceil(PROMPTS_NEEDED / k)
    # The same three prompts in total however they are spread over passes,
    # and control, which fits one prompt, is sent exactly one.
    assert len(prompts["overflow"]) == PROMPTS_NEEDED
    assert len(prompts["control"]) == 1
    assert [r["prompts_sent"].get("overflow") for r in results] == [
        min(k, PROMPTS_NEEDED - k * i) for i in range(len(results))
    ]

    # Every character presented exactly once: _reassemble asserts each span
    # starts where the previous one ended, across passes and within a pass.
    for project, count, padding in (("control", 2, 100), ("overflow", 8, 2000)):
        stitched = _reassemble(prompts[project])
        assert stitched == {
            f"daily/2026-09-21-{project}-{i}": _body(project, i, padding) for i in range(count)
        }
    # No prompt grew: each carries at most the single-prompt budget.
    assert all(_notes_chars(p) <= runner.MAX_NOTES_CHARS for p in prompts["overflow"])

    # Pending until the last pass, which clears it and stamps the gate clock.
    assert [r["notes_pending"] > 0 for r in results] == [True] * (len(results) - 1) + [False]
    assert all(r["status"] == "success" for r in results)
    assert "watermark_resume" not in results[-1]
    assert _marks(store) == {"control": PASS_START, "overflow": PASS_START}
    assert _gate_clock(store) == PASS_START
    assert _hashes(store) == before


def test_at_k_three_one_pass_reports_the_summed_coverage(store, clock, monkeypatch) -> None:
    _k(monkeypatch, 3)
    _reproduction(store)

    prompts: dict[str, list[str]] = {}
    result = runner.run_nightly(llm_fn=_model(prompts))

    assert result["status"] == "success"
    assert (result["notes_selected"], result["notes_presented"], result["notes_pending"]) == (10, 10, 0)
    assert result["coverage"] == {
        "control": {"selected": 2, "presented": 2, "pending": 0},
        "overflow": {"selected": 8, "presented": 8, "pending": 0},
    }
    assert result["prompts_sent"] == {"control": 1, "overflow": 3}
    assert result["projects_resolved"] == ["control", "overflow"]
    assert result["projects_failed"] == 0
    # Each later prompt starts exactly where the previous one stopped.
    first, second, third = (_presented(p) for p in prompts["overflow"])
    assert (second[0][0], second[0][1]) == (first[-1][0], first[-1][2])
    assert (third[0][0], third[0][1]) == (second[-1][0], second[-1][2])


def test_a_project_with_nothing_pending_gets_one_prompt(store, clock, monkeypatch) -> None:
    _k(monkeypatch, 4)
    date = (NOW - timedelta(days=1)).date().isoformat()
    for index in range(2):
        _note(store, f"{date}-control-{index}", ["control"], _body("control", index, 100), WRITTEN)

    prompts: dict[str, list[str]] = {}
    result = runner.run_nightly(llm_fn=_model(prompts))
    assert result["prompts_sent"] == {"control": 1}
    assert len(prompts["control"]) == 1


def test_a_note_written_during_the_pass_is_not_chased(store, clock, monkeypatch) -> None:
    """A stopping point after the pass's start records no resume position, so
    there is nothing to resume from: the pass stops, and the plain mark lets
    the next pass select the late note from its start."""
    _k(monkeypatch, 4)
    _note(store, "2026-09-21-overflow-0", ["overflow"], _body("overflow", 0, 100), WRITTEN)
    _note(store, "2026-09-22-overflow-late", ["overflow"], _body("overflow", 1, 9000),
          NOW + timedelta(seconds=5))

    first = runner.run_nightly(llm_fn=_model({}))
    assert first["prompts_sent"] == {"overflow": 1}
    assert first["notes_pending"] == 1
    assert _marks(store)["overflow"] == PASS_START


# ---------------------------------------------------------------------------
# A failure part-way through a project's prompts
# ---------------------------------------------------------------------------


def _failing_on(prompts: dict[str, list[str]], project: str, call: int, kind: str):
    """The propose seam, failing ``project``'s ``call``-th prompt (1-based) as ``kind``."""
    def _fn(system_prompt: str, user_prompt: str):
        name = user_prompt.split("facts from ", 1)[1].split(".md", 1)[0]
        prompts.setdefault(name, []).append(user_prompt)
        if name == project and len(prompts[name]) == call:
            if kind == "error":
                raise TimeoutError("llm timed out")
            if kind == "truncated":
                return ChatCompletionText('[{"op": "UPDATE"', finish_reason="length"), "fake"
            return "I could not find any operations.", "fake"
        return "[]", "synthetic-noop"
    return _fn


@pytest.mark.parametrize("kind", ["error", "truncated", "invalid"])
@pytest.mark.parametrize("j", [1, 2, 3])
def test_a_failure_on_prompt_j_holds_the_mark_at_the_end_of_prompt_j_minus_1(
    store, clock, monkeypatch, j, kind
) -> None:
    _k(monkeypatch, 4)
    _reproduction(store)
    before = _hashes(store)

    prompts: dict[str, list[str]] = {}
    failed = runner.run_nightly(llm_fn=_failing_on(prompts, "overflow", j, kind))

    # The pass stops at the failed prompt and says so.
    assert failed["status"] == "partial"
    assert failed["failed_projects"] == ["overflow"]
    assert failed["prompts_sent"]["overflow"] == j
    assert len(prompts["overflow"]) == j
    assert _gate_clock(store) is None
    marks = _marks(store)
    assert marks["control"] == PASS_START
    if j == 1:
        # Nothing resolved: exactly the single-prompt contract, no mark at all.
        assert "overflow" not in marks
        assert "overflow" not in failed["projects_resolved"]
        assert "watermark_resume" not in failed
    else:
        last_ref, _, last_end, _ = _presented(prompts["overflow"][j - 2])[-1]
        expected = {"at": watermark.stamp(WRITTEN), "path": f"{last_ref}.md", "offset": last_end}
        assert marks["overflow"] == expected
        assert failed["watermark_resume"] == {"overflow": expected}
        assert "overflow" in failed["projects_resolved"]

    # The next pass resumes there: its first prompt is the one that failed.
    clock["now"] = NOW + timedelta(hours=1)
    retry: dict[str, list[str]] = {}
    later = runner.run_nightly(llm_fn=_model(retry))
    assert later["status"] == "success"
    assert retry["overflow"][0] == prompts["overflow"][j - 1]
    assert "control" not in retry

    # Across both passes, the resolved prompts present every character once.
    resolved = prompts["overflow"][: j - 1] + retry["overflow"]
    assert _reassemble(resolved) == {
        f"daily/2026-09-21-overflow-{i}": _body("overflow", i, 2000) for i in range(8)
    }
    assert later["notes_pending"] == 0
    assert _marks(store)["overflow"] == watermark.stamp(clock["now"])
    assert _hashes(store) == before


# ---------------------------------------------------------------------------
# K=1 is the single-prompt pass
# ---------------------------------------------------------------------------


def test_k_1_reproduces_the_single_prompt_pass(store, clock, monkeypatch) -> None:
    """The reproduction's numbers as the single-prompt pass produces them."""
    _k(monkeypatch, 1)
    _reproduction(store)

    results, prompts = _run_passes()

    first = results[0]
    assert first["status"] == "success"
    assert first["processed_notes"] == 10
    assert (first["notes_selected"], first["notes_presented"], first["notes_pending"]) == (10, 5, 6)
    assert first["coverage"]["overflow"] == {"selected": 8, "presented": 3, "pending": 6}
    assert first["watermark_resume"] == {
        "overflow": {"at": "2026-09-22T11:00:00Z", "path": "daily/2026-09-21-overflow-2.md",
                     "offset": 1749}
    }
    assert [r["notes_pending"] for r in results] == [6, 3, 0]
    assert all(r["prompts_sent"]["overflow"] == 1 for r in results)
    assert len(prompts["overflow"]) == 3


def test_k_1_and_k_4_agree_when_one_prompt_holds_everything(store, clock, monkeypatch) -> None:
    date = (NOW - timedelta(days=1)).date().isoformat()
    for index in range(2):
        _note(store, f"{date}-control-{index}", ["control"], _body("control", index, 100), WRITTEN)

    outcomes = []
    for k in (1, 4):
        _k(monkeypatch, k)
        prompts: dict[str, list[str]] = {}
        # Dry runs: same store, nothing recorded between the two.
        outcomes.append((runner.run_nightly(dry_run=True, llm_fn=_model(prompts)), prompts))
    assert outcomes[0] == outcomes[1]


def test_a_dry_run_sends_every_prompt_and_records_nothing(store, clock, monkeypatch) -> None:
    _k(monkeypatch, 4)
    _reproduction(store)
    prompts: dict[str, list[str]] = {}
    result = runner.run_nightly(dry_run=True, llm_fn=_model(prompts))
    assert result["prompts_sent"] == {"control": 1, "overflow": 3}
    assert result["notes_pending"] == 0
    assert _marks(store) == {}
    assert _gate_clock(store) is None


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


def test_the_api_and_cli_report_prompts_per_project(store, clock, monkeypatch) -> None:
    from click.testing import CliRunner
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from palinode.api.routers.consolidation import router
    from palinode.cli import _api
    from palinode.cli import main as cli

    _k(monkeypatch, 2)
    _reproduction(store)
    monkeypatch.setattr(runner, "_call_llm_with_fallback", _model({}))
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post("/consolidate", json={"nightly": True})
    assert response.status_code == 200
    body = response.json()
    assert body["prompts_sent"] == {"control": 1, "overflow": 2}
    assert (body["notes_selected"], body["notes_presented"], body["notes_pending"]) == (10, 8, 3)
    assert body["coverage"]["overflow"] == {"selected": 8, "presented": 6, "pending": 3}

    monkeypatch.setattr(_api.api_client, "consolidate", lambda **_: body)
    as_json = CliRunner().invoke(cli, ["consolidate", "--nightly", "--format", "json"])
    assert as_json.exit_code == 0, as_json.output
    assert '"prompts_sent"' in as_json.output
    text = CliRunner().invoke(cli, ["consolidate", "--nightly", "--format", "text"])
    assert text.exit_code == 0, text.output
    flat = " ".join(text.output.split())
    assert "3 of 10 selected note(s) did not fit" in flat
    assert "Prompts sent per project: control 1, overflow 2" in flat


def test_each_prompt_sees_what_the_previous_one_applied(store, clock, monkeypatch) -> None:
    """Prompts run in sequence: the second is built after the first's
    operations were applied, and the project counts as compacted once."""
    _k(monkeypatch, 4)
    _reproduction(store)
    seen: list[str] = []

    def _fn(system_prompt: str, user_prompt: str):
        project = user_prompt.split("facts from ", 1)[1].split(".md", 1)[0]
        if project != "overflow":
            return "[]", "synthetic-noop"
        seen.append(user_prompt)
        text = f"Updated by prompt {len(seen)}."
        return f'[{{"op": "UPDATE", "id": "overflow1", "new_text": "{text}"}}]', "fake"

    result = runner.run_nightly(llm_fn=_fn)

    assert len(seen) == PROMPTS_NEEDED
    assert "Updated by prompt 1." in seen[1] and "Updated by prompt 2." in seen[2]
    assert "Updated by prompt 3." in (store / "projects" / "overflow.md").read_text(encoding="utf-8")
    assert result["updated"] == PROMPTS_NEEDED
    assert result["projects_compacted"] == 1
    assert result["prompts_sent"] == {"control": 1, "overflow": 3}
    assert _marks(store)["overflow"] == PASS_START

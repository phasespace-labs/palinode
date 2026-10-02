"""The nightly watermark acknowledges only what the model was shown.

Before this, ``_assemble_prompt`` clipped each note to 1,500 characters and
kept only the newest ~6,000 characters of notes, and a valid ``[]`` then moved
the project's mark to the pass's start — past every note and tail that was
clipped out. The next pass reported ``no_new_notes`` and that text was never
consolidated.

These tests drive the real runner on a real ``tmp_path`` store with a fake at
the propose seam (``llm_fn``) that answers ``[]``. They assert on what the
model was shown, on the state file, and on the source files' bytes. Nothing
here touches the developer's store: ``memory_dir``, ``db_path`` and the audit
log path are all pointed into ``tmp_path``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from palinode.consolidation import activity_gate, runner, watermark
from palinode.core.config import config

#: 2026-09-22 12:00 UTC, the reproduction's clock.
NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
WRITTEN = NOW - timedelta(hours=1)

_ENTRY = re.compile(
    r"^### \S+ \(ref: (?P<ref>[^)]+)\)"
    r"(?: \[excerpt: characters (?P<start>\d+)-(?P<end>\d+) of (?P<length>\d+)\])?\n",
    re.MULTILINE,
)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(activity_gate, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(runner, "_utc_now", lambda: state["now"])
    return state


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """Projects ``control`` and ``overflow``, each with one tagged fact."""
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.audit, "log_path", str(tmp_path / ".audit" / "mcp-calls.jsonl"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    # One prompt per project per pass: these tests pin the single-prompt
    # behaviour, which is what a pass does at max_prompts_per_project=1.
    # Several prompts per pass are covered in the sibling multi-prompt file.
    monkeypatch.setattr(config.consolidation.nightly, "max_prompts_per_project", 1)
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


def _note(store: Path, name: str, projects: list[str], body: str, written_at: datetime) -> Path:
    path = store / "daily" / f"{name}.md"
    entities = ", ".join(f"project/{project}" for project in projects)
    path.write_text(
        f"---\nentities: [{entities}]\ndate: {written_at.date().isoformat()}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    os.utime(path, (written_at.timestamp(), written_at.timestamp()))
    return path


def _body(project: str, index: int, padding: int) -> str:
    return f"HEAD_{project}_{index}\n" + "x" * padding + f"\nTAIL_{project}_{index}"


def _reproduction(store: Path) -> None:
    """The issue's fixture: 2 short control notes, 8 long overflow notes, one mtime."""
    date = (NOW - timedelta(days=1)).date().isoformat()
    for project, (count, padding) in {"control": (2, 100), "overflow": (8, 2000)}.items():
        for index in range(count):
            _note(store, f"{date}-{project}-{index}", [project], _body(project, index, padding), WRITTEN)


def _hashes(store: Path) -> dict[str, str]:
    return {
        str(path.relative_to(store)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("daily", "projects")
        for path in sorted((store / folder).glob("*.md"))
    }


def _marks(store: Path) -> dict:
    path = activity_gate.state_path(store)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("watermarks", {}).get("nightly", {})


def _gate_clock(store: Path) -> str | None:
    path = activity_gate.state_path(store)
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    return state.get("modes", {}).get("nightly", {}).get("last_run_at")


def _model(prompts: dict[str, list[str]], *, fail: set[str] | None = None):
    """The propose seam: records each project's prompt and answers ``[]``."""
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        project = re.search(r"facts from (\w+)\.md", user_prompt).group(1)
        prompts.setdefault(project, []).append(user_prompt)
        if fail and project in fail:
            raise TimeoutError("llm timed out")
        return "[]", "synthetic-noop"
    return _fn


def _presented(prompt: str) -> list[tuple[str, int, int, str]]:
    """``(ref, start, end, text)`` for every note entry in a prompt, in order."""
    section = prompt.split("## RECENT_NOTES", 1)[1].split("\n\nReturn the operations", 1)[0]
    matches = list(_ENTRY.finditer(section))
    entries = []
    for i, match in enumerate(matches):
        stop = matches[i + 1].start() - 2 if i + 1 < len(matches) else len(section)
        text = section[match.end():stop]
        start = int(match["start"]) if match["start"] else 0
        end = int(match["end"]) if match["end"] else len(text)
        entries.append((match["ref"], start, end, text))
    return entries


def _run_until_idle(limit: int = 20, **model_kwargs) -> tuple[list[dict], dict[str, list[str]]]:
    prompts: dict[str, list[str]] = {}
    results = []
    for _ in range(limit):
        result = runner.run_nightly(llm_fn=_model(prompts, **model_kwargs))
        results.append(result)
        if result["status"] == "no_new_notes":
            return results, prompts
    raise AssertionError(f"nightly never went idle in {limit} passes: {results[-1]}")


def _reassemble(prompts: list[str]) -> dict[str, str]:
    """Every note's text as presented across passes, spans stitched in order."""
    seen: dict[str, str] = {}
    for prompt in prompts:
        for ref, start, end, text in _presented(prompt):
            so_far = seen.get(ref, "")
            # Contiguous: each span starts exactly where the last one ended.
            assert start == len(so_far), (ref, start, len(so_far))
            assert end - start == len(text)
            seen[ref] = so_far + text
    return seen


# ---------------------------------------------------------------------------
# The issue's reproduction, turned around
# ---------------------------------------------------------------------------


def test_clipped_material_is_pending_not_acknowledged(store, clock) -> None:
    _reproduction(store)
    before = _hashes(store)

    prompts: dict[str, list[str]] = {}
    first = runner.run_nightly(llm_fn=_model(prompts))

    # Control: both notes presented in full, and its mark is the plain stamp.
    control = prompts["control"][0]
    assert all(f"HEAD_control_{i}" in control and f"TAIL_control_{i}" in control for i in (0, 1))
    assert _marks(store)["control"] == "2026-09-22T12:00:00Z"

    # Overflow: what was shown is an oldest-first contiguous prefix, and the
    # result says so instead of claiming all ten notes.
    shown = _presented(prompts["overflow"][0])
    assert [ref for ref, *_ in shown] == [
        f"daily/2026-09-21-overflow-{i}" for i in range(len(shown))
    ]
    assert first["status"] == "success"
    assert first["processed_notes"] == 10
    assert first["notes_selected"] == 10
    assert first["notes_presented"] == 2 + len(shown)
    assert first["notes_pending"] == 8 - (len(shown) - 1)
    assert first["coverage"]["control"] == {"selected": 2, "presented": 2, "pending": 0}
    assert first["coverage"]["overflow"]["pending"] > 0

    # The overflow mark is a resume position at the end of what was shown,
    # not the pass's start.
    mark = _marks(store)["overflow"]
    assert isinstance(mark, dict)
    last_ref, _, last_end, _ = shown[-1]
    assert mark == {"at": watermark.stamp(WRITTEN), "path": f"{last_ref}.md", "offset": last_end}
    assert first["watermark_resume"] == {"overflow": mark}
    # Pending work keeps the gate's clock unstamped, so the next tick runs.
    assert _gate_clock(store) is None

    # The second pass is not idle while clipped material is pending, and it
    # does not re-send control.
    second_prompts: dict[str, list[str]] = {}
    second = runner.run_nightly(llm_fn=_model(second_prompts))
    assert second["status"] != "no_new_notes"
    assert list(second_prompts) == ["overflow"]
    resumed = _presented(second_prompts["overflow"][0])
    assert resumed[0][0] == last_ref and resumed[0][1] == last_end

    assert _hashes(store) == before


def test_every_character_is_eventually_presented_exactly_once(store, clock) -> None:
    _reproduction(store)
    before = _hashes(store)

    results, prompts = _run_until_idle()

    # Bounded: 8 notes of ~2,030 characters through a 6,000-character budget.
    assert len(results) <= 5
    for project, count, padding in (("control", 2, 100), ("overflow", 8, 2000)):
        stitched = _reassemble(prompts[project])
        for i in range(count):
            assert stitched[f"daily/2026-09-21-{project}-{i}"] == _body(project, i, padding)
    assert len(prompts["control"]) == 1

    assert _marks(store) == {
        "control": "2026-09-22T12:00:00Z",
        "overflow": "2026-09-22T12:00:00Z",
    }
    assert results[-2]["notes_pending"] == 0
    assert _gate_clock(store) == "2026-09-22T12:00:00Z"
    assert _hashes(store) == before


def test_a_note_longer_than_the_whole_budget_is_consumed_in_parts(store, clock) -> None:
    body = "HEAD\n" + "".join(f"line {i:05d}\n" for i in range(1500)) + "TAIL"
    assert len(body) > 2 * runner.MAX_NOTES_CHARS
    _note(store, "2026-09-21-overflow-long", ["overflow"], body, WRITTEN)

    results, prompts = _run_until_idle()

    assert len(prompts["overflow"]) == 3
    assert _reassemble(prompts["overflow"]) == {"daily/2026-09-21-overflow-long": body}
    # Every part is marked as a part, so the model knows the note continues.
    assert all("[excerpt: characters" in prompt for prompt in prompts["overflow"])
    assert [r["notes_pending"] for r in results[:-1]] == [1, 1, 0]


@pytest.mark.parametrize("sizes", [
    [5900], [5950, 10], [6000], [3000, 2950], [2000] * 8, [1, 5999, 1], [12000, 12000],
    [999, 9999, 99999],
])
def test_the_plan_never_exceeds_the_budget_and_is_a_prefix(store, sizes) -> None:
    notes = [
        {
            "filepath": str(store / "daily" / f"n{i:02d}.md"),
            "date": "2026-09-21",
            "modified_at": WRITTEN,
            "content": "y" * size,
        }
        for i, size in enumerate(sizes)
    ]
    plan = runner._plan_nightly_input(notes, resume=None, until=NOW)
    used = sum(len(runner._note_heading(n)) + 1 + len(n["content"]) for n in plan.notes)
    assert 0 < used <= runner.MAX_NOTES_CHARS
    assert [n["filepath"] for n in plan.notes] == list(plan.selection[: plan.presented])
    # Only the last presented note may be partial.
    assert all(n["span"][1] == n["span"][2] for n in plan.notes[:-1])
    assert plan.complete in (plan.presented, plan.presented - 1)
    # And what the prompt renders is exactly the plan.
    (store / "projects" / "p.md").write_text("- Fact. <!-- fact:p1 -->\n", encoding="utf-8")
    assembled = runner._assemble_prompt("p", plan.notes)
    rendered = _presented(assembled.user_prompt)
    assert [text for *_, text in rendered] == [n["content"] for n in plan.notes]


# ---------------------------------------------------------------------------
# The self-heal and the edges the issue lists
# ---------------------------------------------------------------------------


def test_a_failed_pass_holds_the_resume_position(store, clock) -> None:
    _reproduction(store)
    runner.run_nightly(llm_fn=_model({}))
    held = _marks(store)["overflow"]

    failed_prompts: dict[str, list[str]] = {}
    failed = runner.run_nightly(llm_fn=_model(failed_prompts, fail={"overflow"}))
    assert failed["status"] == "partial"
    assert _marks(store)["overflow"] == held
    assert "watermark_resume" not in failed

    retry_prompts: dict[str, list[str]] = {}
    runner.run_nightly(llm_fn=_model(retry_prompts))
    # The retry is shown exactly what the failed pass was shown.
    assert retry_prompts["overflow"] == failed_prompts["overflow"]
    assert _marks(store)["overflow"] != held


def test_an_edited_note_is_re_read_from_the_start(store, clock) -> None:
    _reproduction(store)
    prompts: dict[str, list[str]] = {}
    runner.run_nightly(llm_fn=_model(prompts))
    partial_ref = _presented(prompts["overflow"][0])[-1][0]

    # The partly-presented note is edited before the next pass.
    clock["now"] = NOW + timedelta(hours=1)
    edited = store / f"{partial_ref}.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "EDITED\n", encoding="utf-8")
    os.utime(edited, ((NOW + timedelta(minutes=30)).timestamp(),) * 2)

    results, later = _run_until_idle()
    stitched = _reassemble(later["overflow"])
    # The edit has a new modified time, so the offset no longer applies to it.
    assert stitched[partial_ref].startswith("HEAD_") and stitched[partial_ref].endswith("EDITED")
    # Notes the first pass finished are not sent again.
    finished = [ref for ref, start, end, text in _presented(prompts["overflow"][0])[:-1]]
    assert finished and not set(finished) & set(stitched)
    assert _marks(store)["overflow"] == watermark.stamp(clock["now"])


def test_a_note_written_during_a_pass_is_not_stepped_over(store, clock) -> None:
    """A stopping point after the pass's start is not recorded: the plain mark
    at the start already covers everything before it, and a note written
    during the pass is selected again."""
    _note(store, "2026-09-21-overflow-0", ["overflow"], _body("overflow", 0, 100), WRITTEN)
    # Written five seconds into the pass, and too long for one prompt.
    _note(store, "2026-09-22-overflow-late", ["overflow"], _body("overflow", 1, 9000),
          NOW + timedelta(seconds=5))

    prompts: dict[str, list[str]] = {}
    first = runner.run_nightly(llm_fn=_model(prompts))
    assert first["notes_pending"] == 1
    assert "watermark_resume" not in first
    assert _marks(store)["overflow"] == "2026-09-22T12:00:00Z"

    clock["now"] = NOW + timedelta(hours=1)
    results, later = _run_until_idle()
    # The late note is re-read from its start; the finished one is not re-sent.
    assert _reassemble(later["overflow"]) == {
        "daily/2026-09-22-overflow-late": _body("overflow", 1, 9000)
    }


def test_a_note_shared_by_two_projects_progresses_per_project(store, clock) -> None:
    _note(store, "2026-09-21-both", ["overflow", "shared"], _body("both", 0, 9000), WRITTEN)

    prompts: dict[str, list[str]] = {}
    runner.run_nightly(llm_fn=_model(prompts, fail={"shared"}))
    marks = _marks(store)
    assert isinstance(marks["overflow"], dict)
    assert "shared" not in marks

    results, later = _run_until_idle()
    assert _reassemble(prompts["overflow"] + later["overflow"]) == {
        "daily/2026-09-21-both": _body("both", 0, 9000)
    }
    assert _reassemble(later["shared"]) == {"daily/2026-09-21-both": _body("both", 0, 9000)}


def test_a_dry_run_reports_coverage_and_records_nothing(store, clock) -> None:
    _reproduction(store)
    result = runner.run_nightly(dry_run=True, llm_fn=_model({}))
    assert result["notes_pending"] > 0
    assert "watermark_resume" in result
    assert _marks(store) == {}


# ---------------------------------------------------------------------------
# The state file
# ---------------------------------------------------------------------------


def test_a_resume_position_never_moves_a_mark_backwards(store) -> None:
    ahead = watermark.Resume(NOW, "daily/b.md", 10)
    assert watermark.advance(["p"], NOW, memory_dir=store, resume={"p": ahead}) == ["p"]
    behind = watermark.Resume(NOW, "daily/a.md", 900)
    assert watermark.advance(["p"], NOW, memory_dir=store, resume={"p": behind}) == []
    further = watermark.Resume(NOW, "daily/b.md", 20)
    assert watermark.advance(["p"], NOW, memory_dir=store, resume={"p": further}) == ["p"]
    # A plain stamp at the same moment covers every position at that moment.
    assert watermark.advance(["p"], NOW, memory_dir=store) == ["p"]
    assert watermark.advance(["p"], NOW, memory_dir=store, resume={"p": further}) == []
    assert watermark.load(memory_dir=store) == {"p": NOW}
    assert watermark.load_resume(memory_dir=store) == {}


def test_load_reads_both_shapes(store) -> None:
    watermark.advance(["plain"], NOW, memory_dir=store)
    position = watermark.Resume(WRITTEN, "daily/x.md", 42)
    watermark.advance(["partial"], NOW, memory_dir=store, resume={"partial": position})
    assert watermark.load(memory_dir=store) == {"plain": NOW, "partial": WRITTEN}
    assert watermark.load_resume(memory_dir=store) == {"partial": position}


# ---------------------------------------------------------------------------
# Surfaces: the counts reach the API response and the CLI's text output
# ---------------------------------------------------------------------------


def test_the_api_and_cli_report_the_same_coverage(store, clock, monkeypatch) -> None:
    from click.testing import CliRunner
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from palinode.api.routers.consolidation import router
    from palinode.cli import _api
    from palinode.cli import main as cli

    _reproduction(store)
    monkeypatch.setattr(runner, "_call_llm_with_fallback", _model({}))
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post("/consolidate", json={"nightly": True})
    assert response.status_code == 200
    body = response.json()
    assert (body["notes_selected"], body["notes_presented"], body["notes_pending"]) == (10, 5, 6)
    assert body["coverage"]["overflow"] == {"selected": 8, "presented": 3, "pending": 6}

    monkeypatch.setattr(_api.api_client, "consolidate", lambda **_: body)
    text = CliRunner().invoke(cli, ["consolidate", "--nightly", "--format", "text"])
    assert text.exit_code == 0, text.output
    flat = " ".join(text.output.split())
    assert "6 of 10 selected note(s) did not fit" in flat
    assert "(5 presented)" in flat

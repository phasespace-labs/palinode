"""How the consolidation runner finds a prompt, and what it does when it can't.

Three behaviours, one per failure the packaging gap produced:

1. A store with its own copy keeps winning. Editing prompts is a documented
   workflow; a packaged fallback that quietly outranked the operator's file
   would be worse than the bug it fixes.
2. A store without one falls back to the copy inside the install and says so
   at INFO — the ``pip install palinode`` path, which used to raise
   ``FileNotFoundError`` on the first consolidation.
3. Neither copy present raises. This is the constraint from the
   vacuous-success insight: an earlier shape returned an empty operations list
   for a missing prompt, which the run summary reported as a successful pass
   that compacted nothing — byte-identical to a quiet week, with no LLM ever
   contacted.

Everything goes through ``_read_prompt_body``, so the ``version:``/``active:``
frontmatter that both shipped prompts carry never reaches the model regardless
of which copy was used.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from palinode.consolidation import runner
from palinode.core.config import config
from palinode.prompts import PromptUnavailable, packaged_prompts_dir

STORE_PROMPT = (
    "---\nid: prompt-compaction\nname: compaction\nversion: 99\n---\n\n"
    "OPERATOR EDITED COMPACTION PROMPT\n"
)


@pytest.fixture()
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    return tmp_path


def _store_prompt(memory_dir: Path, name: str, body: str) -> Path:
    prompts = memory_dir / "specs" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    path = prompts / name
    path.write_text(body, encoding="utf-8")
    return path


def test_store_copy_wins_over_the_packaged_one(memory_dir: Path) -> None:
    _store_prompt(memory_dir, "compaction.md", STORE_PROMPT)
    assert runner._system_prompt("compaction.md") == "OPERATOR EDITED COMPACTION PROMPT\n"


def test_store_copy_has_its_frontmatter_stripped(memory_dir: Path) -> None:
    _store_prompt(memory_dir, "compaction.md", STORE_PROMPT)
    body = runner._system_prompt("compaction.md")
    assert "version: 99" not in body
    assert not body.startswith("---")


def test_falls_back_to_the_packaged_copy_and_logs_it(
    memory_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The PyPI-install path: empty store, prompts only inside the wheel."""
    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        body = runner._system_prompt("compaction.md")

    packaged = (packaged_prompts_dir() / "compaction.md").read_text(encoding="utf-8")
    assert body and body in packaged
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("compaction.md" in m and "packaged" in m for m in messages), messages


def test_packaged_fallback_also_strips_frontmatter(memory_dir: Path) -> None:
    """`compaction.md` carries `version:` — it must not reach the model."""
    body = runner._system_prompt("compaction.md")
    assert not body.startswith("---")
    assert "id: prompt-compaction" not in body


def test_nightly_prefers_the_stores_compaction_over_the_packaged_nightly(
    memory_dir: Path,
) -> None:
    """Pre-existing degradation, deliberately kept above the packaged rung.

    A store that predates the nightly prompt has only ``compaction.md``, and
    an operator may well have edited it. Reaching past their file to a
    packaged nightly prompt would silently discard that tuning.
    """
    _store_prompt(memory_dir, "compaction.md", STORE_PROMPT)
    assert (
        runner._system_prompt("nightly-consolidation.md")
        == "OPERATOR EDITED COMPACTION PROMPT\n"
    )


def test_nightly_falls_back_to_the_packaged_nightly_prompt(memory_dir: Path) -> None:
    body = runner._system_prompt("nightly-consolidation.md")
    packaged = (packaged_prompts_dir() / "nightly-consolidation.md").read_text(
        encoding="utf-8"
    )
    assert body in packaged


def test_no_copy_anywhere_raises_rather_than_returning_nothing(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The vacuous-success guard: an unreachable prompt must be an error."""
    empty = tmp_path / "no-packaged-prompts"
    empty.mkdir()
    monkeypatch.setattr("palinode.prompts.packaged_prompts_dir", lambda: empty)

    with pytest.raises(PromptUnavailable) as excinfo:
        runner._system_prompt("compaction.md")

    message = str(excinfo.value)
    assert "compaction.md" in message
    assert str(memory_dir) in message


def test_consolidate_project_propagates_the_error(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_consolidate_project` must not swallow it into an empty op list.

    ``([], "primary")`` is exactly the shape a project with nothing to compact
    returns, so swallowing here is how a broken install looked healthy.
    """
    empty = tmp_path / "no-packaged-prompts"
    empty.mkdir()
    monkeypatch.setattr("palinode.prompts.packaged_prompts_dir", lambda: empty)

    with pytest.raises(PromptUnavailable):
        runner._consolidate_project("some-project", notes=[], llm_fn=None)


def test_update_prompt_falls_back_to_the_packaged_copy(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contradiction check's own read site, the second of the two."""
    seen: dict[str, str] = {}

    def fake_llm(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        seen["system"] = system_prompt
        return '[{"operation": "ADD"}]', "primary"

    monkeypatch.setattr(runner.embedder, "embed", lambda text: [0.0] * 8)
    # A near neighbour, so the contradiction call actually happens and the
    # system prompt it was handed can be inspected.
    monkeypatch.setattr(
        runner.store, "search_internal",
        lambda *a, **k: [{"id": "existing-1", "content": "an older claim"}],
    )

    runner._check_contradictions(
        [{"content": "a new claim", "type": "Insight"}],
        "some-project",
        llm_fn=fake_llm,
    )

    packaged = (packaged_prompts_dir() / "update.md").read_text(encoding="utf-8")
    assert seen.get("system", "") in packaged


def test_update_prompt_missing_everywhere_still_adds_but_warns(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not vacuous success: the items still become ADDs, and the loss is logged.

    Losing the contradiction check is a real degradation, but every candidate
    is still written — unlike the compaction path, no work silently vanishes.
    """
    empty = tmp_path / "no-packaged-prompts"
    empty.mkdir()
    monkeypatch.setattr("palinode.prompts.packaged_prompts_dir", lambda: empty)

    with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
        ops = runner._check_contradictions(
            [{"content": "a new claim"}], "some-project", llm_fn=None
        )

    assert ops == [{"operation": "ADD", "item": {"content": "a new claim"}}]
    assert any("update.md" in r.getMessage() for r in caplog.records)

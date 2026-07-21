"""The injectable consolidation callback (#554).

Previously, the only way to test the runner→executor path was to mock
``_consolidate_project`` wholesale (see test_consolidation_dry_run), which skips
the real fact-extraction, prompt-building, JSON parse/repair, and executor
application — the parts most likely to harbour bugs. The injectable ``llm_fn``
lets a fake adapter return canned operation JSON while driving the real pipeline
end to end.

Live adapter = the default ``_call_llm_with_fallback`` (covered by test_fallback);
fake adapter = the ``_FAKE_*`` callables below. Two adapters → a real seam.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from palinode.consolidation import runner
from palinode.core.config import config


def _seed(memory_dir: Path, monkeypatch, *, fact_text="Old fact needing update.", with_git=False):
    """Seed a project file with one tagged fact + the compaction prompt."""
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    for sub in ("projects", "specs/prompts", "daily"):
        (memory_dir / sub).mkdir(parents=True, exist_ok=True)
    (memory_dir / "specs" / "prompts" / "compaction.md").write_text(
        "Return consolidation operations as a JSON array.\n", encoding="utf-8"
    )
    target = memory_dir / "projects" / "proj.md"
    target.write_text(
        "---\nid: proj\ncategory: project\n---\n\n"
        "# Proj\n\n## Current Work\n"
        f"- [2026-06-01] {fact_text} <!-- fact:f1 -->\n",
        encoding="utf-8",
    )
    if with_git:
        # run_consolidation commits via git; a real repo is needed end to end.
        recent = datetime.now(UTC).strftime("%Y-%m-%d")
        (memory_dir / "daily" / f"{recent}.md").write_text(
            f"---\nid: daily-{recent}\ncategory: daily\n---\n\n"
            "Worked on project/proj; the old fact should be updated.\n",
            encoding="utf-8",
        )
        for args in (
            ["init"], ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"], ["add", "."], ["commit", "-m", "seed"],
        ):
            subprocess.run(["git", *args], cwd=memory_dir, check=True, capture_output=True)
    return target


def _fake_llm(op_json: str):
    """A fake propose seam returning fixed op-JSON, regardless of the prompts."""
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return op_json, "fake-model"
    return _fn


def test_injected_llm_drives_real_parse(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ops_json = json.dumps([{"op": "UPDATE", "id": "f1", "new_text": "Updated fact."}])
    ops, model = runner._consolidate_project("proj", notes=[], llm_fn=_fake_llm(ops_json))
    assert model == "fake-model"
    assert ops == [{"op": "UPDATE", "id": "f1", "new_text": "Updated fact."}]


def test_no_facts_short_circuits_without_calling_llm(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    # Overwrite the project file with no tagged facts.
    (tmp_path / "projects" / "proj.md").write_text(
        "---\nid: proj\n---\n\n# Proj\n\nNo tagged facts here.\n", encoding="utf-8"
    )
    called = {"n": 0}

    def _exploding(system_prompt, user_prompt):
        called["n"] += 1
        raise AssertionError("llm_fn must not be called when there are no facts")

    ops, model = runner._consolidate_project("proj", notes=[], llm_fn=_exploding)
    assert ops == [] and called["n"] == 0


def test_run_consolidation_applies_canned_ops_end_to_end(tmp_path, monkeypatch):
    """Canned operations exercise extraction, parsing, application, and git."""
    target = _seed(tmp_path, monkeypatch, with_git=True)
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    ops_json = json.dumps([{"op": "UPDATE", "id": "f1", "new_text": "Consolidated fact."}])

    result = runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops_json))

    assert isinstance(result, dict)
    # The executor actually rewrote the fact in place.
    body = target.read_text()
    assert "Consolidated fact." in body
    assert "Old fact needing update." not in body

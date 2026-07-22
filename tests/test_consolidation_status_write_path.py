"""The consolidation **write** path for status documents (#679).

``test_consolidation_dry_run.py`` covers the preview path only, and the gap
between preview and write is exactly what shipped the defect: ``--dry-run``
rendered rationales through ``op_reason()`` while the writer hand-rolled
``item.get("reason", "")``, so ARCHIVE/RETRACT (rationale-first in the executor)
previewed correctly and then wrote blank. These are the write-path counterparts.

Every test drives the real runner→executor→status-write path through the
injectable ``llm_fn`` proposer seam (#554) against a real memory dir + real
SQLite under ``tmp_path`` — no mocked store, no mocked ``_consolidate_project``.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from palinode.consolidation import runner, status_doc
from palinode.core.config import config

STATUS_HEAD = (
    "---\n"
    "id: project-proj-status\n"
    "category: project\n"
    "entities:\n"
    "- project/proj\n"
    "memory_count: 99\n"
    "date_range: 2020-01-01 to 2020-01-02\n"
    "last_updated: '2020-01-02T00:00:00+00:00'\n"
    "---\n\n"
    "# Proj Status\n\n"
    "## Current Work\n"
    "- [2026-06-01] The one real fact. <!-- fact:f1 -->\n"
)


def _seed(memory_dir: Path, monkeypatch) -> Path:
    """A real memory repo with one tagged fact and today's daily note."""
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    monkeypatch.setattr(config, "db_path", str(memory_dir / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)

    for sub in ("projects", "specs/prompts", "daily"):
        (memory_dir / sub).mkdir(parents=True, exist_ok=True)
    (memory_dir / "specs" / "prompts" / "compaction.md").write_text(
        "Return consolidation operations as a JSON array.\n", encoding="utf-8"
    )

    target = memory_dir / "projects" / "proj-status.md"
    target.write_text(STATUS_HEAD, encoding="utf-8")

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    (memory_dir / "daily" / f"{today}.md").write_text(
        f"---\nid: daily-{today}\ncategory: daily\n---\n\n"
        "Worked on project/proj today.\n",
        encoding="utf-8",
    )
    for args in (
        ["init"], ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"], ["add", "."], ["commit", "-m", "seed"],
    ):
        subprocess.run(["git", *args], cwd=memory_dir, check=True, capture_output=True)
    return target


def _fake_llm(ops: list[dict]):
    payload = json.dumps(ops)

    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return payload, "fake-model"

    return _fn


def _log_lines(text: str) -> list[str]:
    return re.findall(r"^- \[[A-Z_]+\].*$", text, re.MULTILINE)


def _frontmatter(text: str) -> dict:
    """Strict parse — the whole point of #470 is that this must not raise."""
    match = status_doc.FRONTMATTER_RE.match(text)
    assert match is not None, "status doc lost its frontmatter"
    return yaml.safe_load(match.group(1))


def test_rationale_only_operation_writes_its_rationale(tmp_path, monkeypatch):
    """ARCHIVE/RETRACT are rationale-first in the executor — those were the ops
    that logged blank because the writer only read ``reason``."""
    target = _seed(tmp_path, monkeypatch)
    ops = [{"op": "ARCHIVE", "id": "f1", "rationale": "Rolled into the June summary."}]

    runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops))

    lines = _log_lines(target.read_text(encoding="utf-8"))
    assert lines == ["- [ARCHIVE] f1: Rolled into the June summary."]


def test_missing_op_kind_logs_as_keep_not_update(tmp_path, monkeypatch):
    """The executor treats a missing kind as KEEP; the log must not claim UPDATE."""
    target = _seed(tmp_path, monkeypatch)
    ops = [{"id": "f1", "reason": "Still current."}]

    runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops))

    body = target.read_text(encoding="utf-8")
    assert "- [KEEP] f1: Still current." in body
    assert "[UPDATE]" not in body


def test_keep_without_rationale_emits_no_line(tmp_path, monkeypatch):
    """A KEEP with nothing to say is a no-op — it is not audit data."""
    target = _seed(tmp_path, monkeypatch)
    ops = [{"op": "KEEP", "id": "f1"}]

    runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops))

    body = target.read_text(encoding="utf-8")
    assert _log_lines(body) == []
    assert "## Consolidation Log" not in body


def test_unknown_fact_id_never_reaches_the_file(tmp_path, monkeypatch):
    """Self-nesting id chains and model deliberation in the id slot must never
    be written verbatim."""
    target = _seed(tmp_path, monkeypatch)
    prose_id = "[the original, non-superseded status if it contains unique info]"
    ops = [
        {"op": "SUPERSEDE", "id": prose_id, "reason": "model deliberation as an id"},
        {"op": "ARCHIVE", "id": "supersedes-supersedes-supersedes-f1",
         "rationale": "self-nesting chain"},
    ]

    runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops))

    body = target.read_text(encoding="utf-8")
    assert prose_id not in body
    assert "supersedes-supersedes" not in body
    assert "- [SUPERSEDE] (unresolved): model deliberation as an id" in body
    assert "- [ARCHIVE] (unresolved): self-nesting chain" in body


def test_archived_fact_id_still_resolves(tmp_path, monkeypatch):
    """ARCHIVE removes the fact's marker — validating against post-apply content
    alone would flag every legitimate archive as unresolved."""
    target = _seed(tmp_path, monkeypatch)
    ops = [{"op": "ARCHIVE", "id": "f1", "rationale": "genuinely archived"}]

    runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops))

    body = target.read_text(encoding="utf-8")
    assert "<!-- fact:f1 -->" not in body  # executor really removed it
    assert "- [ARCHIVE] f1: genuinely archived" in body


def test_preview_and_write_agree_on_rationale(tmp_path, monkeypatch):
    """The regression that let this ship: preview and write must render the same
    rationale for the same operation."""
    ops = [{"op": "RETRACT", "id": "f1", "rationale": "Measured wrong."}]

    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    _seed(preview_dir, monkeypatch)
    preview = runner.run_consolidation(dry_run=True, llm_fn=_fake_llm(ops))
    rationale = preview["proposed_changes"][0]["rationale"]

    write_dir = tmp_path / "write"
    write_dir.mkdir()
    target = _seed(write_dir, monkeypatch)
    runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops))

    assert rationale == "Measured wrong."
    assert f"- [RETRACT] f1: {rationale}" in target.read_text(encoding="utf-8")


def test_frontmatter_matches_body_after_run(tmp_path, monkeypatch):
    """Stale mem0-backfill counts must be reconciled, and the result must
    strict-parse (#470)."""
    target = _seed(tmp_path, monkeypatch)
    ops = [{"op": "UPDATE", "id": "f1", "new_text": "[2026-06-02] Revised fact.",
            "reason": "Daily note supersedes it."}]

    runner.run_consolidation(dry_run=False, llm_fn=_fake_llm(ops))

    text = target.read_text(encoding="utf-8")
    meta = _frontmatter(text)
    body = status_doc.split_frontmatter(text)[1]

    assert meta["memory_count"] == len(status_doc.fact_ids(body)) == 1
    assert meta["date_range"] == "2026-06-02 to " + datetime.now(UTC).strftime("%Y-%m-%d")
    assert meta["last_updated"] > "2020-01-02T00:00:00+00:00"
    assert meta["entities"] == ["project/proj"]


def test_log_stays_bounded_across_many_runs(tmp_path, monkeypatch):
    """25 daily runs must not produce 25 date blocks."""
    target = _seed(tmp_path, monkeypatch)
    ops = [{"op": "UPDATE", "id": "f1", "rationale": "another day, another op"}]
    start = datetime(2026, 6, 1, tzinfo=UTC)

    for day in range(25):
        monkeypatch.setattr(
            runner, "_utc_now", lambda d=day: start + timedelta(days=d)
        )
        runner._update_status_summary(str(target), ops, known_fact_ids={"f1"})

    text = target.read_text(encoding="utf-8")
    blocks = re.findall(r"^### \d{4}-\d{2}-\d{2}$", text, re.MULTILINE)
    assert len(blocks) == config.consolidation.status_log_max_blocks == 10
    assert len(_log_lines(text)) == 10
    elision = re.findall(r"^- _\[log elided\].*$", text, re.MULTILINE)
    assert len(elision) == 1
    assert "15 operation line(s) across 15 date block(s)" in elision[0]
    assert "2026-06-01 → 2026-06-15" in elision[0]


def test_same_day_reruns_do_not_duplicate_the_heading(tmp_path, monkeypatch):
    """Two runs in one day must not produce duplicate date headings or log
    entries."""
    target = _seed(tmp_path, monkeypatch)
    ops = [{"op": "UPDATE", "id": "f1", "rationale": "same op, twice"}]

    for _ in range(3):
        runner._update_status_summary(str(target), ops, known_fact_ids={"f1"})

    text = target.read_text(encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert text.count(f"### {today}") == 1
    assert len(_log_lines(text)) == 1


def test_status_write_preserves_session_end_bullets(tmp_path, monkeypatch):
    """``/wrap`` appends ``- [YYYY-MM-DD] summary`` bullets to the same file;
    bounding the log must never eat them."""
    target = _seed(tmp_path, monkeypatch)
    with open(target, "a", encoding="utf-8") as f:
        f.write("\n## Consolidation Log\n\n### 2026-01-01\n- [KEEP] f1: old\n\n"
                "- [2026-01-01] Session: shipped the thing\n")
    ops = [{"op": "UPDATE", "id": "f1", "rationale": "new op"}]

    runner._update_status_summary(str(target), ops, known_fact_ids={"f1"})

    text = target.read_text(encoding="utf-8")
    assert "- [2026-01-01] Session: shipped the thing" in text

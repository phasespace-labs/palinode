from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from palinode.api.routers.consolidation import router as consolidation_router
from palinode.core.config import config


def _run_git(memory_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=memory_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


SEED_FACTS = "- [2026-06-12] Dry-run consolidation still needs verification. <!-- fact:f1 -->\n"


def _seed_memory_repo(memory_dir: Path, monkeypatch, *, facts: str = SEED_FACTS) -> Path:
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    monkeypatch.setattr(config, "db_path", str(memory_dir / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)

    for name in ("daily", "projects", "specs/prompts"):
        (memory_dir / name).mkdir(parents=True, exist_ok=True)

    from palinode.core import store

    store._db_checked = False
    store.init_db()

    target = memory_dir / "projects" / "palinode-status.md"
    target.write_text(
        "---\n"
        "id: project-palinode-status\n"
        "category: project\n"
        "status: active\n"
        "---\n\n"
        "# Palinode Status\n\n"
        "## Current Work\n"
        f"{facts}",
        encoding="utf-8",
    )
    # Date the daily note to *today* so it always falls inside the consolidation
    # lookback window (config.consolidation.lookback_days, default 7). A hardcoded
    # date is a time-bomb: once it ages past the window _collect_daily_notes drops
    # it, no project is consolidated, and the dry-run preview comes back empty.
    recent_date = datetime.now(UTC).strftime("%Y-%m-%d")
    (memory_dir / "daily" / f"{recent_date}.md").write_text(
        f"---\nid: daily-{recent_date}\ncategory: daily\n---\n\n"
        "Worked on project/palinode and confirmed dry-run consolidation should preview only.\n",
        encoding="utf-8",
    )
    (memory_dir / "specs" / "prompts" / "compaction.md").write_text(
        "Return consolidation operations as JSON.\n",
        encoding="utf-8",
    )

    _run_git(memory_dir, "init")
    _run_git(memory_dir, "config", "user.email", "tests@example.com")
    _run_git(memory_dir, "config", "user.name", "Palinode Tests")
    _run_git(memory_dir, "add", "daily", "projects", "specs")
    _run_git(memory_dir, "commit", "-m", "seed memory")
    return target


def _head(memory_dir: Path) -> str:
    return _run_git(memory_dir, "rev-parse", "HEAD")


def test_consolidate_dry_run_previews_without_writes_and_live_run_applies(tmp_path, monkeypatch):
    operation = {
        "op": "UPDATE",
        "id": "f1",
        "new_text": "[2026-06-12] Dry-run consolidation previews proposed operations.",
        "reason": "Daily note confirms the dry-run contract.",
    }

    def post_consolidate(memory_dir: Path, dry_run: bool):
        monkeypatch.setattr(config, "memory_dir", str(memory_dir))
        monkeypatch.setattr(config, "db_path", str(memory_dir / ".palinode.db"))
        monkeypatch.setattr(config.git, "auto_commit", True)
        app = FastAPI()
        app.include_router(consolidation_router)
        client = TestClient(app, raise_server_exceptions=False)
        with mock.patch(
            "palinode.consolidation.runner._consolidate_project",
            return_value=([operation], "test-model"),
        ):
            return client.post("/consolidate", json={"dry_run": dry_run})

    dry_dir = tmp_path / "dry"
    dry_target = _seed_memory_repo(dry_dir, monkeypatch)
    dry_head = _head(dry_dir)
    dry_bytes = dry_target.read_bytes()

    dry_resp = post_consolidate(dry_dir, True)

    assert dry_resp.status_code == 200
    dry_data = dry_resp.json()
    assert dry_data["dry_run"] is True
    assert dry_data["proposed_changes"] == [
        {
            "type": "UPDATE",
            "file": str(dry_target),
            "rationale": "Daily note confirms the dry-run contract.",
        }
    ]
    assert _head(dry_dir) == dry_head
    assert dry_target.read_bytes() == dry_bytes

    live_dir = tmp_path / "live"
    live_target = _seed_memory_repo(live_dir, monkeypatch)
    live_head = _head(live_dir)
    live_bytes = live_target.read_bytes()

    live_resp = post_consolidate(live_dir, False)

    assert live_resp.status_code == 200
    live_data = live_resp.json()
    assert live_data["status"] == "success"
    assert _head(live_dir) != live_head
    assert live_target.read_bytes() != live_bytes


def test_consolidate_dry_run_merge_writes_no_history_and_live_merge_does(tmp_path, monkeypatch):
    # MERGE now appends its retired sources to the `-history.md`
    # sibling. A dry run must not create that sibling — the preview path never
    # reaches the executor — while a live run must, and must commit it.
    facts = (
        "- [2026-06-12] Dry-run consolidation still needs verification. <!-- fact:f1 -->\n"
        "- [2026-06-12] Dry-run consolidation is unverified. <!-- fact:f2 -->\n"
    )
    operation = {
        "op": "MERGE",
        "ids": ["f1", "f2"],
        "new_text": "[2026-06-12] Dry-run consolidation needs verification.",
        "rationale": "Two bullets say the same thing.",
    }

    def post_consolidate(memory_dir: Path, dry_run: bool):
        monkeypatch.setattr(config, "memory_dir", str(memory_dir))
        monkeypatch.setattr(config, "db_path", str(memory_dir / ".palinode.db"))
        monkeypatch.setattr(config.git, "auto_commit", True)
        app = FastAPI()
        app.include_router(consolidation_router)
        client = TestClient(app, raise_server_exceptions=False)
        with mock.patch(
            "palinode.consolidation.runner._consolidate_project",
            return_value=([operation], "test-model"),
        ):
            return client.post("/consolidate", json={"dry_run": dry_run})

    dry_dir = tmp_path / "dry"
    dry_target = _seed_memory_repo(dry_dir, monkeypatch, facts=facts)
    dry_history = dry_target.with_name("palinode-history.md")
    dry_head = _head(dry_dir)
    dry_bytes = dry_target.read_bytes()

    dry_resp = post_consolidate(dry_dir, True)

    assert dry_resp.status_code == 200
    assert dry_resp.json()["dry_run"] is True
    assert dry_resp.json()["proposed_changes"] == [
        {"type": "MERGE", "file": str(dry_target), "rationale": "Two bullets say the same thing."}
    ]
    assert not dry_history.exists()
    assert dry_target.read_bytes() == dry_bytes
    assert _head(dry_dir) == dry_head

    live_dir = tmp_path / "live"
    live_target = _seed_memory_repo(live_dir, monkeypatch, facts=facts)
    live_history = live_target.with_name("palinode-history.md")
    live_head = _head(live_dir)

    live_resp = post_consolidate(live_dir, False)

    assert live_resp.status_code == 200
    assert live_resp.json()["status"] == "success"
    content = live_target.read_text(encoding="utf-8")
    assert "Dry-run consolidation needs verification. <!-- fact:merged-f1 -->" in content
    assert "<!-- fact:f2 -->" not in content
    history = live_history.read_text(encoding="utf-8")
    assert "Dry-run consolidation still needs verification." in history
    assert "Dry-run consolidation is unverified." in history
    assert "<!-- fact:f1 -->" in history and "<!-- fact:f2 -->" in history
    # The sibling is staged in the same commit as the merge, not left untracked.
    assert _head(live_dir) != live_head
    committed = _run_git(live_dir, "show", "--stat", "--format=", "HEAD")
    assert "palinode-history.md" in committed

"""
Regression tests for #191 / #192 / #193 — timestamp-write correctness.

Three related issues, all the same shape: a writer used
``time.strftime("%Y-%m-%dT%H:%M:%SZ")`` (or ``_utc_now().strftime(...Z)``)
and produced either local-time-stamped-as-UTC or UTC-without-tz-info-and-
without-sub-second-precision.

#191 / #192 covered ``save_api``'s ``created_at`` and the watcher's
``metadata.get("created_at")`` read. #193 extends the cleanup to four
batch surfaces that write ``last_updated`` (and one ``created_at``):

- ``palinode/ingest/pipeline.py``        — research-file frontmatter
- ``palinode/consolidation/layer_split.py`` — identity / status / history files
- ``palinode/migration/mem0_generate.py``  — mem0-imported records
- ``palinode/migration/openclaw.py``      — openclaw-migrated records

The audit invariant ``grep -rn 'strftime.*Z' palinode/`` must return zero
non-comment matches after this change.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.config import config

client = TestClient(app)


@pytest.fixture
def mock_memory_dir(tmp_path):
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    yield str(tmp_path)
    config.memory_dir = old


def _frontmatter(file_path: str) -> dict:
    with open(file_path, "r") as f:
        text = f.read()
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"no frontmatter in {file_path}: {text[:120]}"
    return yaml.safe_load(parts[1])


# ---- save_api: created_at is correct UTC ISO-8601 ------------------------


def test_save_api_writes_timezone_aware_utc_created_at(mock_memory_dir):
    """``save_api`` writes ``created_at`` as a parseable timezone-aware UTC
    ISO-8601 string, not local-time-with-Z-suffix.
    """
    before = datetime.now(UTC)
    with patch(
        "palinode.core.store.scan_memory_content", return_value=(True, "OK")
    ):
        res = client.post(
            "/save",
            json={"content": "timestamp-regression-191", "type": "Decision"},
        )
        assert res.status_code == 200, res.text
    after = datetime.now(UTC)

    fm = _frontmatter(res.json()["file_path"])
    raw = fm.get("created_at")
    assert raw, f"created_at missing from frontmatter: {fm!r}"

    # ``yaml.safe_load`` may parse ISO-8601 strings into a ``datetime`` directly
    # depending on the library version. Normalize either form.
    if isinstance(raw, datetime):
        parsed = raw
    else:
        assert isinstance(raw, str), f"unexpected created_at type: {type(raw)!r}"
        # The new code emits "+00:00"; stay tolerant of "Z" too in case a
        # producer re-introduces it deliberately.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))

    # Must carry tz info — otherwise we are back to the local-time bug.
    assert parsed.tzinfo is not None, (
        f"created_at must be timezone-aware (was {raw!r})"
    )
    # Must specifically be UTC.
    assert parsed.utcoffset() == timedelta(0), (
        f"created_at must be UTC (offset was {parsed.utcoffset()!r}, raw={raw!r})"
    )
    # Must be within the test wall-clock window — confirms it's the actual
    # current time, not a corrupted/stale value. Generous tolerance for
    # clock skew, slow CI, etc.
    assert before - timedelta(seconds=5) <= parsed <= after + timedelta(seconds=5), (
        f"created_at {parsed} not within test window [{before}, {after}]"
    )


# ---- watcher: reads the correct metadata key -----------------------------


def test_watcher_populates_created_at_from_frontmatter(tmp_path, monkeypatch):
    """The watcher's per-file processing reads ``metadata.get('created_at')``
    and stores it in ``chunks.created_at`` — not ``metadata.get('created')``,
    which always returned ``""``.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / "palinode.db"))

    # Reload-ish: the watcher and store modules cache no relevant state, so
    # init_db on the new path is enough.
    from palinode.core import store
    from palinode.indexer import watcher as watcher_mod

    store.init_db()

    expected_iso = "2026-04-26T12:00:00+00:00"
    md = (
        "---\n"
        "id: insights-regression-191-watcher\n"
        "category: insights\n"
        "type: Insight\n"
        f"created_at: '{expected_iso}'\n"
        "last_updated: '2026-04-26T12:00:00+00:00'\n"
        "content_hash: deadbeef\n"
        "---\n\n"
        "watcher reads metadata.get('created_at') correctly.\n"
    )
    insights_dir = tmp_path / "insights"
    insights_dir.mkdir()
    file_path = insights_dir / "regression-191-watcher.md"
    file_path.write_text(md)

    # Avoid hitting the real embedder backend in tests.
    handler = watcher_mod.PalinodeHandler()
    with patch.object(
        watcher_mod.embedder, "embed", return_value=[0.0] * 1024
    ):
        handler._process_file(str(file_path))

    db = store.get_db()
    try:
        rows = db.execute(
            "SELECT created_at FROM chunks WHERE file_path = ?",
            (str(file_path),),
        ).fetchall()
    finally:
        db.close()

    assert rows, "watcher did not produce any chunk rows"
    for row in rows:
        assert row["created_at"] == expected_iso, (
            f"chunks.created_at={row['created_at']!r}, expected {expected_iso!r} "
            "— watcher likely read the wrong frontmatter key"
        )


# batch surfaces write timezone-aware UTC ISO-8601 --------------
#
# One assertion shape, four invocation paths. Each test invokes the real
# writer (no mocking of the timestamp call site itself), reads the value
# back, and asserts:
#   * non-empty string
#   * ``datetime.fromisoformat`` parses cleanly
#   * resulting datetime is timezone-aware
#   * offset is exactly UTC
#   * value falls inside the test wall-clock window
#
# Generous five-second tolerance covers slow CI / clock skew.


def _assert_utc_iso8601(raw, before, after, *, field: str = "value") -> None:
    """Common assertions for a frontmatter timestamp written by #193 surfaces."""
    assert raw, f"{field} missing or empty"
    if isinstance(raw, datetime):
        parsed = raw
    else:
        assert isinstance(raw, str), f"{field}: unexpected type {type(raw)!r}"
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, (
        f"{field} must be timezone-aware (was {raw!r})"
    )
    assert parsed.utcoffset() == timedelta(0), (
        f"{field} must be UTC (offset was {parsed.utcoffset()!r}, raw={raw!r})"
    )
    assert (
        before - timedelta(seconds=5) <= parsed <= after + timedelta(seconds=5)
    ), f"{field} {parsed} not within test window [{before}, {after}]"


def test_ingest_pipeline_writes_timezone_aware_utc_last_updated(tmp_path, monkeypatch):
    """``write_research_file`` writes ``last_updated`` as proper UTC ISO-8601."""
    # ``palinode_dir`` is a read-only alias for ``memory_dir`` — set the
    # underlying attribute.
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    from palinode.ingest import pipeline

    before = datetime.now(UTC)
    file_path = pipeline.write_research_file(
        name="ingest 193 regression",
        content="body",
        file_type="text",
    )
    after = datetime.now(UTC)

    fm = _frontmatter(file_path)
    _assert_utc_iso8601(fm.get("last_updated"), before, after, field="last_updated")


def test_layer_split_writes_timezone_aware_utc_timestamps(tmp_path):
    """``split_file`` writes UTC ISO-8601 to identity, status, and history files."""
    src = tmp_path / "demo.md"
    # Mix of identity-keyword and status-keyword sections so all three layers
    # actually get written.
    src.write_text(
        "---\n"
        "id: projects-demo\n"
        "category: projects\n"
        "---\n\n"
        "## Architecture\n\n"
        "How it fits together.\n\n"
        "## Current Status\n\n"
        "What we are doing this week (2026-04-26).\n"
    )

    from palinode.consolidation import layer_split

    before = datetime.now(UTC)
    results = layer_split.split_file(str(src))
    after = datetime.now(UTC)

    # Identity file: last_updated
    fm_id = _frontmatter(results["identity"])
    _assert_utc_iso8601(
        fm_id.get("last_updated"), before, after, field="identity.last_updated"
    )

    # Status file: last_updated (only present if status_sections is non-empty)
    assert "status" in results, "status file should have been written"
    fm_status = _frontmatter(results["status"])
    _assert_utc_iso8601(
        fm_status.get("last_updated"), before, after, field="status.last_updated"
    )

    # History file: created_at (only on first split — history is created empty)
    assert "history" in results, "history file should have been written"
    fm_hist = _frontmatter(results["history"])
    _assert_utc_iso8601(
        fm_hist.get("created_at"), before, after, field="history.created_at"
    )


def test_mem0_generate_writes_timezone_aware_utc_timestamps(tmp_path, monkeypatch):
    """``mem0_generate.generate_files`` writes UTC ISO-8601 to last_updated.

    Also verifies ``created_at`` falls back to ``datetime.now(UTC).isoformat()``
    when the source memory has no ``created_at``.
    """
    import json

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    # Minimal classified-memory fixture: one memory, no source-side
    # ``created_at`` so the fallback branch is exercised.
    classified = [
        {
            "type": "Insight",
            "group": "regression-193",
            "content": "mem0 generate timestamp fix",
            "entities": [],
            "source_agent": "test",
        }
    ]
    migration_dir = tmp_path / "migration"
    migration_dir.mkdir()
    (migration_dir / "mem0_classified.json").write_text(json.dumps(classified))

    # The function shells out to git at the end; let it fail silently
    # (tmp_path is not a repo). The file write happens before that.
    from palinode.migration import mem0_generate

    before = datetime.now(UTC)
    mem0_generate.generate_files()
    after = datetime.now(UTC)

    written = tmp_path / "insights" / "regression-193.md"
    assert written.exists(), f"mem0_generate did not write expected file: {written}"

    fm = _frontmatter(str(written))
    _assert_utc_iso8601(
        fm.get("last_updated"), before, after, field="last_updated"
    )
    # ``created_at`` came from the fallback (no source ``created_at`` in fixture)
    _assert_utc_iso8601(
        fm.get("created_at"), before, after, field="created_at (fallback)"
    )


def test_openclaw_migration_writes_timezone_aware_utc_last_updated(
    tmp_path, monkeypatch
):
    """``openclaw.run_migration`` stamps ``last_updated`` as UTC ISO-8601."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    source = tmp_path / "MEMORY.md"
    source.write_text(
        "## Demo openclaw section\n\n"
        "This is an insight worth importing.\n"
    )

    from palinode.migration import openclaw

    before = datetime.now(UTC)
    result = openclaw.run_migration(str(source))
    after = datetime.now(UTC)

    assert result["files_created"], (
        f"openclaw migration produced no files: {result!r}"
    )
    written = os.path.join(tmp_path, result["files_created"][0])
    fm = _frontmatter(written)
    _assert_utc_iso8601(
        fm.get("last_updated"), before, after, field="last_updated"
    )

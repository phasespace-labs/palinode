"""Lifecycle previews and the copies a retirement does not reach.

Two contracts, pinned against the same disposable store the lifecycle suite
uses (real SQLite, real git, real FTS5, real files under ``tmp_path``; only the
embedder and the write-path scanner are stubbed):

* **A dry run writes nothing.** ``archive``, ``restore``, ``unretract`` and
  ``forget-withdraw`` each take ``dry_run``. Apply stays the default. A dry run
  is proven inert three ways — every file's bytes, git ``HEAD`` plus the
  working-tree status, and the index rows — and it still shows the record, the
  frontmatter delta, the relation recorded or removed, the retained copies and
  the recovery path.
* **Retained copies are named, never changed.** A record that quotes, cites or
  links the retired one stays in default recall; the result lists it with what
  the user can do, bounded with an explicit "N more", and a record the caller
  may not see is counted and never named.

The surfaces (CLI text, MCP text) are driven against the real API through the
test client, so what they render is what the store returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

import tests.test_lifecycle_forget_erase_recover as _lifecycle
from palinode.core.config import config
from tests.test_lifecycle_forget_erase_recover import (
    DECISION_FILE,
    DECISION_QUERY,
    DECISION_REF,
    FORGET_TEXT,
    PREF_TEXT,
    READOUT_FILE,
    _archive,
    _fm,
    _recall_refs,
    _save,
    _seed_world,
)

# The lifecycle suite's disposable store and forget switch, shared rather than
# copied so both suites exercise one fixture.
store = _lifecycle.store
forget_enabled = _lifecycle.forget_enabled

# ── the proof that nothing was written ──────────────────────────────────────


def _snapshot(memory_dir: str) -> dict[str, Any]:
    """Every file's bytes, git HEAD + status, and the index rows.

    ``logs/`` is left out: it holds the process's own operations log (the API
    and CLI attach a logging handler there, and loading config writes an INFO
    line), which is diagnostics about the process, not store content.
    """
    files: dict[str, str] = {}
    for root, dirs, names in os.walk(memory_dir):
        if root == memory_dir:
            dirs[:] = [d for d in dirs if d not in (".git", "logs")]
        for name in names:
            if name.startswith(".palinode.db"):
                continue
            path = os.path.join(root, name)
            with open(path, "rb") as fh:
                files[os.path.relpath(path, memory_dir)] = hashlib.sha256(
                    fh.read()
                ).hexdigest()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", memory_dir, *args],
            check=True, capture_output=True, text=True,
        ).stdout

    db = sqlite3.connect(os.path.join(memory_dir, ".palinode.db"))
    try:
        rows = db.execute(
            "SELECT id, file_path, content, metadata, content_hash "
            "FROM chunks ORDER BY id"
        ).fetchall()
    finally:
        db.close()
    return {
        "files": files,
        "head": git("rev-parse", "HEAD"),
        "status": git("status", "--porcelain"),
        "commits": git("rev-list", "--count", "HEAD"),
        "rows": rows,
    }


def _assert_inert(memory_dir: str, before: dict[str, Any]) -> None:
    after = _snapshot(memory_dir)
    assert after["files"] == before["files"], "a dry run changed file bytes"
    assert after["head"] == before["head"], "a dry run moved git HEAD"
    assert after["commits"] == before["commits"]
    assert after["status"] == before["status"], "a dry run dirtied the tree"
    assert after["rows"] == before["rows"], "a dry run changed index rows"


def _post(client, path: str, **body: Any) -> dict[str, Any]:
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ═════════════════════════════════════════════════════════════════════════════
# 1. Each of the four previews, and what each preview shows
# ═════════════════════════════════════════════════════════════════════════════


def test_archive_dry_run_writes_nothing_and_shows_the_whole_change(store):
    client, memory_dir = store
    _seed_world(client)
    _save(client, "Hollowpine replacement route.", "Decision", "tidewater-v2")
    before = _snapshot(memory_dir)

    out = _post(
        client, "/archive", file_path=DECISION_FILE, reason="closed",
        superseded_by="decisions/tidewater-v2.md", dry_run=True,
    )

    _assert_inert(memory_dir, before)
    assert out["dry_run"] is True
    assert out["status"] == "would_archive"
    assert out["file"] == DECISION_FILE
    assert out["committed"] is False
    assert out["frontmatter_delta"]["status"]["to"] == "archived"
    assert out["frontmatter_delta"]["superseded_by"] == {
        "from": None, "to": "decisions/tidewater-v2.md",
    }
    assert out["relation"]["recorded"] == [
        "superseded_by: decisions/tidewater-v2.md"
    ]
    assert out["recovery"]["command"] == f"palinode restore {DECISION_FILE}"
    assert [r["file"] for r in out["retained_copies"]["records"]] == [READOUT_FILE]
    # Still current: the preview did not retire anything.
    assert DECISION_FILE in _recall_refs(client, DECISION_QUERY)


def test_restore_dry_run_writes_nothing_and_names_the_relation_removed(store):
    client, memory_dir = store
    _seed_world(client)
    _save(client, "Hollowpine replacement route.", "Decision", "tidewater-v2")
    _archive(
        client, DECISION_FILE, superseded_by="decisions/tidewater-v2.md",
    )
    before = _snapshot(memory_dir)

    out = _post(client, "/restore", file_path=DECISION_FILE, dry_run=True)

    _assert_inert(memory_dir, before)
    assert out["status"] == "would_restore"
    assert out["frontmatter_delta"]["status"] == {"from": "archived", "to": "active"}
    assert out["frontmatter_delta"]["superseded_by"] == {
        "from": "decisions/tidewater-v2.md", "to": None,
    }
    assert out["relation"]["removed"] == [
        "superseded_by: decisions/tidewater-v2.md"
    ]
    assert out["recovery"]["command"] == (
        f"palinode archive {DECISION_FILE} --superseded-by decisions/tidewater-v2.md"
    )
    assert "retained_copies" in out
    assert _fm(memory_dir, DECISION_FILE)["status"] == "archived"


CLOSEOUT_FILE = "projects/closeout.md"
CLOSEOUT_TEXT = (
    "The pavilion build closed on schedule. "
    "I settle my own invoices through the Hollowpine clearing account. "
    "The volunteer board signed off on the roster."
)


def test_unretract_dry_run_writes_nothing_and_counts_the_spans(store):
    from palinode.consolidation.retract import retract_mentions

    client, memory_dir = store
    _save(client, CLOSEOUT_TEXT, "ProjectSnapshot", "closeout")
    pref = "I settle my own invoices through Hollowpine"
    struck = retract_mentions(CLOSEOUT_FILE, pref)
    assert struck["status"] == "retracted"
    before = _snapshot(memory_dir)

    out = _post(client, "/unretract", file_path=CLOSEOUT_FILE, pref=pref, dry_run=True)

    _assert_inert(memory_dir, before)
    assert out["status"] == "would_unretract"
    assert out["mentions"] == struck["mentions"] >= 1
    assert out["frontmatter_delta"]["retracted_prefs"]["to"] is None
    assert out["relation"]["removed"]
    assert out["recovery"]["note"]
    assert "retained_copies" in out


def test_forget_withdraw_dry_run_writes_nothing_and_lists_each_step(
    store, forget_enabled
):
    client, memory_dir = store
    _save(client, PREF_TEXT, "Insight", "pref-hollowpine")
    _save(client, FORGET_TEXT, "Insight", "forget-hollowpine")
    before = _snapshot(memory_dir)

    out = _post(
        client, "/forget-withdraw",
        file_path="insights/forget-hollowpine.md", dry_run=True,
    )

    _assert_inert(memory_dir, before)
    assert out["status"] == "would_withdraw"
    assert [r["file"] for r in out["would_restore"]] == ["insights/pref-hollowpine.md"]
    assert out["would_restore"][0]["status"] == "would_restore"
    assert [r["file"] for r in out["requests_to_archive"]] == [
        "insights/forget-hollowpine.md"
    ]
    assert out["requests_to_archive"][0]["status"] == "would_archive"
    assert "retained_copies" in out
    assert out["recovery"]["note"]
    # The preference is still retired; the request is still live.
    assert _fm(memory_dir, "insights/pref-hollowpine.md")["status"] == "archived"


def test_a_noop_dry_run_says_so_and_writes_nothing(store):
    client, memory_dir = store
    _seed_world(client)
    before = _snapshot(memory_dir)
    out = _post(client, "/restore", file_path=DECISION_FILE, dry_run=True)
    _assert_inert(memory_dir, before)
    assert out["status"] == "not_archived"
    assert out["dry_run"] is True


def test_apply_is_still_the_default(store):
    """No default flip: an archive without ``dry_run`` retires, as before."""
    client, memory_dir = store
    _seed_world(client)
    out = _archive(client, DECISION_FILE, reason="closed")
    assert out["status"] == "archived"
    assert "dry_run" not in out
    assert _fm(memory_dir, DECISION_FILE)["status"] == "archived"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Retained copies: named, bounded, visibility-filtered, never changed
# ═════════════════════════════════════════════════════════════════════════════


def _write(memory_dir: str, rel: str, meta: dict[str, Any], body: str) -> None:
    import frontmatter

    path = os.path.join(memory_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter.dumps(frontmatter.Post(body, **meta)) + "\n")


def test_retained_copies_are_bounded_and_filtered(store):
    client, memory_dir = store
    _seed_world(client)
    quote = {"sources": [{"ref": DECISION_REF, "quote": "Hollowpine clearing"}]}
    for i in range(12):
        _write(memory_dir, f"insights/copy-{i:02d}.md", dict(quote), f"Copy {i}.")
    _write(
        memory_dir, "insights/private-copy.md",
        {**quote, "visibility": "private"}, "Private copy.",
    )
    _write(
        memory_dir, "insights/retired-copy.md",
        {**quote, "status": "archived"}, "Already retired copy.",
    )
    _write(memory_dir, "insights/cites.md", {"backed_by": [DECISION_REF]}, "Cites it.")
    before_bytes = _snapshot(memory_dir)["files"]

    out = _archive(client, DECISION_FILE, reason="closed")
    block = out["retained_copies"]

    named = [r["file"] for r in block["records"]]
    assert len(named) == 10
    # readout + 12 copies + the citing record = 14 visible and live.
    assert block["more"] == 4
    assert block["not_visible"] == 1
    assert block["total"] == 15
    assert "insights/private-copy.md" not in str(block)
    assert "insights/retired-copy.md" not in str(block)
    assert all(r["in_default_recall"] for r in block["records"])
    assert all(r["of"] == DECISION_FILE for r in block["records"])
    assert "stays in default recall" in block["note"]
    assert "palinode archive" in next(
        r["action"] for r in block["records"] if "sources (quoted)" in r["relations"]
    )
    # Reported, never changed: every copy's bytes are what they were. (The
    # citing record gains the existing `stale_backing` review flag, which is
    # the archive's own documented propagation, not a retained-copy write.)
    after_bytes = _snapshot(memory_dir)["files"]
    for rel, digest in before_bytes.items():
        if rel.startswith("insights/copy-") or rel in (
            "insights/private-copy.md", "insights/retired-copy.md", READOUT_FILE,
        ):
            assert after_bytes[rel] == digest, f"{rel} was changed"


def test_a_failed_copy_scan_does_not_fail_a_landed_archive(store, monkeypatch):
    """Reporting runs after the write; it must not turn success into a 500."""
    import palinode.corrections.review as review

    client, memory_dir = store
    _seed_world(client)

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(review, "retained_copies", _boom)
    out = _archive(client, DECISION_FILE, reason="closed")
    assert out["status"] == "archived"
    assert _fm(memory_dir, DECISION_FILE)["status"] == "archived"
    assert out["retained_copies"]["total"] is None
    assert "not checked" in out["retained_copies"]["error"]

    from palinode.core.lifecycle_render import render_retained_copies

    assert render_retained_copies(out["retained_copies"])[0].startswith(
        "Retained copies not checked"
    )


def test_forget_result_names_the_copies_it_did_not_reach(store, forget_enabled):
    client, memory_dir = store
    _save(client, PREF_TEXT, "Insight", "pref-hollowpine")
    # Cites the preference by typed link, in words that share nothing with it,
    # so the forget does not resolve it as a target of its own.
    _save(
        client, "Treasury follow-up owed to the pavilion volunteers.",
        "Insight", "treasury-follow-up",
        backed_by=["insights/pref-hollowpine"],
    )

    out = _save(client, FORGET_TEXT, "Insight", "forget-hollowpine")

    assert out["forget"]["archived"] == ["insights/pref-hollowpine.md"]
    block = out["forget"]["retained_copies"]
    assert [r["file"] for r in block["records"]] == ["insights/treasury-follow-up.md"]
    assert block["records"][0]["relations"] == ["backed_by"]
    assert _fm(memory_dir, "insights/treasury-follow-up.md").get("status") != "archived"


# ═════════════════════════════════════════════════════════════════════════════
# 3. The same information on the CLI and MCP surfaces
# ═════════════════════════════════════════════════════════════════════════════


def _via(client):
    """An ``api_client`` method stand-in that posts to the real test API."""

    def make(path: str, *positional: str):
        def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            body = dict(zip(positional, args, strict=False))
            body.update({k: v for k, v in kwargs.items() if v is not None})
            if not body.get("dry_run"):
                body.pop("dry_run", None)
            return _post(client, path, **body)

        return call

    return make


def test_cli_dry_runs_render_the_preview_and_write_nothing(store, forget_enabled):
    from click.testing import CliRunner

    from palinode.cli import main

    client, memory_dir = store
    _seed_world(client)
    make = _via(client)
    runner = CliRunner()
    before = _snapshot(memory_dir)
    with patch("palinode.cli._api.api_client.archive", make("/archive", "file_path")):
        archived = runner.invoke(
            main, ["archive", DECISION_FILE, "--dry-run", "--format", "text"]
        )
    _assert_inert(memory_dir, before)

    # The forget request retires the preference (and the decision, which
    # shares its words); the reversals are previewed against that state.
    _save(client, PREF_TEXT, "Insight", "pref-hollowpine")
    _save(client, FORGET_TEXT, "Insight", "forget-hollowpine")
    before = _snapshot(memory_dir)
    with (
        patch("palinode.cli._api.api_client.archive", make("/archive", "file_path")),
        patch("palinode.cli._api.api_client.restore", make("/restore", "file_path")),
        patch(
            "palinode.cli._api.api_client.forget_withdraw",
            make("/forget-withdraw", "file_path"),
        ),
    ):
        restored = runner.invoke(
            main, ["restore", "insights/pref-hollowpine.md", "--dry-run",
                   "--format", "text"],
        )
        withdrawn = runner.invoke(
            main, ["forget-withdraw", "insights/forget-hollowpine.md", "--dry-run",
                   "--format", "text"],
        )

    _assert_inert(memory_dir, before)
    for result in (archived, restored, withdrawn):
        assert result.exit_code == 0, result.output
        assert "Nothing written" in result.output
    assert "Would archive" in archived.output
    assert READOUT_FILE in archived.output
    assert "still in default recall" in archived.output
    assert f"Recovery: palinode restore {DECISION_FILE}" in archived.output
    assert "status: (unset) → archived" in archived.output
    assert "Would restore" in restored.output
    assert "Would archive request records: insights/forget-hollowpine.md" in (
        withdrawn.output
    )


def test_cli_archive_names_retained_copies_after_applying(store):
    from click.testing import CliRunner

    from palinode.cli import main

    client, _ = store
    _seed_world(client)
    with patch(
        "palinode.cli._api.api_client.archive", _via(client)("/archive", "file_path")
    ):
        result = CliRunner().invoke(main, ["archive", DECISION_FILE, "--format", "text"])
    assert result.exit_code == 0, result.output
    assert "Archived" in result.output
    assert READOUT_FILE in result.output
    assert "reported, never changed" in result.output


def test_cli_retained_copies_say_n_more_and_count_the_unseen():
    from palinode.core.lifecycle_render import render_retained_copies

    block = {
        "records": [{
            "file": "insights/a.md", "of": "decisions/x.md",
            "relations": ["backed_by"], "action": "review it",
        }],
        "total": 5, "more": 3, "not_visible": 1, "note": "",
    }
    text = "\n".join(render_retained_copies(block))
    assert "insights/a.md" in text
    assert "… and 3 more" in text
    assert "1 more you cannot see (counted, not named)" in text
    assert render_retained_copies({"records": [], "total": 0}) == []


@pytest.fixture()
def mcp_over_api(store, monkeypatch, tmp_path):
    """Route ``palinode.mcp._post`` to the real test API; keep audit in tmp."""
    import palinode.mcp as mcp
    from palinode.core.audit import AuditLogger

    client, _ = store
    monkeypatch.setattr(
        mcp, "_audit", AuditLogger(str(tmp_path), config.audit), raising=False
    )

    async def _post_via_client(path, json=None, timeout=30.0):
        return client.post(path, json=json or {})

    monkeypatch.setattr(mcp, "_post", _post_via_client)
    return mcp


def _mcp_text(mcp, tool: str, args: dict[str, Any]) -> str:
    return asyncio.run(mcp._dispatch_tool(tool, args))[0].text


def test_mcp_dry_runs_render_the_same_preview_and_write_nothing(
    store, mcp_over_api, forget_enabled
):
    client, memory_dir = store
    _seed_world(client)
    before = _snapshot(memory_dir)
    archived = _mcp_text(
        mcp_over_api, "palinode_archive", {"file_path": DECISION_FILE, "dry_run": True}
    )
    _assert_inert(memory_dir, before)

    _save(client, PREF_TEXT, "Insight", "pref-hollowpine")
    _save(client, FORGET_TEXT, "Insight", "forget-hollowpine")
    before = _snapshot(memory_dir)
    restored = _mcp_text(
        mcp_over_api, "palinode_restore",
        {"file_path": "insights/pref-hollowpine.md", "dry_run": True},
    )
    unretracted = _mcp_text(
        mcp_over_api, "palinode_unretract",
        {"file_path": DECISION_FILE, "pref": "anything", "dry_run": True},
    )
    withdrawn = _mcp_text(
        mcp_over_api, "palinode_forget_withdraw",
        {"file_path": "insights/forget-hollowpine.md", "dry_run": True},
    )

    _assert_inert(memory_dir, before)
    for text in (archived, restored, unretracted, withdrawn):
        assert "Nothing written" in text, text
    assert READOUT_FILE in archived and "still in default recall" in archived
    assert f"Recovery: palinode restore {DECISION_FILE}" in archived
    assert "Would restore" in restored
    assert "no change" in unretracted
    assert "Would withdraw" in withdrawn


def test_mcp_archive_names_retained_copies_after_applying(store, mcp_over_api):
    client, _ = store
    _seed_world(client)
    text = _mcp_text(mcp_over_api, "palinode_archive", {"file_path": DECISION_FILE})
    assert text.startswith("Archived")
    assert READOUT_FILE in text
    assert "reported, never changed" in text

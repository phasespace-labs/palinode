"""Envelope-markup guard on session-end, plus the hook fix at its source (#682).

Two entry points put a tool envelope into a session-end string:

  1. a malformed model tool-call, whose tail is absorbed into the preceding
     string parameter — which also swallows the arrays that followed it, so the
     corruption signature is *fragment present AND arrays absent*;
  2. the SessionEnd hook's ``jq`` transcript extraction, which lifted Claude
     Code harness markup straight out of the first user turn and could produce
     a stored summary ending
     ``Topic: <command-message>palinode-session</command-message>``.

The guard must fail loud on both without becoming unusable for its actual
audience: palinode is a memory system for developers, and a note *about*
tool-call syntax has to stay saveable. These tests pin both halves — the
rejections and the non-rejections.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from palinode.api.routers.session import _envelope_complaint
from palinode.core.config import config

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "examples" / "hooks" / "palinode-session-end.sh"


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))


def _call(tmp_path, monkeypatch, **kwargs):
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)
    with mock.patch("palinode.api.routers.session._check_session_end_dedup",
                    return_value=(None, None)), \
         mock.patch("palinode.api.routers.session.save_api",
                    return_value={"file_path": None}):
        from palinode.api.server import SessionEndRequest, session_end_api

        kwargs.setdefault("source", "test")
        return session_end_api(SessionEndRequest(**kwargs))


# ── Rejections: the three corroborating signals ──────────────────────────────


def test_absorbed_envelope_with_missing_arrays_is_rejected(tmp_path, monkeypatch):
    """The mechanism-1 signature: envelope tail present, arrays swallowed."""
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch,
              summary="Shipped the parser rewrite</decisions>\n</invoke>")

    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert "summary" in detail
    assert "</invoke>" in detail or "</decisions>" in detail, detail
    # The message has to tell the caller what to do about it.
    assert "JSON arrays" in detail and "fenced code block" in detail, detail


def test_harness_markup_from_the_hook_is_rejected(tmp_path, monkeypatch):
    """The confirmed live-store case, byte-for-byte, as the hook used to send it
    (``decisions``/``blockers`` explicitly empty)."""
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch,
              summary=("Auto-captured session (25 messages). Topic: "
                       "<command-message>palinode-session</command-message>"),
              decisions=[], blockers=[], project="palinode")

    assert exc.value.status_code == 400
    assert "command-message" in exc.value.detail


def test_unmatched_closing_tag_is_rejected_even_with_arrays(tmp_path, monkeypatch):
    """Structural invalidity: a closer with no opener is not prose."""
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch,
              summary="Fixed the ranker</parameter> and moved on to the indexer",
              decisions=["kept RRF"], blockers=["smoke the rig"])

    assert exc.value.status_code == 400
    assert "no matching opener" in exc.value.detail


def test_trailing_envelope_is_rejected_even_with_arrays(tmp_path, monkeypatch):
    """Positional: absorption lands the envelope at the very tail."""
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch,
              summary="Wrote up the <tool_use> lifecycle </tool_use>",
              decisions=["d"], blockers=["b"])

    assert exc.value.status_code == 400
    assert "very end" in exc.value.detail


def test_corrupt_array_entry_is_rejected_and_named(tmp_path, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch,
              summary="A clean summary",
              decisions=["keep the executor deterministic", "ship it</invoke>"])

    assert exc.value.status_code == 400
    assert "decisions[1]" in exc.value.detail, exc.value.detail


def test_rejection_writes_nothing_at_all(tmp_path, monkeypatch):
    """Fail loud *before* any write — a rejected request must not leave a
    half-captured session behind in the daily note or the status file."""
    os.makedirs(os.path.join(str(tmp_path), "projects"))
    status_path = os.path.join(str(tmp_path), "projects", "palinode-status.md")
    with open(status_path, "w") as f:
        f.write("# palinode status\n")

    with pytest.raises(HTTPException):
        _call(tmp_path, monkeypatch,
              summary="Broken</decisions>", project="palinode")

    assert not os.path.exists(os.path.join(str(tmp_path), "daily"))
    assert open(status_path).read() == "# palinode status\n"


def test_wire_level_400_carries_the_detail(tmp_path, monkeypatch):
    """The hook only sees an HTTP status; curl -f turns >=400 into a fallback
    write. Confirm the boundary really returns 400 rather than a 422/500."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    from palinode.api.server import app

    with TestClient(app) as client:
        res = client.post("/session-end", json={
            "summary": "broke it</decisions>", "decisions": [], "blockers": [],
        })
    assert res.status_code == 400, res.text
    assert "</decisions>" in res.json()["detail"]


# ── Non-rejections: the false positives that would make this unusable ────────


def test_fenced_code_block_is_always_legitimate(tmp_path, monkeypatch):
    """The escape hatch. Note that arrays are absent here too — a code fence
    beats the co-occurrence signal, deliberately."""
    result = _call(tmp_path, monkeypatch, summary=(
        "Documented the corruption signature:\n"
        "```\n"
        "Topic: <command-message>palinode-session</command-message></invoke>\n"
        "```\n"
    ))
    assert result["daily_file"]


def test_inline_backticks_are_legitimate(tmp_path, monkeypatch):
    result = _call(tmp_path, monkeypatch,
                   summary="The absorbed fragment was `</decisions>` at the tail")
    assert result["daily_file"]


def test_matched_midstring_markup_with_arrays_passes(tmp_path, monkeypatch):
    """A genuine note about tool-call syntax: opener and closer both present,
    not at the tail, and the arrays arrived. Nothing corrupt about it."""
    result = _call(
        tmp_path, monkeypatch,
        summary="Explained how <invoke> and </invoke> bracket a tool call, then moved on",
        decisions=["document the envelope shape"], blockers=["add a test"],
    )
    assert result["daily_file"]


def test_unrelated_angle_brackets_are_ignored(tmp_path, monkeypatch):
    result = _call(tmp_path, monkeypatch,
                   summary="Fixed the <div> nesting in the docs site</div>")
    assert result["daily_file"]


def test_ordinary_summary_still_passes(tmp_path, monkeypatch):
    result = _call(tmp_path, monkeypatch, summary="Landed hybrid search",
                   decisions=["RRF"], blockers=["smoke"])
    assert result["daily_file"]


@pytest.mark.parametrize("text", [
    "",
    "a normal sentence about memory consolidation",
    "arrow -> and comparison a < b > c",
    "the executor applies KEEP/UPDATE/MERGE ops",
])
def test_clean_text_never_complains(text):
    assert _envelope_complaint(text, "summary", arrays_present=False) is None


# ── The hook, fixed at its source ────────────────────────────────────────────

_requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="jq/bash unavailable",
)

_MARKUP_TRANSCRIPT = "\n".join(json.dumps(row) for row in [
    {"type": "user", "message": {"role": "user", "content": (
        "<command-message>palinode-session</command-message>\n"
        "<command-name>/palinode-session</command-name>\n"
        "<system-reminder>never show this to the user</system-reminder>\n"
        "fix the <div> nesting bug"
    )}},
    {"type": "user", "message": {"role": "user", "content": "second turn"}},
    {"type": "user", "message": {"role": "user", "content": "third turn"}},
]) + "\n"


def _run_hook(tmp_path: Path, transcript: str) -> dict:
    """Run the canonical hook in dry-run and return the payload it would POST."""
    t = tmp_path / "transcript.jsonl"
    t.write_text(transcript, encoding="utf-8")
    stdin = json.dumps({"transcript_path": str(t), "cwd": str(tmp_path), "reason": "clear"})

    env = dict(os.environ, PALINODE_HOOK_DRYRUN="1")
    proc = subprocess.run(["bash", str(HOOK)], input=stdin,
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"hook must exit 0; got {proc.returncode}: {proc.stderr}"
    body = proc.stdout.split("\n", 1)[1]
    return json.loads(body)


@_requires_jq
def test_hook_strips_harness_markup_from_the_topic(tmp_path):
    payload = _run_hook(tmp_path, _MARKUP_TRANSCRIPT)
    summary = payload["summary"]

    for tag in ("<command-message>", "</command-message>", "<command-name>",
                "<system-reminder>", "never show this to the user"):
        assert tag not in summary, f"{tag!r} survived into: {summary!r}"
    # The human-meaningful text — including non-harness markup — is preserved.
    assert "fix the <div> nesting bug" in summary, summary


@_requires_jq
def test_hook_output_passes_the_boundary_guard(tmp_path):
    """The two halves of #682 have to agree: what the hook now sends must not
    be what the boundary now rejects."""
    payload = _run_hook(tmp_path, _MARKUP_TRANSCRIPT)
    assert _envelope_complaint(
        payload["summary"], "summary",
        arrays_present=bool(payload["decisions"] or payload["blockers"]),
    ) is None, payload["summary"]


@_requires_jq
def test_hook_still_captures_a_plain_session(tmp_path):
    """Regression guard on the rewritten jq: an ordinary transcript still
    produces the same topic line it always did."""
    plain = "\n".join(json.dumps(
        {"type": "user", "message": {"role": "user", "content": text}}
    ) for text in ["refactor the wrap bug", "now PR it", "and smoke it"]) + "\n"

    payload = _run_hook(tmp_path, plain)
    assert "refactor the wrap bug" in payload["summary"], payload

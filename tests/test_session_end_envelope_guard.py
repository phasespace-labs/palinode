"""Envelope-markup guard on session-end, plus the hook fix at its source.

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

from palinode.core.envelope import envelope_complaint
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
    with open(status_path, "w", encoding="utf-8") as f:
        f.write("# palinode status\n")

    with pytest.raises(HTTPException):
        _call(tmp_path, monkeypatch,
              summary="Broken</decisions>", project="palinode")

    assert not os.path.exists(os.path.join(str(tmp_path), "daily"))
    assert open(status_path, encoding="utf-8").read() == "# palinode status\n"


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
    assert envelope_complaint(
        text, "summary", missing_params=("decisions", "blockers")
    ) is None


# ── Empty is not absent, and a rejection you can quote back ──────────────────


def test_empty_arrays_are_not_reported_as_never_arriving(tmp_path, monkeypatch):
    """Present-but-empty is not absent.

    Absorption swallows parameters whole; it does not deliver them as ``[]``.
    Truthiness-testing them conflated the two, so a caller with nothing to
    report was told their arrays never arrived and pointed at a malformed tool
    call that had not happened. The summary here is still rejected — on the
    structural signal, which is the honest reason.
    """
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch,
              summary="Reviewed the guard</summary>",
              decisions=[], blockers=[])

    detail = exc.value.detail
    assert exc.value.status_code == 400
    assert "no matching opener" in detail, detail
    assert "arrived with it" not in detail, detail


def test_genuinely_absent_arrays_still_trigger_cooccurrence(tmp_path, monkeypatch):
    """Signal 1 is the only near-zero-false-positive detector of absorption.
    Narrowing it to true absence must not switch it off."""
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch, summary="Shipped it <summary>x</summary>")

    assert "arrived with it" in exc.value.detail, exc.value.detail


def test_rejection_message_can_be_quoted_back_without_re_rejecting(tmp_path, monkeypatch):
    """The self-perpetuating rejection loop.

    The message names the offending fragment, and the caller's natural next
    move is to quote it — into a retry summary, an issue, a session note. While
    that fragment was rendered with ``repr`` it came back unprotected, so the
    retry was rejected identically and six attempts in a row read as a
    deterministic transport fault. Backticks are the guard's own documented
    escape hatch; its own message has to use them.
    """
    with pytest.raises(HTTPException) as first:
        _call(tmp_path, monkeypatch, summary="Wrapped the session</summary>")

    detail = first.value.detail
    assert "`</summary>`" in detail, detail

    result = _call(tmp_path, monkeypatch,
                   summary=f"Retrying after rejection: {detail}",
                   decisions=["quote the guard verbatim"], blockers=[])
    assert result["daily_file"]


def test_cooccurrence_message_offers_both_causes_and_asserts_neither(tmp_path, monkeypatch):
    """The message must report the observation, not diagnose the mechanism.

    Absence is all the server sees. "The parameters were destroyed in transit"
    and "the caller did not send them" arrive identically, so a message that
    names absorption states a hypothesis in the grammar of a finding. Readers
    relay it back: two separate investigations have concluded "the tool
    envelope broke" citing nothing but this sentence, and gone looking in the
    transport while the cause sat in the payload. Whatever the message
    explains is what reaches the issue tracker, so it has to explain only what
    is known.
    """
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch, summary="Wrapped it</invoke>")

    detail = exc.value.detail
    # Reports what was observed.
    assert "no `decisions`/`blockers` arrived with it" in detail, detail
    # Names both candidates and hands the discrimination to the caller.
    assert "only you can tell them apart" in detail, detail
    assert "did not send" in detail, detail
    # Does not present absorption as the established cause.
    assert "the signature of" not in detail, detail


def test_structural_signal_does_not_speculate_about_cause(tmp_path, monkeypatch):
    """When the arrays *did* arrive there is no ambiguity to explain, and the
    both-causes sentence would be noise. It must not appear."""
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch, summary="Fixed the ranker</parameter> then moved on",
              decisions=["d"], blockers=["b"])

    assert "only you can tell them apart" not in exc.value.detail, exc.value.detail


def test_remediation_covers_the_caller_who_did_send_the_arrays(tmp_path, monkeypatch):
    """"Re-send with the arrays" is unactionable for half the callers who see it.

    When the arrays were sent and lost, that instruction names a fix the caller
    cannot perform — and it reads as the whole remedy, so the rational response
    is to retry unchanged. One caller followed it three times, each attempt
    failing identically. The message must cover both branches and name the
    escape that works when retrying cannot.
    """
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch, summary="Wrapped the session</summary>")

    detail = exc.value.detail
    assert "If you did not send them" in detail, detail
    assert "build the call fresh rather than editing the one that failed" in detail, detail
    assert "/session-end" in detail and "HTTP API" in detail, detail
    assert "dry_run" in detail, detail


def test_no_array_advice_when_the_arrays_arrived(tmp_path, monkeypatch):
    """Telling a caller to re-send arrays it already sent is the noise that
    made the other message misleading. Where they arrived, say so instead."""
    with pytest.raises(HTTPException) as exc:
        _call(tmp_path, monkeypatch, summary="Fixed it</parameter> then moved on",
              decisions=["d"], blockers=["b"])

    detail = exc.value.detail
    assert "The arrays arrived" in detail, detail
    assert "re-send with" not in detail.lower(), detail


def test_mcp_forwards_empty_arrays_instead_of_eliding_them(tmp_path, monkeypatch):
    """The MCP surface dropped falsy arrays before the request left the client,
    manufacturing the absorption signature server-side."""
    import palinode.mcp as pmcp

    captured: dict = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"daily_file": "daily/x.md", "entry": ""}

    async def _fake_post(path, json=None, timeout=None):
        captured.update(json or {})
        return _Resp()

    monkeypatch.setattr(pmcp, "_post", _fake_post)
    import asyncio
    asyncio.run(pmcp.call_tool("palinode_session_end", {
        "summary": "a clean summary", "decisions": [], "blockers": [],
    }))

    assert captured.get("decisions") == [], captured
    assert captured.get("blockers") == [], captured


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
                          capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace")
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
    """The two halves of the tool-envelope validation have to agree: what the hook now sends must not
    be what the boundary now rejects.

    Calls the boundary's own helper rather than re-deriving its presence rule.
    The re-derived copy silently encoded the older truthiness semantics and
    would have kept asserting agreement with a boundary that had moved.
    """
    from palinode.api.routers.session import _first_envelope_complaint
    from palinode.api.server import SessionEndRequest

    payload = _run_hook(tmp_path, _MARKUP_TRANSCRIPT)
    assert _first_envelope_complaint(
        SessionEndRequest(**payload)
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


# ── dry_run: characterise a failure without paying for it ────────────────────


def _dry(tmp_path, monkeypatch, **kwargs):
    kwargs["dry_run"] = True
    return _call(tmp_path, monkeypatch, **kwargs)


def test_dry_run_writes_absolutely_nothing(tmp_path, monkeypatch):
    """The whole point. Diagnosing a session-end failure previously required
    issuing real ones, so characterising a write bug meant vandalising the
    record to do it — twice, in practice."""
    os.makedirs(os.path.join(str(tmp_path), "projects"))
    status_path = os.path.join(str(tmp_path), "projects", "palinode-status.md")
    with open(status_path, "w", encoding="utf-8") as f:
        f.write("# palinode status\n")

    result = _dry(tmp_path, monkeypatch, summary="A perfectly valid summary",
                  decisions=["d"], blockers=["b"], project="palinode")

    assert result["dry_run"] is True
    assert result["committed"] is False and result["pushed"] is False
    # Nothing on disk moved.
    assert not os.path.exists(os.path.join(str(tmp_path), "daily"))
    assert open(status_path, encoding="utf-8").read() == "# palinode status\n"


def test_dry_run_renders_the_entry_it_would_write(tmp_path, monkeypatch):
    """Validate-only is useless if it does not show you the result."""
    result = _dry(tmp_path, monkeypatch, summary="Landed hybrid search",
                  decisions=["kept RRF"], blockers=["smoke the rig"])

    entry = result["entry"]
    assert "Landed hybrid search" in entry
    assert "kept RRF" in entry and "smoke the rig" in entry
    assert result["daily_file"].startswith("daily/")


def test_dry_run_still_rejects_a_corrupt_payload(tmp_path, monkeypatch):
    """A dry run that skipped validation would report success on exactly the
    payloads it exists to diagnose. The guard runs before the dry-run exit."""
    with pytest.raises(HTTPException) as exc:
        _dry(tmp_path, monkeypatch, summary="Wrapped it</invoke>",
             decisions=["d"], blockers=["b"])
    assert exc.value.status_code == 400


def test_dry_run_reports_status_file_only_when_it_exists(tmp_path, monkeypatch):
    """The real path appends to the project status file only if it is already
    there, so the dry run must not promise a write that would not happen."""
    absent = _dry(tmp_path, monkeypatch, summary="s", project="palinode")
    assert absent["status_file"] is None

    os.makedirs(os.path.join(str(tmp_path), "projects"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "projects", "palinode-status.md"), "w", encoding="utf-8") as f:
        f.write("# palinode status\n")
    present = _dry(tmp_path, monkeypatch, summary="s", project="palinode")
    assert present["status_file"] == "projects/palinode-status.md"


def test_default_is_not_dry_run(tmp_path, monkeypatch):
    """Absent the flag, session-end must still actually capture."""
    result = _call(tmp_path, monkeypatch, summary="Landed it", decisions=["d"])
    assert not result.get("dry_run")
    assert os.path.exists(os.path.join(str(tmp_path), "daily"))

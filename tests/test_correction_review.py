"""The correction contract's refusals, inheritances and recoveries.

Real SQLite, real git, real files under ``tmp_path`` throughout — the only
thing stubbed is the embedder, which is the network.

The tests are grouped by what they protect:

* **Refusals.** A stale revision, an ambiguous ref, an ambiguous claim id and a
  correction that names no target are each refused *with what the caller needs
  to decide* — never resolved by guesswork, and never half-applied.
* **Blast radius.** A correction changes its target. Records that quote or
  derive from it are reported and left byte-identical; an open contradiction is
  shown, not settled.
* **Inheritance.** ``update_policy`` comes from the target, not from the
  correction.
* **The candidate queue.** Applied and dismissed rows are marked and kept, so a
  re-scan cannot resurrect either; a candidate that named no target has its
  target chosen by the reviewer.
* **Recovery.** Undo previews before it writes, names the three different
  things it is not, and refuses to resurrect what a separate act retired.
* **Controls.** A paused store refuses an explicit correction exactly as it
  refuses an explicit save.
* **A half-finished write.** A document apply is two writes; a failure at the
  second is reported as ``applied: "partial"`` naming what was written, what
  was not and the command that completes it — on every surface.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from palinode.core.config import config
from palinode.corrections import review
from palinode.corrections.queue import (
    QUEUE_RELATIVE_PATH,
    CorrectionCandidate,
    append_candidates,
    load_candidates,
)

_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", False)
    for directory in ("decisions", "insights", "people", "projects", "research"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    from palinode.core import store as store_module

    store_module.init_db()
    return tmp_path


def _write(store_dir: Path, rel: str, frontmatter: str, body: str) -> str:
    path = store_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(store_dir), "add", rel], check=True)
    subprocess.run(
        ["git", "-C", str(store_dir), "commit", "-q", "-m", f"seed {rel}"], check=True
    )
    return rel


def _decision(store_dir: Path, slug: str, body: str, extra: str = "") -> str:
    return _write(
        store_dir,
        f"decisions/{slug}.md",
        f"id: decisions-{slug}\ntype: Decision\ncategory: decisions\n"
        f"title: {slug}\nstatus: active{extra}",
        body,
    )


def _embedded(fn, *args, **kwargs):
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR), patch(
        "palinode.core.store.scan_memory_content", return_value=(True, "OK")
    ):
        return fn(*args, **kwargs)


# ── refusals ────────────────────────────────────────────────────────────────
def test_a_stale_revision_is_refused_with_both_hashes(store: Path) -> None:
    """The file changed between preview and apply: refuse, and say so."""
    rel = _decision(store, "alpha", "The cache is in-process.")
    preview = review.preview_correction(target=rel, replacement="The cache is shared.")
    revision = preview["confirm"]["expect_revision"]

    (store / rel).write_text(
        (store / rel).read_text(encoding="utf-8") + "\nA later edit.\n", encoding="utf-8"
    )

    with pytest.raises(review.StaleRevisionError) as caught:
        _embedded(
            review.apply_correction,
            target=rel,
            replacement="The cache is shared.",
            expect_revision=revision,
            confirm=True,
        )
    assert caught.value.expected == revision
    assert caught.value.actual != revision
    # Nothing was written: the target is still active.
    assert "status: archived" not in (store / rel).read_text(encoding="utf-8")


def test_apply_without_confirmation_writes_nothing(store: Path) -> None:
    rel = _decision(store, "alpha", "The cache is in-process.")
    revision = review.file_revision(str(store / rel))

    with pytest.raises(review.CorrectionError, match="explicit confirmation"):
        review.apply_correction(
            target=rel, replacement="x", expect_revision=revision, confirm=False
        )
    assert "status: archived" not in (store / rel).read_text(encoding="utf-8")


def test_an_ambiguous_slug_is_refused_with_the_candidates(store: Path) -> None:
    """Two memories share a slug. Name both; pick neither."""
    _decision(store, "storage", "Decisions copy.")
    _write(
        store,
        "insights/storage.md",
        "id: insights-storage\ntype: Insight\ncategory: insights\ntitle: storage",
        "Insights copy.",
    )

    with pytest.raises(review.AmbiguousTargetError) as caught:
        review.preview_correction(target="storage", replacement="x")
    files = {row["file"] for row in caught.value.candidates}
    assert files == {"decisions/storage.md", "insights/storage.md"}
    assert caught.value.as_dict()["error"] == "ambiguous_target"


def test_a_duplicated_claim_id_is_refused_with_both_lines(store: Path) -> None:
    """A fact id is derived from line text, so identical lines share one."""
    rel = _decision(
        store,
        "log",
        "- Session wrapped <!-- fact:dup -->\n- Session wrapped <!-- fact:dup -->",
    )
    with pytest.raises(review.AmbiguousTargetError) as caught:
        review.preview_correction(target=rel, claim_id="dup", replacement="x")
    assert len(caught.value.candidates) == 2


def test_a_correction_with_no_target_is_refused_not_inferred(store: Path) -> None:
    """A candidate whose span named no replacement target names nothing."""
    _seed_candidate(store, span="no, that's wrong", replaced=None, replacement=None)
    candidate = load_candidates()[0]

    with pytest.raises(review.TargetRequiredError) as caught:
        review.preview_correction(candidate_id=candidate["candidate_id"])
    assert caught.value.candidates == []
    assert "choose the target explicitly" in str(caught.value)


def test_an_unknown_target_is_a_not_found_not_a_guess(store: Path) -> None:
    with pytest.raises(review.TargetNotFoundError):
        review.preview_correction(target="decisions/never-existed", replacement="x")


def test_a_traversing_target_is_refused_by_the_path_guard(store: Path) -> None:
    from palinode.core.path_guard import PathTraversalError

    with pytest.raises(PathTraversalError):
        review.preview_correction(target="../../etc/passwd", replacement="x")


def test_a_traversing_evidence_ref_is_refused_before_it_is_written(store: Path) -> None:
    from palinode.core.path_guard import PathTraversalError

    rel = _decision(store, "alpha", "The cache is in-process.")
    with pytest.raises(PathTraversalError):
        review.preview_correction(
            target=rel, replacement="x", backed_by=["../../../etc/passwd"]
        )


# ── blast radius ────────────────────────────────────────────────────────────
def test_records_that_reference_the_target_are_reported_and_untouched(store: Path) -> None:
    target = _decision(store, "storage", "Use the local file store.")
    citing = _write(
        store,
        "insights/rollout.md",
        "id: insights-rollout\ntype: Insight\ncategory: insights\ntitle: rollout\n"
        "backed_by:\n- decisions/storage",
        "The rollout plan assumes the storage decision.",
    )
    before = (store / citing).read_bytes()

    preview = review.preview_correction(target=target, replacement="Use the hosted store.")
    referenced = {row["file"]: row["relation"] for row in preview["referencing_records"]}
    assert referenced == {"insights/rollout.md": "backed_by"}

    _embedded(
        review.apply_correction,
        target=target,
        replacement="Use the hosted store.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )
    # The dependent may gain a `stale_backing` review flag from the existing
    # propagation, but its *claim* is never rewritten by the correction.
    after = (store / citing).read_text(encoding="utf-8")
    assert "The rollout plan assumes the storage decision." in after
    assert "Use the hosted store." not in after
    if (store / citing).read_bytes() != before:
        assert "stale_backing" in after, "the only sanctioned change is a review flag"


def test_an_unaffected_neighbour_is_byte_identical(store: Path) -> None:
    target = _decision(store, "storage", "Use the local file store.")
    neighbour = _decision(store, "timestamps", "Store event timestamps in UTC.")
    before = (store / neighbour).read_bytes()

    preview = review.preview_correction(target=target, replacement="Use the hosted store.")
    _embedded(
        review.apply_correction,
        target=target,
        replacement="Use the hosted store.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )
    assert (store / neighbour).read_bytes() == before


def test_an_open_conflict_is_shown_and_not_settled(store: Path) -> None:
    """Palinode records disagreement. A correction does not pick a winner."""
    north = _decision(
        store, "region-north", "Deploy in the north region.",
        extra="\ncontradicts:\n- decisions/region-south",
    )
    south = _decision(
        store, "region-south", "Deploy in the south region.",
        extra="\ncontradicts:\n- decisions/region-north",
    )
    south_before = (store / south).read_bytes()

    preview = review.preview_correction(target=north, replacement="Deploy in the east region.")
    relations = {row["relation"] for row in preview["referencing_records"] if row["file"] == south}
    assert "contradicts" in relations, "the conflicting record must be shown"

    _embedded(
        review.apply_correction,
        target=north,
        replacement="Deploy in the east region.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )
    assert (store / south).read_bytes() == south_before, (
        "correcting one side of a contradiction must not resolve the other"
    )


def test_an_unevidenced_correction_says_so_rather_than_citing_the_original(
    store: Path
) -> None:
    rel = _decision(store, "storage", "Use the local file store.")
    preview = review.preview_correction(target=rel, replacement="Use the hosted store.")
    assert preview["evidence"]["backed_by"] == []
    assert any("cites no supporting record" in w for w in preview["warnings"])
    assert "verified quote establishes what a source said" in " ".join(preview["notes"])


# ── inheritance ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("policy", ["append", "replace"])
def test_the_replacement_inherits_the_targets_update_policy(store: Path, policy: str) -> None:
    rel = _decision(store, "profile", "The service runs single-region.",
                    extra=f"\nupdate_policy: {policy}")
    preview = review.preview_correction(target=rel, replacement="The service runs multi-region.")
    assert preview["target"]["update_policy"] == policy
    assert "inherited from the target" in preview["target"]["update_policy_source"]

    result = _embedded(
        review.apply_correction,
        target=rel,
        replacement="The service runs multi-region.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
        slug="profile-corrected",
    )
    replacement = store / result["replacement"]["rel_path"]
    assert f"update_policy: {policy}" in replacement.read_text(encoding="utf-8"), (
        "the correction must carry the target's write semantics, not impose its own"
    )


def test_a_living_document_refuses_a_claim_level_fork(store: Path) -> None:
    """ADR-015's replace guard: a living doc is corrected whole, not forked."""
    rel = _decision(
        store, "living", "- The service runs single-region. <!-- fact:region -->",
        extra="\nupdate_policy: replace",
    )
    preview = review.preview_correction(
        target=rel, claim_id="region", replacement="The service runs multi-region."
    )
    assert "update_policy: replace" in preview["refused"]
    assert preview["confirm"]["command"] is None

    with pytest.raises(review.CorrectionRefused):
        _embedded(
            review.apply_correction,
            target=rel,
            claim_id="region",
            replacement="The service runs multi-region.",
            expect_revision=review.file_revision(str(store / rel)),
            confirm=True,
        )


def test_a_claim_level_correction_supersedes_only_that_claim(store: Path) -> None:
    rel = _decision(
        store,
        "log",
        "- The cache is in-process. <!-- fact:cache -->\n"
        "- Timestamps are UTC. <!-- fact:clock -->",
    )
    preview = review.preview_correction(
        target=rel, claim_id="cache", replacement="The cache is shared."
    )
    assert preview["level"] == "claim"
    assert preview["old_text"] == "The cache is in-process."

    _embedded(
        review.apply_correction,
        target=rel,
        claim_id="cache",
        replacement="The cache is shared.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )
    text = (store / rel).read_text(encoding="utf-8")
    assert "~~The cache is in-process.~~" in text
    assert "The cache is shared." in text
    assert "- Timestamps are UTC. <!-- fact:clock -->" in text, (
        "the neighbouring claim must be untouched"
    )


# ── the candidate queue ─────────────────────────────────────────────────────
def _seed_candidate(
    store_dir: Path,
    *,
    span: str,
    replaced: str | None,
    replacement: str | None,
    session_id: str = "session-1",
    project: str | None = "harbor-notes",
    classification: str = "needs_review",
) -> str:
    candidate = CorrectionCandidate(
        harness="claude-code",
        session_id=session_id,
        turn_index=4,
        turn_uuid="uuid-4",
        span=span,
        span_hash=f"hash-of-{span[:12]}",
        grep_family="correction",
        matched_rules=("no-thats-wrong",),
        classification=classification,
        classifier={"decided": False, "model": None, "config_role": None, "reason": "not run"},
        window_turns=(2, 6),
        occurred_at="2026-09-01T10:00:00Z",
        detected_at="2026-09-02T10:00:00Z",
        project=project,
        rationale="the user said so",
        replaced=replaced,
        replacement=replacement,
    )
    append_candidates([candidate])
    return candidate.candidate_id


def test_a_needs_review_candidate_is_reviewable_like_any_other(store: Path) -> None:
    rel = _decision(store, "storage", "Use the local file store.")
    candidate_id = _seed_candidate(
        store,
        span="no, use the hosted store",
        replaced="Use the local file store.",
        replacement="Use the hosted store.",
        classification="needs_review",
    )

    preview = review.preview_correction(target=rel, candidate_id=candidate_id)
    assert preview["source"]["kind"] == "candidate"
    assert preview["source"]["classification"] == "needs_review"
    assert preview["new_text"] == "Use the hosted store."
    assert preview["scope"]["project"] == "harbor-notes"


def test_applying_marks_the_candidate_and_names_it_in_the_provenance(store: Path) -> None:
    rel = _decision(store, "storage", "Use the local file store.")
    candidate_id = _seed_candidate(
        store,
        span="no, use the hosted store",
        replaced="Use the local file store.",
        replacement="Use the hosted store.",
    )
    preview = review.preview_correction(target=rel, candidate_id=candidate_id)
    result = _embedded(
        review.apply_correction,
        target=rel,
        candidate_id=candidate_id,
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )

    assert result["candidate"]["status"] == "applied"
    assert result["actor"] == f"reviewed-correction candidate:{candidate_id}"
    log = subprocess.run(
        ["git", "-C", str(store), "log", "--format=%s", "-3"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"candidate:{candidate_id}" in log
    assert f"candidate:{candidate_id}" in (
        store / "decisions" / "storage-history.md"
    ).read_text(encoding="utf-8")


def test_a_dismissal_is_recorded_and_needs_a_reason(store: Path) -> None:
    candidate_id = _seed_candidate(
        store, span="no, that's wrong", replaced=None, replacement=None
    )
    with pytest.raises(review.CorrectionError, match="needs a reason"):
        review.dismiss_candidate(candidate_id, reason="  ")

    result = review.dismiss_candidate(candidate_id, reason="the span is about a code change")
    assert result["applied"] is False
    assert result["candidate"]["status"] == "dismissed"
    assert result["candidate"]["resolution"]["reason"] == "the span is about a code change"


def test_a_rescan_does_not_resurrect_a_dismissed_candidate(store: Path) -> None:
    """The dedupe key is (session id, span hash), read from every row."""
    candidate_id = _seed_candidate(
        store, span="no, that's wrong", replaced=None, replacement=None
    )
    review.dismiss_candidate(candidate_id, reason="not a memory correction")

    # The same detection, run again — byte-identical span, same session.
    again = _seed_candidate(
        store, span="no, that's wrong", replaced=None, replacement=None
    )
    assert again == candidate_id
    rows = load_candidates()
    assert len(rows) == 1, "a re-scan must not append a second row"
    assert rows[0]["status"] == "dismissed", "nor flip a decided row back to proposed"


def test_a_resolved_candidate_is_not_silently_re_resolved(store: Path) -> None:
    candidate_id = _seed_candidate(
        store, span="no, that's wrong", replaced=None, replacement=None
    )
    review.dismiss_candidate(candidate_id, reason="first decision")
    second = review.dismiss_candidate(candidate_id, reason="second decision")
    assert second["candidate"]["already_resolved"] is True
    assert second["candidate"]["resolution"]["reason"] == "first decision"


def test_the_queue_file_is_never_deleted_by_a_resolution(store: Path) -> None:
    _seed_candidate(store, span="no, that's wrong", replaced=None, replacement=None)
    assert (store / QUEUE_RELATIVE_PATH).exists()
    review.dismiss_candidate(load_candidates()[0]["candidate_id"], reason="x")
    assert (store / QUEUE_RELATIVE_PATH).exists()


def test_an_unavailable_transcript_does_not_weaken_the_span(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue stores the span, not the transcript. Say what is missing."""
    monkeypatch.setattr(config.capture.transcripts, "harness_paths", {})
    rel = _decision(store, "storage", "Use the local file store.")
    candidate_id = _seed_candidate(
        store,
        span="no, use the hosted store",
        replaced="Use the local file store.",
        replacement="Use the hosted store.",
    )

    preview = review.preview_correction(target=rel, candidate_id=candidate_id)
    source = preview["source"]
    assert source["available"] is False
    assert "the source is unavailable" in source["unavailable_reason"]
    assert "stored span is still the evidence" in source["unavailable_reason"]
    assert source["span"] == "no, use the hosted store"
    # The correction is still reviewable and appliable — the span is the evidence.
    assert preview["confirm"]["command"] is not None


def test_candidates_naming_a_record_match_on_quoted_text_not_on_a_guess(
    store: Path
) -> None:
    rel = _decision(store, "storage", "Use the local file store.")
    named = _seed_candidate(
        store,
        span="no, use the hosted store",
        replaced="Use the local file store.",
        replacement="Use the hosted store.",
        session_id="session-named",
    )
    _seed_candidate(
        store,
        span="no, that's wrong",
        replaced=None,
        replacement=None,
        session_id="session-unnamed",
    )

    matched = [row["candidate_id"] for row in review.candidates_naming(rel)]
    assert matched == [named], (
        "a candidate that quoted no replaced text names nothing — which is "
        "correct, and is why the reviewer chooses its target"
    )


# ── recovery ────────────────────────────────────────────────────────────────
def test_undo_previews_before_it_writes_and_names_what_it_is_not(store: Path) -> None:
    rel = _decision(store, "storage", "Use the local file store.")
    preview = review.preview_correction(target=rel, replacement="Use the hosted store.")
    _embedded(
        review.apply_correction,
        target=rel,
        replacement="Use the hosted store.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )

    undo_preview = review.preview_undo(target=rel)
    assert undo_preview["applied"] is False
    assert undo_preview["target"]["status"] == "archived"
    assert undo_preview["target"]["superseded_by"]
    assert "restores the previous assertion" in undo_preview["restores"]
    assert "does not delete history" in undo_preview["does_not_delete_history"]
    assert "cannot undo external actions" in undo_preview["cannot_reach_external_actions"]
    assert "second decision" in " ".join(undo_preview["also_true"])
    # Still archived: a preview writes nothing.
    assert "status: archived" in (store / rel).read_text(encoding="utf-8")

    result = _embedded(
        review.apply_undo,
        target=rel,
        expect_revision=undo_preview["confirm"]["expect_revision"],
        confirm=True,
    )
    assert result["applied"] is True
    restored = (store / rel).read_text(encoding="utf-8")
    assert "status: active" in restored
    assert "superseded_by" not in restored
    assert "Use the local file store." in restored
    # History is not deleted: the audit sibling and the replacement both remain.
    assert (store / "decisions" / "storage-history.md").exists()
    assert (store / "decisions" / "storage-corrected.md").exists()


def test_undo_refuses_a_stale_revision(store: Path) -> None:
    rel = _decision(store, "storage", "Use the local file store.")
    preview = review.preview_correction(target=rel, replacement="Use the hosted store.")
    _embedded(
        review.apply_correction,
        target=rel,
        replacement="Use the hosted store.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )
    with pytest.raises(review.StaleRevisionError):
        review.apply_undo(target=rel, expect_revision="0" * 64, confirm=True)


def test_undo_refuses_a_record_that_is_not_archived(store: Path) -> None:
    rel = _decision(store, "storage", "Use the local file store.")
    preview = review.preview_undo(target=rel)
    assert "nothing for an undo to restore" in preview["refused"]
    assert preview["confirm"]["command"] is None


def test_undo_refuses_to_resurrect_a_separately_retracted_record(store: Path) -> None:
    """Restore is not a back door around retraction or a forget request."""
    rel = _decision(
        store, "storage", "Use the local file store.",
        extra="\nstatus: archived\nsuperseded_by: decisions/storage-corrected\n"
              "retracted_prefs:\n- prefers-local-storage",
    )
    preview = review.preview_undo(target=rel)
    assert "mention-level retraction" in preview["refused"]
    with pytest.raises(review.CorrectionRefused):
        review.apply_undo(target=rel, expect_revision="x", confirm=True)


def test_undo_refuses_to_reverse_a_forget_request(store: Path) -> None:
    rel = _decision(
        store, "storage", "Use the local file store.",
        extra="\nstatus: archived\nforgotten_at: 2026-09-01T00:00:00Z",
    )
    preview = review.preview_undo(target=rel)
    assert "forget request" in preview["refused"]


# ── controls ────────────────────────────────────────────────────────────────
def test_a_paused_store_refuses_an_explicit_correction(store: Path) -> None:
    """The capture policy's own rule for explicit writes, not a new one.

    `evaluate_capture_policy` checks the pause *before* the automatic/explicit
    split, so a store-wide pause stops an explicit MCP/API call too. The
    correction routes are in the middleware's capture list for exactly that
    reason, and the preview reports the refusal before the reviewer meets it.
    """
    from fastapi.testclient import TestClient

    from palinode.api.server import _rate_counters, app
    from palinode.core.capture_policy import update_capture_policy

    rel = _decision(store, "storage", "Use the local file store.")
    _rate_counters.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        live = client.post(
            "/corrections/preview", json={"target": rel, "replacement": "Use the hosted store."}
        ).json()
        assert live["capture_policy"]["apply_allowed"] is True

        update_capture_policy(capture_paused=True)

        paused_preview = client.post(
            "/corrections/preview", json={"target": rel, "replacement": "Use the hosted store."}
        )
        assert paused_preview.status_code == 200, "a preview writes nothing and stays readable"
        policy = paused_preview.json()["capture_policy"]
        assert policy["apply_allowed"] is False
        assert policy["reason"] == "capture_paused"

        refused = client.post(
            "/corrections/apply",
            json={
                "target": rel,
                "replacement": "Use the hosted store.",
                "expect_revision": live["confirm"]["expect_revision"],
                "confirm": True,
            },
        )
        assert refused.status_code == 403
        assert refused.json()["detail"] == "capture_paused"

        undo_refused = client.post(
            "/corrections/undo", json={"target": rel, "confirm": True, "expect_revision": "x"}
        )
        assert undo_refused.status_code == 403

        update_capture_policy(capture_paused=False)
    _rate_counters.clear()
    assert "status: archived" not in (store / rel).read_text(encoding="utf-8")


# ── surface shape ───────────────────────────────────────────────────────────
def test_the_api_returns_the_candidates_a_caller_needs_to_choose(store: Path) -> None:
    from fastapi.testclient import TestClient

    from palinode.api.server import _rate_counters, app

    _decision(store, "storage", "Decisions copy.")
    _write(
        store,
        "insights/storage.md",
        "id: insights-storage\ntype: Insight\ncategory: insights\ntitle: storage",
        "Insights copy.",
    )
    _rate_counters.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/corrections/preview", json={"target": "storage", "replacement": "x"}
        )
    _rate_counters.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "ambiguous_target"
    assert {row["file"] for row in detail["candidates"]} == {
        "decisions/storage.md", "insights/storage.md"
    }


def test_the_cli_previews_then_applies_and_never_applies_by_default(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTY-aware: piped output is JSON, and apply needs both explicit flags."""
    import importlib

    from click.testing import CliRunner

    from palinode.cli import main as cli_root

    corrections_cli = importlib.import_module("palinode.cli.corrections")

    class _Unreachable:
        @staticmethod
        def correction_review(*_args, **_kwargs):
            from palinode.cli._api import RequestError

            raise RequestError("no server")

    monkeypatch.setattr(corrections_cli, "api_client", _Unreachable())
    rel = _decision(store, "storage", "Use the local file store.")

    runner = CliRunner()
    previewed = runner.invoke(
        cli_root,
        ["corrections", "preview", "--target", rel, "--replacement", "Use the hosted store."],
        env={"PALINODE_MEMORY_DIR": str(store)},
    )
    assert previewed.exit_code == 0, previewed.output
    payload = json.loads(previewed.output)
    assert payload["applied"] is False
    revision = payload["confirm"]["expect_revision"]

    # Apply without --confirm is refused (exit 1), and writes nothing.
    refused = runner.invoke(
        cli_root,
        [
            "corrections", "apply", "--target", rel,
            "--replacement", "Use the hosted store.",
            "--expect-revision", revision,
        ],
    )
    assert refused.exit_code == 1
    assert "status: archived" not in (store / rel).read_text(encoding="utf-8")

    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR), patch(
        "palinode.core.store.scan_memory_content", return_value=(True, "OK")
    ):
        applied = runner.invoke(
            cli_root,
            [
                "corrections", "apply", "--target", rel,
                "--replacement", "Use the hosted store.",
                "--expect-revision", revision, "--confirm",
            ],
        )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["applied"] is True
    assert "status: archived" in (store / rel).read_text(encoding="utf-8")


def test_bare_corrections_still_lists_the_queue(store: Path, monkeypatch) -> None:
    """The group keeps its old no-subcommand behaviour."""
    import importlib

    from click.testing import CliRunner

    from palinode.cli import main as cli_root

    corrections_cli = importlib.import_module("palinode.cli.corrections")

    class _Unreachable:
        @staticmethod
        def corrections(*_args, **_kwargs):
            from palinode.cli._api import RequestError

            raise RequestError("no server")

    monkeypatch.setattr(corrections_cli, "api_client", _Unreachable())
    _seed_candidate(store, span="no, that's wrong", replaced=None, replacement=None)

    result = CliRunner().invoke(cli_root, ["corrections"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["count"] == 1


# ── a half-finished write ───────────────────────────────────────────────────
#
# A document-level supersession is two writes and nothing wraps them. These
# inject a failure at the second one — a real store on disk, a real first
# write, only the archive replaced — and assert that what comes back describes
# the state the store is actually in.
class _ArchiveFailed(RuntimeError):
    """Stands in for whatever makes an archive fail: a lock, a disk, a guard."""


def _archive_raises():
    """Fail the second write, and only for as long as the block lasts."""
    return patch(
        "palinode.consolidation.archive.archive_memory",
        side_effect=_ArchiveFailed("the archive step could not write"),
    )


def _partial_apply(rel: str) -> dict:
    preview = review.preview_correction(target=rel, replacement="Use the hosted store.")
    with _archive_raises():
        return _embedded(
            review.apply_correction,
            target=rel,
            replacement="Use the hosted store.",
            expect_revision=preview["confirm"]["expect_revision"],
            confirm=True,
        )


def test_a_failed_archive_is_a_partial_result_not_an_exception(store: Path) -> None:
    """Both halves are named, because neither is visible from the other."""
    rel = _decision(store, "storage", "Use the local file store.")

    result = _partial_apply(rel)

    assert result["applied"] == "partial"
    assert result["phase"] == "apply"
    partial = result["partial"]
    assert partial["failed_step"] == "archive_memory"
    assert "_ArchiveFailed" in partial["error"]

    # What was written: the replacement is on disk, active, and claims lineage.
    successor = store / result["replacement"]["rel_path"]
    replacement_text = successor.read_text(encoding="utf-8")
    assert "Use the hosted store." in replacement_text
    assert "supersedes: decisions/storage" in replacement_text
    assert partial["successor"] == result["replacement"]["rel_path"]
    assert partial["successor"] in partial["written"]

    # What was not: the original is untouched and still current.
    original_text = (store / rel).read_text(encoding="utf-8")
    assert "status: archived" not in original_text
    assert "superseded_by" not in original_text
    assert partial["original"] == rel
    assert "still current" in partial["not_written"]
    assert "on the replacement only" in result["relation"]["recorded_as"], (
        "the relation must not read as a record of both halves"
    )

    # And the exact command that finishes it.
    assert partial["complete_command"].startswith(
        f"palinode archive {rel} --superseded-by {partial['successor']}"
    )


def test_the_partials_complete_command_is_the_one_that_finishes_it(store: Path) -> None:
    """The named recovery is run, and the store lands where the apply meant to."""
    rel = _decision(store, "storage", "Use the local file store.")
    partial = _partial_apply(rel)["partial"]

    from palinode.consolidation.archive import archive_memory

    archived = archive_memory(
        partial["original"],
        reason="completing a partially applied correction",
        superseded_by=partial["successor"],
    )

    assert archived["status"] == "archived"
    finished = (store / rel).read_text(encoding="utf-8")
    assert "status: archived" in finished
    assert partial["successor"].removesuffix(".md") in finished


def test_the_correction_undo_cannot_unwind_a_partial_and_says_so(store: Path) -> None:
    """The claim in the result is checked against what undo actually does.

    Undo restores an *archived* original. In a partial neither record is
    archived, so it refuses from both ends — which is why the partial names the
    unwind as a separate, second decision instead of pointing at undo.
    """
    rel = _decision(store, "storage", "Use the local file store.")
    partial = _partial_apply(rel)["partial"]

    assert "cannot unwind this" in partial["undo"]

    from_original = review.preview_undo(target=partial["original"])
    assert "not archived" in from_original["refused"]
    assert from_original["confirm"]["command"] is None

    from_successor = review.preview_undo(target=partial["successor"])
    assert "not archived" in from_successor["refused"]

    with pytest.raises(review.CorrectionRefused):
        review.apply_undo(
            target=partial["original"],
            expect_revision=review.file_revision(str(store / rel)),
            confirm=True,
        )

    # The unwind on offer is retiring the replacement, and it is spelled out.
    assert partial["unwind_command"].startswith(
        f"palinode archive {partial['successor']}"
    )


def test_a_failed_retirement_stays_an_exception(store: Path) -> None:
    """Nothing is written before the archive, so there is no partial to report."""
    rel = _decision(store, "storage", "Use the local file store.")
    revision = review.file_revision(str(store / rel))

    with _archive_raises(), pytest.raises(_ArchiveFailed):
        _embedded(
            review.apply_correction,
            target=rel,
            action="retire",
            reason="the prototype was cancelled",
            expect_revision=revision,
            confirm=True,
        )
    assert "status: archived" not in (store / rel).read_text(encoding="utf-8")


def test_a_partial_leaves_the_candidate_row_unresolved(store: Path) -> None:
    """Marking it `applied` would be the store's only record that this landed."""
    rel = _decision(store, "storage", "Use the local file store.")
    candidate_id = _seed_candidate(
        store,
        span="no, use the hosted store",
        replaced="Use the local file store.",
        replacement="Use the hosted store.",
    )
    preview = review.preview_correction(target=rel, candidate_id=candidate_id)

    with _archive_raises():
        result = _embedded(
            review.apply_correction,
            target=rel,
            candidate_id=candidate_id,
            expect_revision=preview["confirm"]["expect_revision"],
            confirm=True,
        )

    assert result["applied"] == "partial"
    assert "candidate" not in result, "an unfinished correction resolves nothing"
    assert candidate_id in result["partial"]["candidate"]
    assert load_candidates()[0]["status"] == "proposed"


def test_a_claim_level_apply_has_no_partial_of_this_shape(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One executor call, one atomic write — a failed commit is not a partial."""
    rel = _decision(
        store,
        "log",
        "- The cache is in-process. <!-- fact:cache -->",
    )
    preview = review.preview_correction(
        target=rel, claim_id="cache", replacement="The cache is shared."
    )
    monkeypatch.setattr(
        "palinode.core.git_tools.commit_memory_files", lambda *_a, **_k: False
    )

    result = _embedded(
        review.apply_correction,
        target=rel,
        claim_id="cache",
        replacement="The cache is shared.",
        expect_revision=preview["confirm"]["expect_revision"],
        confirm=True,
    )

    assert result["applied"] is True
    assert result["committed"] is False
    assert "partial" not in result
    text = (store / rel).read_text(encoding="utf-8")
    assert "~~The cache is in-process.~~" in text
    assert "The cache is shared." in text


def test_the_api_returns_409_with_the_partial_payload(store: Path) -> None:
    """Non-2xx so no caller reads it as success; the payload is the report."""
    from fastapi.testclient import TestClient

    from palinode.api.server import _rate_counters, app

    rel = _decision(store, "storage", "Use the local file store.")
    revision = review.file_revision(str(store / rel))

    _rate_counters.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        with _archive_raises(), patch(
            "palinode.core.embedder.embed", return_value=_FAKE_VECTOR
        ), patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            response = client.post(
                "/corrections/apply",
                json={
                    "target": rel,
                    "replacement": "Use the hosted store.",
                    "expect_revision": revision,
                    "confirm": True,
                },
            )
    _rate_counters.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["applied"] == "partial"
    assert "error" not in detail, "a refusal carries `error`; a partial does not"
    assert detail["partial"]["original"] == rel
    assert detail["partial"]["complete_command"].startswith("palinode archive")
    assert "status: archived" not in (store / rel).read_text(encoding="utf-8")


def test_the_cli_exits_non_zero_and_prints_both_halves(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON when piped, the two halves and the completing command when not."""
    import importlib

    from click.testing import CliRunner

    from palinode.cli import main as cli_root

    corrections_cli = importlib.import_module("palinode.cli.corrections")

    class _Unreachable:
        @staticmethod
        def correction_review(*_args, **_kwargs):
            from palinode.cli._api import RequestError

            raise RequestError("no server")

    monkeypatch.setattr(corrections_cli, "api_client", _Unreachable())
    rel = _decision(store, "storage", "Use the local file store.")
    revision = review.file_revision(str(store / rel))

    args = [
        "corrections", "apply", "--target", rel,
        "--replacement", "Use the hosted store.",
        "--expect-revision", revision, "--confirm",
    ]
    with _archive_raises(), patch(
        "palinode.core.embedder.embed", return_value=_FAKE_VECTOR
    ), patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        piped = CliRunner().invoke(cli_root, args)

    assert piped.exit_code == 1, piped.output
    payload = json.loads(piped.output)
    assert payload["applied"] == "partial"

    with _archive_raises(), patch(
        "palinode.core.embedder.embed", return_value=_FAKE_VECTOR
    ), patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        human = CliRunner().invoke(cli_root, [*args, "--format", "text"])

    assert human.exit_code == 1, human.output
    flat = " ".join(human.output.split())
    assert "Correction partially applied" in flat
    assert "still current" in flat
    assert "palinode archive" in flat


def test_the_mcp_tool_leads_with_partial_not_refused(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent told "refused" would re-run the correction instead of finishing it."""
    import palinode.mcp as mcp

    class _Response:
        status_code = 409

        @staticmethod
        def json():
            return {
                "detail": {
                    "applied": "partial",
                    "partial": {
                        "complete_command": "palinode archive decisions/x.md "
                                            "--superseded-by decisions/x-corrected.md",
                    },
                }
            }

    async def fake_post(path, json=None, timeout=30.0):
        return _Response()

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = asyncio.run(
        mcp._dispatch_tool(
            "palinode_correction_apply",
            {"target": "decisions/x.md", "expect_revision": "abc", "confirm": True},
        )
    )

    text = result[0].text
    assert "PARTIALLY applied" in text
    assert "refused" not in text
    assert "palinode archive" in text


def test_the_plugin_hands_back_the_partial_payload() -> None:
    """The plugin's fetch used to drop a non-2xx body; the payload is the answer."""
    source = (
        Path(__file__).resolve().parent.parent / "plugin" / "index.ts"
    ).read_text(encoding="utf-8")
    block = source.split("const correctionCall")[1].split("const correctionBody")[0]

    assert "palinodeFetchWithStatus" in block
    assert 'detail.applied === "partial"' in block
    assert "PARTIALLY applied" in block

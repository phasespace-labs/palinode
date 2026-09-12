"""Restore surface and forget-request lifecycle: the inverse of archival and
of mention-level retraction, on every surface, plus re-trigger safety.

Three ops and one guard:

- ``restore_memory`` — the inverse of every archive path. Frontmatter is
  reconstructed from the archived file itself (``created_at`` and every other
  field survive), ``restored_at`` / ``restored_from`` make the resurrection
  visible, and the memory returns to default recall.
- ``unretract_mentions`` — the inverse of a retraction: un-strikes exactly the
  pref's own markers and clears its ``retracted_prefs`` record.
- ``withdraw_forget_request`` — takes a request back by composing the two over
  the request's targets and archiving the request record(s).
- The resolution boundary: a re-saved request (same slug, or the same text
  re-captured into a new file) resolves only against memories that existed
  when the request was first made, so it can never archive a memory created
  after the original resolution.

Real SQLite + real git + real FTS5 in ``tmp_path``, no DB mocking (repo rule).
Only the embedder and the security scanner are patched; the embedder fake is
bag-of-words-hash based so hybrid ranking stays meaningful.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import subprocess
from typing import Any
from unittest.mock import patch

import frontmatter
import pytest
import yaml

from palinode.consolidation import archive as archive_mod
from palinode.consolidation import forget as forget_mod
from palinode.consolidation import retract as retract_mod
from palinode.core.config import config

EMBED_DIM = 1024


def _hash_embed(text: str, backend: str = "local") -> list[float]:
    vec = [0.0] * EMBED_DIM
    for tok in set(text.lower().split()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient over a git-backed tmp memory_dir with hash-embed vectors."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email",
                    "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name",
                    "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    from fastapi.testclient import TestClient
    with (
        patch("palinode.core.store.scan_memory_content",
              return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", side_effect=_hash_embed),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            yield c, str(tmp_path)
    srv._rate_counters.clear()


def _save(client, content: str, slug: str) -> dict:
    r = client.post("/save", json={
        "content": content, "type": "Insight", "slug": slug,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _search_paths(client, query: str) -> list[str]:
    r = client.post("/search", json={"query": query, "limit": 10,
                                     "threshold": 0.0, "hybrid": True})
    assert r.status_code == 200, r.text
    return [os.path.basename(h["file_path"]) for h in r.json()]


def _write_and_index(memory_dir: str, relpath: str, body: str, fm: dict) -> str:
    from palinode.indexer.index_file import index_file

    path = os.path.join(memory_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = yaml.safe_dump(fm, default_flow_style=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{text}---\n\n{body}\n")
    index_file(path)
    return path


def _post(memory_dir: str, rel: str) -> frontmatter.Post:
    return frontmatter.load(os.path.join(memory_dir, rel))


def _git_log(memory_dir: str) -> str:
    return subprocess.run(
        ["git", "-C", memory_dir, "log", "--format=%s"],
        check=True, capture_output=True, text=True,
    ).stdout


_DENSE_SNAPSHOT = (
    "Moonrise Fair 2031 judging pipeline closeout. Handed the judging "
    "workflow to Wilhelmina Cragg after the June stage flood. Versions "
    "frozen at release candidate seven; ingest queue drained and verified "
    "against the submission manifest. Outstanding recovery items: rebuild "
    "thumbnail cache, reconcile duplicate performer registrations, restore "
    "the staging gallery snapshots, audit volunteer access grants. Flood "
    "log migrated to the tracker with twelve linked entries. Next steps: "
    "schedule retrospective, publish judging rubric, confirm pavilion "
    "contract renewal, decommission obsolete submission portals, rotate "
    "the shared credentials, document the escalation path Wilhelmina "
    "proposed during cleanup."
)


# ── restore: core ─────────────────────────────────────────────────────────────


def test_restore_round_trip_preserves_frontmatter_and_created_at(api_client):
    """Archive → restore reconstructs the frontmatter from the archived file:
    every field the memory had before archival comes back byte-identical,
    ``created_at`` above all, and the only additions are the provenance."""
    client, memory_dir = api_client
    original = {
        "id": "insights-openfair-closeout",
        "category": "insights",
        "type": "Insight",
        "created_at": "2026-04-17T09:12:33.000123+00:00",
        "last_updated": "2026-04-17T09:12:33.000123+00:00",
        "entities": ["project/openfair"],
        "epistemic": "inference",
        "priority": 4,
    }
    rel = "insights/openfair-closeout.md"
    _write_and_index(memory_dir, rel, "the openfair closeout record", original)

    archived = archive_mod.archive_memory(
        rel, reason="dogfood mistake", superseded_by="insights/forget-openfair.md"
    )
    assert archived["status"] == "archived"

    out = archive_mod.restore_memory(rel, reason="wrongly retired")

    assert out["status"] == "active"
    assert out["file"] == rel
    assert out["restored_from"] == "insights/forget-openfair.md"
    assert out["history_file"] == "insights/openfair-closeout-history.md"
    assert out["committed"] is True
    assert out["chunks_updated"] >= 1

    post = _post(memory_dir, rel)
    assert post["status"] == "active"
    assert "superseded_by" not in post.metadata
    assert post["restored_from"] == "insights/forget-openfair.md"
    assert post["restored_at"] == out["restored_at"]
    # Everything that was there before archival is there after restore,
    # unchanged — reconstructed from the record, not re-typed by hand.
    for key, value in original.items():
        assert post.metadata[key] == value, key
    assert post.content.strip() == "the openfair closeout record"

    history = open(os.path.join(memory_dir, out["history_file"]), encoding="utf-8").read()
    assert "Restored: insights/openfair-closeout.md" in history
    assert "was superseded by insights/forget-openfair.md" in history
    assert "(reason: wrongly retired)" in history
    assert "<!-- fact:insights-openfair-closeout -->" in history

    log = _git_log(memory_dir)
    assert "restore: insights/openfair-closeout.md <- insights/forget-openfair.md" in log
    assert log.index("restore:") < log.index("supersede:")  # newest first


def test_restore_plain_archive_records_restored_from_archived(api_client):
    client, memory_dir = api_client
    rel = "insights/plain.md"
    _write_and_index(memory_dir, rel, "a plain finding", {"id": "insights-plain"})
    archive_mod.archive_memory(rel)

    out = archive_mod.restore_memory(rel)

    assert out["restored_from"] == "archived"
    assert _post(memory_dir, rel)["restored_from"] == "archived"
    assert "Restored: insights/plain.md (was archived)" in open(
        os.path.join(memory_dir, "insights/plain-history.md"), encoding="utf-8"
    ).read()
    assert "restore: insights/plain.md" in _git_log(memory_dir)


def test_restore_returns_memory_to_default_recall(api_client):
    """The residue check for the archive tier: after archival the memory is
    absent from default search on the API surface and reachable only under an
    explicit archived-inclusive store query; after restore it is back."""
    from palinode.core import store

    client, memory_dir = api_client
    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    assert "pref-sneakers.md" in _search_paths(client, "vintage sneakers drops")

    archive_mod.archive_memory("insights/pref-sneakers.md")
    assert "pref-sneakers.md" not in _search_paths(client, "vintage sneakers drops")
    explicit = store.search(
        _hash_embed("vintage sneakers drops"),
        status_exclude_list=[], threshold=0.0, record_access=False,
    )
    assert any(h["file_path"].endswith("pref-sneakers.md") for h in explicit)

    archive_mod.restore_memory("insights/pref-sneakers.md")
    assert "pref-sneakers.md" in _search_paths(client, "vintage sneakers drops")


def test_restore_is_a_reported_no_op_on_a_live_memory(api_client):
    client, memory_dir = api_client
    rel = "insights/live.md"
    _save(client, "still live", "live")
    before = open(os.path.join(memory_dir, rel), encoding="utf-8").read()
    head = _git_log(memory_dir)

    out = archive_mod.restore_memory(rel)

    assert out["status"] == "not_archived"
    assert out["committed"] is False
    assert open(os.path.join(memory_dir, rel), encoding="utf-8").read() == before
    assert _git_log(memory_dir) == head
    assert not os.path.exists(os.path.join(memory_dir, "insights/live-history.md"))


def test_restore_does_not_unstrike_a_retraction(api_client):
    """Restore must not resurrect: a retraction inside the file is separate
    lifecycle state and survives the restore untouched."""
    client, memory_dir = api_client
    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    rel = "insights/moonrise-closeout.md"
    struck = retract_mod.retract_mentions(rel, "I know Wilhelmina Cragg")
    assert struck["status"] == "retracted"
    body_after_strike = _post(memory_dir, rel).content

    archive_mod.archive_memory(rel, reason="whole file retired later")
    out = archive_mod.restore_memory(rel)

    assert out["status"] == "active"
    post = _post(memory_dir, rel)
    assert post.content == body_after_strike
    assert post["retracted_prefs"] == ["i know wilhelmina cragg"]
    assert post.content.count("[RETRACTED") == 2


def test_restore_surfaces_a_still_past_expiry(api_client):
    client, memory_dir = api_client
    rel = "insights/ephemeral.md"
    _write_and_index(memory_dir, rel, "short-lived", {
        "id": "insights-ephemeral", "expires_at": "2020-01-01T00:00:00+00:00",
    })
    archive_mod.archive_memory(rel)

    out = archive_mod.restore_memory(rel)

    assert out["status"] == "active"
    assert out["expires_at"] == "2020-01-01T00:00:00+00:00"
    assert _post(memory_dir, rel)["expires_at"] == "2020-01-01T00:00:00+00:00"


def test_restore_rejects_traversal_and_missing(api_client):
    with pytest.raises(ValueError):
        archive_mod.restore_memory("../outside.md")
    with pytest.raises(FileNotFoundError):
        archive_mod.restore_memory("insights/absent.md")


# ── unretract: core ───────────────────────────────────────────────────────────


def test_unretract_removes_markers_and_the_record(api_client):
    client, memory_dir = api_client
    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    rel = "insights/moonrise-closeout.md"
    original_body = _post(memory_dir, rel).content
    struck = retract_mod.retract_mentions(
        rel, "I know Wilhelmina Cragg", superseded_by="insights/forget-w.md"
    )
    assert struck["mentions"] == 2

    out = retract_mod.unretract_mentions(rel, "I know Wilhelmina Cragg", reason="misfire")

    assert out["status"] == "unretracted"
    assert out["mentions"] == 2
    assert out["retraction_id"] == struck["retraction_id"]
    assert out["committed"] is True
    assert out["indexed_vec"] is True and out["indexed_fts"] is True
    post = _post(memory_dir, rel)
    assert post.content == original_body
    assert "retracted_prefs" not in post.metadata
    assert post.get("status") != "archived"

    history = open(os.path.join(memory_dir, "insights/moonrise-closeout-history.md"),
                   encoding="utf-8").read()
    assert f'Unretracted 2 mention(s) [r:{out["retraction_id"]}]: "I know Wilhelmina Cragg"' in history
    assert "(reason: misfire)" in history
    assert "unretract: insights/moonrise-closeout.md (2 mentions)" in _git_log(memory_dir)


def test_unretract_touches_only_its_own_pref(api_client):
    """Two prefs struck in one file: withdrawing one leaves the other's
    markers and record exactly as they were."""
    client, memory_dir = api_client
    _save(client, _DENSE_SNAPSHOT + " The pavilion contract was renewed by "
                  "Ottoline Marsh last spring.", "moonrise-closeout")
    rel = "insights/moonrise-closeout.md"
    first = retract_mod.retract_mentions(rel, "I know Wilhelmina Cragg")
    second = retract_mod.retract_mentions(rel, "I hired Ottoline Marsh")
    assert first["mentions"] == 2 and second["mentions"] == 1

    out = retract_mod.unretract_mentions(rel, "I know Wilhelmina Cragg")

    assert out["mentions"] == 2
    post = _post(memory_dir, rel)
    assert post["retracted_prefs"] == ["i hired ottoline marsh"]
    assert post.content.count("[RETRACTED") == 1
    assert f"r:{second['retraction_id']}" in post.content
    assert f"r:{first['retraction_id']}" not in post.content
    assert "~~Handed the judging" not in post.content


def test_unretract_is_a_reported_no_op_without_a_record(api_client):
    client, memory_dir = api_client
    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    rel = "insights/moonrise-closeout.md"
    head = _git_log(memory_dir)

    out = retract_mod.unretract_mentions(rel, "I know Wilhelmina Cragg")

    assert out == {"file": rel, "status": "not_retracted", "mentions": 0}
    assert _git_log(memory_dir) == head


def test_unretract_clears_a_record_whose_markers_were_hand_removed(api_client):
    client, memory_dir = api_client
    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    rel = "insights/moonrise-closeout.md"
    retract_mod.retract_mentions(rel, "I know Wilhelmina Cragg")
    post = _post(memory_dir, rel)
    post.content = _DENSE_SNAPSHOT  # a hand edit put the prose back
    with open(os.path.join(memory_dir, rel), "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post) + "\n")

    out = retract_mod.unretract_mentions(rel, "I know Wilhelmina Cragg")

    assert out["status"] == "unretracted"
    assert out["mentions"] == 0
    assert "retracted_prefs" not in _post(memory_dir, rel).metadata


def test_unretracted_file_is_strikeable_again_by_a_new_request(api_client, monkeypatch):
    """Clearing the record is what makes the file eligible again: a later
    request for the same pref re-strikes it instead of skipping it."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)
    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    rel = "insights/moonrise-closeout.md"
    _save(client, "Please forget that I know Wilhelmina Cragg.", "forget-w")
    assert _post(memory_dir, rel)["retracted_prefs"] == ["i know wilhelmina cragg"]

    retract_mod.unretract_mentions(rel, "I know Wilhelmina Cragg")
    out = _save(client, "Please forget that I know Wilhelmina Cragg.", "forget-w-2")

    assert [r["path"] for r in out["forget"]["retracted"]] == [rel]
    assert _post(memory_dir, rel).content.count("[RETRACTED") == 2


# ── re-trigger safety ─────────────────────────────────────────────────────────


def test_resaved_request_never_archives_a_memory_created_after_first_resolution(
        api_client, monkeypatch):
    """The archival-path re-trigger hazard, both shapes: the request re-saved
    on its own slug (a floor-hook capture landing twice) and the same request
    text re-captured into a new file. Neither may retire a pref memory the
    user created after the original request was resolved."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    first = _save(client, "Please forget that I collect vintage sneakers.",
                  "forget-sneakers")
    assert first["forget"]["archived"] == ["insights/pref-sneakers.md"]
    assert "resolved_before" in first["forget"]
    assert "prior_requests" not in first["forget"]

    # Created AFTER the request was resolved: not the request's to retire.
    _save(client, "Bought two new pairs of vintage sneakers for the collection "
                  "at the weekend sneaker market.", "pref-sneakers-new")
    assert "pref-sneakers-new.md" in _search_paths(client, "vintage sneakers collection")

    # Shape 1: same slug, same content — created_at is preserved, so the
    # boundary is the original request's.
    again = _save(client, "Please forget that I collect vintage sneakers.",
                  "forget-sneakers")
    assert again["save_outcome"] == "replaced"
    assert again["forget"]["archived"] == []
    assert again["forget"]["resolved_before"] == first["forget"]["resolved_before"]
    assert _post(memory_dir, "insights/pref-sneakers-new.md").get("status") != "archived"

    # Shape 2: a new file carrying the same request — the earlier live
    # record bounds it.
    recapture = _save(client, "Session summary. User said: please forget that "
                              "I collect vintage sneakers. Then discussed "
                              "breakfast options.", "session-end-recapture")
    assert recapture["forget"]["archived"] == []
    assert recapture["forget"]["prior_requests"] == ["insights/forget-sneakers.md"]
    assert recapture["forget"]["resolved_before"] == first["forget"]["resolved_before"]
    assert _post(memory_dir, "insights/pref-sneakers-new.md").get("status") != "archived"
    assert "pref-sneakers-new.md" in _search_paths(client, "vintage sneakers collection")

    # The original request record is a tombstone, never a target.
    assert _post(memory_dir, "insights/forget-sneakers.md").get("status") != "archived"


def test_restored_memory_is_out_of_reach_of_the_request_that_retired_it(
        api_client, monkeypatch):
    """Restore must not re-trigger: ``restored_at`` is the memory's effective
    creation, so a re-capture of the original request leaves it alone — while
    a genuinely new request made after the restore can still reach it."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    _save(client, "Please forget that I collect vintage sneakers.", "forget-sneakers")
    assert _post(memory_dir, "insights/pref-sneakers.md")["status"] == "archived"

    restored = archive_mod.restore_memory("insights/pref-sneakers.md")
    assert restored["status"] == "active"

    again = _save(client, "Please forget that I collect vintage sneakers.",
                  "forget-sneakers")
    assert again["forget"]["archived"] == []
    assert _post(memory_dir, "insights/pref-sneakers.md")["status"] == "active"

    # A new request after the withdrawal of the old one reaches it again.
    forget_mod.withdraw_forget_request("insights/forget-sneakers.md")
    fresh = _save(client, "Please forget that I collect vintage sneakers.",
                  "forget-sneakers-fresh")
    assert fresh["forget"]["archived"] == ["insights/pref-sneakers.md"]
    assert "prior_requests" not in fresh["forget"]


def test_a_new_request_still_reaches_older_memories_beyond_the_first_pass(
        api_client, monkeypatch):
    """The boundary is about creation time, not about repeat requests per se:
    a memory that predates the request but was beyond ``max_targets`` on the
    first pass is reached by a later request for the same pref."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)
    monkeypatch.setattr(config.consolidation.forget, "max_targets", 1)

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers-a")
    _save(client, "My vintage sneakers shelf holds forty pairs, sorted by "
                  "release year.", "pref-sneakers-b")
    first = _save(client, "Please forget that I collect vintage sneakers.",
                  "forget-sneakers")
    assert len(first["forget"]["archived"]) == 1
    second = _save(client, "Please forget that I collect vintage sneakers.",
                   "forget-sneakers-again")
    assert len(second["forget"]["archived"]) == 1
    assert set(first["forget"]["archived"] + second["forget"]["archived"]) == {
        "insights/pref-sneakers-a.md", "insights/pref-sneakers-b.md",
    }


# ── withdraw ──────────────────────────────────────────────────────────────────


def test_withdraw_restores_archived_unstrikes_retracted_and_retires_the_record(
        api_client, monkeypatch):
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    dense_body = _post(memory_dir, "insights/moonrise-closeout.md").content
    _save(client, "Met Wilhelmina Cragg at the fair; she judges the Moonrise "
                  "panel.", "pref-wilhelmina")
    req = _save(client, "Please forget that I know Wilhelmina Cragg.", "forget-w")
    assert req["forget"]["archived"] == ["insights/pref-wilhelmina.md"]
    assert [r["path"] for r in req["forget"]["retracted"]] == ["insights/moonrise-closeout.md"]

    out = forget_mod.withdraw_forget_request("insights/forget-w.md", reason="changed my mind")

    assert out["status"] == "withdrawn"
    assert out["pref"] == "I know Wilhelmina Cragg"
    assert out["restored"] == ["insights/pref-wilhelmina.md"]
    assert out["unretracted"] == [{"path": "insights/moonrise-closeout.md", "mentions": 2}]
    assert out["requests_archived"] == ["insights/forget-w.md"]
    assert "failed" not in out

    pref = _post(memory_dir, "insights/pref-wilhelmina.md")
    assert pref["status"] == "active"
    assert pref["restored_from"] == "insights/forget-w.md"
    dense = _post(memory_dir, "insights/moonrise-closeout.md")
    assert dense.content == dense_body
    assert "retracted_prefs" not in dense.metadata
    record = _post(memory_dir, "insights/forget-w.md")
    assert record["status"] == "archived"

    # Recall reflects the withdrawal: the pref is back, the record is gone.
    hits = _search_paths(client, "Wilhelmina Cragg Moonrise judging")
    assert "pref-wilhelmina.md" in hits
    assert "forget-w.md" not in hits

    history = open(os.path.join(memory_dir, "insights/forget-w-history.md"),
                   encoding="utf-8").read()
    assert 'forget request withdrawn: "I know Wilhelmina Cragg"' not in history
    assert "(reason: changed my mind)" in history


def test_withdraw_covers_every_live_record_for_the_pref(api_client, monkeypatch):
    """A re-captured request is a second live record for the same pref; a
    withdrawal from either retires both, so neither keeps bounding or
    standing as the retraction."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    _save(client, "Please forget that I collect vintage sneakers.", "forget-sneakers")
    _save(client, "Please forget that I collect vintage sneakers.", "forget-sneakers-recapture")

    out = forget_mod.withdraw_forget_request("insights/forget-sneakers-recapture.md")

    assert out["restored"] == ["insights/pref-sneakers.md"]
    assert set(out["requests_archived"]) == {
        "insights/forget-sneakers-recapture.md", "insights/forget-sneakers.md",
    }
    for rel in out["requests_archived"]:
        assert _post(memory_dir, rel)["status"] == "archived"


def test_withdraw_rejects_a_memory_with_no_request(api_client):
    client, memory_dir = api_client
    _save(client, "Prefers green tea over coffee in the mornings.", "pref-tea")
    with pytest.raises(forget_mod.NotAForgetRequest):
        forget_mod.withdraw_forget_request("insights/pref-tea.md")
    with pytest.raises(FileNotFoundError):
        forget_mod.withdraw_forget_request("insights/absent.md")


# ── REST ──────────────────────────────────────────────────────────────────────


def test_restore_endpoint(api_client):
    client, memory_dir = api_client
    _save(client, "obsolete endpoint finding", "endpoint-target")
    archive_mod.archive_memory("insights/endpoint-target.md")

    res = client.post("/restore", json={"file_path": "insights/endpoint-target.md",
                                        "reason": "not obsolete"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "active"
    assert res.json()["restored_from"] == "archived"
    assert _post(memory_dir, "insights/endpoint-target.md")["status"] == "active"

    again = client.post("/restore", json={"file_path": "insights/endpoint-target.md"})
    assert again.status_code == 200 and again.json()["status"] == "not_archived"
    assert client.post("/restore", json={"file_path": "insights/absent.md"}).status_code == 404
    res = client.post("/restore", json={"file_path": "../../etc/passwd"})
    assert res.status_code == 403 and res.json()["detail"] == "Invalid path"
    assert client.post("/restore", json={}).status_code == 422


def test_unretract_endpoint(api_client):
    client, memory_dir = api_client
    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    retract_mod.retract_mentions("insights/moonrise-closeout.md", "I know Wilhelmina Cragg")

    res = client.post("/unretract", json={
        "file_path": "insights/moonrise-closeout.md", "pref": "I know Wilhelmina Cragg",
    })
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "unretracted"
    assert res.json()["mentions"] == 2
    assert "retracted_prefs" not in _post(memory_dir, "insights/moonrise-closeout.md").metadata

    assert client.post("/unretract", json={"file_path": "insights/moonrise-closeout.md"}).status_code == 422
    assert client.post("/unretract", json={"file_path": "insights/absent.md", "pref": "x"}).status_code == 404
    assert client.post("/unretract", json={"file_path": "../x.md", "pref": "x"}).status_code == 403


def test_forget_withdraw_endpoint(api_client, monkeypatch):
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)
    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    _save(client, "Please forget that I collect vintage sneakers.", "forget-sneakers")
    _save(client, "Prefers green tea over coffee in the mornings.", "pref-tea")

    res = client.post("/forget-withdraw", json={"file_path": "insights/forget-sneakers.md"})
    assert res.status_code == 200, res.text
    assert res.json()["restored"] == ["insights/pref-sneakers.md"]
    assert res.json()["requests_archived"] == ["insights/forget-sneakers.md"]

    not_request = client.post("/forget-withdraw", json={"file_path": "insights/pref-tea.md"})
    assert not_request.status_code == 409
    assert client.post("/forget-withdraw", json={"file_path": "insights/absent.md"}).status_code == 404
    assert client.post("/forget-withdraw", json={"file_path": "../x.md"}).status_code == 403


# ── CLI ───────────────────────────────────────────────────────────────────────


class _FakeAPI:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def restore(self, file_path, reason=None):
        self.calls.append(("restore", {"file_path": file_path, "reason": reason}))
        return self._payload

    def unretract(self, file_path, pref, reason=None):
        self.calls.append(("unretract", {"file_path": file_path, "pref": pref, "reason": reason}))
        return self._payload

    def forget_withdraw(self, file_path, reason=None):
        self.calls.append(("forget_withdraw", {"file_path": file_path, "reason": reason}))
        return self._payload


def _cli_module():
    # importlib.import_module: the package's `restore` attribute is shadowed by
    # the Command re-exported in cli/__init__ (same idiom as cli/archive).
    return importlib.import_module("palinode.cli.restore")


def test_cli_restore_text_and_json():
    from click.testing import CliRunner

    mod = _cli_module()
    payload = {"file": "insights/x.md", "status": "active", "restored_from": "insights/y.md",
               "restored_at": "2026-09-05T00:00:00+00:00", "history_file": "insights/x-history.md",
               "chunks_updated": 3, "committed": True, "expires_at": "2020-01-01T00:00:00+00:00"}
    fake = _FakeAPI(payload)
    with patch.object(mod, "api_client", fake):
        res_json = CliRunner().invoke(mod.restore, ["insights/x.md", "--reason", "oops", "--format", "json"])
        assert res_json.exit_code == 0, res_json.output
        assert json.loads(res_json.output)["restored_from"] == "insights/y.md"
        res_text = CliRunner().invoke(mod.restore, ["insights/x.md", "--format", "text"])
        assert res_text.exit_code == 0, res_text.output
        assert "Restored: insights/x.md" in res_text.output
        assert "expires_at is still" in res_text.output
    assert fake.calls[0] == ("restore", {"file_path": "insights/x.md", "reason": "oops"})

    with patch.object(mod, "api_client", _FakeAPI({"file": "insights/x.md", "status": "not_archived"})):
        res = CliRunner().invoke(mod.restore, ["insights/x.md", "--format", "text"])
    assert res.exit_code == 0 and "not archived" in res.output


def test_cli_unretract_text_and_json():
    from click.testing import CliRunner

    mod = _cli_module()
    payload = {"file": "insights/x.md", "status": "unretracted", "mentions": 2,
               "retraction_id": "abcdef01", "history_file": "insights/x-history.md"}
    fake = _FakeAPI(payload)
    with patch.object(mod, "api_client", fake):
        res_json = CliRunner().invoke(mod.unretract, ["insights/x.md", "I know Someone", "--format", "json"])
        assert res_json.exit_code == 0, res_json.output
        assert json.loads(res_json.output)["mentions"] == 2
        res_text = CliRunner().invoke(mod.unretract, ["insights/x.md", "I know Someone", "--format", "text"])
        assert "Unretracted 2 mention(s) in insights/x.md" in res_text.output
    assert fake.calls[0] == ("unretract", {"file_path": "insights/x.md", "pref": "I know Someone", "reason": None})

    with patch.object(mod, "api_client", _FakeAPI({"file": "insights/x.md", "status": "not_retracted", "mentions": 0})):
        res = CliRunner().invoke(mod.unretract, ["insights/x.md", "p", "--format", "text"])
    assert res.exit_code == 0 and "no retraction" in res.output


def test_cli_forget_withdraw_text_and_json():
    from click.testing import CliRunner

    mod = _cli_module()
    payload = {"file": "insights/forget-x.md", "status": "withdrawn", "pref": "I collect x",
               "restored": ["insights/x.md"], "unretracted": [{"path": "insights/y.md", "mentions": 1}],
               "requests_archived": ["insights/forget-x.md"], "failed": [{"path": "insights/z.md", "op": "restore"}]}
    fake = _FakeAPI(payload)
    with patch.object(mod, "api_client", fake):
        res_json = CliRunner().invoke(mod.forget_withdraw, ["insights/forget-x.md", "--format", "json"])
        assert res_json.exit_code == 0, res_json.output
        assert json.loads(res_json.output)["restored"] == ["insights/x.md"]
        res_text = CliRunner().invoke(mod.forget_withdraw, ["insights/forget-x.md", "--format", "text"])
        assert "Withdrawn: insights/forget-x.md" in res_text.output
        assert "insights/y.md" in res_text.output
        assert "failed: insights/z.md (restore)" in res_text.output
    assert fake.calls[0] == ("forget_withdraw", {"file_path": "insights/forget-x.md", "reason": None})


def test_cli_commands_default_to_json_when_piped():
    from click.testing import CliRunner

    mod = _cli_module()
    with patch.object(mod, "api_client", _FakeAPI({"file": "insights/x.md", "status": "active"})):
        res = CliRunner().invoke(mod.restore, ["insights/x.md"])
    assert res.exit_code == 0 and json.loads(res.output)["file"] == "insights/x.md"


def test_cli_error_exits_non_zero():
    from click.testing import CliRunner

    mod = _cli_module()

    class _Boom:
        def restore(self, *a, **k):
            raise RuntimeError("api down")

    with patch.object(mod, "api_client", _Boom()):
        res = CliRunner().invoke(mod.restore, ["insights/x.md"])
    assert res.exit_code == 1


# ── MCP ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_reversal_tools_registered_full_not_core(monkeypatch):
    from palinode.mcp import list_tools

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    full = {t.name: t for t in await list_tools()}
    assert full["palinode_restore"].input_schema["required"] == ["file_path"]
    assert set(full["palinode_restore"].input_schema["properties"]) == {"file_path", "reason"}
    assert full["palinode_unretract"].input_schema["required"] == ["file_path", "pref"]
    assert set(full["palinode_unretract"].input_schema["properties"]) == {"file_path", "pref", "reason"}
    assert full["palinode_forget_withdraw"].input_schema["required"] == ["file_path"]

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "core")
    core = {t.name for t in await list_tools()}
    assert not {"palinode_restore", "palinode_unretract", "palinode_forget_withdraw"} & core


@pytest.mark.parametrize("tool,args,path,body,expect", [
    ("palinode_restore",
     {"file_path": "insights/x.md", "reason": "oops"},
     "/restore", {"file_path": "insights/x.md", "reason": "oops"},
     "Restored: insights/x.md (was insights/y.md)"),
    ("palinode_unretract",
     {"file_path": "insights/x.md", "pref": "I know Someone"},
     "/unretract", {"file_path": "insights/x.md", "pref": "I know Someone"},
     "Unretracted 2 mention(s) in insights/x.md"),
    ("palinode_forget_withdraw",
     {"file_path": "insights/forget-x.md"},
     "/forget-withdraw", {"file_path": "insights/forget-x.md"},
     "Withdrawn: insights/forget-x.md"),
])
@pytest.mark.asyncio
async def test_mcp_reversal_dispatch_forwards_and_renders(monkeypatch, tool, args, path, body, expect):
    import palinode.mcp as mcp

    payloads = {
        "/restore": {"file": "insights/x.md", "status": "active", "restored_from": "insights/y.md",
                     "history_file": "insights/x-history.md", "chunks_updated": 2},
        "/unretract": {"file": "insights/x.md", "status": "unretracted", "mentions": 2,
                       "history_file": "insights/x-history.md"},
        "/forget-withdraw": {"file": "insights/forget-x.md", "status": "withdrawn", "pref": "I collect x",
                             "restored": ["insights/x.md"], "unretracted": [],
                             "requests_archived": ["insights/forget-x.md"]},
    }
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self):
            return payloads[captured["path"]]

    async def _fake_post(p, json=None, timeout=30.0):
        captured["path"] = p
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(mcp, "_post", _fake_post)
    result = await mcp._dispatch_tool(tool, args)
    assert captured["path"] == path
    assert captured["json"] == body
    assert expect in result[0].text


@pytest.mark.asyncio
async def test_mcp_reversal_tools_require_file_path():
    import palinode.mcp as mcp

    for tool in ("palinode_restore", "palinode_unretract", "palinode_forget_withdraw"):
        result = await mcp._dispatch_tool(tool, {})
        assert "file_path" in result[0].text and "required" in result[0].text, tool


# ── parity ────────────────────────────────────────────────────────────────────


def test_reversal_ops_are_registered_in_the_parity_contract():
    from palinode.core import parity

    restore = parity.by_name("restore")
    assert (restore.cli_command, restore.mcp_tool, restore.api_endpoint) == (
        "restore", "palinode_restore", ("POST", "/restore"))
    unretract = parity.by_name("unretract")
    assert unretract.api_endpoint == ("POST", "/unretract")
    assert {p.name for p in unretract.canonical_params if p.required} == {"file_path", "pref"}
    withdraw = parity.by_name("forget_withdraw")
    assert (withdraw.cli_command, withdraw.mcp_tool) == ("forget-withdraw", "palinode_forget_withdraw")
    for op in (restore, unretract, withdraw):
        assert parity.required_surfaces(op) == frozenset({"cli", "mcp", "api"})

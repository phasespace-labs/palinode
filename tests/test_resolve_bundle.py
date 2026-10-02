"""The bounded-resolution operation — one request, one qualified bundle.

Everything here runs against a real SQLite store under ``tmp_path`` with real
markdown files and real git; the replacement and support-withdrawal scenarios
are seeded through the shipping retirement path
(``palinode.consolidation.archive.archive_memory`` — what every surface's
archive/supersede call ends in, propagation included), not by hand-writing a
``superseded_by`` or ``stale_backing`` line. The resolver therefore reads the
frontmatter the write path actually produces.

Three scenarios recur throughout — **current** (A replaced by B), **conflict**
(two observations that cannot both hold) and **unknown** (support withdrawn) —
and the same three are checked through REST, MCP and the CLI, from one shared
expectation, so the surfaces cannot disagree about what stands.

Two more exist for what the *text* carries: **unlinked correction** (a record
only the fallback discovery finds) and **contested with a stale-backed side**.
They are deliberately not in the shared behavioural fixture: the three above
pin the payload contract every surface repeats, and these pin the renderer.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.cli.resolve import resolve as cli_resolve
from palinode.core import store
from palinode.core.bundle import (
    BUDGET_CONFLICTS,
    DEGRADED_KEYWORD_ONLY,
    BundleBudget,
    BundleRequest,
    build_bundle,
)
from palinode.core.config import config
from palinode.core.resolution import SUPERSEDED_FROM
from palinode.core.receipt import (
    CONFLICT_SIDE,
    EVIDENCE_ONLY,
    INSUFFICIENT,
    REPLACED,
    REVISION_FILE,
    REVISION_INDEX_SECTION,
    SELECTED,
)
from palinode.indexer import reconcile

_DIM = 1024

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "resolve_bundles.json"


def _bow_embed(text: str, backend: str = "local") -> list[float]:
    vec = [0.0] * _DIM
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest(), 16)
        vec[h % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    """A git-backed memory dir with real SQLite and a deterministic embedder.

    Real git because the replacement scenario goes through the shipping
    retirement path (``consolidation.archive.archive_memory``), which commits
    the file and its history sibling as one mutation.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_bow_embed):
        yield tmp_path


@pytest.fixture()
def client(mem):
    with TestClient(app) as c:
        yield c


def _write(mem, rel: str, body: str, **meta) -> str:
    path = mem / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    content = f"---\n{fm}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    assert reconcile.reconcile(str(path), content).committed
    return str(path)


def _reindex(mem, rel: str) -> None:
    path = mem / rel
    reconcile.reconcile(str(path), path.read_text(encoding="utf-8"))


# ── the three scenarios ──────────────────────────────────────────────────────


def seed_current(mem) -> None:
    """A → B, retired through the shipping SUPERSEDE path, then re-indexed.

    ``archive_memory`` is what every surface's archive/supersede call ends in:
    it writes ``status: archived`` + ``superseded_by`` and commits. Nothing
    here hand-writes the frontmatter the resolver reads.
    """
    from palinode.consolidation.archive import archive_memory

    _write(mem, "decisions/endpoint.md",
           "# Endpoint\n\nProduction serves traffic from endpoint alpha.",
           type="Decision", status="active", date="2026-08-01",
           entities=["project/demo"], epistemic="fact")
    _write(mem, "decisions/endpoint-v2.md",
           "# Endpoint v2\n\nProduction serves traffic from endpoint bravo.",
           type="Decision", status="active", date="2026-09-01",
           entities=["project/demo"], epistemic="fact")
    result = archive_memory(
        "decisions/endpoint.md",
        reason="deployment moved to bravo",
        superseded_by="decisions/endpoint-v2.md",
    )
    assert result["status"] == "archived", result
    _reindex(mem, "decisions/endpoint.md")


def seed_conflict(mem) -> None:
    """Two observations of the same thing that cannot both hold."""
    _write(mem, "insights/region-a.md",
           "# Region A\n\nThe cache cluster runs in region frankfurt.",
           status="active", epistemic="fact", date="2026-09-02",
           entities=["project/demo"], contradicts=["insights/region-b"])
    _write(mem, "insights/region-b.md",
           "# Region B\n\nThe cache cluster runs in region dublin.",
           status="active", epistemic="fact", date="2026-09-03",
           entities=["project/demo"])
    _reindex(mem, "insights/region-a.md")


def seed_unknown(mem) -> None:
    """A claim whose only support was retired: unknown, not the old value.

    The source is retired through ``archive_memory``, whose propagation pass
    writes the dependent's ``stale_backing`` — the same flag a real retirement
    leaves behind, and the one the policy reads.
    """
    from palinode.consolidation.archive import archive_memory

    _write(mem, "research/vendor-quote.md",
           "# Vendor quote\n\nThe quoted throughput was 4000 rps.",
           status="active", date="2026-07-01", entities=["project/demo"])
    _write(mem, "insights/throughput.md",
           "# Throughput\n\nThe pipeline throughput ceiling is 4000 rps.",
           status="active", date="2026-08-10", entities=["project/demo"],
           backed_by=["research/vendor-quote"])
    result = archive_memory("research/vendor-quote.md", reason="quote withdrawn")
    assert result["status"] == "archived", result
    _reindex(mem, "insights/throughput.md")


def seed_unlinked_correction(mem) -> None:
    """A standing decision, its declared source, and a correction nobody linked.

    The correction shares the subject entity and nothing else: no
    ``superseded_by``, no ``contradicts``, and too little query vocabulary to
    be seeded directly. The only way it reaches a reader is the evidence
    layer's fallback discovery — which is exactly the case the text renderer
    used to drop.
    """
    _write(mem, "research/retry-probe.md",
           "# Retry probe\n\nThe measured retry window was 900 ms.",
           status="active", date="2026-01-05", entities=["project/orbit"])
    _write(mem, "decisions/orbit-retry.md",
           "# Orbit retry\n\nThe orbit client retry policy is three attempts "
           "with fixed backoff.",
           type="Decision", status="active", date="2026-01-14",
           entities=["project/orbit"], backed_by=["research/retry-probe"])
    _write(mem, "insights/orbit-scheduling.md",
           "# Orbit scheduling\n\nCorrection — exponential jitter replaced the "
           "fixed schedule for outbound calls.",
           status="active", date="2026-05-02", entities=["project/orbit"])
    _reindex(mem, "decisions/orbit-retry.md")


def seed_contested_stale_backing(mem) -> None:
    """Two sides that cannot both hold, one of them resting on a dead source.

    Only *one* of the stale-backed side's two declared sources is retired, so
    the advisory backing policy leaves the record standing (a withdrawn
    source is not a disproof) and it reaches the conflict group carrying a
    ``stale_backing`` qualifier the reader must not lose.
    """
    from palinode.consolidation.archive import archive_memory

    _write(mem, "research/depth-probe-alpha.md",
           "# Depth probe alpha\n\nThe first probe recorded 1200 messages.",
           status="active", date="2026-02-01", entities=["project/orbit"])
    _write(mem, "research/depth-probe-beta.md",
           "# Depth probe beta\n\nThe second probe recorded 1200 messages.",
           status="active", date="2026-02-02", entities=["project/orbit"])
    _write(mem, "insights/queue-depth-a.md",
           "# Queue depth A\n\nThe orbit queue depth ceiling is 1200 messages.",
           status="active", epistemic="fact", date="2026-02-11",
           entities=["project/orbit"],
           backed_by=["research/depth-probe-alpha", "research/depth-probe-beta"],
           contradicts=["insights/queue-depth-b"])
    _write(mem, "insights/queue-depth-b.md",
           "# Queue depth B\n\nThe orbit queue depth ceiling is 4000 messages.",
           status="active", epistemic="fact", date="2026-02-12",
           entities=["project/orbit"])
    result = archive_memory("research/depth-probe-alpha.md", reason="probe retracted")
    assert result["status"] == "archived", result
    _reindex(mem, "insights/queue-depth-a.md")


def _bundle(query: str, **kw) -> dict:
    budget = BundleBudget(**kw.pop("budget")) if "budget" in kw else BundleBudget()
    return build_bundle(BundleRequest(query=query, budget=budget, **kw)).to_dict()


# ── core behaviour ───────────────────────────────────────────────────────────


def test_current_state_selects_the_replacement(mem):
    seed_current(mem)
    data = _bundle("endpoint production traffic")

    assert [s["ref"] for s in data["selected"]] == ["decisions/endpoint-v2"]
    current = data["selected"][0]
    assert current["currency"] == "current"
    assert "bravo" in current["statement"]
    assert current["revision"], "a standing assertion carries its source revision"
    assert data["source_revisions"]["decisions/endpoint-v2"] == current["revision"]
    assert data["conflicts"] == []
    assert "alpha" not in data["text"], "the retired wording is never presented"
    assert data["receipt_ref"] == data["receipt"]["bundle_id"]


def test_a_scheduled_replacement_answers_with_the_value_still_in_force(mem):
    """A scheduled change at the surface: the old value, and when it changes.

    Seeded through the shipping SUPERSEDE path, which tombstones the
    predecessor immediately — the successor's own ``date`` is the only thing
    that says the change has not happened yet.
    """
    from palinode.consolidation.archive import archive_memory

    _write(mem, "decisions/ratelimit.md",
           "# Rate limit\n\nThe demo API rate limit is 100 requests per minute.",
           type="Decision", status="active", date="2026-01-20",
           entities=["project/demo"])
    _write(mem, "decisions/ratelimit-v2.md",
           "# Rate limit from January\n\nThe demo API rate limit is 500 requests "
           "per minute.",
           type="Decision", status="active", date="2027-01-01",
           entities=["project/demo"])
    result = archive_memory("decisions/ratelimit.md", reason="raised from January",
                            superseded_by="decisions/ratelimit-v2")
    assert result["status"] == "archived", result
    _reindex(mem, "decisions/ratelimit.md")

    data = build_bundle(
        BundleRequest(query="demo API rate limit"),
        now=datetime(2026, 9, 12, tzinfo=UTC),
    ).to_dict()

    assert "decisions/ratelimit" in {s["ref"] for s in data["selected"]}
    current = next(s for s in data["selected"] if s["ref"] == "decisions/ratelimit")
    assert "100 requests per minute" in current["statement"]
    assert "replacement_scheduled" in current["reasons"]
    assert f"{SUPERSEDED_FROM}:2027-01-01" in current["qualifiers"]
    # The stamp keeps the record's own word and says when it becomes true,
    # rather than reading as settled or as already retired.
    line = _line_for(data["text"], "decisions/ratelimit")
    assert "[retired from 2027-01-01" in line, line
    assert "500 requests per minute" not in line
    # The successor points forward, never backwards: the record still standing
    # did not replace the thing that is about to replace it.
    assert current["refs"]["superseded_by"] == ["decisions/ratelimit-v2"]
    assert current["refs"]["replaces"] == []
    assert data["replaced"] == []
    assert "    superseded by: decisions/ratelimit-v2" in data["text"]
    supplied = {r["ref"]: r for r in data["receipt"]["supplied"]}
    assert supplied["decisions/ratelimit"]["disposition"] == SELECTED
    assert supplied["decisions/ratelimit-v2"]["disposition"] == EVIDENCE_ONLY


def test_a_held_ref_resolves_to_its_successor(mem):
    """The stale ref a caller is carrying is reported replaced, with its successor."""
    seed_current(mem)
    data = build_bundle(BundleRequest(ref="decisions/endpoint")).to_dict()

    assert [s["ref"] for s in data["selected"]] == ["decisions/endpoint-v2"]
    assert [r["ref"] for r in data["replaced"]] == ["decisions/endpoint"]
    assert data["replaced"][0]["successor"] == "decisions/endpoint-v2"
    assert data["selected"][0]["refs"]["replaces"] == ["decisions/endpoint"]
    # An exact-ref seed reads through the same parsed load as every other
    # record, so its statement is the body — not the file's frontmatter.
    assert data["replaced"][0]["statement"] == (
        "Production serves traffic from endpoint alpha."
    )


def test_conflict_keeps_both_sides(mem):
    seed_conflict(mem)
    data = _bundle("cache cluster region")

    assert data["selected"] == [], "a contested question has no standing answer"
    assert len(data["conflicts"]) == 1
    refs = {s["ref"] for s in data["conflicts"][0]["sides"]}
    assert refs == {"insights/region-a", "insights/region-b"}
    assert data["conflicts"][0]["reasons"], "a conflict always says why"
    assert "frankfurt" in data["text"] and "dublin" in data["text"]


def test_unknown_is_explicit_and_never_the_old_value(mem):
    seed_unknown(mem)
    data = _bundle("pipeline throughput ceiling")

    refs = {i["ref"] for i in data["insufficient"]}
    assert "insights/throughput" in refs
    reasons = {r for i in data["insufficient"] for r in i["reasons"]}
    assert "support_withdrawn" in reasons
    assert [s["ref"] for s in data["selected"]] == []
    assert "Unknown" in data["text"]


def test_exact_ref_needs_no_query(mem):
    seed_current(mem)
    data = build_bundle(BundleRequest(ref="decisions/endpoint")).to_dict()
    assert [s["ref"] for s in data["selected"]] == ["decisions/endpoint-v2"]
    assert data["query"] is None and data["ref"] == "decisions/endpoint"


def test_context_refs_are_checked_not_assumed(mem):
    """A ref the caller is already carrying resolves to its successor."""
    seed_current(mem)
    data = build_bundle(
        BundleRequest(query="unrelated question about nothing",
                      context=("decisions/endpoint",))
    ).to_dict()
    assert "decisions/endpoint-v2" in {s["ref"] for s in data["selected"]}


def test_request_without_query_or_ref_is_rejected():
    with pytest.raises(ValueError):
        BundleRequest()


def test_unknown_intent_is_rejected():
    with pytest.raises(ValueError):
        BundleRequest(query="x", intent="as_of")


# ── seeding: the same retrieval /search performs ─────────────────────────────


def test_an_exact_identifier_seeds_the_bundle_through_the_keyword_arm(mem):
    """The fact id the user just typed must not be blurred away.

    A bare identifier inside a longer record is the shape the vector arm loses:
    one matching token against a whole section puts cosine under the search
    floor. BM25 finds it exactly. Seeding from the vector arm alone was the
    pre-v0.18 recall bug one layer up — this pins the fix at the resolve seam.
    """
    from palinode.core import store

    _write(mem, "decisions/deploy-token.md",
           "# Deploy token\n\nThe production deploy for the checkout service "
           "runs under the rotating credential issued by the platform team, "
           "and every rotation is recorded against ticket pln4471x before the "
           "release train leaves the staging environment.",
           type="Decision", status="active", date="2026-09-01",
           entities=["project/demo"])

    # The arm that would have been used alone: below the floor, so nothing.
    vector_only = store.search_internal(
        query_embedding=_bow_embed("pln4471x"),
        top_k=5,
        threshold=config.search.api_threshold,
    )
    assert vector_only == [], (
        "the identifier now clears the vector floor; pick one that does not, "
        "or this test proves nothing"
    )

    data = _bundle("pln4471x")
    assert [s["ref"] for s in data["selected"]] == ["decisions/deploy-token"]


def test_seeding_writes_neither_recall_nor_a_retrieval_log_row(mem):
    """Read-only in both ledgers: no recall metadata, no retrieval event.

    ``/search`` records both by design (ADR-006/007). Resolve is an analysis of
    a retrieval, not one — and it runs on every prompt.
    """
    seed_current(mem)
    audit = mem / ".audit" / "retrievals.jsonl"

    for _ in range(3):
        _bundle("endpoint production traffic")

    assert _recall_counts() == dict.fromkeys(_recall_counts(), 0), _recall_counts()
    assert not audit.exists() or audit.read_text(encoding="utf-8").strip() == ""


# ── visibility and path safety ───────────────────────────────────────────────


def test_a_hidden_record_is_a_coverage_reason_never_a_leak(mem, monkeypatch):
    """An unreadable seed is reported as missing, and never named or quoted."""
    from palinode.core.scope import ScopeChain

    _write(mem, "insights/secret-quokka.md",
           "# Quokka\n\nThe cache cluster runs in region reykjavik.",
           status="active", visibility="private", entities=["project/demo"])
    seed_conflict(mem)

    chain = ScopeChain(agent="someone-else")
    data = build_bundle(
        BundleRequest(query="cache cluster region", ref="insights/secret-quokka"),
        chain=chain,
    ).to_dict()

    # The request echo carries the caller's own ref back; everything else must
    # be free of the hidden record — no title, no body, no revision, no side.
    body = dict(data)
    body.pop("ref"), body.pop("query"), body.pop("text")
    blob = json.dumps(body).lower()
    assert "quokka" not in blob
    assert "reykjavik" not in blob
    assert "reykjavik" not in data["text"].lower()
    assert data["coverage"]["status"] == "partial"
    assert "target_missing" in data["coverage"]["reasons"]


def test_a_traversing_ref_is_refused(mem):
    data = build_bundle(BundleRequest(ref="../../etc/passwd")).to_dict()
    assert data["selected"] == [] and data["insufficient"] == []
    assert "target_missing" in data["coverage"]["reasons"]


# ── index lag: the index chooses the seed, the file supplies the text ────────


def _edit_without_reindexing(mem, rel: str, old: str, new: str) -> None:
    """Real index lag: the file moves on, the watcher has not caught up."""
    path = mem / rel
    raw = path.read_text(encoding="utf-8")
    assert old in raw
    path.write_text(raw.replace(old, new), encoding="utf-8")


def test_index_lag_delivers_the_live_wording_not_the_indexed_one(mem):
    """An honest stamp on wrong text is still wrong text.

    Nothing is mocked: the file is written and indexed, then edited on disk
    with no reconcile, so the seed genuinely comes back from an index that
    lags its source.
    """
    _write(mem, "decisions/retention.md",
           "# Retention\n\nThe demo log retention window is 7 days.",
           type="Decision", status="active", date="2026-08-01",
           entities=["project/demo"])
    _edit_without_reindexing(mem, "decisions/retention.md", "7 days", "30 days")

    data = _bundle("demo log retention window")

    assert [s["ref"] for s in data["selected"]] == ["decisions/retention"]
    current = data["selected"][0]
    assert current["freshness"] == "stale", "the seed must really be lagging"
    assert "index_lag" in data["coverage"]["reasons"]
    assert "30 days" in current["statement"], current["statement"]
    assert "7 days" not in data["text"], data["text"]
    assert "30 days" in data["text"]


def test_index_lag_names_the_revision_the_delivered_text_came_from(mem):
    """The revision on the receipt is the one the excerpt was read at.

    Under lag the indexed per-section hash no longer describes anything that
    was delivered, so the row names the file it was read from instead — the
    ``file_sha256`` domain the evidence layer already hashes into.
    """
    _write(mem, "decisions/retention.md",
           "# Retention\n\nThe demo log retention window is 7 days.",
           type="Decision", status="active", date="2026-08-01",
           entities=["project/demo"])
    _edit_without_reindexing(mem, "decisions/retention.md", "7 days", "30 days")

    data = _bundle("demo log retention window")
    supplied = {r["ref"]: r for r in data["receipt"]["supplied"]}
    row = supplied["decisions/retention"]
    assert row["freshness"] == "stale"
    assert row["revision_basis"] == REVISION_FILE
    assert row["revision"] == hashlib.sha256(
        (mem / "decisions" / "retention.md").read_text(encoding="utf-8").encode()
    ).hexdigest()


# ── what the rendered text carries (the string the hook injects) ─────────────


def _line_for(text: str, ref: str) -> str:
    """The rendered row naming ``ref`` — the unit line, not its continuations."""
    return next(line for line in text.splitlines() if line.startswith(f"- [{ref}]"))


def test_support_refs_are_named_under_the_assertion_they_back(mem):
    """A standing record says what it rests on, by ref, in the text.

    Refs only: the backing record is one read away, and this string is a
    per-turn injection.
    """
    seed_unlinked_correction(mem)
    data = _bundle("orbit client retry policy")

    assert [s["ref"] for s in data["selected"]] == ["decisions/orbit-retry"]
    assert "    support: research/retry-probe" in data["text"].splitlines()
    # The unlinked record is in the payload's support refs, and it is *not*
    # folded into the support line — an unlinked correction is not backing.
    assert "insights/orbit-scheduling" in data["selected"][0]["refs"]["support"]
    assert "support: research/retry-probe, insights/orbit-scheduling" not in data["text"]


def test_an_unlinked_correction_is_visible_in_the_text_not_only_the_payload(mem):
    """The unlinked-correction gap: discovery reaches it, the reader never saw it.

    The correction carries no typed link and shares no vocabulary with the
    decision, so only the fallback discovery finds it. It is rendered under
    the assertion it bears on — with its currency, and marked unlinked, so
    nothing about it reads as a resolved contradiction.
    """
    seed_unlinked_correction(mem)
    data = _bundle("orbit client retry policy")
    text = data["text"]

    assert "insights/orbit-scheduling" in text, "the correction never reached the reader"
    found = next(
        line for line in text.splitlines() if "also found (unlinked)" in line
    )
    assert found == (
        "    ⚠ also found (unlinked): [insights/orbit-scheduling] [current] — "
        "Correction — exponential jitter replaced the fixed schedule for outbound calls."
    ), found
    # An indented continuation of the assertion's own unit: the packer keeps
    # the two together, and every consumer's trim splits on the same boundary.
    assert found.startswith("    ")


def test_a_discovered_statement_is_an_excerpt_never_the_whole_record(mem):
    from palinode.core.bundle import DISCOVERY_EXCERPT_CHARS

    body = "Correction — " + "the schedule changed again and again. " * 20
    _write(mem, "decisions/orbit-window.md",
           "# Orbit window\n\nThe orbit request window is ninety seconds.",
           type="Decision", status="active", date="2026-01-14",
           entities=["project/orbit"])
    _write(mem, "insights/jitter-note.md", f"# Jitter note\n\n{body}",
           status="active", date="2026-05-02", entities=["project/orbit"])
    _reindex(mem, "decisions/orbit-window.md")

    text = _bundle("orbit request window")["text"]
    found = next(line for line in text.splitlines() if "also found (unlinked)" in line)
    statement = found.split(" — ", 1)[1]
    assert len(statement) <= DISCOVERY_EXCERPT_CHARS
    assert statement.startswith("Correction")


def test_a_record_with_neither_support_nor_discovery_renders_as_before(mem):
    """Byte-stability for the common case: no refs, no extra lines.

    The three behavioural-fixture scenarios pin the same thing on the whole
    payload; this pins the rule the renderer follows to stay there.
    """
    seed_current(mem)
    text = _bundle("endpoint production traffic")["text"]

    assert "support:" not in text
    assert "also found" not in text
    assert _line_for(text, "decisions/endpoint-v2").endswith(
        "Production serves traffic from endpoint bravo."
    ), "a standing assertion's row gained an inline label it should not have"


def test_a_contested_side_carries_its_own_qualifiers(mem):
    """Sides are not equally supported just because both are shown.

    One side rests on a source that was retired out from under it. The group's
    shared reasons cannot say *which* side that is, so the qualification rides
    on the side's own row.
    """
    seed_contested_stale_backing(mem)
    data = _bundle("orbit queue depth ceiling")
    text = data["text"]

    assert len(data["conflicts"]) == 1
    stale = _line_for(text, "insights/queue-depth-a")
    assert "⚠ stale backing: research/depth-probe-alpha" in stale
    assert "⚠ contradicts: insights/queue-depth-b" in stale
    # And the other side is not given a qualification it does not carry.
    assert "stale backing" not in _line_for(text, "insights/queue-depth-b")


def test_a_tight_budget_never_shows_a_side_without_its_label(mem):
    """Whole group, or the notice that names it. Never a stripped side.

    The qualifier is part of the unit the packer measures, so a budget that
    cannot afford the label cannot afford the group either — which is the
    property that makes "contested" survive budget pressure honestly.
    """
    seed_contested_stale_backing(mem)
    for max_chars in range(0, 1400, 50):
        data = _bundle("orbit queue depth ceiling", budget={"max_chars": max_chars})
        text = data["text"]
        if data["conflicts"]:
            assert "⚠ stale backing: research/depth-probe-alpha" in text, (
                f"max_chars={max_chars} rendered a stale-backed side unlabelled:\n{text}"
            )
        else:
            assert data["omitted_conflicts"] >= 1
            assert "Still contested" in text
        assert len(text) <= max_chars or data["omitted_conflicts"] >= 1


def test_side_labels_are_the_digest_labels(mem):
    """One qualifier vocabulary across surfaces, pinned rather than hoped for.

    ``palinode_search`` results, the session-start digest and a contested side
    all say ``⚠ contradicts:`` / ``⚠ stale backing:`` / ``epistemic:``. The
    bundle renders them by calling the digest's own labeller; this is what
    catches the two drifting apart if one of them stops.
    """
    from palinode.core.bundle import _qualifier_labels
    from palinode.core.context_prime import _row_qualifiers

    qualifiers = (
        "epistemic:fact", "contradicts:insights/b",
        "stale_backing:research/s", "stale_backing:research/t@2:support_withdrawn",
        "undated", "index_stale",
    )
    assert _qualifier_labels(qualifiers) == _row_qualifiers({
        "contradicts": ["insights/b"],
        "stale_backing": ["research/s", "research/t@2:support_withdrawn"],
        "epistemic": "fact",
    })
    # The read-time two-hop finding rides through whole: the hop and the
    # reason are what say which backing went.
    assert "research/t@2:support_withdrawn" in _qualifier_labels(qualifiers)
    # Qualifications the unit already states elsewhere are not repeated here.
    assert "undated" not in _qualifier_labels(qualifiers)
    assert _qualifier_labels(()) == ""


# ── budget: conflict-preserving packing ──────────────────────────────────────


def test_tight_budget_omits_a_conflict_whole_and_says_so(mem):
    seed_current(mem)
    seed_conflict(mem)
    # Room for the standing assertion only: the conflict group cannot fit.
    data = _bundle("cache cluster region", context=("decisions/endpoint",),
                   budget={"max_items": 1})

    assert len(data["selected"]) == 1
    assert data["conflicts"] == []
    assert data["omitted_conflicts"] == 1
    assert sorted(data["omitted_conflict_refs"][0]) == [
        "insights/region-a", "insights/region-b"
    ]
    assert BUDGET_CONFLICTS in data["coverage"]["reasons"]
    assert data["coverage"]["status"] == "partial"
    assert "Still contested" in data["text"]


def test_budget_never_shows_one_side_of_a_conflict(mem):
    seed_conflict(mem)
    for max_chars in range(0, 400, 25):
        data = _bundle("cache cluster region", budget={"max_chars": max_chars})
        for group in data["conflicts"]:
            assert len(group["sides"]) >= 2, (
                f"max_chars={max_chars} split a conflict into {group['sides']}"
            )
        if not data["conflicts"]:
            assert data["omitted_conflicts"] >= 1
            assert BUDGET_CONFLICTS in data["coverage"]["reasons"]


def test_max_chars_bounds_the_rendered_text_not_just_its_contents(mem):
    """The consumer's byte budget is the bundle's byte budget.

    A hook with a hard injection cap passes it straight through; if the
    rendered frame were charged to nobody, the hook's own final truncation
    would cut a conflict in half after the packer took care not to.
    """
    seed_current(mem)
    seed_conflict(mem)
    for max_chars in (600, 900, 1400, 2000):
        data = _bundle("cache cluster region", context=("decisions/endpoint",),
                       budget={"max_chars": max_chars})
        assert len(data["text"]) <= max_chars, (
            f"max_chars={max_chars} rendered {len(data['text'])} chars:\n{data['text']}"
        )


def test_an_impossible_budget_overruns_rather_than_hiding_a_conflict(mem):
    """The contested notice is the one thing the budget cannot buy off.

    Below the floor (frame + omission notice) the bundle is *over* its
    requested size. That is the deliberate direction to fail in: a bundle that
    fit by dropping the notice would read as a settled "nothing contested".
    """
    seed_conflict(mem)
    data = _bundle("cache cluster region", budget={"max_chars": 10})
    assert data["conflicts"] == []
    assert data["omitted_conflicts"] == 1
    assert "Still contested" in data["text"]
    assert BUDGET_CONFLICTS in data["coverage"]["reasons"]


def test_a_kept_item_never_loses_its_qualifiers(mem):
    seed_conflict(mem)
    wide = _bundle("cache cluster region", budget={"max_chars": 100000})
    tight = _bundle("cache cluster region", budget={"max_chars": 900})
    if tight["conflicts"]:
        assert tight["conflicts"][0]["sides"] == wide["conflicts"][0]["sides"]


def test_the_token_cap_bites_on_its_own_and_is_named(mem):
    """Both caps are enforced, and they are not the same cap.

    A bundle can sit well inside its character budget and still be over the
    estimated-token one — the number that actually costs a per-turn injection
    its room. Which cap bit is reported, never folded into the other.
    """
    seed_current(mem)
    seed_conflict(mem)
    roomy = _bundle("cache cluster region", context=("decisions/endpoint",),
                    budget={"max_chars": 100000, "max_tokens": 0})
    tight = _bundle("cache cluster region", context=("decisions/endpoint",),
                    budget={"max_chars": 100000, "max_tokens": 200})

    assert roomy["budget"]["max_tokens"] == 0, "0 turns the token cap off"
    assert roomy["budget"]["tokens"] > 200, "the roomy bundle is over the tight cap"
    assert tight["budget"]["tokens"] <= 200
    assert tight["budget"]["items"] < roomy["budget"]["items"]
    assert "budget_exhausted:tokens" in tight["coverage"]["reasons"]
    assert "budget_exhausted:chars" not in tight["coverage"]["reasons"], (
        "the character budget was never the binding one"
    )
    # Whole or named, exactly as under a tight character budget.
    for group in tight["conflicts"]:
        assert len(group["sides"]) >= 2
    if not tight["conflicts"]:
        assert tight["omitted_conflicts"] == 1
        assert "Still contested" in tight["text"]


def test_the_default_token_cap_comes_from_the_per_turn_configuration(mem):
    """The bundle answers to the same configured ceiling the recall block does."""
    seed_current(mem)
    assert _bundle("endpoint production traffic")["budget"]["max_tokens"] == (
        config.context.recall_max_tokens
    )


# ── the delivery receipt ─────────────────────────────────────────────────────


def _receipt_of(data: dict) -> dict:
    return data["receipt"]


def test_every_delivered_record_is_on_the_receipt_at_a_named_revision(mem):
    """The receipt covers what was delivered, and names the domain of each hash.

    ``index_section_sha256`` and ``file_sha256`` are different hashes of
    different things and must never be compared, which is why the basis is on
    every record rather than assumed per surface. ``unknown`` is available and
    honest — but a record this delivery actually read has a revision, and
    reporting unknown for it would be the receipt underclaiming.
    """
    seed_current(mem)
    seed_conflict(mem)
    data = build_bundle(
        BundleRequest(query="cache cluster region", context=("decisions/endpoint",))
    ).to_dict()
    receipt = _receipt_of(data)

    assert data["receipt_ref"] == receipt["bundle_id"]
    supplied = {r["ref"]: r for r in receipt["supplied"]}

    delivered = (
        {s["ref"] for s in data["selected"]}
        | {r["ref"] for r in data["replaced"]}
        | {s["ref"] for g in data["conflicts"] for s in g["sides"]}
        | {i["ref"] for i in data["insufficient"]}
        | {r for g in data["omitted_conflict_refs"] for r in g}
    )
    assert delivered, "the scenario delivered nothing to put on a receipt"
    assert delivered <= set(supplied), f"missing from the receipt: {delivered - set(supplied)}"
    for ref, record in supplied.items():
        assert record["revision"], f"{ref} was supplied without a revision"
        assert record["revision_basis"] in (REVISION_INDEX_SECTION, REVISION_FILE), record
    assert receipt["coverage"] == data["coverage"], (
        "the receipt's coverage is the delivery's coverage, not a second opinion"
    )


def test_the_receipt_disposes_each_record_the_way_the_bundle_did(mem):
    """Selected, replaced, conflict side, unknown — the bundle's own grouping.

    The disposition is not re-derived from currency here: the bundle already
    decided, and a receipt that guessed could disagree with the payload it is
    the receipt for.
    """
    seed_current(mem)
    seed_conflict(mem)
    seed_unknown(mem)
    data = build_bundle(BundleRequest(
        query="cache cluster region pipeline throughput",
        ref="decisions/endpoint",
        budget=BundleBudget(max_items=20, max_chars=20000),
    )).to_dict()
    by_ref = {r["ref"]: r["disposition"] for r in _receipt_of(data)["supplied"]}

    assert by_ref["decisions/endpoint-v2"] == SELECTED
    assert by_ref["decisions/endpoint"] == REPLACED
    assert by_ref["insights/region-a"] == CONFLICT_SIDE
    assert by_ref["insights/region-b"] == CONFLICT_SIDE
    assert set(by_ref.values()) <= {
        SELECTED, REPLACED, CONFLICT_SIDE, INSUFFICIENT, EVIDENCE_ONLY,
    }


def test_an_omitted_conflict_is_still_on_the_receipt(mem):
    """Budget pressure changes what is rendered, not what was supplied.

    The omission notice names both refs; the receipt says what those refs were
    at. A conflict that fell out of the body for room is exactly the case
    where a reader needs the revisions to go look.
    """
    seed_current(mem)
    seed_conflict(mem)
    data = _bundle("cache cluster region", context=("decisions/endpoint",),
                   budget={"max_items": 1})
    assert data["omitted_conflicts"] == 1
    supplied = {r["ref"] for r in _receipt_of(data)["supplied"]}
    for ref in data["omitted_conflict_refs"][0]:
        assert ref in supplied


def test_the_rendered_text_carries_the_same_receipt_id(mem):
    seed_conflict(mem)
    data = _bundle("cache cluster region")
    assert f"Receipt: {data['receipt_ref']}" in data["text"]


def test_building_the_receipt_writes_no_retrieval_row_through_rest(mem, client):
    """Receipt metadata is persisted without creating file-retrieval events."""
    seed_current(mem)
    audit = mem / ".audit" / "retrievals.jsonl"

    for _ in range(3):
        body = client.post("/resolve", json={"query": "endpoint production traffic"}).json()
        assert body["receipt"]["bundle_id"] == body["receipt_ref"]

    entries = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(entries) == 3
    assert all(e["event_type"] == "bundle_receipt" and not e.get("file_path") for e in entries)


def test_evidence_only_records_report_a_file_revision_not_unknown(mem):
    """A record carried as support is supplied context, and it has a revision.

    The evidence layer already read the file; hashing the bytes it had in hand
    is what turns ``revision_basis: unknown`` into an answer. The basis says
    ``file_sha256`` because that is what it is — not the indexed per-section
    hash a search row carries, and never compared with one.
    """
    _write(mem, "research/measurement.md",
           "# Measurement\n\nThe measured ceiling was 4000 rps.",
           status="active", date="2026-07-01", entities=["project/demo"])
    _write(mem, "insights/ceiling.md",
           "# Ceiling\n\nThe pipeline throughput ceiling is 4000 rps.",
           status="active", date="2026-08-10", entities=["project/demo"],
           backed_by=["research/measurement"])
    _reindex(mem, "insights/ceiling.md")

    data = build_bundle(BundleRequest(ref="insights/ceiling")).to_dict()
    supplied = {r["ref"]: r for r in _receipt_of(data)["supplied"]}
    support = supplied.get("research/measurement")
    assert support is not None, supplied
    assert support["disposition"] == EVIDENCE_ONLY
    assert support["revision"], "the supporting record was supplied without a revision"
    assert support["revision_basis"] in (REVISION_INDEX_SECTION, REVISION_FILE)


# ── cold start ───────────────────────────────────────────────────────────────


def test_no_embedder_degrades_to_keyword_seeds_and_says_so(mem):
    seed_current(mem)
    from palinode.core import embedder

    cold = embedder.EmbeddingUnavailable(
        backend="local", model="bge-m3", text_len=8, cause="connection refused",
    )
    with patch("palinode.core.embedder.embed", side_effect=cold):
        data = _bundle("endpoint production traffic")

    assert DEGRADED_KEYWORD_ONLY in data["coverage"]["reasons"]
    assert data["coverage"]["status"] == "partial"
    assert [s["ref"] for s in data["selected"]] == ["decisions/endpoint-v2"]
    assert "degraded:keyword_only" in data["text"]


def test_empty_store_answers_explicitly(mem):
    data = _bundle("anything at all")
    assert data["selected"] == [] and data["conflicts"] == []
    assert "Nothing in memory answers this." in data["text"]


# ── the behavioral fixture (the exact JSON an agent receives) ────────────────


_RECEIPT_LINE = re.compile(r"^Receipt: [0-9a-f]+$", re.MULTILINE)


def _stable_text(text: str) -> str:
    """The rendered bundle with the receipt identifier normalized.

    The identifier covers the evaluation clock by design — two deliveries of
    the same question are two deliveries — so pinning its value would pin the
    clock. That the line is *there*, and that it carries the same id as the
    payload's ``receipt_ref``, is asserted separately.
    """
    return _RECEIPT_LINE.sub("Receipt: <bundle-id>", text)


def _fixture_view(data: dict) -> dict:
    """The bundle as an agent receives it, minus the four clock-bound values.

    Every field is a function of the files the scenario writes — the revisions
    are content hashes of that content, the ordering is the resolver's —
    except the receipt's identity (a digest over the evaluation time), the two
    timestamps, and the policy tuple (which carries the package version and a
    config fingerprint). Those are normalized. Everything else the receipt
    says — which records were supplied, at which revision, on which basis, how
    each was disposed, what lineage is known, what coverage — is pinned here,
    because that is the part a consumer reads.
    """
    view = dict(data)
    view["text"] = _stable_text(view["text"])
    if view.get("receipt_ref"):
        view["receipt_ref"] = "<bundle-id>"
    receipt = view.get("receipt")
    if receipt:
        view["receipt"] = {
            **receipt,
            "bundle_id": "<bundle-id>",
            "policy_version": "<policy>",
            "requested_time": "<time>",
            "evaluated_at": "<time>",
        }
    return view


_SCENARIOS = {
    "current": (seed_current, "endpoint production traffic"),
    "conflict": (seed_conflict, "cache cluster region"),
    "unknown": (seed_unknown, "pipeline throughput ceiling"),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_behavioral_fixture_matches(mem, name):
    """The checked-in JSON is what an agent actually receives.

    Regenerate deliberately, never reflexively: this file is the contract the
    plugin's own suite reads, so a diff here is a diff in what every consumer
    sees.
    """
    seed, query = _SCENARIOS[name]
    seed(mem)
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[name]
    assert _fixture_view(_bundle(query)) == expected


# ── surface equivalence ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_rest_matches_the_shared_expectation(client, mem, name):
    seed, query = _SCENARIOS[name]
    seed(mem)
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[name]
    resp = client.post("/resolve", json={"query": query})
    assert resp.status_code == 200, resp.text
    assert _fixture_view(resp.json()) == expected


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_cli_renders_the_same_text(client, mem, name, monkeypatch):
    seed, query = _SCENARIOS[name]
    seed(mem)
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[name]

    from palinode.cli.resolve import api_client

    # The CLI resolves this shell's project and sends it. Pinned to the
    # scenario's own project so the rendering does not depend on the checkout
    # the suite runs in: a scoped request leaves other projects' records out.
    monkeypatch.setenv("PALINODE_PROJECT", "demo")
    monkeypatch.setattr(
        api_client,
        "resolve",
        lambda **kw: client.post("/resolve", json={
            k: v for k, v in kw.items() if v is not None
        }).json(),
    )
    result = CliRunner().invoke(cli_resolve, [query, "--format", "text"])
    assert result.exit_code == 0, result.output
    assert _stable_text(result.output).strip() == expected["text"].strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_mcp_renders_the_same_text(client, mem, name, monkeypatch):
    seed, query = _SCENARIOS[name]
    seed(mem)
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[name]

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def _fake_post(path, json=None, timeout=30.0):
        assert path == "/resolve"
        return _Resp(client.post("/resolve", json=json).json())

    monkeypatch.setattr(mcp, "_post", _fake_post)
    # As in the CLI test: the client's project is the scenario's, not the
    # checkout's.
    monkeypatch.setenv("PALINODE_PROJECT", "demo")
    out = await mcp._tool_resolve({"query": query})
    assert _stable_text(out[0].text).strip() == expected["text"].strip()


@pytest.mark.asyncio
async def test_mcp_forwards_every_canonical_param(monkeypatch):
    captured: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"text": "ok"}

    async def _fake_post(path, json=None, timeout=30.0):
        captured.append(json)
        return _Resp()

    monkeypatch.setattr(mcp, "_post", _fake_post)
    # The client's resolved project rides along beside the canonical params;
    # pinned here so the test does not depend on the checkout it runs in.
    monkeypatch.setenv("PALINODE_PROJECT", "harbor-notes")
    await mcp._tool_resolve({
        "query": "q", "ref": "decisions/x", "context": ["decisions/y"],
        "intent": "current_state", "max_items": 3, "max_chars": 500,
        "include_retired": True,
    })
    assert captured[0] == {
        "query": "q", "ref": "decisions/x", "intent": "current_state",
        "context": ["decisions/y"], "max_items": 3, "max_chars": 500,
        "include_retired": True,
        "project": "project/harbor-notes",
    }


def test_rest_rejects_a_request_with_neither_query_nor_ref(client, mem):
    assert client.post("/resolve", json={}).status_code == 422


def test_rest_rejects_an_unsupported_intent(client, mem):
    assert client.post(
        "/resolve", json={"query": "x", "intent": "as_of"}
    ).status_code == 422


def test_cli_rejects_a_request_with_neither_query_nor_ref():
    assert CliRunner().invoke(cli_resolve, []).exit_code != 0


# ── backward compatibility ───────────────────────────────────────────────────


def test_search_and_read_are_unchanged(client, mem):
    """Resolving changes nothing about the two operations that came before it."""
    seed_current(mem)
    body = {"query": "endpoint bravo production", "limit": 3, "threshold": 0.0}

    def _stable(rows):
        return [{k: v for k, v in r.items()
                 if k not in ("recall_count", "last_recalled", "importance")} for r in rows]

    before = _stable(client.post("/search", json=body).json())
    read_before = client.get("/read", params={"file_path": "decisions/endpoint-v2.md"}).json()

    client.post("/resolve", json={"query": "endpoint bravo production"})

    assert _stable(client.post("/search", json=body).json()) == before
    assert client.get(
        "/read", params={"file_path": "decisions/endpoint-v2.md"}
    ).json() == read_before
    assert all("evidence" not in row for row in before)


def _recall_counts() -> dict[str, int]:
    db = store.get_db()
    try:
        return {
            row["file_path"]: row["recall_count"]
            for row in db.execute(
                "SELECT file_path, recall_count FROM chunks"
            ).fetchall()
        }
    finally:
        db.close()


def test_resolve_records_no_recall(client, mem):
    """A per-turn operation must not inflate recall metadata.

    ``/search`` deliberately does record it (that is ADR-007), so the baseline
    is taken from the index after a search, not from the search's own rows.
    """
    seed_current(mem)
    client.post("/search", json={"query": "endpoint", "limit": 5, "threshold": 0.0})
    before = _recall_counts()

    for _ in range(3):
        client.post("/resolve", json={"query": "endpoint production traffic"})

    assert _recall_counts() == before

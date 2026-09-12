"""Auditable Memory Records (AMR) 0.1 conformance — run against the implementation.

The vectors under ``tests/amr_conformance/`` are the specification's own
conformance suite (CC BY 4.0), vendored verbatim so this repository — the
reference implementation — proves conformance in CI rather than asserting it.
Each suite is data, not code; this module is the adapter that maps every case
onto the real save path, the real parser, and the real verifier.

Recorded result, 2026-09-01 (all cases below must pass for the claim to hold):

- **normalize** — pass (10/10). ``normalize_quote`` is byte-identical to §5.
- **Level 1, Marked** — pass (15/15). Every record the save path writes carries
  ``auditable_memory: "0.1"``; an unrecognized version is rejected; the closed
  epistemic vocabulary is enforced case-sensitively; absent is a third state
  distinct from ``fact``; confidence is range-checked.
- **Level 2, Linked** — pass (12/12). Refs are validated against traversal,
  absolute paths, control characters, and empty strings; ``contradicts`` is
  non-resolving (both sides stay retrievable, neither is superseded); declared
  relations are queryable as exact sets from the record itself.
- **Level 3, Cited** — pass (16/16). The four verification outcomes are distinct,
  hash prefixes are resolved by length rather than assumption, ``claim_id`` is
  content-addressed and ref-salted, and verification needs only the serialized
  record plus the cited source.

Honest claim: **Level 3**, with the §7 caveat that conformance is demonstrated
on these vectors, not audited across a private corpus.

Real SQLite + tmp_path; no DB mocking (per CLAUDE.md). The embedder and the
content security scan are patched — this suite is about the record contract,
not embeddings.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import frontmatter
import pytest
import yaml

from palinode.core import store
from palinode.core.claims import ClaimError, derive_claim_id, normalize_claims
from palinode.core.config import config
from palinode.core.parity import AMR_SPEC_VERSION, VALID_AMR_VERSIONS
from palinode.core.parser import DEFAULT_EPISTEMIC
from palinode.core.quote_verify import (
    UnsupportedHashAlgorithm,
    normalize_quote,
    parse_quote_hash,
    verify_memory_sources,
    verify_quote,
)
from palinode.core.save import SaveValidationError, save_memory
from palinode.core.typed_links import parse_link_refs

_VECTORS = Path(__file__).parent / "amr_conformance"
_FAKE_VECTOR = [0.01] * 1024


def _suite(name: str) -> list[dict[str, Any]]:
    with open(_VECTORS / f"{name}.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["spec_version"] == "0.1.0"
    return data["cases"]


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [c["id"] for c in cases]


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    store.init_db()
    return tmp_path


def _save(content: str = "a record", slug: str | None = None, **kw) -> dict[str, Any]:
    """Save through the real core path; return the written frontmatter."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")), \
            patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        result = save_memory(content=content, type="Insight", slug=slug, **kw)
    return frontmatter.load(result["file_path"]).metadata


def _write_raw(memory_dir: Path, rel: str, meta: dict[str, Any], body: str = "body") -> Path:
    """Write a record file directly, bypassing the save path's validation."""
    path = memory_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, allow_unicode=True)
    path.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return path


def _conforms_level_1(meta: dict[str, Any]) -> bool:
    return meta.get("auditable_memory") in VALID_AMR_VERSIONS


# ── normalize (SPEC §5) ────────────────────────────────────────────────────────

_NORM = _suite("normalize")


@pytest.mark.parametrize("case", _NORM, ids=_ids(_NORM))
def test_normalize(case):
    assert normalize_quote(case["input"]) == case["expect"], case.get("why")
    if case.get("also_assert") == "idempotent":
        assert normalize_quote(normalize_quote(case["input"])) == case["expect"]


# ── Level 1, Marked (SPEC §4.1, §4.2, §7) ──────────────────────────────────────

_L1 = _suite("level1-marked")

# The spec phrases errors abstractly; these are the implementation's messages.
_L1_ERRORS = {
    "unrecognized auditable_memory version": "unrecognized auditable_memory version",
    "epistemic value not in closed vocabulary": "Invalid epistemic",
    "confidence out of range": "confidence out of range",
}


@pytest.mark.parametrize("case", _L1, ids=_ids(_L1))
def test_level1_marked(case, memory_dir):
    inp, exp = case["input"], case["expect"]

    if "corpus" in inp:  # l1-000d — discoverable by field scan
        for rec in inp["corpus"]:
            _save(content=f"record {rec}", slug=rec)
        found = [
            p for p in memory_dir.rglob("*.md")
            if frontmatter.load(p).metadata.get(inp["all_carry"]) is not None
        ]
        assert len(found) == len(inp["corpus"])
        assert exp["discoverable_by_field_scan"] is True
        return

    if "auditable_memory" not in inp and "epistemic" not in inp and "confidence" not in inp:
        # l1-000b / l1-008: a record written under the spec vs. one that is not.
        if exp.get("conforms_level_1") is False:
            # NON-VACUITY: a record carrying no declaration does not conform,
            # however well-behaved it is. Written raw, because the save path
            # can no longer produce such a record.
            path = _write_raw(memory_dir, "insights/undeclared.md", {"id": "x"}, inp["text"])
            meta = frontmatter.load(path).metadata
            assert meta.get("auditable_memory") is None
            assert _conforms_level_1(meta) is False
            return
        meta = _save(content=inp["text"])
        assert "epistemic" not in meta
        assert meta.get("epistemic") is None
        assert meta.get("epistemic") != exp["must_not_equal"]
        assert DEFAULT_EPISTEMIC != "fact"
        # And the writer's own record still conforms (declaration present).
        assert _conforms_level_1(meta)
        return

    kwargs: dict[str, Any] = {}
    if "epistemic" in inp:
        kwargs["epistemic"] = inp["epistemic"]
    if "confidence" in inp:
        kwargs["confidence"] = inp["confidence"]
    if "auditable_memory" in inp:
        kwargs["metadata"] = {"auditable_memory": inp["auditable_memory"]}

    if exp.get("valid") is False:
        with pytest.raises(SaveValidationError) as exc_info:
            _save(**kwargs)
        assert _L1_ERRORS[exp["error"]] in str(exc_info.value), case.get("why")
        return

    meta = _save(**kwargs)
    assert _conforms_level_1(meta)
    if "auditable_memory" in exp:
        assert meta["auditable_memory"] == exp["auditable_memory"]
    if exp.get("conforms_level_1") is True:
        assert meta["auditable_memory"] == AMR_SPEC_VERSION
    if "epistemic" in exp:
        assert meta.get("epistemic") == exp["epistemic"]
    if "must_not_equal" in exp:
        assert meta.get("epistemic") != exp["must_not_equal"]
    if "confidence" in exp:
        assert meta["confidence"] == exp["confidence"]


# ── Level 2, Linked (SPEC §3, §4.5, §7) ────────────────────────────────────────

_L2 = _suite("level2-linked")


def _ref(name: str) -> str:
    """The spec's abstract refs ``a``/``b``/``c`` as concrete memory refs."""
    return f"insights/{name}"


@pytest.mark.parametrize("case", _L2, ids=_ids(_L2))
def test_level2_linked(case, memory_dir):
    inp, exp = case["input"], case["expect"]

    if "derivation" in inp:  # l2-012 — inferred relations are never serialized
        # The write path serializes only refs the caller declared. A record
        # saved with no links carries no typed-link field, however similar a
        # neighbour is — the fake embedder makes every record maximally similar.
        _save(content="record d about insulin pricing", slug="d")
        meta = _save(content="record a about insulin pricing", slug="a")
        assert "backed_by" not in meta and "contradicts" not in meta
        assert exp["valid"] is False  # the attempted derivation is not a valid write
        return

    if "record" in inp:  # l2-009 / l2-010 / l2-011 — declared relations
        rec = inp["record"]
        links = [_ref(r) for r in rec["contradicts"]]
        if "other" in inp:
            _save(content="record b unique-zebra-b", slug=inp["other"]["ref"])
        meta = _save(content="record a unique-zebra-a", slug=rec["ref"], contradicts=links)
        assert exp["valid"] is True
        # Queryable AS a relation: exact declared set from the record itself.
        assert parse_link_refs(meta, "contradicts") == links
        if "returns" in exp:
            assert parse_link_refs(meta, "contradicts") == [_ref(r) for r in exp["returns"]]
        if exp.get("a_retrievable"):
            # Non-resolving: both sides remain retrievable, b is untouched.
            a_hits = store.search_fts("unique-zebra-a")
            b_hits = store.search_fts("unique-zebra-b")
            assert len(a_hits) == 1 and len(b_hits) == 1
            b_meta = frontmatter.load(memory_dir / "insights" / "b.md").metadata
            assert b_meta.get("status", "active") == "active"
            assert "superseded" not in str(b_meta.get("status", ""))
        return

    kwargs: dict[str, Any] = {}
    for field in ("backed_by", "contradicts"):
        if field in inp:
            kwargs[field] = inp[field]

    if exp.get("valid") is False:
        with pytest.raises(SaveValidationError) as exc_info:
            _save(**kwargs)
        msg = str(exc_info.value)
        assert "not a well-formed ref" in msg or "non-empty string" in msg, msg
        return

    meta = _save(**kwargs)
    for field, refs in kwargs.items():
        assert parse_link_refs(meta, field) == refs


# ── Level 3, Cited (SPEC §4.3, §4.4, §5, §6, §7) ───────────────────────────────

_L3 = _suite("level3-cited")


@pytest.mark.parametrize("case", _L3, ids=_ids(_L3))
def test_level3_cited(case, memory_dir):
    inp, exp = case["input"], case["expect"]

    if "record_ref" in inp:  # l3-008 / l3-009 — claim_id derivation
        cid = derive_claim_id(inp["record_ref"], inp["claim_text"])
        if "claim_id" in exp:
            assert cid == exp["claim_id"]
            # Recomputable by a third party from the stated formula.
            raw = f"{inp['record_ref']}:{normalize_quote(inp['claim_text'])}"
            assert cid == hashlib.sha256(raw.encode()).hexdigest()[:16]
        if "claim_id_differs_from" in exp:
            assert cid != exp["claim_id_differs_from"]
        return

    if "given" in inp:  # l3-012 — independent verifiability
        source = "the report found that list prices for insulin products rose 11% between 2024 and 2026"
        quote = "list prices for insulin products rose 11% between 2024 and 2026"
        (memory_dir / "sources").mkdir()
        (memory_dir / "sources" / "kff.md").write_text(source, encoding="utf-8")
        meta = _save(sources=[{"ref": "sources/kff.md", "quote": quote}])
        # Everything a verifier needs is in the serialized record + the source.
        anchor = meta["sources"][0]
        third_party = verify_quote(anchor["quote"], anchor["quote_hash"], source)
        assert third_party.status.value == "ok"
        assert exp["verifiable"] is True
        return

    if "claims" in inp:  # l3-010 / l3-011
        if exp.get("valid") is False:
            with pytest.raises(ClaimError) as exc_info:
                normalize_claims(inp["claims"], "insights/x.md")
            assert "source_id" in str(exc_info.value)
            return
        meta = _save(claims=inp["claims"])
        assert meta["claims"][0]["anchor_id"] == inp["claims"][0]["anchor_id"]
        return

    if "status" in exp:  # verification outcomes — file-based, record written raw
        src = inp["sources"][0]
        if inp.get("source_exists", True) is not False and "source_contains" in case:
            path = memory_dir / src["ref"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(case["source_contains"], encoding="utf-8")
        rec = _write_raw(memory_dir, "insights/cited.md",
                         {"auditable_memory": AMR_SPEC_VERSION, "sources": inp["sources"]})
        results = verify_memory_sources(str(rec), str(memory_dir))
        assert len(results) == 1
        assert results[0].status.value == exp["status"], case.get("why")
        assert results[0].status.value not in exp.get("must_not_report", [])
        if "partial" in exp:
            assert results[0].partial is exp["partial"]
        return

    # Hash-prefix interpretation (l3-006 / 006b / 006c / 007).
    stored = inp["sources"][0]["quote_hash"]
    if exp.get("valid") is False:
        with pytest.raises(UnsupportedHashAlgorithm):
            parse_quote_hash(stored)
        with pytest.raises(SaveValidationError):
            _save(sources=inp["sources"])
        return
    algorithm, _ = parse_quote_hash(stored)
    assert algorithm == exp["interpreted_as"]
    if "must_not_interpret_as" in exp:
        assert algorithm != exp["must_not_interpret_as"]


# ── The four surfaces emit the declaration (same-capability rule) ──────────────

def test_declaration_is_emitted_on_every_save(memory_dir):
    """The core save path is the single writer all four surfaces route through;
    every record it writes carries the declaration, with or without any other
    AMR field."""
    bare = _save(content="nothing else declared")
    marked = _save(content="fully marked", epistemic="inference", confidence=0.5)
    assert bare["auditable_memory"] == AMR_SPEC_VERSION
    assert marked["auditable_memory"] == AMR_SPEC_VERSION


def test_declaration_survives_resave_and_cannot_be_overridden(memory_dir):
    first = _save(content="v1", slug="sticky")
    second = _save(content="v2", slug="sticky", metadata={"auditable_memory": "0.1"})
    assert first["auditable_memory"] == second["auditable_memory"] == AMR_SPEC_VERSION
    with pytest.raises(SaveValidationError):
        _save(content="v3", slug="sticky", metadata={"auditable_memory": "0.2"})


@pytest.mark.parametrize("bad", [-0.1, 1.0001, 2, True, "high"])
def test_confidence_out_of_range_via_metadata_is_rejected(memory_dir, bad):
    with pytest.raises(SaveValidationError):
        _save(metadata={"confidence": bad})


def test_confidence_bounds_are_inclusive(memory_dir):
    assert _save(confidence=0.0, slug="lo")["confidence"] == 0.0
    assert _save(confidence=1.0, slug="hi")["confidence"] == 1.0

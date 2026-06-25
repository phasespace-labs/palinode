"""Tests for the source-quote integrity primitive (provenance Q2, #459).

Covers the deterministic core requested in public issue #65 (Q2): a quote anchor
is OK only when (a) its stored hash matches the hash of its stored quote and
(b) the quote still appears in the cited source. Smart-punctuation and
whitespace differences must NOT cause false drift.

Real tmp files only — no mocks (repo rule). The primitive is network-free.
"""
from __future__ import annotations

import pytest

from palinode.core.quote_verify import (
    QuoteStatus,
    normalize_quote,
    quote_hash,
    verify_memory_sources,
    verify_quote,
)


def test_normalize_is_idempotent_and_folds_punctuation():
    raw = "  “Curly”   quotes—and\tspaces  "
    once = normalize_quote(raw)
    assert once == '"Curly" quotes-and spaces'
    assert normalize_quote(once) == once  # idempotent


def test_quote_hash_stable_across_cosmetic_variation():
    a = 'The model “drifts” without—knowing it.'
    b = 'The model "drifts" without-knowing   it.'  # straight quotes, hyphen, extra ws
    assert quote_hash(a) == quote_hash(b)


def test_verify_quote_ok_when_present_and_hash_matches():
    quote = "every claim has a citation"
    src = "Intro. We argue that every claim has a citation. Conclusion."
    r = verify_quote(quote, quote_hash(quote), src)
    assert r.status is QuoteStatus.OK
    assert r.ok


def test_verify_quote_ok_ignores_cosmetic_source_differences():
    quote = '"every claim" has a citation'
    src = "We argue that “every claim”   has a citation."  # curly + extra ws
    r = verify_quote(quote, quote_hash(quote), src)
    assert r.status is QuoteStatus.OK


def test_source_drift_detected():
    quote = "every claim has a citation"
    src = "The source was rewritten and no longer says that."
    r = verify_quote(quote, quote_hash(quote), src)
    assert r.status is QuoteStatus.SOURCE_DRIFTED
    assert not r.ok


def test_anchor_tamper_detected():
    quote = "every claim has a citation"
    src = "every claim has a citation"
    r = verify_quote(quote, "deadbeef" * 4, src)  # wrong stored hash
    assert r.status is QuoteStatus.ANCHOR_TAMPERED


def test_empty_expected_hash_skips_anchor_check_but_still_checks_source():
    quote = "present passage"
    assert verify_quote(quote, "", "a present passage here").status is QuoteStatus.OK
    assert verify_quote(quote, "", "absent").status is QuoteStatus.SOURCE_DRIFTED


# ── verify_memory_sources (file-level) ────────────────────────────────────────

def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_no_sources_is_clean_noop(tmp_path):
    mem = tmp_path / "decisions" / "plain.md"
    _write(mem, "---\ntype: Decision\n---\nNo anchors here.\n")
    assert verify_memory_sources(str(mem), str(tmp_path)) == []


def test_memory_sources_ok_and_missing(tmp_path):
    _write(tmp_path / "research" / "paper.md", "Body: every claim has a citation. End.")
    quote = "every claim has a citation"
    mem = tmp_path / "decisions" / "cited.md"
    _write(
        mem,
        "---\n"
        "type: Decision\n"
        "sources:\n"
        f"  - ref: research/paper.md\n"
        f"    quote: \"{quote}\"\n"
        f"    quote_hash: \"{quote_hash(quote)}\"\n"
        "  - ref: research/gone.md\n"
        "    quote: \"anything\"\n"
        "    quote_hash: \"x\"\n"
        "---\n"
        "Claim body.\n",
    )
    results = verify_memory_sources(str(mem), str(tmp_path))
    assert len(results) == 2
    by_ref = {r.ref: r.status for r in results}
    assert by_ref["research/paper.md"] is QuoteStatus.OK
    assert by_ref["research/gone.md"] is QuoteStatus.SOURCE_MISSING


def test_memory_sources_detects_drift(tmp_path):
    _write(tmp_path / "research" / "paper.md", "The source no longer contains it.")
    quote = "every claim has a citation"
    mem = tmp_path / "decisions" / "drift.md"
    _write(
        mem,
        "---\n"
        "sources:\n"
        f"  - ref: research/paper.md\n"
        f"    quote: \"{quote}\"\n"
        f"    quote_hash: \"{quote_hash(quote)}\"\n"
        "---\n"
        "body\n",
    )
    [r] = verify_memory_sources(str(mem), str(tmp_path))
    assert r.status is QuoteStatus.SOURCE_DRIFTED

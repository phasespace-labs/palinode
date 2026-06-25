"""Source-quote integrity verification (provenance Q2, #459).

The deterministic, offline, network-free core of the quote-drift check
requested in public issue #65 (Q2): given a claim that cites a quote from a
source, confirm the quote (a) is internally consistent with its recorded hash
and (b) still appears in the cited source. This is the analog of yopedia's
Phase C anchor-verifier lint.

Scope boundary: this module is the *primitive* only — pure verification logic
plus a thin file reader over the proposed ``sources:`` anchor shape. It does
NOT define the capture path or commit the live frontmatter schema (that needs
sign-off; see #459). It is intentionally a plain integrity check — verifying
that a cited quote still matches its source involves no cryptographic signing or
attestation, so it has no dependency on the separate memory-attestation work.

Proposed anchor shape (per #459, not yet a captured schema)::

    sources:
      - ref: research/some-paper.md     # path under memory_dir
        quote: "the exact cited passage"
        quote_hash: "<md5 of normalize_quote(quote)>"
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

from palinode.core.hashing import stable_md5_hexdigest
from palinode.core.parser import parse_markdown

# Smart punctuation → ASCII, so a quote copied through a renderer still matches
# the source. Determinism is the whole point: the same logical text must always
# produce the same normalized form and therefore the same hash.
_PUNCT_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
    "…": "...",
}
_PUNCT_RE = re.compile("|".join(re.escape(k) for k in _PUNCT_MAP))
_WS_RE = re.compile(r"\s+")


def normalize_quote(text: str) -> str:
    """Canonicalize a quote for stable hashing and substring matching.

    Folds smart punctuation to ASCII, collapses all whitespace runs to single
    spaces, and strips. Idempotent.
    """
    folded = _PUNCT_RE.sub(lambda m: _PUNCT_MAP[m.group()], text)
    return _WS_RE.sub(" ", folded).strip()


def quote_hash(text: str) -> str:
    """Stable hash of a quote's normalized form (reuses the dedup hasher)."""
    return stable_md5_hexdigest(normalize_quote(text))


class QuoteStatus(str, Enum):
    OK = "ok"
    ANCHOR_TAMPERED = "anchor_tampered"  # stored hash != hash(stored quote)
    SOURCE_DRIFTED = "source_drifted"    # quote no longer present in source
    SOURCE_MISSING = "source_missing"    # cited source file does not exist


@dataclass
class VerifyResult:
    status: QuoteStatus
    ref: str
    expected_hash: str = ""
    actual_hash: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is QuoteStatus.OK


def verify_quote(quote: str, expected_hash: str, source_text: str, ref: str = "") -> VerifyResult:
    """Verify one quote anchor against its source. Pure; no I/O.

    Two independent failure axes:
      - ANCHOR_TAMPERED: the stored ``expected_hash`` does not match the hash of
        the stored ``quote`` — the anchor was edited without re-hashing.
      - SOURCE_DRIFTED: the quote no longer appears in the source — the source
        changed out from under the claim.
    """
    actual = quote_hash(quote)
    if expected_hash and actual != expected_hash:
        return VerifyResult(
            QuoteStatus.ANCHOR_TAMPERED, ref, expected_hash, actual,
            "stored quote_hash does not match hash of stored quote",
        )
    if normalize_quote(quote) not in normalize_quote(source_text):
        return VerifyResult(
            QuoteStatus.SOURCE_DRIFTED, ref, expected_hash, actual,
            "cited quote not found in source",
        )
    return VerifyResult(QuoteStatus.OK, ref, expected_hash, actual)


def verify_memory_sources(file_path: str, memory_dir: str) -> list[VerifyResult]:
    """Verify every ``sources:`` anchor in one memory file.

    Returns ``[]`` when the file carries no ``sources:`` anchors — the check is
    a clean no-op on today's corpus, so it is safe to run before any anchors
    are captured.
    """
    full_path = file_path if os.path.isabs(file_path) else os.path.join(memory_dir, file_path)
    with open(full_path, encoding="utf-8") as f:
        metadata, _ = parse_markdown(f.read())

    sources = metadata.get("sources")
    if not isinstance(sources, list):
        return []

    results: list[VerifyResult] = []
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("ref", ""))
        quote = str(entry.get("quote", ""))
        expected = str(entry.get("quote_hash", ""))
        src_path = os.path.join(memory_dir, ref)
        if not ref or not os.path.exists(src_path):
            results.append(VerifyResult(
                QuoteStatus.SOURCE_MISSING, ref, expected, "",
                f"cited source not found: {ref or '(empty ref)'}",
            ))
            continue
        with open(src_path, encoding="utf-8") as sf:
            results.append(verify_quote(quote, expected, sf.read(), ref))
    return results

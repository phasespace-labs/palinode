import pytest
import os
import time
import hashlib
from palinode.core.store import check_freshness
from palinode.core import parser as _parser
from palinode.core.config import config

def test_fresh_result_marked_valid(tmp_path, monkeypatch):
    """File unchanged since indexing → freshness: valid"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = "---\nid: test\n---\nHello"
    file_path = "test_valid.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)

    # Hash the body only (below frontmatter), matching what check_freshness does
    body_hash = hashlib.sha256("Hello".encode()).hexdigest()[:16]
    results = [{"file_path": file_path, "metadata": {"content_hash": body_hash}}]

    checked = check_freshness(results)
    assert checked[0]["freshness"] == "valid"

def test_modified_file_marked_stale(tmp_path, monkeypatch):
    """File changed after indexing → freshness: stale"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = "---\n---\nHello"
    file_path = "test_stale.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)
    
    db_hash = "wrong1234567890a"
    results = [{"file_path": file_path, "metadata": {"content_hash": db_hash}}]
    
    checked = check_freshness(results)
    assert checked[0]["freshness"] == "stale"

def test_missing_hash_marked_unknown(tmp_path, monkeypatch):
    """Old memories without content_hash → freshness: unknown"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = "---\n---\nHello"
    file_path = "test_unknown.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)
    
    results = [{"file_path": file_path, "metadata": {}}] # No content_hash
    checked = check_freshness(results)
    assert checked[0]["freshness"] == "unknown"

def test_deleted_file_marked_stale(tmp_path, monkeypatch):
    """Source file deleted → freshness: stale"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    file_path = "test_deleted.md"
    # Do not create file
    results = [{"file_path": file_path, "metadata": {"content_hash": "somehash"}}]
    checked = check_freshness(results)
    assert checked[0]["freshness"] == "stale"

def test_freshness_check_performance(tmp_path, monkeypatch):
    """100 results checked in <50ms (just file reads + hash)"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    results = []
    for i in range(100):
        file_path = f"test_perf_{i}.md"
        content = f"Test content {i}"
        (tmp_path / file_path).write_text(content)
        current_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        results.append({"file_path": file_path, "metadata": {"content_hash": current_hash}})

    start = time.time()
    checked = check_freshness(results)
    duration = time.time() - start

    assert duration < 0.05  # <50ms
    assert len(checked) == 100
    assert all(r["freshness"] == "valid" for r in checked)


# ---------------------------------------------------------------------------
# Issue multi-section files must compare per-section hashes
# ---------------------------------------------------------------------------

def _make_multisection_content() -> str:
    """Return a >2000-char markdown body with two named sections so the parser
    splits it into multiple chunks instead of keeping it as a single root."""
    # Use >2000 chars total body to force section splitting.
    filler = "x" * 800
    return (
        "---\n"
        "id: multi-section-test\n"
        "category: insights\n"
        "type: Insight\n"
        "---\n\n"
        f"Preamble text that is long enough to form its own root chunk. {filler}\n\n"
        f"## Section Alpha\n\nContent of alpha section. {filler}\n\n"
        f"## Section Beta\n\nContent of beta section. {filler}\n"
    )


def test_multisection_fresh_file_marked_valid(tmp_path, monkeypatch):
    """Multi-section file just indexed → every chunk must report freshness: valid.

    Pre-fix behaviour: check_freshness hashed the whole body and compared
    it to the per-section content_hash → mismatch → all chunks stale (#203).
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = _make_multisection_content()
    file_path = "multi_fresh.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)

    # Parse the file the same way the indexer does so we get the real section
    # content strings and can compute the expected per-section hashes.
    _, sections = _parser.parse_markdown(content)
    assert len(sections) > 1, "test file did not split into multiple sections"

    results = [
        {
            "file_path": file_path,
            "section_id": sec["section_id"],
            "content_hash": hashlib.sha256(sec["content"].encode()).hexdigest(),
        }
        for sec in sections
    ]

    checked = check_freshness(results)
    for r in checked:
        assert r["freshness"] == "valid", (
            f"section {r['section_id']!r} reported {r['freshness']!r} but should be valid (#203)"
        )


def test_multisection_modified_section_marked_stale(tmp_path, monkeypatch):
    """Modifying one section of a multi-section file → that chunk reports stale,
    unmodified chunks remain valid."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = _make_multisection_content()
    file_path = "multi_modify.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)

    _, sections = _parser.parse_markdown(content)
    assert len(sections) > 1

    # Compute hashes from the *original* content (simulates what the indexer stored).
    original_hashes = {
        sec["section_id"]: hashlib.sha256(sec["content"].encode()).hexdigest()
        for sec in sections
    }

    # Modify the file on disk — change Section Alpha's content.
    modified_content = content.replace(
        "Content of alpha section.", "Content of alpha section MODIFIED."
    )
    full_path.write_text(modified_content)

    results = [
        {
            "file_path": file_path,
            "section_id": sec_id,
            "content_hash": orig_hash,
        }
        for sec_id, orig_hash in original_hashes.items()
    ]

    checked = check_freshness(results)
    freshness_by_id = {r["section_id"]: r["freshness"] for r in checked}

    # The alpha section was changed — it must be stale.
    alpha_slug = next(
        sec["section_id"] for sec in sections if "alpha" in sec["section_id"].lower()
    )
    assert freshness_by_id[alpha_slug] == "stale", (
        f"Modified section {alpha_slug!r} should be stale but got {freshness_by_id[alpha_slug]!r}"
    )

    # Unmodified sections should still be valid.
    for sec in sections:
        sid = sec["section_id"]
        if sid != alpha_slug:
            assert freshness_by_id[sid] == "valid", (
                f"Unmodified section {sid!r} should be valid but got {freshness_by_id[sid]!r}"
            )

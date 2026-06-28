"""Tests for #73 — mechanical untyped cross-linking (`cross_refs`).

Covers the pure matchers (detect_refs / build_registry), then the file-mutating
orchestrator (update_file_cross_refs) on a real tmp memory_dir with git off and
the embedder untouched (no indexing happens here — we call the hook directly).
"""
from __future__ import annotations

import frontmatter
import pytest

from palinode.core import cross_refs
from palinode.core.config import config


# ── pure matcher: detect_refs ────────────────────────────────────────────────

def _reg(**entries):
    """Build a registry dict {ref: {slug, title}} from kwargs ref->title."""
    out = {}
    for ref, title in entries.items():
        slug = ref.split("/")[-1]
        out[ref] = {"slug": slug, "title": title}
    return out


def test_detect_matches_full_path_ref():
    reg = _reg(**{"decisions/drop-legacy-browser": ""})
    body = "As recorded in decisions/drop-legacy-browser, we moved on."
    assert cross_refs.detect_refs(body, reg) == ["decisions/drop-legacy-browser"]


def test_detect_matches_hyphenated_slug():
    reg = _reg(**{"decisions/drop-legacy-browser": ""})
    body = "We finally did drop-legacy-browser last week."
    assert cross_refs.detect_refs(body, reg) == ["decisions/drop-legacy-browser"]


def test_detect_matches_distinctive_title():
    reg = _reg(**{"decisions/dlb": "Drop Legacy Browser"})
    body = "The Drop Legacy Browser call was unpopular."
    assert cross_refs.detect_refs(body, reg) == ["decisions/dlb"]


def test_detect_skips_short_generic_slug():
    """A bare short slug like `api` must NOT match inside prose."""
    reg = _reg(**{"projects/api": ""})
    body = "The api responded quickly and rapidly."
    assert cross_refs.detect_refs(body, reg) == []


def test_detect_skips_stopword_title():
    reg = _reg(**{"insights/x": "Status"})
    body = "The status of the build is green."
    assert cross_refs.detect_refs(body, reg) == []


def test_detect_whole_token_no_substring_false_positive():
    reg = _reg(**{"projects/log": "Log"})
    body = "We use structured logging throughout the blog."
    # 'log' is short+generic (slug skipped, title is a stopword) → no match,
    # and even a longer token must not match as a substring of 'blog'/'logging'.
    assert cross_refs.detect_refs(body, reg) == []


def test_detect_multiple_and_sorted():
    reg = _reg(**{
        "decisions/drop-legacy-browser": "",
        "insights/embed-host-choice": "",
    })
    body = "embed-host-choice informed decisions/drop-legacy-browser."
    assert cross_refs.detect_refs(body, reg) == [
        "decisions/drop-legacy-browser", "insights/embed-host-choice",
    ]


# ── build_registry ───────────────────────────────────────────────────────────

def _write(path, title, body="body text"):
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, title=title, category=path.parent.name)
    path.write_text(frontmatter.dumps(post) + "\n")


def test_build_registry_excludes_self_and_skip_dirs(tmp_path):
    _write(tmp_path / "decisions" / "a.md", "Alpha Decision")
    _write(tmp_path / "insights" / "b.md", "Beta Insight")
    _write(tmp_path / "daily" / "2026-06-28.md", "A Daily Note")  # skip dir

    reg = cross_refs.build_registry(str(tmp_path), exclude_ref="decisions/a")
    assert set(reg.keys()) == {"insights/b"}
    assert reg["insights/b"]["title"] == "Beta Insight"


# ── update_file_cross_refs (file mutation) ───────────────────────────────────

@pytest.fixture()
def memdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", True)
    monkeypatch.setattr(config.capture.cross_refs, "min_token_len", 6)
    return tmp_path


def _meta(path):
    return frontmatter.load(str(path)).metadata


def test_update_writes_cross_refs_when_mentioned(memdir):
    _write(memdir / "decisions" / "drop-legacy-browser.md", "Drop Legacy Browser")
    src = memdir / "insights" / "why.md"
    _write(src, "Why We Dropped It",
           body="This follows from decisions/drop-legacy-browser.")

    res = cross_refs.update_file_cross_refs(str(src))
    assert res["changed"] is True
    assert res["refs"] == ["decisions/drop-legacy-browser"]
    assert _meta(src)["cross_refs"] == ["decisions/drop-legacy-browser"]


def test_update_is_idempotent(memdir):
    _write(memdir / "decisions" / "drop-legacy-browser.md", "Drop Legacy Browser")
    src = memdir / "insights" / "why.md"
    _write(src, "Why", body="see decisions/drop-legacy-browser")

    first = cross_refs.update_file_cross_refs(str(src))
    assert first["changed"] is True
    # Second pass over the now-updated file must NOT rewrite (loop terminates).
    second = cross_refs.update_file_cross_refs(str(src))
    assert second["changed"] is False
    assert second["refs"] == ["decisions/drop-legacy-browser"]


def test_update_removes_stale_cross_refs(memdir):
    src = memdir / "insights" / "why.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post("No mentions here at all.", title="Why",
                            category="insights",
                            cross_refs=["decisions/gone"])
    src.write_text(frontmatter.dumps(post) + "\n")

    res = cross_refs.update_file_cross_refs(str(src))
    assert res["changed"] is True
    assert res["refs"] == []
    assert "cross_refs" not in _meta(src)


def test_update_no_write_when_nothing_found_and_none_existing(memdir):
    src = memdir / "insights" / "lonely.md"
    _write(src, "Lonely", body="A memory that mentions nobody.")
    res = cross_refs.update_file_cross_refs(str(src))
    assert res["changed"] is False
    assert res["refs"] == []
    assert "cross_refs" not in _meta(src)


def test_update_respects_disabled_config(memdir, monkeypatch):
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    _write(memdir / "decisions" / "drop-legacy-browser.md", "Drop Legacy Browser")
    src = memdir / "insights" / "why.md"
    _write(src, "Why", body="see decisions/drop-legacy-browser")
    res = cross_refs.update_file_cross_refs(str(src))
    assert res["changed"] is False
    assert "cross_refs" not in _meta(src)


def test_update_excludes_self_reference(memdir):
    """A memory that mentions its own ref/title doesn't cross-link to itself."""
    src = memdir / "insights" / "self-aware.md"
    _write(src, "Self Aware",
           body="This is insights/self-aware talking about Self Aware.")
    res = cross_refs.update_file_cross_refs(str(src))
    assert res["refs"] == []
    assert res["changed"] is False

"""The whole-tree frontmatter readers keep their exact behaviour while
parsing each frontmatter block once (perf work on the O(N²) cross-refs
registry and the per-call re-glob-and-parse in ``/list`` and the session
digest).

Two layers, both checked against the *reference* behaviour rather than
against a hand-written expectation:

- ``parser.parse_frontmatter`` must return, for any input, what
  ``frontmatter.loads`` / ``parser.parse_markdown`` would — including the
  degenerate inputs (no frontmatter, leading blank line, ``----`` boundary,
  unclosed block, invalid YAML, scalar YAML, colliding / non-string keys,
  JSON frontmatter, CRLF).
- ``build_registry``, ``collect_memory_files`` and ``_scan_memories`` must
  produce identical results (values *and* ordering) to the pre-cache path on a
  fixture corpus that includes those degenerate files. The pre-cache path is
  reproduced by swapping in the reference parser, so the comparison is old
  code vs new code on the same tree, not new code vs itself.
"""
from __future__ import annotations

import glob
import os
import random
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from palinode.core import context_prime, cross_refs, parser
from palinode.core.config import config


def _reference_parse(content: str) -> tuple[dict[str, Any], str]:
    """What every reader did before: ``frontmatter.loads`` with the
    ``parse_markdown`` fallback."""
    try:
        post = frontmatter.loads(content)
        return post.metadata, post.content
    except Exception:
        return {}, content


DEGENERATE: dict[str, str] = {
    "plain": "# No frontmatter\n\nbody\n",
    "leading-blank": "\n---\ntitle: Hidden\n---\nbody\n",
    "long-dashes": "----\ntitle: Four Dashes\n----\nbody\n",
    "unclosed": "---\ntitle: Never Closed\nbody\n",
    "invalid-yaml": "---\ntitle: [unclosed\n---\nbody\n",
    "scalar-yaml": "---\njust a string\n---\nbody\n",
    "content-key": "---\ncontent: collides\ntitle: X\n---\nbody\n",
    "int-key": "---\n1: one\ntitle: X\n---\nbody\n",
    "json": '{\n"title": "JSON Front"\n}\nbody\n',
    "crlf": "---\r\ntitle: CRLF\r\ncore: true\r\n---\r\nbody line\r\n",
    "empty": "",
    "only-fm": "---\ntitle: Only\n---\n",
    "dates": "---\ntitle: Dates\ncreated_at: 2026-01-02\nlast_updated: 2026-01-02T03:04:05Z\nentities:\n  - a\n  - b\n---\nbody\n",
    "nested": "---\ntitle: Nested\nmeta:\n  kind: telemetry\n  tags: [x, y]\n---\nbody\n",
}


@pytest.mark.parametrize("name", sorted(DEGENERATE))
def test_parse_frontmatter_matches_reference_on_degenerate_inputs(name: str) -> None:
    content = DEGENERATE[name]
    assert parser.parse_frontmatter(content) == _reference_parse(content)
    assert parser.parse_frontmatter(content)[0] == parser.parse_markdown(content)[0]


def test_parse_frontmatter_returns_a_private_copy() -> None:
    content = DEGENERATE["dates"]
    first, _ = parser.parse_frontmatter(content)
    first["entities"].append("mutated")
    first["title"] = "mutated"
    second, _ = parser.parse_frontmatter(content)
    assert second == _reference_parse(content)[0]


def test_parse_frontmatter_caches_by_block_not_by_body() -> None:
    parser._load_frontmatter_block.cache_clear()
    parser.parse_frontmatter("---\ntitle: Same\n---\nbody one\n")
    parser.parse_frontmatter("---\ntitle: Same\n---\nbody two, longer\n")
    info = parser._load_frontmatter_block.cache_info()
    assert (info.hits, info.misses) == (1, 1)
    parser.parse_frontmatter("---\ntitle: Different\n---\nbody one\n")
    assert parser._load_frontmatter_block.cache_info().misses == 2


# ── fixture corpus ───────────────────────────────────────────────────────────

def _build_corpus(root: Path, n: int = 120, seed: int = 3) -> list[Path]:
    rnd = random.Random(seed)
    cats = ["decisions", "insights", "projects", "people", "research", "inbox", "daily"]
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet".split()
    refs = [f"{cats[i % len(cats)]}/{rnd.choice(words)}-{rnd.choice(words)}-{i}" for i in range(n)]
    paths: list[Path] = []
    for i, ref in enumerate(refs):
        cat, slug = ref.split("/")
        title = f"{rnd.choice(words).title()} {rnd.choice(words).title()} {i}"
        mentions = " ".join(rnd.choice(refs) for _ in range(3))
        body = " ".join(rnd.choice(words) for _ in range(40))
        visibility = "private" if i % 17 == 0 else "inherited"
        content = (
            "---\n"
            f"title: {title}\n"
            f"type: Decision\n"
            f"core: {'true' if i % 10 == 0 else 'false'}\n"
            f"visibility: {visibility}\n"
            f"created_at: 2026-0{1 + i % 8}-{1 + i % 27:02d}T10:00:00Z\n"
            f"last_updated: 2026-0{1 + i % 8}-{1 + i % 27:02d}T12:{i % 60:02d}:00Z\n"
            f"description: {body[:40]}\n"
            f"entities:\n  - project/{rnd.choice(words)}\n"
            "---\n"
            f"# {title}\n\n{body}\n\nSee {mentions}.\n"
        )
        p = root / cat / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        paths.append(p)
    for name, content in DEGENERATE.items():
        p = root / "insights" / f"degenerate-{name}.md"
        p.write_text(content, newline="")
        paths.append(p)
    (root / "decisions" / "old-history.md").write_text("---\ntitle: History\n---\nold\n")
    return paths


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", True)
    parser._load_frontmatter_block.cache_clear()
    cross_refs._registry_cache.pop(str(tmp_path), None)
    return _build_corpus(tmp_path)


def _reference_registry(memory_dir: str, exclude_ref: str | None) -> dict[str, dict[str, str]]:
    """``build_registry`` as it was before the stamp cache: glob + parse every file."""
    registry: dict[str, dict[str, str]] = {}
    for filepath in glob.glob(os.path.join(memory_dir, "**", "*.md"), recursive=True):
        rel = os.path.relpath(filepath, memory_dir)
        parts = rel.split(os.sep)
        if parts[0] in cross_refs.SKIP_DIRS:
            continue
        ref = cross_refs.path_to_ref(rel)
        if exclude_ref is not None and ref == exclude_ref:
            continue
        slug = parts[-1][:-3] if parts[-1].endswith(".md") else parts[-1]
        title = ""
        try:
            meta = frontmatter.load(filepath).metadata
            title = str(meta.get("title") or meta.get("name") or "").strip()
        except Exception:
            pass
        registry[ref] = {"slug": slug, "title": title}
    return registry


def test_build_registry_matches_reference_cold_and_warm(corpus: list[Path]) -> None:
    root = config.memory_dir
    expected = _reference_registry(root, "decisions/nothing")
    cold = cross_refs.build_registry(root, exclude_ref="decisions/nothing")
    warm = cross_refs.build_registry(root, exclude_ref="decisions/nothing")
    assert cold == expected
    assert warm == expected
    assert list(warm) == list(expected)
    self_ref = cross_refs.path_to_ref(os.path.relpath(str(corpus[0]), root))
    assert cross_refs.build_registry(root, exclude_ref=self_ref) == _reference_registry(root, self_ref)


def test_build_registry_sees_added_changed_and_removed_files(corpus: list[Path]) -> None:
    root = config.memory_dir
    cross_refs.build_registry(root)

    added = Path(root) / "decisions" / "brand-new.md"
    added.write_text("---\ntitle: Brand New Title\n---\nbody\n")
    changed = corpus[3]
    changed.write_text("---\ntitle: A Much Longer Retitled Memory\n---\nnew body\n")
    removed = corpus[5]
    removed.unlink()

    assert cross_refs.build_registry(root) == _reference_registry(root, None)
    reg = cross_refs.build_registry(root)
    assert reg["decisions/brand-new"]["title"] == "Brand New Title"
    assert reg[cross_refs.path_to_ref(os.path.relpath(str(changed), root))]["title"] == "A Much Longer Retitled Memory"
    assert cross_refs.path_to_ref(os.path.relpath(str(removed), root)) not in reg


def test_reindex_parses_each_frontmatter_block_about_once(
    corpus: list[Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N ``update_file_cross_refs`` calls used to parse N files each (N²).
    With the stamp cache the registry parses each file once, and the only
    per-call parses are the scanned file itself and whatever it rewrote."""
    calls: list[str] = []
    real = parser._load_frontmatter_block.__wrapped__

    def counting(handler_cls: type, block: str):
        calls.append(block)
        return real(handler_cls, block)

    parser._load_frontmatter_block.cache_clear()
    monkeypatch.setattr(parser, "_load_frontmatter_block", counting)
    linkable = [p for p in corpus if p.relative_to(config.memory_dir).parts[0] not in cross_refs.SKIP_DIRS]
    for p in linkable:
        cross_refs.update_file_cross_refs(str(p))
    n = len(linkable)
    # Registry: one parse per file, once. Per call: the file's rewrite (if any)
    # changes its stamp, so it is re-read once more on the next call. Bound is
    # linear; the old path was n*n.
    assert len(calls) <= 3 * n + 20, (len(calls), n)


def test_update_file_cross_refs_results_match_reference_registry(corpus: list[Path]) -> None:
    from palinode.core.scope import ScopeChain, other_project, project_entities

    root = config.memory_dir
    for p in corpus:
        rel = os.path.relpath(str(p), root)
        if rel.split(os.sep)[0] in cross_refs.SKIP_DIRS:
            continue
        self_ref = cross_refs.path_to_ref(rel)
        try:
            post = frontmatter.load(str(p))
            body = post.content
        except Exception:
            expected: list[str] = []  # unparseable source: no refs, error reported
        else:
            expected = cross_refs.detect_refs(
                body, _reference_registry(root, self_ref),
                min_token_len=config.capture.cross_refs.min_token_len,
            )
            projects = project_entities(post.metadata)
            if projects:
                expected = [ref for ref in expected if any(
                    not other_project(ScopeChain(project=project), frontmatter.load(
                        os.path.join(root, ref + ".md"),
                    ).metadata)
                    for project in projects
                )]
        assert cross_refs.update_file_cross_refs(str(p))["refs"] == expected


def test_detect_refs_prefilter_matches_regex_semantics() -> None:
    reg = {
        "decisions/foo": {"slug": "foo", "title": "Foo Bar"},
        "decisions/foo-bar": {"slug": "foo-bar", "title": ""},
        "insights/rapid": {"slug": "rapid", "title": "Rapidly Growing"},
    }
    body = "foo-bar happened; Foo Bar too. rapidly is not rapid."
    # "rapid" is too short to be a slug candidate; "foo" alone never matches
    # inside "foo-bar" but "Foo Bar" (title) does.
    assert cross_refs.detect_refs(body, reg) == ["decisions/foo", "decisions/foo-bar"]
    assert cross_refs.detect_refs("it was rapidly growing", reg) == ["insights/rapid"]
    assert cross_refs.detect_refs("nothing here", reg) == []
    assert cross_refs.detect_refs("rapidly", reg) == []
    assert cross_refs.detect_refs("foo-barred", reg) == []


def _with_reference_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parser, "parse_frontmatter", _reference_parse)


def test_collect_memory_files_matches_reference(corpus: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from palinode.api.routers import memory as mem

    new = mem.collect_memory_files()
    new_core = mem.collect_memory_files(core_only=True)
    new_cat = mem.collect_memory_files(category="decisions")
    with pytest.MonkeyPatch.context() as mp:
        _with_reference_parser(mp)
        old = mem.collect_memory_files()
        old_core = mem.collect_memory_files(core_only=True)
        old_cat = mem.collect_memory_files(category="decisions")
    assert new == old and new_core == old_core and new_cat == old_cat
    assert len(new) > 50
    assert [r["file"] for r in new] == [r["file"] for r in old]


def test_scan_memories_matches_reference(corpus: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root = config.memory_dir
    new = context_prime._scan_memories(root)
    with pytest.MonkeyPatch.context() as mp:
        _with_reference_parser(mp)
        old = context_prime._scan_memories(root)
    assert new == old
    assert [e["file"] for e in new] == [e["file"] for e in old]
    assert len(new) > 50


def test_context_digest_matches_reference(corpus: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    new = context_prime.build_context_digest(project="alpha")
    with pytest.MonkeyPatch.context() as mp:
        _with_reference_parser(mp)
        old = context_prime.build_context_digest(project="alpha")
    assert new == old

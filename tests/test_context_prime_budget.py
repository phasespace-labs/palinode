"""The session-start digest under an injection budget.

The digest's ``MAX_*`` count bounds and ``MAX_LINE_CHARS`` decide what is
*selected*; the budget decides what that selection may cost once rendered. This
pins the join between them, inspected **after all formatting** — the string a
harness actually receives, not the rows behind it:

* budget unset → byte-identical to the unbudgeted rendering;
* a resolved row may be demoted to its gist-plus-pointer form;
* a contested row is kept whole or replaced by the explicit contested-partial
  stub, never rendered as one settled side;
* an insufficiency-qualified row keeps its qualifier or is not shown at all;
* startup and per-turn caps are independent knobs.

Real frontmatter files under ``tmp_path``; no DB (the digest is
frontmatter-only by design).
"""
from __future__ import annotations

from typing import Any

import pytest
import yaml

from palinode.core.config import config
from palinode.core.context_prime import (
    build_context_digest,
    format_context_digest,
)
from palinode.core.packing import SURFACE_PER_TURN, SURFACE_STARTUP, budget_from_config


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.context, "auto_detect", True)
    return tmp_path


def _budget(monkeypatch, *, chars: int = 0, tokens: int = 0) -> None:
    monkeypatch.setattr(config.context, "injection_max_chars", chars)
    monkeypatch.setattr(config.context, "injection_max_tokens", tokens)


def _seed(memory_dir, rel: str, meta: dict[str, Any], body: str = "body") -> None:
    path = memory_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(meta)}---\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def three_outcomes(memory_dir):
    """One resolved row, one contested pair, one insufficient-evidence row."""
    _seed(memory_dir, "decisions/resolved.md", {
        "type": "Decision", "entities": ["project/demo"],
        "title": "Ship on Postgres",
        "description": "Chosen after the load test; migrations are written",
    })
    _seed(memory_dir, "decisions/contested-a.md", {
        "type": "Decision", "entities": ["project/demo"],
        "title": "Use endpoint A",
        "description": "The client already speaks it",
        "contradicts": ["decisions/contested-b"],
    })
    _seed(memory_dir, "decisions/contested-b.md", {
        "type": "Decision", "entities": ["project/demo"],
        "title": "Use endpoint B",
        "description": "A is deprecated upstream",
        "contradicts": ["decisions/contested-a"],
    })
    _seed(memory_dir, "insights/unsure.md", {
        "type": "Insight", "core": True,
        "title": "Cache maybe cold on deploy",
        "description": "Seen once, never reproduced",
        "epistemic": "open_question",
    })
    return memory_dir


# ── unset budget: byte-identical to the unbudgeted digest ────────────────────


def test_unset_budget_renders_exactly_the_unbudgeted_digest(three_outcomes, monkeypatch):
    """Snapshot: with both caps at 0 the rendering is the pre-budget one."""
    _budget(monkeypatch, chars=0, tokens=0)
    digest = build_context_digest(project="demo")
    text = format_context_digest(digest)

    assert text == "\n".join([
        "## Session context: project/demo (explicit; known)",
        "### Core memories",
        "- [insights/unsure.md] Cache maybe cold on deploy — Seen once, never "
        "reproduced [epistemic: open_question]",
        "### Recent decisions",
        "- [decisions/contested-b.md] Use endpoint B — A is deprecated upstream "
        "[⚠ contradicts: decisions/contested-a]",
        "- [decisions/contested-a.md] Use endpoint A — The client already speaks it "
        "[⚠ contradicts: decisions/contested-b]",
        "- [decisions/resolved.md] Ship on Postgres — Chosen after the load test; "
        "migrations are written",
        "",
        digest["_palinode_hint"],
    ])
    assert "_budget" not in digest
    assert "_contested_partial" not in digest


def test_unset_budget_leaves_no_budget_keys_in_the_json(three_outcomes, monkeypatch):
    _budget(monkeypatch, chars=0, tokens=0)
    digest = build_context_digest(project="demo")
    assert set(digest) == {
        "project", "project_resolved_by", "project_known", "core_memories", "recent_decisions",
        "open_action_items", "recent_snapshots", "_palinode_hint",
        "other_projects_withheld",
    }


# ── a generous budget changes nothing ────────────────────────────────────────


def test_a_budget_nobody_hits_is_transparent(three_outcomes, monkeypatch):
    _budget(monkeypatch, chars=0, tokens=0)
    unbudgeted = format_context_digest(build_context_digest(project="demo"))
    _budget(monkeypatch, chars=60000, tokens=15000)
    digest = build_context_digest(project="demo")
    assert format_context_digest(digest) == unbudgeted
    assert digest["_budget"]["omitted"] == 0
    assert digest["_budget"]["demoted"] == 0
    assert digest["_budget"]["truncated_reason"] is None


def test_the_default_budget_does_not_trim_an_ordinary_digest(three_outcomes):
    digest = build_context_digest(project="demo")
    assert digest["_budget"]["omitted"] == 0
    assert digest["_budget"]["demoted"] == 0
    assert digest["_budget"]["chars"] <= config.context.injection_max_chars


# ── a tight budget: honest, never settled-looking ────────────────────────────


def _rendered_under(monkeypatch, chars: int, project: str = "demo") -> str:
    _budget(monkeypatch, chars=chars, tokens=0)
    return format_context_digest(build_context_digest(project=project))


def test_a_tight_cap_holds_the_rendered_payload(three_outcomes, monkeypatch):
    text = _rendered_under(monkeypatch, 420)
    assert len(text) <= 420


# The smallest cap includes the resolution label plus the conflict stub.
@pytest.mark.parametrize("cap", [400, 420, 480, 540, 600, 700, 900])
def test_a_contested_row_is_never_rendered_as_one_settled_side(
    three_outcomes, monkeypatch, cap
):
    """Across every cap, either both sides are present or the stub says so."""
    text = _rendered_under(monkeypatch, cap)
    sides = [
        "decisions/contested-a.md" in text,
        "decisions/contested-b.md" in text,
    ]
    if any(sides):
        # A side that survived must still carry its contradiction qualifier —
        # that is what keeps the reader from taking it as settled.
        for line in text.splitlines():
            if line.startswith("- ") and "contested-" in line:
                assert "⚠ contradicts:" in line
    if not all(sides):
        assert "conflict" in text and "omitted for budget" in text


def test_the_contested_partial_stub_keeps_its_source_pointers(
    three_outcomes, monkeypatch
):
    text = _rendered_under(monkeypatch, 420)
    stub = next(ln for ln in text.splitlines() if "omitted for budget" in ln)
    assert stub.startswith("⚠ ")
    # Each contested record is named once, whichever spelling reached the stub.
    assert "decisions/contested-a" in stub
    assert "decisions/contested-b" in stub


def test_a_resolved_row_is_demoted_to_gist_and_pointer_before_being_dropped(
    three_outcomes, monkeypatch
):
    _budget(monkeypatch, chars=430, tokens=0)
    digest = build_context_digest(project="demo")
    text = format_context_digest(digest)
    if "decisions/resolved.md" in text:
        line = next(ln for ln in text.splitlines() if "resolved.md" in ln)
        assert line == "- [decisions/resolved.md] Ship on Postgres"
        assert digest["_budget"]["demoted"] >= 1


def test_an_unverified_row_keeps_its_epistemic_marker_or_is_not_shown(
    three_outcomes, monkeypatch
):
    for cap in (380, 440, 500, 560):
        text = _rendered_under(monkeypatch, cap)
        if "insights/unsure.md" in text:
            line = next(ln for ln in text.splitlines() if "unsure.md" in ln)
            assert "epistemic: open_question" in line


def test_omission_is_reported_in_the_json(three_outcomes, monkeypatch):
    _budget(monkeypatch, chars=300, tokens=0)
    digest = build_context_digest(project="demo")
    report = digest["_budget"]
    assert report["max_chars"] == 300
    assert report["omitted"] >= 1
    assert "omitted for budget" in report["truncated_reason"]


def test_the_token_cap_bites_on_its_own(three_outcomes, monkeypatch):
    _budget(monkeypatch, chars=0, tokens=80)
    digest = build_context_digest(project="demo")
    assert digest["_budget"]["tokens"] <= 80
    assert digest["_budget"]["omitted"] + digest["_budget"]["demoted"] >= 1


def test_a_budget_that_fits_nothing_says_so_instead_of_claiming_an_empty_store(
    three_outcomes, monkeypatch
):
    text = _rendered_under(monkeypatch, 320)
    assert text.splitlines()[0] == "## Session context: project/demo (explicit; known)"
    assert "(no memories in scope yet)" not in text
    assert "⚠ 4 memories withheld for budget" in text
    assert "palinode_search" in text  # the memory contract survives


def test_an_actually_empty_scope_still_says_so(memory_dir, monkeypatch):
    _budget(monkeypatch, chars=400, tokens=0)
    text = format_context_digest(build_context_digest(project="demo"))
    assert "(no memories in scope yet)" in text


# ── the two surfaces are budgeted separately ─────────────────────────────────


def test_startup_and_per_turn_caps_are_independent(three_outcomes, monkeypatch):
    monkeypatch.setattr(config.context, "injection_max_chars", 300)
    monkeypatch.setattr(config.context, "injection_max_tokens", 0)
    monkeypatch.setattr(config.context, "recall_max_chars", 9000)
    monkeypatch.setattr(config.context, "recall_max_tokens", 0)

    assert budget_from_config(SURFACE_STARTUP).max_chars == 300
    assert budget_from_config(SURFACE_PER_TURN).max_chars == 9000
    # The startup digest is bounded by the startup key alone.
    digest = build_context_digest(project="demo")
    assert digest["_budget"]["max_chars"] == 300


def test_relaxing_the_per_turn_cap_does_not_relax_the_startup_digest(
    three_outcomes, monkeypatch
):
    _budget(monkeypatch, chars=300, tokens=0)
    tight = format_context_digest(build_context_digest(project="demo"))
    monkeypatch.setattr(config.context, "recall_max_chars", 100000)
    assert format_context_digest(build_context_digest(project="demo")) == tight

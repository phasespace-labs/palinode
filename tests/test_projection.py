"""The current-text projection is pure, narrow, versioned and idempotent.

It removes exactly what the executor's own retirement renderings look like —
pinned here against the real ``executor._supersede_fact`` /
``_retract_fact`` output and the real mention-level ``retract`` output — and
nothing else: ordinary strikethrough, malformed markers, fenced code and
inline-code examples are content. Unaffected lines survive byte-for-byte.
"""
from __future__ import annotations

import pytest

from palinode.core.embedding_preprocess import AUTO_FOOTER_MARKER
from palinode.core.lifecycle import RETIRED_MENTION_RE
from palinode.core.projection import PROJECTION_VERSION, Projected, project_current_text

SUPERSEDED = "- ~~Use endpoint A.~~ [superseded 2026-09-12] <!-- fact:endpoint -->\n"
SUCCESSOR = "- Use endpoint B. <!-- fact:supersedes-endpoint -->\n"
RETRACTED = "- ~~The sky is green.~~ [RETRACTED 2026-09-12 — never true] <!-- fact:sky -->\n"
MENTION = "~~Alice lives in Paris~~ [RETRACTED 2026-09-10 r:0badc0de]."


def test_version_constant_is_stamped_on_every_projection():
    # 2 since the auto-footer joined the retirement tombstones.
    assert PROJECTION_VERSION == 2
    out = project_current_text("plain\n")
    assert isinstance(out, Projected)
    assert out.version == PROJECTION_VERSION
    assert out.text == "plain\n" and out.removed == () and not out.changed


def test_mixed_claim_file_keeps_every_unaffected_line_byte_for_byte():
    head = "# Endpoints\n\n- Keep me.   <!-- fact:keep -->\n"
    tail = "\nTrailing prose with  odd   spacing.\n\t- indented item\n"
    out = project_current_text(head + SUPERSEDED + SUCCESSOR + RETRACTED + tail)
    assert out.text == head + SUCCESSOR + tail
    assert out.removed == (SUPERSEDED.rstrip("\n"), RETRACTED.rstrip("\n"))


def test_frontmatter_is_untouched():
    fm = "---\nid: x\nentities:\n- person/alice\nstatus: active\n---\n"
    out = project_current_text(fm + SUPERSEDED + SUCCESSOR)
    assert out.text == fm + SUCCESSOR


def test_indented_and_numbered_tombstones_are_recognised():
    doc = (
        "1. ~~first~~ [superseded 2026-01-01] <!-- fact:a -->\n"
        "  * ~~nested~~ [RETRACTED 2026-01-01] <!-- fact:b -->\n"
        "~~bare line~~ [superseded 2026-01-01]\n"
        "2. current <!-- fact:c -->\n"
    )
    assert project_current_text(doc).text == "2. current <!-- fact:c -->\n"


# ── pinned against the real writers ──────────────────────────────────────────


def test_executor_tombstones_are_projected_out(tmp_path, monkeypatch):
    from palinode.consolidation import executor

    monkeypatch.setattr("palinode.core.config.config.git.auto_commit", False)
    target = tmp_path / "projects" / "demo.md"
    target.parent.mkdir()
    body = (
        "- Use endpoint A. <!-- fact:endpoint -->\n"
        "- The sky is green. <!-- fact:sky -->\n"
        "- Deploys go through CI. <!-- fact:ci -->\n"
    )
    revised = executor._supersede_fact(
        body, "endpoint", "Use endpoint B.", "Explicit replacement", str(target)
    )
    revised = executor._retract_fact(revised, "sky", "never true", str(target))
    assert "~~Use endpoint A.~~" in revised and "~~The sky is green.~~" in revised

    out = project_current_text(revised)
    assert out.text == (
        "- Use endpoint B. <!-- fact:supersedes-endpoint -->\n"
        "- Deploys go through CI. <!-- fact:ci -->\n"
    )
    assert len(out.removed) == 2


def test_mention_level_strike_removes_the_span_and_keeps_the_sentence():
    """The ``r:<id>`` form, pinned against ``consolidation.retract``'s writer.

    A struck mention inside an otherwise-current paragraph or bullet is a
    span, not a line: the sentences around it stay. An item that is nothing
    but the struck mention goes whole.
    """
    from palinode.consolidation.retract import _retract_in_body, retraction_id

    pref = "Alice lives in Paris"
    marker = f"[RETRACTED 2026-09-10 r:{retraction_id(pref)}]."
    body = (
        "Alice is great. Alice lives in Paris. She likes tea.\n"
        "\n"
        "- Alice lives in Paris.\n"
        "- Alice likes tea.\n"
    )
    struck, n = _retract_in_body(
        body, {"alice", "lives", "paris"}, {"paris"}, 2, marker
    )
    assert n == 2
    spans = RETIRED_MENTION_RE.findall(struck)
    assert spans == ["Alice lives in Paris.", "Alice lives in Paris."]

    out = project_current_text(struck)
    assert out.text == (
        "Alice is great. She likes tea.\n"
        "\n"
        "- Alice likes tea.\n"
    )
    assert all(RETIRED_MENTION_RE.fullmatch(r) for r in out.removed)


def test_mention_strike_at_line_start_keeps_the_rest_of_the_line():
    out = project_current_text(f"{MENTION} She likes tea.\n- {MENTION} Still true.\n")
    assert out.text == "She likes tea.\n- Still true.\n"


def test_two_mention_strikes_on_one_line():
    line = f"A. {MENTION} B. {MENTION} C.\n"
    assert project_current_text(line).text == "A. B. C.\n"


# ── what is NOT a tombstone ───────────────────────────────────────────────────


def test_fenced_code_is_content():
    doc = (
        "```markdown\n" + SUPERSEDED + "```\n"
        "~~~\n" + RETRACTED + f"{MENTION}\n" + "~~~\n"
        "```\n~~~\n" + SUPERSEDED + "~~~\n```\n"
    )
    assert project_current_text(doc).text == doc


def test_inline_code_example_is_content():
    doc = (
        "The executor writes `~~old~~ [superseded 2026-09-12]` in place.\n"
        f"A mention looks like `{MENTION}` in prose.\n"
    )
    assert project_current_text(doc).text == doc


@pytest.mark.parametrize("line", [
    "- ~~bad date~~ [superseded 2026-9-1] <!-- fact:m -->",
    "- ~~no bracket~~ superseded 2026-09-01 <!-- fact:m -->",
    "- ~~wrong case~~ [Superseded 2026-09-01] <!-- fact:m -->",
    "- ~~wrong case~~ [retracted 2026-09-01] <!-- fact:m -->",
    "- Not struck [superseded 2026-09-10]",
    "- ~~unterminated [superseded 2026-09-10]",
    "- ~~glued date~~ [RETRACTED 2026-09-10x] <!-- fact:m -->",
    "- ~~short day~~ [RETRACTED 2026-09-1] <!-- fact:m -->",
    # Mention-form near-misses inside a current sentence: wrong id length,
    # missing terminator, missing id.
    "Alice is here. ~~short id~~ [RETRACTED 2026-09-10 r:0bad]. She stays.",
    "Alice is here. ~~no terminator~~ [RETRACTED 2026-09-10 r:0badc0de] She stays.",
    "Alice is here. ~~no id~~ [RETRACTED 2026-09-10]. She stays.",
])
def test_malformed_marker_syntax_stays(line):
    doc = f"{line}\n"
    assert project_current_text(doc).text == doc


def test_whole_line_strike_with_an_odd_tail_is_still_the_recognizers_call():
    """A line that *is* ``~~text~~ [RETRACTED YYYY-MM-DD …]`` is a tombstone
    under the shared recognizer whatever trails the date — the projection
    defers to it rather than growing a stricter grammar of its own."""
    from palinode.core.lifecycle import is_retired_fact_text

    for line in (
        "- ~~short id~~ [RETRACTED 2026-09-10 r:0bad].",
        "- ~~no terminator~~ [RETRACTED 2026-09-10 r:0badc0de]",
    ):
        assert is_retired_fact_text(line[2:])
        assert project_current_text(f"{line}\nnext\n").text == "next\n"


@pytest.mark.parametrize("line", [
    "We ~~thought~~ knew the answer.",
    "~~old idea~~ we decided against this",
    "- ~~done~~ ship the release",
    "> ~~quoted~~ [superseded 2026-09-10]",
])
def test_ordinary_strikethrough_stays(line):
    doc = f"{line}\n"
    assert project_current_text(doc).text == doc


# ── the generated auto-footer ─────────────────────────────────────────────────


FOOTER = (
    "## See also\n"
    f"{AUTO_FOOTER_MARKER}\n"
    "- [[alice-smith]]\n"
    "- [[q3-planning]]\n"
)


def test_auto_footer_is_projected_out_and_the_body_survives():
    body = "# Release\n\nThe batch window is 15 minutes.\n"
    out = project_current_text(body + "\n" + FOOTER)
    assert out.text == body
    assert out.changed
    assert "## See also" in out.removed and AUTO_FOOTER_MARKER in out.removed
    assert "- [[alice-smith]]" in out.removed


def test_a_section_that_is_only_a_footer_projects_to_nothing():
    """The shape ``indexer.reconcile`` turns into *no row at all*."""
    assert project_current_text(FOOTER).text == ""


def test_a_user_written_see_also_without_the_marker_is_content():
    doc = "# Note\n\n## See also\n\n- [[alice-smith]] — she wrote the spec\n"
    assert project_current_text(doc).text == doc


def test_a_fenced_example_of_the_marker_is_content():
    """``strip_auto_footer`` would truncate here; the index must not."""
    doc = (
        "# How the footer works\n\n"
        "```markdown\n"
        "## See also\n"
        f"{AUTO_FOOTER_MARKER}\n"
        "- [[alice-smith]]\n"
        "```\n\n"
        "The marker is the first line of the block.\n"
    )
    assert project_current_text(doc).text == doc


def test_frontmatter_entities_survive_a_footer_projection():
    fm = "---\nid: x\nentities:\n- person/alice-smith\n---\n"
    out = project_current_text(fm + "Body.\n\n" + FOOTER)
    assert out.text == fm + "Body.\n"


# ── determinism ───────────────────────────────────────────────────────────────


def test_projection_is_deterministic_and_idempotent():
    doc = (
        "---\nstatus: active\n---\n\n# T\n\n"
        + SUPERSEDED + SUCCESSOR + RETRACTED
        + f"Alice is great. {MENTION} She likes tea.\n"
        + "```\n" + SUPERSEDED + "```\n"
    )
    once = project_current_text(doc)
    assert project_current_text(doc) == once
    twice = project_current_text(once.text)
    assert twice.text == once.text
    assert twice.removed == ()


def test_line_endings_are_preserved():
    doc = "a\r\n" + SUPERSEDED.replace("\n", "\r\n") + "b\r\n"
    assert project_current_text(doc).text == "a\r\nb\r\n"


def test_empty_and_whitespace_input():
    assert project_current_text("").text == ""
    assert project_current_text("\n\n").text == "\n\n"

"""The Quickstart correction journey says what a quote does, and what it does not.

The example replaces a local-prototype storage decision with a hosted-service
one. It cites the retired decision — and the retired decision argues for the
*old* choice, so citing it as if it supported the new rationale would teach the
opposite of what Palinode does. A verified quote establishes what a source
said. It does not establish that the claim is true.

The settled shape, which this test pins in every copy of the journey:

* the ``supersedes`` / ``superseded_by`` links and the archived original stay,
  as **decision history**;
* the new decision cites a **separately saved requirement** that actually
  supports it, via a typed ``backed_by`` link;
* any reference to the retired decision is labelled as historical rather than
  presented as support;
* the unaffected UTC neighbour is retained;
* the docs say so in words.

Copies covered: ``docs/QUICKSTART.md`` (the walkthrough), ``docs/UI.md`` (the
inspector guide, which repeats the commands), ``docs/FIRST-USE-PARTICIPANT-
CARDS.md`` (the task a participant is handed) and
``tests/integration/test_flagship_client_journey.py`` (the fixture that
validates it). They drifted once; a test is what keeps them together.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
QUICKSTART = REPO / "docs" / "QUICKSTART.md"
UI_DOC = REPO / "docs" / "UI.md"
CARDS = REPO / "docs" / "FIRST-USE-PARTICIPANT-CARDS.md"
FLAGSHIP = REPO / "tests" / "integration" / "test_flagship_client_journey.py"

RETIRED_SLUG = "harbor-notes-storage"
REPLACEMENT_SLUG = "harbor-notes-storage-shared"
REQUIREMENT_SLUG = "harbor-notes-concurrent-write-requirement"
NEIGHBOUR_SLUG = "harbor-notes-timestamps"

_WALKTHROUGHS = pytest.mark.parametrize(
    "path", [QUICKSTART, UI_DOC], ids=["quickstart", "ui-guide"]
)


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


@_WALKTHROUGHS
def test_the_supersession_and_the_archived_original_are_preserved(path: Path) -> None:
    """History is kept. The correction is never a delete."""
    text = _flat(path)
    assert f'"supersedes":"{RETIRED_SLUG}' in text.replace(" ", "") or (
        f'"supersedes":"decisions/{RETIRED_SLUG}.md"' in text.replace(" ", "")
    ), f"{path.name} must record the supersession link"
    assert f"--superseded-by decisions/{REPLACEMENT_SLUG}" in text, (
        f"{path.name} must archive the original with its replacement named"
    )


@_WALKTHROUGHS
def test_the_new_rationale_cites_a_separately_saved_requirement(path: Path) -> None:
    """The support for the new decision is its own record, saved on purpose."""
    text = _flat(path)
    assert REQUIREMENT_SLUG in text, (
        f"{path.name} must save the concurrent-writers requirement separately"
    )
    assert f"--backed-by decisions/{REQUIREMENT_SLUG}" in text, (
        f"{path.name} must link the requirement as typed support for the "
        "replacement — a citation of the retired decision is not support"
    )


@_WALKTHROUGHS
def test_the_retired_quote_is_labelled_historical_not_supporting(path: Path) -> None:
    text = _flat(path).lower()
    assert "historical lineage" in text or "not that the old sqlite rationale" in text, (
        f"{path.name} must label the retired quote as history"
    )
    assert (
        "does not establish that a quoted claim is true" in text
        or "not that the old sqlite rationale" in text
    ), f"{path.name} must say a verified quote does not make a claim true"


@_WALKTHROUGHS
def test_the_unaffected_neighbour_is_retained(path: Path) -> None:
    text = _flat(path)
    assert NEIGHBOUR_SLUG in text or "Store event timestamps in UTC." in text, (
        f"{path.name} must keep the unaffected UTC neighbour in the journey"
    )


def test_the_participant_card_asks_for_the_requirement_before_the_replacement() -> None:
    """The task a person is handed follows the same order the docs do."""
    text = _flat(CARDS)
    requirement = text.index("requires transactional coordination")
    replacement = text.index("Use PostgreSQL for")
    assert requirement < replacement, (
        "the supporting requirement is saved before the replacement, so the "
        "replacement has something to cite that is not the retired decision"
    )
    assert "leave it unchanged" in text, "the unaffected neighbour is still named"


def test_the_fixture_validating_the_journey_is_aligned() -> None:
    """The integration fixture and the docs describe one journey, not two."""
    source = FLAGSHIP.read_text(encoding="utf-8")
    assert REQUIREMENT_SLUG in source
    assert '"backed_by": ["decisions/harbor-notes-concurrent-write-requirement"]' in source
    # The retired decision is still quoted — as lineage — and the fixture says so.
    assert "preserved as lineage, not repurposed as support" in source
    assert NEIGHBOUR_SLUG in source


def test_no_walkthrough_presents_the_retired_quote_as_the_only_citation() -> None:
    """The failure this settles: one --cite, and it argued for the old choice.

    Every ``--cite`` of the retired decision must be accompanied, in the same
    command, by a citation of the requirement. A lone historical quote reads as
    the reason the new decision is right.
    """
    for path in (QUICKSTART, UI_DOC):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"palinode[^\n]*save(?:[^\n]*\\\n)*[^\n]*", text):
            if f"decisions/{RETIRED_SLUG}.md::" not in block:
                continue
            assert REQUIREMENT_SLUG in block, (
                f"{path.name}: a save citing the retired decision must also cite "
                f"the requirement that supports the new one:\n{block}"
            )

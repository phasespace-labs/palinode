"""Guard: every issue reference under `## Unreleased` is followable in public.

`docs/CHANGELOG.md` is the one documented exemption from
`test_no_private_issue_refs_shipping` — the public sync strips the private-repo
suffix off each bullet, so citing a private issue in a changelog bullet is the
intended workflow. That reasoning covers the qualified form and only the
qualified form: a *bare* number has no suffix to strip, so it survives the sync
untouched and renders on the public repo as a link to whatever unrelated issue
happens to hold that number.

The semantic scrub pass caught exactly this class by hand at four consecutive
cuts — v0.15.0, v0.16.0, v0.17.0 and v0.18.0, the last one a bare number used
as an *example string* inside a compatibility paragraph, which is the form no
"strip the suffix" habit will ever catch. Four releases, same class, human eyes
every time. This is that check made mechanical.

Scope is the `## Unreleased` section only. Released sections are already
public-scrubbed and are frozen by hash in `test_changelog_structure`; scanning
them would only produce a second test fighting the immutability guard over
text nobody is allowed to edit anyway.

What passes:

- a reference qualified with the private-repo prefix (`...dev` + number), which
  the sync strips;
- a markdown link whose target is `https://github.com/phasespace-labs/palinode/...`
  — the contributor-credit form, followable by exactly the reader it is for;
- anything inside a fenced code block (sample payloads and IDs are not issue
  references, and rewriting one to please a linter would corrupt the sample).

Everything else — a bare number in prose, in parentheses after a bullet, or as
an illustrative string — is flagged with its file line. This is the mechanical
floor under the semantic pass, not a replacement for it: wording classes are
still human work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).resolve().parent.parent / "docs" / "CHANGELOG.md"

_RELEASE_HEADING = re.compile(r"^## ", re.M)

#: Forms that are allowed to carry a `#`-number. Masked out (blanked in place,
#: preserving line structure) before the bare-reference scan runs, so an
#: allowed form can neither match nor hide an offender beside it on the line.
_ALLOWED_FORMS: tuple[re.Pattern[str], ...] = (
    # A number carrying the private-repo prefix, in either spelling — the
    # public sync strips that suffix off the bullet, so it never ships.
    re.compile(r"(?:palinode-)?dev#\d+"),
    # A markdown link into the PUBLIC repo, e.g. a contributor credit. The
    # whole link is masked (text and target), so numbers in the link text are
    # covered too. Character classes cross newlines, so a wrapped link works.
    re.compile(r"\[[^\]]*\]\(\s*https://github\.com/phasespace-labs/palinode/[^)]+\)"),
)

#: An unqualified reference: `#` + 2–5 digits, not preceded by a word character
#: (hex colours, anchors) or a slash (URL fragments). Longer digit runs are not
#: issue numbers — `\b` after the 5th digit rules them out.
_BARE_REF = re.compile(r"(?:^|[^\w/])(#\d{2,5})\b", re.M)

_FENCE_LINE = re.compile(r"^\s{0,3}(?:```|~~~)")


def _blank(match: re.Match[str]) -> str:
    """Replace a match with spaces, keeping newlines so line numbers hold."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _mask_fenced_blocks(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def bare_issue_refs(section: str) -> list[tuple[int, str, str]]:
    """Unqualified issue references in *section*.

    Returns ``(line_number_within_section, reference, source_line)`` triples,
    1-based, so a caller can add its own offset and name a real file line.
    """
    source_lines = section.split("\n")
    masked = _mask_fenced_blocks(section)
    for pattern in _ALLOWED_FORMS:
        masked = pattern.sub(_blank, masked)

    found: list[tuple[int, str, str]] = []
    for match in _BARE_REF.finditer(masked):
        line_no = masked.count("\n", 0, match.start(1)) + 1
        found.append((line_no, match.group(1), source_lines[line_no - 1].strip()))
    return found


def _unreleased_section() -> tuple[str, int]:
    """The `## Unreleased` body and the file line its first line sits on."""
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.find("## Unreleased")
    assert start != -1, "docs/CHANGELOG.md has no `## Unreleased` section"
    body_start = start + len("## Unreleased")
    rest = text[body_start:]
    nxt = _RELEASE_HEADING.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    return section, text.count("\n", 0, body_start) + 1


# --- fixtures: what the checker treats as followable ------------------------
#
# The tokens below are assembled rather than written out because this file
# ships, and the shipping-tree guard forbids a literal private-repo reference
# in any shipping text file — including the one policing the same class.

_DEV = "dev"
_PUBLIC = "https://github.com/phasespace-labs/palinode"


@pytest.mark.parametrize(
    "label,section",
    [
        ("qualified dev ref", f"### Fixed\n\n- fusion no longer drops hits ({_DEV}#654).\n"),
        (
            "qualified dev ref, long prefix",
            f"### Fixed\n\n- see palinode-{_DEV}#654 for background.\n",
        ),
        (
            "public credit link",
            f"### Fixed\n\n- normalised BM25 floor\n  ([#201]({_PUBLIC}/pull/201),\n  thanks @someone).\n",
        ),
        (
            "public issue link in prose",
            f"### Changed\n\n- [#154]({_PUBLIC}/issues/154) put the floor on the wrong scale.\n",
        ),
        (
            "sample id inside a fence",
            "### Added\n\n- order-id extraction:\n\n  ```json\n  {\"order\": \"#000000301\", \"prior\": \"#301\"}\n  ```\n",
        ),
        ("hex colour", "### Added\n\n- badge colour is now #fff on dark themes.\n"),
        ("url path number", f"### Fixed\n\n- see {_PUBLIC}/issues/199 for the measurement.\n"),
        ("empty section", "\n\n### Added\n\n### Changed\n\n### Fixed\n\n"),
    ],
)
def test_followable_forms_pass(label: str, section: str) -> None:
    assert bare_issue_refs(section) == [], label


@pytest.mark.parametrize(
    "label,section,expected",
    [
        ("bare ref in prose", "### Fixed\n\n- fusion no longer drops hits (#654).\n", "#654"),
        ("bare ref as an example string", "### Changed\n\n- a marker such as #654 is rewritten.\n", "#654"),
        ("bare ref at line start", "### Fixed\n\n- context:\n  #1109 was the cause.\n", "#1109"),
        (
            "bare ref beside an allowed one",
            f"### Fixed\n\n- floor fix ({_DEV}#1267) and also #898.\n",
            "#898",
        ),
        (
            "bare ref beside a public link",
            f"### Fixed\n\n- [#201]({_PUBLIC}/pull/201) supersedes #1142.\n",
            "#1142",
        ),
        (
            "bare ref after a fenced block closes",
            "### Added\n\n```json\n{\"order\": \"#301\"}\n```\n\n- the extractor (#1215).\n",
            "#1215",
        ),
    ],
)
def test_bare_forms_are_flagged(label: str, section: str, expected: str) -> None:
    hits = bare_issue_refs(section)
    assert [ref for _, ref, _ in hits] == [expected], f"{label}: {hits}"


def test_flagged_line_number_is_reported() -> None:
    """A failure has to name the line, or the fix is a hunt through the section."""
    section = "\n\n### Fixed\n\n- one line\n- the offender (#654) here\n"
    assert bare_issue_refs(section) == [(6, "#654", "- the offender (#654) here")]


# --- the live file ----------------------------------------------------------


def test_unreleased_section_has_no_bare_issue_refs() -> None:
    section, offset = _unreleased_section()
    offenders = [
        f"  docs/CHANGELOG.md:{offset + line - 1}: {ref}  →  {text[:90]}"
        for line, ref, text in bare_issue_refs(section)
    ]
    assert not offenders, (
        "Unqualified issue reference(s) under `## Unreleased`. The public sync "
        "strips the private-repo suffix off a qualified reference; a bare number "
        "has nothing to strip and ships as a link to an unrelated public issue. "
        "Qualify it with the dev-repo prefix, link it to "
        "https://github.com/phasespace-labs/palinode/, or name the change "
        "instead:\n" + "\n".join(offenders)
    )

"""``palinode/prompts/`` must stay byte-identical to ``specs/prompts/``.

``specs/prompts/*.md`` is the source of truth in the repo — it is what the
docs, the ADRs and the public-scrub carve-out all name, and what an operator
diffs against. But ``specs/`` sits at the root of the source tree and never
entered the wheel, so a PyPI install shipped no prompts at all: ``init`` wrote
none into the store and the first ``consolidate`` died on a missing file.

The fix duplicates the nine files into a real package. Duplication is only
safe if drift is impossible, which is this file's whole job: edit one side and
CI fails here, naming the file and telling you to copy it across.

``shipped-hashes.json`` is the second half of the contract. ``palinode prompt
sync`` uses it to tell a stale-but-pristine store copy from an operator-edited
one, so a prompt whose new hash was never recorded would be indistinguishable
from someone's local tuning and would never be refreshed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from palinode.prompts import (
    content_hash,
    iter_packaged_prompts,
    packaged_prompt_names,
    packaged_prompts_dir,
    shipped_hashes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PROMPTS = REPO_ROOT / "specs" / "prompts"

#: The nine prompt files as of the packaging change. A tenth is welcome — add
#: it to both trees and to this list. Spelled out rather than globbed so that
#: "the package lost a file" and "the source tree lost a file" are different
#: failures.
EXPECTED_PROMPTS = (
    "compaction.md",
    "consolidation.md",
    "context-assembly.md",
    "digest.md",
    "extraction.md",
    "ingestion.md",
    "nightly-consolidation.md",
    "trajectory-extraction.md",
    "update.md",
)


def test_accessor_lists_every_prompt() -> None:
    assert packaged_prompt_names() == sorted(EXPECTED_PROMPTS)


def test_source_tree_has_the_same_set() -> None:
    assert sorted(p.name for p in SOURCE_PROMPTS.glob("*.md")) == sorted(EXPECTED_PROMPTS)


@pytest.mark.parametrize("name", EXPECTED_PROMPTS)
def test_packaged_copy_is_byte_identical_to_source(name: str) -> None:
    source = SOURCE_PROMPTS / name
    packaged = packaged_prompts_dir() / name
    assert source.is_file(), f"specs/prompts/{name} is the source of truth and is missing"
    assert packaged.is_file(), (
        f"palinode/prompts/{name} is missing — the wheel would ship without it. "
        f"Copy specs/prompts/{name} into palinode/prompts/."
    )
    assert packaged.read_bytes() == source.read_bytes(), (
        f"palinode/prompts/{name} has drifted from specs/prompts/{name}. "
        f"specs/prompts/ is the source of truth: copy it across, then add the "
        f"new sha256 to palinode/prompts/shipped-hashes.json."
    )


@pytest.mark.parametrize("name", EXPECTED_PROMPTS)
def test_current_hash_is_recorded_as_shipped(name: str) -> None:
    """Every prompt palinode ships must be in the hash manifest.

    Without this, ``palinode prompt sync`` sees the *next* release's store
    copies as unrecognised — i.e. edited — and refuses to ever refresh them
    again.
    """
    digest = content_hash((SOURCE_PROMPTS / name).read_bytes())
    known = shipped_hashes().get(name, [])
    assert digest in known, (
        f"{name} changed but palinode/prompts/shipped-hashes.json does not list "
        f'its new hash. Prepend "{digest}" to the "{name}" list — the previous '
        f"hash stays, it is how a store still on the old copy is recognised as "
        f"pristine."
    )


def test_manifest_has_no_stray_entries() -> None:
    assert sorted(shipped_hashes()) == sorted(EXPECTED_PROMPTS)


def test_manifest_hashes_are_unique_per_prompt() -> None:
    for name, hashes in shipped_hashes().items():
        assert len(hashes) == len(set(hashes)), f"{name} lists a hash twice"


def test_manifest_is_valid_json_with_a_schema_version() -> None:
    raw = json.loads(
        (packaged_prompts_dir() / "shipped-hashes.json").read_text(encoding="utf-8")
    )
    assert raw["schema"] == 1
    assert isinstance(raw["prompts"], dict)


def test_iter_packaged_prompts_yields_readable_files() -> None:
    paths = list(iter_packaged_prompts())
    assert len(paths) == len(EXPECTED_PROMPTS)
    for path in paths:
        assert path.read_text(encoding="utf-8").strip()

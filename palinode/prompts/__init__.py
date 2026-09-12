"""The prompts palinode ships inside the wheel, and how anything finds them.

Consolidation reads its prompts from the **memory store**
(``$PALINODE_DIR/specs/prompts/*.md``) so an operator can edit them. Until
this package existed there was nowhere else to read them from: ``specs/`` sits
at the root of the *source tree* and never entered the wheel, so a
``pip install palinode`` had no prompt files at all, ``palinode init`` wrote
none into the store, and the first ``palinode consolidate`` died on a bare
``open()``. Every store that worked had been cloned from, or rsynced against,
a checkout.

``specs/prompts/*.md`` stays the single source of truth in the repo. The copies
next to this module are what the wheel carries, and
``tests/test_packaged_prompts_match_source.py`` asserts the two are
byte-identical, so drift fails CI rather than shipping.

Three consumers, one accessor:

- ``palinode.consolidation.runner`` resolves store-copy-first, packaged-copy
  second, via :func:`resolve_prompt` — and raises :class:`PromptUnavailable`
  when neither exists rather than returning a "nothing to compact" result that
  is indistinguishable from a genuinely quiet week.
- ``palinode init`` provisions a fresh store from :func:`iter_packaged_prompts`.
- the ``prompts_current`` doctor check compares the store against
  :func:`packaged_prompts_dir`, which is present on a wheel install where the
  source tree is not.

``shipped-hashes.json`` records every sha256 palinode has ever shipped for each
prompt. It is what lets ``palinode prompt sync`` tell a stale-but-pristine
store copy (safe to replace) from one the operator edited (never replace).

Standard library only, deliberately: ``palinode init`` and the doctor check
both reach for this before any config or database is guaranteed to exist.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

__all__ = [
    "PromptUnavailable",
    "STORE_PROMPTS_SUBPATH",
    "content_hash",
    "iter_packaged_prompts",
    "packaged_prompt_names",
    "packaged_prompt_path",
    "packaged_prompts_dir",
    "resolve_prompt",
    "shipped_hashes",
    "store_prompts_dir",
]

#: Where a memory store keeps the prompts the consolidation runner reads,
#: relative to ``config.memory_dir``. One definition — the runner, ``init``,
#: ``prompt sync`` and the doctor check all join these two segments.
STORE_PROMPTS_SUBPATH = ("specs", "prompts")

_HASH_MANIFEST = "shipped-hashes.json"


class PromptUnavailable(RuntimeError):
    """A required prompt is in neither the memory store nor the package.

    Its own type on purpose: the consolidation runner must fail loudly here.
    An earlier shape returned an empty operation list for a missing prompt,
    which the run summary reported as a successful pass that compacted
    nothing — the same output a week with nothing to compact produces, so the
    broken install looked exactly like a healthy idle one.
    """


def packaged_prompts_dir() -> Path:
    """The directory holding the prompts shipped with this install.

    Resolved through ``importlib.resources`` so it is correct for a wheel
    install, an editable install, and a source checkout alike. Palinode is
    never imported from a zip (the console scripts and the MCP server both
    need real files on disk), so a non-filesystem loader falls back to this
    module's own directory rather than materialising a temporary copy.
    """
    root = resources.files(__name__)
    if isinstance(root, Path):
        return root
    return Path(__file__).resolve().parent


def packaged_prompt_names() -> list[str]:
    """Every packaged prompt filename, sorted."""
    return sorted(path.name for path in packaged_prompts_dir().glob("*.md"))


def iter_packaged_prompts() -> Iterator[Path]:
    """Yield each packaged prompt file, sorted by name."""
    directory = packaged_prompts_dir()
    for name in packaged_prompt_names():
        yield directory / name


def packaged_prompt_path(name: str) -> Path:
    """Return the packaged copy of *name*, or raise :class:`PromptUnavailable`.

    *name* is a bare filename (``"compaction.md"``); anything with a path
    separator or a parent reference is refused rather than resolved, so a
    caller-supplied prompt name can never read outside the package.
    """
    if name != Path(name).name or name in ("", ".", ".."):
        raise PromptUnavailable(f"not a prompt filename: {name!r}")
    path = packaged_prompts_dir() / name
    if not path.is_file():
        raise PromptUnavailable(
            f"{name} is not among the packaged prompts "
            f"({', '.join(packaged_prompt_names()) or 'none'})"
        )
    return path


def store_prompts_dir(memory_dir: str | Path) -> Path:
    """``<memory_dir>/specs/prompts`` — where the runner looks first."""
    return Path(memory_dir).joinpath(*STORE_PROMPTS_SUBPATH)


def resolve_prompt(name: str, memory_dir: str | Path) -> tuple[Path, bool]:
    """Locate prompt *name* for a run against *memory_dir*.

    Returns ``(path, from_store)``. The store's copy wins whenever it exists —
    operators edit prompts and ``docs/HOW-MEMORY-WORKS.md`` tells them to — and
    the packaged copy is the fallback for a store that was never provisioned
    with one. Raises :class:`PromptUnavailable` when neither exists.
    """
    store_path = store_prompts_dir(memory_dir) / name
    if store_path.is_file():
        return store_path, True
    try:
        return packaged_prompt_path(name), False
    except PromptUnavailable as exc:
        raise PromptUnavailable(
            f"no prompt {name!r}: absent from the store ({store_path}) and from "
            f"the installed package ({packaged_prompts_dir()}). Run "
            f"`palinode init` to provision the store's prompts, or reinstall "
            f"palinode if the packaged copies are missing."
        ) from exc


def content_hash(data: bytes) -> str:
    """The sha256 used throughout for prompt identity."""
    return hashlib.sha256(data).hexdigest()


def shipped_hashes() -> dict[str, list[str]]:
    """Every sha256 palinode has shipped, per prompt filename, newest first.

    An unreadable or absent manifest yields ``{}`` — which makes
    ``palinode prompt sync`` treat every store copy as operator-edited and
    refuse to overwrite it. Failing closed is the right direction for a
    command whose only irreversible act is replacing a file.
    """
    path = packaged_prompts_dir() / _HASH_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    prompts = data.get("prompts")
    if not isinstance(prompts, dict):
        return {}
    return {
        name: [str(h) for h in hashes]
        for name, hashes in prompts.items()
        if isinstance(hashes, list)
    }

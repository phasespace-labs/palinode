"""Check: prompts_current

Consolidation reads its prompts from the **memory store**
(``$PALINODE_DIR/specs/prompts/*.md``), never from the installed package. A
store keeps whatever prompt files it was provisioned with, so a release that
changes a prompt changes nothing on an install that predates it — the new
behaviour is reachable only on stores whose copy was refreshed. Nothing told
the operator; doctor checked the DB, the embedder, the circuit, the config and
git, but never whether the store's prompts still match the ones shipped.

This check compares the ``version:`` frontmatter of each packaged prompt to the
store's copy and warns when they diverge. Prompts that declare no ``version:``
cannot be compared and are reported as uncovered rather than silently counted
as current.

"Packaged" means ``palinode/prompts/`` — the copies inside the install, reached
through ``palinode.prompts.packaged_prompts_dir()``. It used to mean
``specs/prompts/`` in the source tree, which exists only in a checkout, so this
check was blind on the install it was written for.

``fast``: a bounded read of a handful of small files, no network.
"""
from __future__ import annotations

import logging
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext
from palinode.prompts import STORE_PROMPTS_SUBPATH, packaged_prompts_dir

logger = logging.getLogger(__name__)

#: The prompts shipped *inside* the install — ``palinode/prompts/``, resolved
#: through the same accessor the consolidation runner falls back to. This used
#: to point at ``specs/prompts`` in the source tree, which meant the check
#: reported "no packaged prompts to compare against" on exactly the install
#: where the comparison matters most: a wheel from PyPI, which carried no
#: prompts at all. Module-level so tests can point it at a fixture directory.
PACKAGED_PROMPTS_DIR: Path = packaged_prompts_dir()

#: Where the store keeps the prompts the consolidation runner actually reads.
_STORE_PROMPTS_SUBPATH = STORE_PROMPTS_SUBPATH


def _declared_version(path: Path) -> str | None:
    """Return the ``version:`` frontmatter of *path* as a string, or None.

    None covers all three "cannot compare" cases identically — no frontmatter,
    no ``version`` key, or a file that will not parse — because the check's
    answer is the same for each: this prompt is not covered.
    """
    import frontmatter

    try:
        metadata = frontmatter.loads(path.read_text(encoding="utf-8")).metadata
    except Exception as exc:  # noqa: BLE001 — an unparseable prompt is "uncovered", not a crash
        logger.debug("prompts_current: could not parse %s: %r", path, exc)
        return None
    version = metadata.get("version")
    if version is None:
        return None
    return str(version).strip()


def _lags(store_version: str, packaged_version: str) -> bool:
    """True when both versions are integers and the store's is the older one.

    Anything else — equal, non-numeric, or a store ahead of the package (a
    locally bumped prompt) — is drift worth reporting but is not a lag, and the
    message says so rather than accusing the operator of being behind.
    """
    try:
        return int(store_version) < int(packaged_version)
    except ValueError:
        return False


@register(tags=("fast",))
def prompts_current(ctx: DoctorContext) -> CheckResult:
    """Warn when the store's prompts do not match the packaged versions."""
    packaged_dir = PACKAGED_PROMPTS_DIR
    if not packaged_dir.is_dir():
        return CheckResult(
            name="prompts_current",
            severity="info",
            passed=True,
            message=(
                f"No packaged prompts to compare against — {packaged_dir} is not "
                f"present in this install."
            ),
            remediation=None,
            tags=("fast",),
        )

    packaged = {
        path.name: _declared_version(path)
        for path in sorted(packaged_dir.glob("*.md"))
    }
    versioned = {name: v for name, v in packaged.items() if v is not None}
    unversioned = len(packaged) - len(versioned)

    if not versioned:
        return CheckResult(
            name="prompts_current",
            severity="info",
            passed=True,
            message=(
                f"No packaged prompt under {packaged_dir} declares a version, so "
                f"there is nothing to compare the store against."
            ),
            remediation=None,
            tags=("fast",),
        )

    store_dir = Path(ctx.config.memory_dir).joinpath(*_STORE_PROMPTS_SUBPATH)
    if not store_dir.is_dir():
        return CheckResult(
            name="prompts_current",
            severity="info",
            passed=True,
            message=(
                f"The store has no prompts directory — {store_dir} does not exist, "
                f"so no store prompt can lag the {len(versioned)} versioned packaged "
                f"prompt(s). Consolidation reads its prompts from that path."
            ),
            remediation=None,
            tags=("fast",),
        )

    details: list[str] = []
    for name, packaged_version in sorted(versioned.items()):
        store_path = store_dir / name
        if not store_path.is_file():
            details.append(
                f"{name} (absent from the store, packaged version {packaged_version})"
            )
            continue
        store_version = _declared_version(store_path)
        if store_version == packaged_version:
            continue
        shown = store_version if store_version is not None else "none declared"
        verb = "lags" if store_version is not None and _lags(store_version, packaged_version) else "differs from"
        details.append(
            f"{name} (store version {shown} {verb} packaged version {packaged_version})"
        )

    if details:
        return CheckResult(
            name="prompts_current",
            severity="warn",
            passed=False,
            message=(
                f"{len(details)} of {len(versioned)} versioned prompt(s) in {store_dir} "
                f"do not match the packaged copies: {'; '.join(details)}."
            ),
            remediation=(
                f"Consolidation reads the store's copy, so the packaged behaviour stays "
                f"unreachable until you refresh it. Run `palinode prompt sync` — it "
                f"replaces only the copies that still match a released version and "
                f"reports any you have edited. `--dry-run` first to see the plan; "
                f"the packaged originals are in {packaged_dir}, the store's in {store_dir}."
            ),
            tags=("fast",),
        )

    message = (
        f"All {len(versioned)} versioned prompt(s) in {store_dir} match the packaged copies."
    )
    if unversioned:
        message += (
            f" {unversioned} packaged prompt(s) declare no version and are not compared."
        )
    return CheckResult(
        name="prompts_current",
        severity="info",
        passed=True,
        message=message,
        remediation=None,
        tags=("fast",),
    )

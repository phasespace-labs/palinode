"""The hook scripts palinode installs, and the record of every version shipped.

The scripts are real files under ``scripts/``, shipped as package data
(ADR-023 §1). ``palinode init`` writes them into a harness's hook directory,
``palinode hooks update`` refreshes them, and ``examples/hooks/`` is generated
from them by ``scripts/render-hook-examples.py``.

Each script carries a ``# palinode-hook-version: X.Y.Z`` stamp on its second
line. ``shipped-hashes.json`` records every ``(script, version, sha256)``
palinode has shipped (ADR-023 §2), regenerated from git history by
``scripts/regen-hook-hashes.py``. A test fails when a script's content changes
while its stamp still names a version a release shipped with other content.

This package imports nothing from :mod:`palinode.cli`.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import cache
from importlib import resources
from typing import Any

#: Every hook script palinode installs, by file name.
SCRIPT_NAMES = (
    "palinode-session-start.sh",
    "palinode-session-end.sh",
    "palinode-user-prompt-submit.sh",
    "palinode-post-compact.sh",
    "palinode-cursor-hook.sh",
    "palinode-agy-hook.sh",
)

HOOK_VERSION_PREFIX = "# palinode-hook-version:"
_VERSION_RE = re.compile(r"^# palinode-hook-version:\s*(\S+)\s*$", re.MULTILINE)

#: The manifest of shipped script versions, beside the scripts.
MANIFEST_NAME = "shipped-hashes.json"


def script(name: str) -> str:
    """The packaged content of one hook script."""
    if name not in SCRIPT_NAMES:
        raise KeyError(name)
    return resources.files(__name__).joinpath("scripts", name).read_text(encoding="utf-8")


def packaged_scripts() -> dict[str, str]:
    """Every hook script's packaged content, by file name."""
    return {name: script(name) for name in SCRIPT_NAMES}


def hook_version(text: str) -> str | None:
    """The ``palinode-hook-version`` stamp in *text*, or None when unstamped."""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def content_sha256(text: str) -> str:
    """sha256 of a hook's exact bytes: what init installed and doctor compares."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@cache
def shipped_hashes() -> dict[str, Any]:
    """The parsed ``shipped-hashes.json`` manifest."""
    text = resources.files(__name__).joinpath(MANIFEST_NAME).read_text(encoding="utf-8")
    return json.loads(text)

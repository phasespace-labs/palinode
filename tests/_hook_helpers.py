"""Test helpers for installed-hook drift (ADR-023)."""
from __future__ import annotations

import pytest

from palinode.hooks import content_sha256, hook_version
from palinode.hooks import installed


def ship(monkeypatch: pytest.MonkeyPatch, name: str, content: str, released: str = "0.20.0") -> None:
    """Record ``content`` as a version of script ``name`` some release shipped.

    A fixture's "older release" copy is made-up content, so without this the
    manifest would not know it and the copy would be ``edited``, not ``stale``.
    """
    real = installed._shipped

    def shipped(script: str) -> dict[str, dict]:
        known = dict(real(script))
        if script == name:
            sha = content_sha256(content)
            known[sha] = {"sha256": sha, "version": hook_version(content), "released": released}
        return known

    monkeypatch.setattr(installed, "_shipped", shipped)

"""Keep the shipped example config aligned with the code defaults."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import yaml

from palinode.core.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "palinode.config.yaml.example"

_ALLOWED_DIFFERENCES = {
    # Informational only: the actual consolidation schedule lives in crontab.
    ("consolidation", "schedule"): ("0 11 * * 0", "0 3 * * 0"),
    # None is a sentinel resolved to <memory_dir>/.palinode.db by load_config().
    ("db_path",): (".palinode.db", None),
}


def _scalar_leaves(
    value: object, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _scalar_leaves(child, (*path, key))
        return
    yield path, value


def _value_at_path(value: object, path: tuple[str, ...]) -> object:
    for part in path:
        if isinstance(value, dict):
            value = value[part]
        else:
            value = getattr(value, part)
    return value


def test_example_config_scalar_values_match_defaults() -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")) or {}
    defaults = Config()

    differences = {
        path: (example_value, default_value)
        for path, example_value in _scalar_leaves(raw)
        if example_value != (default_value := _value_at_path(defaults, path))
    }
    unexpected = {
        path: values
        for path, values in differences.items()
        if _ALLOWED_DIFFERENCES.get(path) != values
    }
    stale_allowlist = sorted(set(_ALLOWED_DIFFERENCES) - set(differences))

    assert not unexpected and not stale_allowlist, (
        "palinode.config.yaml.example differs from Config defaults; "
        f"unexpected={unexpected}, stale_allowlist={stale_allowlist}"
    )

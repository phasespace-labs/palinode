"""One fixture store and the expected set per surface group, for issue 251.

Every walker is driven against the same eighteen files, so a walker that
bypasses the shared "never memory" rule shows up as a set that differs from its
row. Stems are store-relative paths without ``.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

FILES: tuple[str, ...] = (
    "notes/a",
    "archive/a",
    "daily/2026-01-01",
    "inbox/i",
    "inbox/processed/p",
    "specs/amr",
    "specs/prompts/p",
    "prompts/p",
    "logs/l",
    ".obsidian/o",
    ".trash/t",
    "team/.drafts/d",
    "team/archive/n",
    "team/daily/n",
    "node_modules/pkg/readme",
    "team/__pycache__/x",
    "logs",
    "team/specsheet/s",
)

INDEX: frozenset[str] = frozenset({
    "notes/a", "archive/a", "daily/2026-01-01", "inbox/i",
    "team/archive/n", "team/daily/n", "logs", "team/specsheet/s",
})
LISTING: frozenset[str] = frozenset({"notes/a", "logs", "team/specsheet/s"})
DIGEST: frozenset[str] = frozenset({"notes/a", "inbox/i", "logs", "team/specsheet/s"})
LINT: frozenset[str] = frozenset({
    "notes/a", "daily/2026-01-01", "inbox/i", "team/daily/n", "logs", "team/specsheet/s",
})
TTL: frozenset[str] = frozenset({
    "notes/a", "archive/a", "inbox/i", "team/archive/n", "logs", "team/specsheet/s",
})
CROSS_REFS: frozenset[str] = frozenset({"notes/a", "specs/amr", "logs", "team/specsheet/s"})


def write_store(root: Path, **frontmatter: Any) -> Path:
    """Write every file in :data:`FILES` under ``root`` and return ``root``.

    Each file carries ``frontmatter`` on top of an id, a type and a category, so
    a surface that only acts on a particular field (an expiry, a backing source)
    can ask for it and still see the whole fixture. A ``None`` value drops the field; a callable
    is called with the stem and gives that file's value.
    """
    for stem in FILES:
        path = root / f"{stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fields: dict[str, Any] = {
            "id": stem.replace("/", "-"),
            "type": "Decision",
            "category": "notes",
            "description": f"fixture {stem}",
            "entities": ["project/harbor"],
            **frontmatter,
        }
        lines = []
        for key, value in fields.items():
            if callable(value):
                value = value(stem)
            if value is None:
                continue
            if isinstance(value, list):
                lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
            elif isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            else:
                lines.append(f"{key}: {value}")
        path.write_text(
            "---\n" + "\n".join(lines) + f"\n---\n\n# {stem}\n\nfixture body harbor {stem}\n",
            encoding="utf-8",
        )
    return root

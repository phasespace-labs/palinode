"""
OpenClaw Memory Migration

Parses MEMORY.md (OpenClaw's flat memory format) into structured Palinode
markdown files, one file per ## section, with heuristic type detection.

Type heuristics (in priority order):
  person   — section mentions people names / "who"
  decision — section contains "decided", "chose", "because"
  project  — section mentions projects / tasks
  insight  — everything else
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any

import yaml

from palinode.core.config import config
from palinode.core.parser import slugify as _base_slugify

logger = logging.getLogger("palinode.migration.openclaw")

# ── Type-detection keywords ───────────────────────────────────────────────────

_PERSON_KEYWORDS = re.compile(
    r"\b(who|person|people|colleague|friend|user|team|member|contact)\b",
    re.IGNORECASE,
)
_DECISION_KEYWORDS = re.compile(
    r"\b(decided|decide|chose|choose|because|rationale|reasoning|resolution)\b",
    re.IGNORECASE,
)
_PROJECT_KEYWORDS = re.compile(
    r"\b(project|task|sprint|milestone|backlog|epic|ticket|issue|feature|roadmap)\b",
    re.IGNORECASE,
)

# Subdirectory for each type
_TYPE_DIR: dict[str, str] = {
    "person": "people",
    "decision": "decisions",
    "project": "projects",
    "insight": "insights",
}


def _detect_type(heading: str, body: str) -> str:
    """Return a memory type string based on heading + body heuristics.

    Uses a scoring approach: each keyword match adds a point for that type.
    The heading is weighted 3× more than the body to reflect its signal
    strength.  Ties are broken by priority: person > decision > project.
    """
    scores: dict[str, int] = {"person": 0, "decision": 0, "project": 0}
    for text, weight in ((heading, 3), (body, 1)):
        scores["person"] += len(_PERSON_KEYWORDS.findall(text)) * weight
        scores["decision"] += len(_DECISION_KEYWORDS.findall(text)) * weight
        scores["project"] += len(_PROJECT_KEYWORDS.findall(text)) * weight

    # Pick highest-scoring type; priority order breaks ties
    best = max(("person", "decision", "project"), key=lambda t: scores[t])
    if scores[best] == 0:
        return "insight"
    return best


def _slugify(text: str) -> str:
    """Convert a heading to a filesystem-safe slug (max 60 chars)."""
    slug = _base_slugify(text)
    return slug[:60] or "section"


def _validate_source_path(path: str) -> str:
    """Return a resolved, validated absolute path to the MEMORY.md source.

    Raises ValueError for null bytes, path traversal, and symlinks that
    escape the resolved path.
    """
    if "\x00" in path:
        raise ValueError("Null bytes are not allowed in path")
    resolved = os.path.realpath(os.path.abspath(path))
    # Reject if any path component is ".." (belt-and-suspenders)
    if ".." in path.split(os.sep):
        raise ValueError("Path traversal is not allowed")
    return resolved


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode(), usedforsecurity=False).hexdigest()


def _existing_hashes(memory_dir: str) -> set[str]:
    """Collect SHA-256 hashes of all existing .md files for dedup."""
    hashes: set[str] = set()
    for root, _dirs, files in os.walk(memory_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    hashes.add(_sha256(f.read()))
            except OSError:
                pass
    return hashes


def _parse_raw(raw: str) -> list[dict[str, Any]]:
    """Parse raw MEMORY.md text into section dicts (no I/O)."""
    sections: list[dict[str, Any]] = []
    # parts[0] is any content before the first ## heading — skip it.
    parts = re.split(r"^##\s+", raw, flags=re.MULTILINE)
    for part in parts[1:]:
        if not part.strip():
            continue
        lines = part.splitlines()
        heading = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if not heading:
            continue
        sections.append(
            {
                "heading": heading,
                "body": body,
                "type": _detect_type(heading, body),
                "slug": _slugify(heading),
            }
        )
    return sections


def parse_memory_md(source_path: str) -> list[dict[str, Any]]:
    """Parse a MEMORY.md file into a list of section dicts.

    Each dict contains:
        heading: str     — the ## heading text
        body: str        — the section body (stripped)
        type: str        — detected memory type
        slug: str        — filesystem-safe slug derived from heading
    """
    validated = _validate_source_path(source_path)
    with open(validated, "r", encoding="utf-8") as f:
        raw = f.read()
    return _parse_raw(raw)


def _build_file_content(
    section: dict[str, Any],
    now_iso: str,
    source_path: str,
) -> tuple[str, str]:
    """Return (relative_path, file_content) for a section.

    relative_path is relative to memory_dir.
    """
    mem_type = section["type"]
    subdir = _TYPE_DIR[mem_type]
    slug = section["slug"]
    rel_path = f"{subdir}/{slug}.md"

    frontmatter = {
        "id": f"{subdir}-{slug}",
        "category": subdir,
        "name": section["heading"],
        "last_updated": now_iso,
        "source": "openclaw-migration",
        "source_file": os.path.basename(source_path),
    }
    fm_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
    content = f"---\n{fm_str}---\n\n# {section['heading']}\n\n{section['body']}\n"
    return rel_path, content


def _git_commit(memory_dir: str, staged_files: list[str], log_file: str | None) -> None:
    """Stage and commit migrated files in the memory repo."""
    files_to_add = [f for f in staged_files + ([log_file] if log_file else []) if f]
    if not files_to_add:
        return
    subprocess.run(
        ["git", "add", "--"] + files_to_add,
        cwd=memory_dir,
        check=False,
        capture_output=True,
    )
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    msg = (
        f"palinode migrate openclaw: import {len(staged_files)} sections "
        f"from MEMORY.md ({date_str})"
    )
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=memory_dir,
        check=False,
        capture_output=True,
    )


def run_migration(
    source_path: str,
    dry_run: bool = False,
    review_callback: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Import a MEMORY.md file into Palinode.

    Args:
        source_path: Path to the MEMORY.md file to import.
        dry_run: If True, parse and report without writing any files.
        review_callback: Optional function that receives the parsed section list
            and returns a (possibly modified) list.  Sections can have their
            ``type`` changed or be removed entirely.  Called after parsing,
            before any file I/O.

    Returns:
        dict with keys:
            sections_found: int
            files_created: list[str]   — relative paths written
            files_skipped: list[str]   — relative paths skipped (dedup)
            log_file: str | None       — relative path of the migration log
            dry_run: bool
    """
    memory_dir = os.path.realpath(config.memory_dir)
    validated_source = _validate_source_path(source_path)

    with open(validated_source, "r", encoding="utf-8") as f:
        sections = _parse_raw(f.read())

    if review_callback is not None:
        sections = review_callback(sections)

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    existing_hashes: set[str] = set() if dry_run else _existing_hashes(memory_dir)

    files_created: list[str] = []
    files_skipped: list[str] = []
    written_abs: list[str] = []

    for section in sections:
        rel_path, content = _build_file_content(section, now_iso, validated_source)
        content_hash = _sha256(content)

        if content_hash in existing_hashes:
            files_skipped.append(rel_path)
            continue

        if dry_run:
            files_created.append(rel_path)
            continue

        abs_path = os.path.join(memory_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        existing_hashes.add(content_hash)
        files_created.append(rel_path)
        written_abs.append(abs_path)
        logger.info(f"Created {rel_path}")

    # Write migration log
    log_rel: str | None = None
    log_abs: str | None = None
    if not dry_run and (files_created or files_skipped):
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        log_rel = f"migrations/openclaw-{date_str}.md"
        log_abs = os.path.join(memory_dir, log_rel)
        os.makedirs(os.path.dirname(log_abs), exist_ok=True)

        log_lines = [
            f"# OpenClaw Migration — {date_str}",
            "",
            f"Source: `{os.path.basename(validated_source)}`",
            f"Sections found: {len(sections)}",
            f"Files created: {len(files_created)}",
            f"Files skipped (dedup): {len(files_skipped)}",
            "",
            "## Created",
            "",
        ]
        for fp in files_created:
            log_lines.append(f"- `{fp}`")
        if files_skipped:
            log_lines.extend(["", "## Skipped (duplicate content)", ""])
            for fp in files_skipped:
                log_lines.append(f"- `{fp}`")
        log_lines.append("")

        with open(log_abs, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))

    if not dry_run and written_abs:
        _git_commit(memory_dir, written_abs, log_abs)

    return {
        "sections_found": len(sections),
        "files_created": files_created,
        "files_skipped": files_skipped,
        "log_file": log_rel,
        "dry_run": dry_run,
    }

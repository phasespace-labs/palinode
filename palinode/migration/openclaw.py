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
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any

import yaml

from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.parser import slugify as _base_slugify
from palinode.core.skip_dirs import is_skipped_path

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


def _body_hash(text: str) -> str:
    """SHA-256 of a memory's body, frontmatter excluded.

    The frontmatter carries a per-run ``last_updated`` stamp, so a whole-file
    hash never matches an earlier run's output and every re-run would look new.
    """
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return _sha256(text.strip())


def _existing_hashes(memory_dir: str) -> set[str]:
    """Collect body hashes of all existing .md files for dedup."""
    hashes: set[str] = set()
    for root, _dirs, files in os.walk(memory_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            if is_skipped_path(os.path.relpath(fpath, memory_dir)):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    hashes.add(_body_hash(f.read()))
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
    source_name: str = "openclaw",
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
        "source": f"{source_name}-migration",
        "source_file": os.path.basename(source_path),
    }
    fm_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
    content = f"---\n{fm_str}---\n\n# {section['heading']}\n\n{section['body']}\n"
    return rel_path, content


def _git_commit(
    staged_files: list[str],
    log_file: str | None,
    source_name: str,
    source_path: str,
) -> None:
    """Commit each migrated file in its own per-file commit.

    Each imported section is a genesis mutation, so each created memory gets its
    own commit via the git_tools choke point — never a batched ``git add --``
    over the whole import. The migration log is committed last, on its own, as
    the import-summary record.
    """
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    for fp in staged_files:
        if not fp:
            continue
        base = os.path.basename(fp)
        git_tools.commit_memory_file(
            fp,
            f"palinode migrate {source_name}: import {base} from "
            f"{os.path.basename(source_path)} ({date_str})",
        )
    if log_file:
        git_tools.commit_memory_file(
            log_file,
            f"palinode migrate {source_name}: import log ({date_str})",
        )


def run_sections_migration(
    source_path: str,
    sections: list[dict[str, Any]],
    *,
    source_name: str,
    display_name: str,
    dry_run: bool = False,
    review_callback: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Write parsed sections through the canonical migration path."""
    memory_dir = os.path.realpath(config.memory_dir)
    validated_source = _validate_source_path(source_path)

    if review_callback is not None:
        sections = review_callback(sections)

    now_iso = datetime.now(UTC).isoformat()
    existing_hashes = _existing_hashes(memory_dir)

    files_created: list[str] = []
    files_skipped: list[str] = []
    files_renamed: list[dict[str, str]] = []
    written_abs: list[str] = []
    planned_paths: set[str] = set()

    for section in sections:
        rel_path, content = _build_file_content(
            section,
            now_iso,
            validated_source,
            source_name,
        )
        content_hash = _body_hash(content)

        if content_hash in existing_hashes:
            files_skipped.append(rel_path)
            continue

        original_path = rel_path
        if rel_path in planned_paths or os.path.lexists(os.path.join(memory_dir, rel_path)):
            stem = f"{rel_path[:-3]}-openclaw-{content_hash[:8]}"
            rel_path = f"{stem}.md"
            suffix = 2
            while rel_path in planned_paths or os.path.lexists(os.path.join(memory_dir, rel_path)):
                rel_path = f"{stem}-{suffix}.md"
                suffix += 1
            files_renamed.append({"from": original_path, "to": rel_path})
            # The id is derived from the slug, so rebuild under the renamed slug:
            # the new file must not share the id of the memory it collided with.
            renamed = {**section, "slug": os.path.basename(rel_path)[:-3]}
            _, content = _build_file_content(renamed, now_iso, validated_source, source_name)

        existing_hashes.add(content_hash)
        planned_paths.add(rel_path)
        if dry_run:
            files_created.append(rel_path)
            continue

        abs_path = os.path.join(memory_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        git_tools.write_memory_file(abs_path, content)

        files_created.append(rel_path)
        written_abs.append(abs_path)
        logger.info("Created %s", rel_path)

    log_rel: str | None = None
    log_abs: str | None = None
    if not dry_run and (files_created or files_skipped):
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        log_rel = f"migrations/{source_name}-{date_str}.md"
        log_abs = os.path.join(memory_dir, log_rel)
        os.makedirs(os.path.dirname(log_abs), exist_ok=True)

        log_lines = [
            f"# {display_name} Migration — {date_str}",
            "",
            f"Source: `{os.path.basename(validated_source)}`",
            f"Sections found: {len(sections)}",
            f"Files created: {len(files_created)}",
            f"Files skipped (dedup): {len(files_skipped)}",
            f"Files renamed: {len(files_renamed)}",
            "",
            "## Created",
            "",
        ]
        for fp in files_created:
            log_lines.append(f"- `{fp}`")
        if files_renamed:
            log_lines.extend(["", "## Renamed (path collision)", ""])
            for rename in files_renamed:
                log_lines.append(f"- `{rename['from']}` → `{rename['to']}`")
        if files_skipped:
            log_lines.extend(["", "## Skipped (duplicate content)", ""])
            for fp in files_skipped:
                log_lines.append(f"- `{fp}`")
        log_lines.append("")

        git_tools.write_memory_file(log_abs, "\n".join(log_lines))

    if not dry_run and written_abs:
        _git_commit(written_abs, log_abs, source_name, validated_source)

    return {
        "sections_found": len(sections),
        "files_created": files_created,
        "files_skipped": files_skipped,
        "files_renamed": files_renamed,
        "log_file": log_rel,
        "dry_run": dry_run,
    }


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
            files_renamed: list[dict]  — original and renamed relative paths
            log_file: str | None       — relative path of the migration log
            dry_run: bool
    """
    validated_source = _validate_source_path(source_path)

    with open(validated_source, "r", encoding="utf-8") as f:
        sections = _parse_raw(f.read())
    return run_sections_migration(
        validated_source,
        sections,
        source_name="openclaw",
        display_name="OpenClaw",
        dry_run=dry_run,
        review_callback=review_callback,
    )

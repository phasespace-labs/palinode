"""palinode.import_.vault — Obsidian vault import logic for `palinode import --from-vault`.

Walks an existing Obsidian vault directory, maps each .md file to a palinode
category, translates wikilinks where possible, and writes the results into
memory_dir. Testable independently of Click.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter as fm_lib

from palinode.core import git_tools
from palinode.core.parser import slugify, parse_markdown

logger = logging.getLogger("palinode.import_vault")

# Directories inside the source vault that are always skipped.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".obsidian",
    ".trash",
    ".git",
    ".palinode",
})

# PARA-convention top-level directory → palinode category mapping.
_PARA_MAP: dict[str, str] = {
    "projects": "projects",
    "areas": "decisions",
    "resources": "research",
    "archive": "archive",
    "archives": "archive",
}

# Daily-note filename pattern: YYYY-MM-DD.md (or YYYY-MM-DD - Title.md)
_DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ _-].*)?\.md$", re.IGNORECASE)

# Wikilink regex: [[Target]] or [[Target|Display]]
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


@dataclass
class ImportPlan:
    """Describes a single file that would be imported."""
    source_path: Path
    dest_path: Path
    category: str
    category_reason: str
    # Whether dest_path already exists in memory_dir
    dest_exists: bool
    # The final content that would be written
    content: str


@dataclass
class ImportResult:
    """Summary of a completed import run."""
    plans: list[ImportPlan] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.plans)


def _infer_category(
    source_rel: Path,
    metadata: dict,
    into_category: Optional[str],
) -> tuple[str, str]:
    """Return (category, reason) for a given source file.

    Priority order:
    1. --into-category override
    2. PARA top-level directory (Projects/ Areas/ Resources/ Archive/)
    3. Daily-note filename pattern
    4. frontmatter `type:` field
    5. Fallback to `archive/`
    """
    if into_category:
        cat = into_category.rstrip("/")
        return cat, f"--into-category override"

    # PARA: check each component of the path (case-insensitive)
    for part in source_rel.parts[:-1]:  # all directory components
        part_lower = part.lower()
        if part_lower in _PARA_MAP:
            return _PARA_MAP[part_lower], f"PARA directory '{part}'"

    # Daily-note pattern on the filename
    if _DAILY_RE.match(source_rel.name):
        return "daily", "daily-note filename pattern (YYYY-MM-DD)"

    # frontmatter `type:` field
    fm_type = metadata.get("type") or metadata.get("category") or ""
    if isinstance(fm_type, str) and fm_type.strip():
        ft = fm_type.strip().lower()
        # Map common palinode types
        _type_map = {
            "decision": "decisions",
            "decisions": "decisions",
            "insight": "insights",
            "insights": "insights",
            "research": "research",
            "project": "projects",
            "projects": "projects",
            "person": "people",
            "people": "people",
            "daily": "daily",
            "archive": "archive",
        }
        if ft in _type_map:
            return _type_map[ft], f"frontmatter type: '{fm_type}'"

    return "archive", "fallback (no matching PARA dir, date pattern, or type frontmatter)"


def _slugify_path_parts(path: Path) -> list[str]:
    """Slugify each component of a relative path (directories + stem)."""
    parts = list(path.parts)
    # Slugify directory components
    slugged_dirs = [slugify(p) for p in parts[:-1]]
    # Slugify stem, keep extension
    stem = slugify(path.stem)
    return slugged_dirs + [stem]


def _make_dest_path(
    memory_dir: Path,
    source_rel: Path,
    category: str,
    used_paths: set[Path],
) -> Path:
    """Compute the destination path, disambiguating slug collisions.

    Layout: <memory_dir>/<category>/<slugified-subdirs>/<slugified-stem>.md
    Collision → append -2, -3, ...
    """
    parts = _slugify_path_parts(source_rel)
    # Drop PARA top-level dirs that were consumed for category inference
    # (e.g. Projects/Foo/notes.md → just Foo/notes under projects/)
    top_dir_lower = source_rel.parts[0].lower() if len(source_rel.parts) > 1 else ""
    if top_dir_lower in _PARA_MAP or top_dir_lower in {"projects", "areas", "resources", "archive", "archives"}:
        parts = parts[1:]  # drop the PARA root

    # Build relative sub-path (without category prefix)
    if len(parts) == 1:
        sub_parts = parts
    else:
        sub_parts = parts

    base = memory_dir / category / Path(*sub_parts).with_suffix(".md") if sub_parts else memory_dir / category / "unnamed.md"

    # Disambiguate
    candidate = base
    suffix = 2
    while candidate in used_paths:
        candidate = base.with_stem(base.stem + f"-{suffix}")
        suffix += 1
    used_paths.add(candidate)
    return candidate


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_id(content: str) -> str:
    """Short deterministic ID from content (first 8 hex chars of MD5)."""
    return hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:8]  # noqa: S324


def _build_slug_map(plans: list[ImportPlan]) -> dict[str, str]:
    """Build a mapping from source-file slug → dest relative path (without .md).

    Used for wikilink translation: if a source [[Note]] matches a file being
    imported, rewrite the link to the dest path slug.
    """
    slug_map: dict[str, str] = {}
    for plan in plans:
        # Source stem slug
        src_slug = slugify(plan.source_path.stem)
        # Destination relative to memory_dir (without extension)
        dest_rel = plan.dest_path.stem
        slug_map[src_slug] = dest_rel
        # Also index the raw stem for case-insensitive matching
        slug_map[plan.source_path.stem.lower()] = dest_rel
    return slug_map


def _translate_wikilinks(
    body: str,
    slug_map: dict[str, str],
    orphan_warnings: list[str],
    source_rel: Path,
) -> str:
    """Rewrite [[wikilinks]] using slug_map.

    - If the target slug is in slug_map → rewrite [[Old]] to [[new-slug]]
    - Otherwise → leave as-is, log a warning
    """
    def replace(m: re.Match) -> str:
        raw = m.group(1).strip()
        raw_slug = slugify(raw)
        if raw_slug in slug_map:
            return f"[[{slug_map[raw_slug]}]]"
        if raw.lower() in slug_map:
            return f"[[{slug_map[raw.lower()]}]]"
        # Leave untouched
        orphan_warnings.append(
            f"{source_rel}: wikilink [[{raw}]] has no matching import target — "
            "leaving as-is (run palinode orphan-repair post-import)"
        )
        return m.group(0)

    return _WIKILINK_RE.sub(replace, body)


def _add_palinode_frontmatter(
    existing_meta: dict,
    category: str,
    source_path: Path,
) -> dict:
    """Return a merged frontmatter dict with palinode required fields.

    Preserves existing frontmatter; only adds fields that are absent.
    """
    meta = dict(existing_meta)

    if "id" not in meta:
        meta["id"] = _generate_id(str(source_path))
    if "category" not in meta:
        meta["category"] = category
    if "created_at" not in meta:
        meta["created_at"] = _now_iso()
    if "last_updated" not in meta:
        meta["last_updated"] = _now_iso()

    # Mark import origin — always set so it's clear these came from a vault import
    meta["source"] = "vault-import"

    return meta


def _render_frontmatter_and_body(meta: dict, body: str) -> str:
    """Render a complete markdown file string from metadata dict + body."""
    post = fm_lib.Post(body, **meta)
    return fm_lib.dumps(post)


def plan_import(
    source_vault: Path,
    memory_dir: Path,
    into_category: Optional[str] = None,
) -> tuple[list[ImportPlan], list[str]]:
    """Walk source_vault and build an ImportPlan for each .md file.

    Returns:
        (plans, orphan_warnings) — plans is the list of ImportPlan objects;
        orphan_warnings is a list of human-readable messages about unresolved
        wikilinks (populated during wikilink translation on the second pass).
    """
    # Pass 1: collect all .md files and compute category + dest paths
    plans_pre: list[tuple[Path, Path, dict, str, str, str]] = []
    # (source_abs, source_rel, metadata, body, category, category_reason)

    used_dest_paths: set[Path] = set()

    source_files = []
    for dirpath, dirnames, filenames in os.walk(source_vault):
        # Prune hidden/skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            if fname.endswith(".md"):
                source_files.append(Path(dirpath) / fname)

    for src_abs in sorted(source_files):
        src_rel = src_abs.relative_to(source_vault)
        try:
            raw = src_abs.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Skipping %s: read error: %s", src_rel, exc)
            continue

        try:
            metadata, _ = parse_markdown(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s: parse error: %s", src_rel, exc)
            metadata = {}

        # Re-parse body separately for wikilink rewriting
        try:
            post = fm_lib.loads(raw)
            body = post.content
        except Exception:
            body = raw

        category, reason = _infer_category(src_rel, metadata, into_category)
        dest_path = _make_dest_path(memory_dir, src_rel, category, used_dest_paths)
        plans_pre.append((src_abs, src_rel, metadata, body, category, reason, dest_path))

    # Build slug map from the full set of planned destinations (before wikilink rewrite)
    slug_map: dict[str, str] = {}
    for src_abs, src_rel, metadata, body, category, reason, dest_path in plans_pre:
        src_slug = slugify(src_abs.stem)
        dest_rel = dest_path.stem
        slug_map[src_slug] = dest_rel
        slug_map[src_abs.stem.lower()] = dest_rel

    # Pass 2: translate wikilinks and build final ImportPlan objects
    plans: list[ImportPlan] = []
    orphan_warnings: list[str] = []

    for src_abs, src_rel, metadata, body, category, reason, dest_path in plans_pre:
        translated_body = _translate_wikilinks(body, slug_map, orphan_warnings, src_rel)

        merged_meta = _add_palinode_frontmatter(metadata, category, src_abs)
        content = _render_frontmatter_and_body(merged_meta, translated_body)

        plans.append(ImportPlan(
            source_path=src_abs,
            dest_path=dest_path,
            category=category,
            category_reason=reason,
            dest_exists=dest_path.exists(),
            content=content,
        ))

    return plans, orphan_warnings


def execute_import(
    plans: list[ImportPlan],
    overwrite: bool = False,
) -> ImportResult:
    """Write planned import files to disk.

    Args:
        plans: List of ImportPlan objects from plan_import().
        overwrite: If True, replace existing dest files. If False, skip them
            with a warning.

    Returns:
        ImportResult summary.
    """
    result = ImportResult(plans=plans)

    for plan in plans:
        if plan.dest_exists and not overwrite:
            result.skipped.append((plan.dest_path, "already exists (use --overwrite to replace)"))
            continue

        try:
            plan.dest_path.parent.mkdir(parents=True, exist_ok=True)
            # Route the write through the git_tools mutation choke point so the
            # vault-import write uses the same atomic primitive as every other
            # memory mutation (#564). Committing is left to the import CLI layer
            # (this function may target a dir other than config.memory_dir).
            git_tools.write_memory_file(str(plan.dest_path), plan.content)
            result.written.append(plan.dest_path)
        except OSError as exc:
            result.errors.append((plan.dest_path, f"write error: {exc}"))

    return result

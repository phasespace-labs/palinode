"""
Fact ID Generator

Adds inline fact IDs (<!-- fact:slug -->) to list items in memory files.
Run once to bootstrap IDs for existing files, then the consolidation
executor maintains them going forward.
"""
from __future__ import annotations

import os
import re
from palinode.core.config import config
from palinode.core import parser, git_tools
from palinode.core.hashing import stable_md5_hexdigest
from palinode.core.path_guard import resolve_memory_path, to_rel_path

#: The marker the consolidation runner harvests. Kept here, next to the code
#: that mints it, so the writer and the reader cannot drift: a bullet the
#: runner cannot see is a bullet consolidation will never compact.
FACT_LINE_RE = re.compile(r"^[\s]*[-*]\s+(.*?)<!-- fact:(\S+) -->", re.MULTILINE)

#: A markdown list item in a file *body*. Same shape the tagger matches.
_BULLET_RE = re.compile(r"^[\s]*[-*]\s+", re.MULTILINE)

#: Substring that marks a line as already carrying an id. Checked rather than
#: re-matching FACT_LINE_RE so a malformed marker still suppresses a second one.
_MARKER_PREFIX = "<!-- fact:"


def generate_fact_id(file_path: str, line_text: str) -> str:
    """Generate a deterministic fact ID from file path + content.
    
    Format: {category}-{file_slug}-{content_hash[:6]}
    Example: my-app-arch-a3f2b1
    """
    file_slug = os.path.splitext(os.path.basename(file_path))[0]
    content_hash = stable_md5_hexdigest(line_text.strip())[:6]
    return f"{file_slug}-{content_hash}"


def stamp_fact_id(file_path: str, line: str) -> str:
    """Return *line* with its deterministic fact marker appended.

    The single minting point. Every surface that appends a bullet to a
    consolidation target renders its line and then passes it through here, so a
    line written by the session-end append carries exactly the id a later
    ``bootstrap-ids`` pass would have given it — the runner harvests both the
    same way, and re-running the bootstrap over the file is a no-op.

    Idempotent: a line that already carries a marker is returned unchanged
    (minus a trailing newline), never double-stamped.
    """
    stripped = line.rstrip("\n")
    if _MARKER_PREFIX in stripped:
        return stripped
    return f"{stripped} {_MARKER_PREFIX}{generate_fact_id(file_path, stripped)} -->"


def count_body_facts(file_path: str) -> tuple[int, int]:
    """Return ``(body_bullets, tagged_bullets)`` for a memory file.

    Frontmatter is excluded on both counts, matching what the tagger writes and
    what the runner harvests — a ``- project/foo`` under ``entities:`` is YAML,
    not a fact. ``bullets > 0 and tagged == 0`` is the inert-target signature:
    the document has material to compact and the runner can address none of it.

    A file that cannot be read counts as ``(0, 0)`` — callers of this helper
    are reporters (the doctor check, the runner's skip partition), and neither
    should raise on an unreadable file when "nothing to compact" is the
    operationally identical answer.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return 0, 0
    _, body = parser.split_frontmatter(content)
    return len(_BULLET_RE.findall(body)), len(FACT_LINE_RE.findall(body))


def add_fact_ids_to_file(file_path: str) -> int:
    """Add fact IDs to all list items in a markdown file's **body**.

    Skips items that already have a fact ID comment, and never descends into
    the YAML frontmatter block: a frontmatter list entry (``- project/foo``
    under ``entities:``) is YAML syntax, not a memory fact. Tagging it made the
    consolidation executor treat it as a fact and rewrite it with LLM prose,
    which is how ``entities:`` came to hold status sentences that break strict
    ``yaml.safe_load``.

    Returns the number of IDs added.
    """
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    frontmatter_block, body = parser.split_frontmatter(content)
    lines = body.splitlines(keepends=True)

    modified = False
    count = 0
    new_lines = []

    for line in lines:
        # Match markdown list items (- or *) that don't already have a fact ID
        if re.match(r'^[\s]*[-*]\s+', line) and _MARKER_PREFIX not in line:
            # Through the shared stamp, so the bootstrap walk and the
            # session-end append cannot mint different ids for the same text.
            new_lines.append(stamp_fact_id(file_path, line) + "\n")
            modified = True
            count += 1
        else:
            new_lines.append(line)
    
    if modified:
        git_tools.write_memory_file(file_path, frontmatter_block + "".join(new_lines))
        if config.git.auto_commit:
            git_tools.commit_memory_file(
                file_path,
                f"{config.git.commit_prefix} bootstrap fact ids: {os.path.basename(file_path)}",
            )

    return count


def bootstrap_fact_ids_for_file(file_path: str) -> dict:
    """Tag exactly one memory file, named relative to the store.

    The whole-store walk is the wrong shape for the case that motivated this:
    one ``projects/<slug>-status.md`` fed by session-end holds hundreds of
    untagged bullets while every curated file around it is already tagged or
    deliberately untagged, and an operator told by ``doctor`` which document is
    inert should be able to fix that document alone.

    ``file_path`` crosses :func:`resolve_memory_path`, so an absolute path, a
    ``../`` traversal, or a symlink pointing out of the store is rejected before
    anything is read. Commits with the same provenance as the full walk —
    :func:`add_fact_ids_to_file` is the shared writer.

    Raises:
        PathTraversalError: the path resolves outside ``memory_dir``.
        FileNotFoundError: the path resolves inside the store but is not a file.
    """
    _, resolved = resolve_memory_path(file_path)
    if not os.path.isfile(resolved):
        raise FileNotFoundError(file_path)

    count = add_fact_ids_to_file(resolved)
    return {
        "files": 1 if count else 0,
        "facts_tagged": count,
        "file": to_rel_path(resolved),
    }


def bootstrap_all_fact_ids() -> dict:
    """Add fact IDs to all memory files in people/, projects/, decisions/, insights/.

    Operator-invoked, not automatic: reachable as ``POST /bootstrap-fact-ids``
    and ``palinode bootstrap-ids``, both registered admin capabilities. It walks
    all four memory directories, which is why frontmatter marker injection was
    never confined to status documents — every curated ``people/``,
    ``decisions/`` and ``insights/`` file was in range too. That is fixed at the
    source in :func:`add_fact_ids_to_file`, which now tags the body only;
    ``palinode repair-status --scope all`` removes markers a previous run left
    behind.

    Returns stats dict.
    """
    stats = {"files": 0, "facts_tagged": 0}
    dirs = ["people", "projects", "decisions", "insights"]
    
    for d in dirs:
        full_dir = os.path.join(config.memory_dir, d)
        if not os.path.exists(full_dir):
            continue
        for f in os.listdir(full_dir):
            if not f.endswith('.md'):
                continue
            fp = os.path.join(full_dir, f)
            count = add_fact_ids_to_file(fp)
            if count > 0:
                stats["files"] += 1
                stats["facts_tagged"] += count
    
    return stats

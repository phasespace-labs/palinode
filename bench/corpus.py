"""Deterministic synthetic corpus generator for the ingestion benchmark.

The generator is a **pure function of ``(seed, size)``**: identical inputs
always produce byte-identical files. That property is what makes the
determinism axis (bench axis 2) measurable — if the input corpus were not
reproducible, we could not attribute a fingerprint diff to the pipeline.

Content is entirely synthetic fixture material — ``my-project`` /
``other-project``, people ``Alice`` / ``Bob`` / ``Carol`` / ``Dave``, and a
fixed vocabulary of neutral engineering topics. No real memories, no secrets,
no host or infrastructure references.

Dates are derived deterministically from the file index (never ``now()``) so a
regenerated corpus carries identical frontmatter every time.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import yaml

# --- Fixture vocabulary (synthetic; scrub-clean) --------------------------

PROJECTS: tuple[str, ...] = ("my-project", "other-project")
PEOPLE: tuple[str, ...] = ("Alice", "Bob", "Carol", "Dave")

# Neutral engineering topics. Each topic recurs across several files, which is
# what makes keyword recall (bench axes 3/4) return deterministic hits.
TOPICS: tuple[str, ...] = (
    "database migration",
    "caching strategy",
    "auth token rotation",
    "search ranking",
    "config validation",
    "retry backoff",
    "index rebuild",
    "rate limiting",
    "schema versioning",
    "queue draining",
)

MEMORY_TYPES: tuple[str, ...] = ("Decision", "Insight", "ProjectSnapshot", "ActionItem")

CATEGORY_FOR_TYPE: dict[str, str] = {
    "Decision": "decisions",
    "Insight": "insights",
    "ProjectSnapshot": "projects",
    "ActionItem": "projects",
}

# Sentence templates. Sampled with the seeded RNG in a fixed order, so the
# generated prose is deterministic while still varied enough to be realistic.
_SENTENCE_TEMPLATES: tuple[str, ...] = (
    "{person} proposed handling {topic} in {project} by splitting the work into stages.",
    "The team agreed that {topic} needs a deterministic path before {project} can scale.",
    "In {project}, {topic} was the main source of friction this cycle.",
    "{person} noted that {topic} interacts badly with the existing retry logic.",
    "We measured {topic} on {project} and found the cold path dominates latency.",
    "A follow-up on {topic} is open: {person} will confirm the {project} rollout plan.",
    "The rationale for the {topic} change in {project} is repeatability, not speed.",
    "{person} reviewed {topic} and flagged one edge case in {project}.",
    "For {project}, {topic} should degrade gracefully when the backend is offline.",
    "The {topic} work in {project} is tracked so it can be diffed later.",
)

# A long file needs a body over the parser's 2000-char single-chunk threshold
# so it splits into multiple sections (multiple "facts"). These headings drive
# that split.
_LONG_SECTIONS: tuple[str, ...] = ("Context", "Decision", "Consequences", "Follow-up")

_BASE_DATE = date(2026, 1, 1)


@dataclass(frozen=True)
class Corpus:
    """Description of a generated corpus."""

    palinode_dir: str
    files: tuple[str, ...]          # absolute paths, sorted
    num_files: int
    seed: int
    queries: tuple[str, ...]        # recall queries guaranteed to have hits


def _iso_for_index(index: int) -> str:
    """Deterministic UTC ISO-8601 timestamp for a file index (never now())."""
    d = _BASE_DATE + timedelta(days=index)
    # Fixed time-of-day so the stamp is fully determined by the index.
    dt = datetime.combine(d, time(hour=9, minute=0, second=0), tzinfo=timezone.utc)
    return dt.isoformat()


def _sentences(rng: random.Random, project: str, person: str, topic: str, count: int) -> str:
    """Build *count* deterministic sentences for the given fixtures."""
    out = []
    for _ in range(count):
        template = rng.choice(_SENTENCE_TEMPLATES)
        out.append(template.format(project=project, person=person, topic=topic))
    return " ".join(out)


def _body_for(
    rng: random.Random,
    *,
    long: bool,
    project: str,
    person: str,
    topic: str,
    mem_type: str,
) -> str:
    """Render the markdown body (title + prose) for one memory file."""
    title = f"{mem_type}: {topic} in {project}"
    if not long:
        # Short file → single "root" chunk (< 2000 chars).
        prose = _sentences(rng, project, person, topic, count=3)
        return f"# {title}\n\n{prose}\n"

    # Long file → several H2 sections, each a "fact". Enough sentences per
    # section to push the body past the parser's 2000-char split threshold.
    parts = [f"# {title}", ""]
    for heading in _LONG_SECTIONS:
        parts.append(f"## {heading}")
        parts.append(_sentences(rng, project, person, topic, count=7))
        parts.append("")
    return "\n".join(parts) + "\n"


def _frontmatter(
    *,
    index: int,
    mem_type: str,
    category: str,
    slug: str,
    project: str,
    person: str,
    topic: str,
    core: bool,
) -> dict:
    """Deterministic YAML frontmatter for one memory file."""
    stamp = _iso_for_index(index)
    fm = {
        "id": f"{category}-{slug}",
        "category": category,
        "type": mem_type,
        "project": project,
        "tags": sorted({topic.split()[0], project, person.lower()}),
        "date": (_BASE_DATE + timedelta(days=index)).isoformat(),
        "created_at": stamp,
        "last_updated": stamp,
    }
    if core:
        fm["core"] = True
    return fm


def generate(palinode_dir: str, *, seed: int = 1337, size: int = 60) -> Corpus:
    """Write *size* synthetic memory files under *palinode_dir*.

    Pure function of ``(seed, size)`` (and the fixed *palinode_dir* prefix):
    the file bodies and frontmatter are byte-identical across calls.

    Args:
        palinode_dir: root directory to write files under (created if absent).
        seed: RNG seed controlling prose selection.
        size: number of memory files to generate.

    Returns:
        A :class:`Corpus` describing what was written.
    """
    rng = random.Random(seed)
    root = Path(palinode_dir)
    written: list[str] = []

    for index in range(size):
        mem_type = MEMORY_TYPES[index % len(MEMORY_TYPES)]
        category = CATEGORY_FOR_TYPE[mem_type]
        project = PROJECTS[index % len(PROJECTS)]
        person = PEOPLE[index % len(PEOPLE)]
        topic = TOPICS[index % len(TOPICS)]
        # Every 4th file is a multi-section (multi-fact) document.
        long = (index % 4) == 0
        # A small, deterministic subset is marked core (session-start material).
        core = (index % 11) == 0

        topic_slug = topic.replace(" ", "-")
        slug = f"{mem_type.lower()}-{index:03d}-{topic_slug}"
        stamp_date = (_BASE_DATE + timedelta(days=index)).isoformat()
        filename = f"{stamp_date}-{slug}.md"

        fm = _frontmatter(
            index=index,
            mem_type=mem_type,
            category=category,
            slug=slug,
            project=project,
            person=person,
            topic=topic,
            core=core,
        )
        body = _body_for(
            rng,
            long=long,
            project=project,
            person=person,
            topic=topic,
            mem_type=mem_type,
        )
        # sort_keys=True keeps the YAML byte-stable across runs.
        fm_yaml = yaml.dump(fm, default_flow_style=False, sort_keys=True)
        doc = f"---\n{fm_yaml}---\n\n{body}"

        dest_dir = root / category
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / filename
        path.write_text(doc, encoding="utf-8")
        written.append(str(path))

    return Corpus(
        palinode_dir=str(root),
        files=tuple(sorted(written)),
        num_files=size,
        seed=seed,
        # Queries are the topic phrases — each recurs across files, so every
        # query has deterministic keyword hits regardless of embedder state.
        queries=TOPICS,
    )

"""The versioned relevance/abstention fixture: records, questions, loading.

Two YAML files next to this module are the corpus. ``corpus.yaml`` holds the
records that get written into a throwaway ``PALINODE_DIR`` and indexed through
the real pipeline; ``questions.yaml`` holds the labelled question set. Both are
entirely synthetic — invented projects, people and identifiers — because this
directory ships publicly.

The corpus carries the noise the workstream named, not just the answers:

* **Old context notes.** Records that were true about an earlier release and
  still read as present-tense statements. Nothing marks them retired — no
  ``superseded_by`` chain, no archive location — because that is the common
  case in a real store and the one where recency alone must not settle the
  question.
* **Auto-footers.** Long records carry a ``## See also`` block behind the
  ``<!-- palinode-auto-footer -->`` marker. A body over the parser's 2000-char
  single-chunk threshold splits on H2, so the footer becomes its own chunk and
  a footer-only hit is reachable rather than hypothetical.
* **Near-duplicates.** Two ``dup_group`` pairs restate an existing record in
  different words, so "did the slate spend a slot saying the same thing twice"
  is measurable.
* **A second project.** ``tidewater`` shares vocabulary with the project under
  evaluation (both rejected dual-write, both have a batch window) so project
  isolation is tested by attraction, not by absence.

Held-out variants are authored rather than generated: a paraphrase that shares
few content words with the record answering it, and an exact-identifier query,
for a subset of the base questions. Generating them would make the held-out
split a function of the same templates the dev split uses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

#: Bumped when the records or the question set change in a way that invalidates
#: comparison with an earlier baseline's numbers.
CORPUS_VERSION = 1

#: Bumped independently of the corpus: a question may be relabelled without the
#: records moving.
QUESTION_SET_VERSION = 1

#: The four question classes the workstream names.
CLASSES: tuple[str, ...] = (
    "release_state",
    "rejected_approach",
    "changed_decision",
    "no_answer",
)

#: The minimum authored base questions per class. The workstream asks for at
#: least 40 real-shaped questions across four classes; ten each is the floor
#: that makes every class a denominator rather than an anecdote.
MIN_BASE_PER_CLASS = 10

#: The minimum total base questions.
MIN_BASE_QUESTIONS = 40

#: The minimum held-out variants of each kind.
MIN_PARAPHRASES = 8
MIN_IDENTIFIERS = 8

VARIANTS: tuple[str, ...] = ("base", "paraphrase", "identifier")

#: Record roles. The scorer does not branch on role — it exists so the corpus
#: says what each record is *for*, and so a coverage check can fail when a
#: noise class quietly disappears from the fixture.
ROLES: tuple[str, ...] = (
    "current_state",
    "current_decision",
    "superseded",
    "proposal",
    "scoped_note",
    "rejected",
    "old_context",
    "duplicate",
    "noise",
    "cross_project",
)

#: Roles the corpus must always contain at least one of. These are the noise
#: classes the reproduction lead names; a fixture that lost one would score
#: better for the wrong reason.
REQUIRED_ROLES: frozenset[str] = frozenset({
    "current_state",
    "current_decision",
    "superseded",
    "proposal",
    "rejected",
    "old_context",
    "duplicate",
    "noise",
    "cross_project",
})

AUTO_FOOTER_MARKER = "<!-- palinode-auto-footer -->"

_DEFAULT_RECORDS = os.path.join(os.path.dirname(__file__), "corpus.yaml")
_DEFAULT_QUESTIONS = os.path.join(os.path.dirname(__file__), "questions.yaml")


# ── schema ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Record:
    """One fixture memory, as authored."""

    id: str
    project: str
    category: str
    type: str
    role: str
    date: str
    title: str
    body: str = ""
    sections: tuple[tuple[str, str], ...] = ()
    entities: tuple[str, ...] = ()
    dup_group: str | None = None
    epistemic: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.date}-{self.id}"

    @property
    def rel_path(self) -> str:
        return os.path.join(self.category, f"{self.slug}.md")


@dataclass(frozen=True)
class Question:
    """One labelled question."""

    id: str
    cls: str
    variant: str
    project: str
    ask: str
    relevant: tuple[str, ...] = ()
    tolerated: tuple[str, ...] = ()
    must_not_be_top: tuple[str, ...] = ()
    of: str | None = None

    @property
    def answerable(self) -> bool:
        return bool(self.relevant)


@dataclass(frozen=True)
class Fixture:
    """A loaded corpus plus its question set."""

    corpus_version: int
    question_set_version: int
    records: tuple[Record, ...]
    questions: tuple[Question, ...]
    projects: dict[str, str] = field(default_factory=dict)
    records_source: str = ""
    questions_source: str = ""

    @property
    def by_id(self) -> dict[str, Record]:
        return {record.id: record for record in self.records}

    def question(self, question_id: str) -> Question:
        for question in self.questions:
            if question.id == question_id:
                return question
        raise KeyError(question_id)


# ── rendering ────────────────────────────────────────────────────────────────


def _entity_slug(ref: str) -> str:
    """``project/harborlight`` → ``harborlight`` (the wikilink target)."""
    return ref.rsplit("/", 1)[-1]


def render_footer(entities: tuple[str, ...]) -> str:
    """Render the auto-generated ``## See also`` block for *entities*.

    Same shape the save path's footer writer emits (``## See also`` heading,
    the marker as the first line of the block, one wikilink bullet per entity),
    because the thing being measured is what that footer does to retrieval.
    """
    if not entities:
        return ""
    links = "\n".join(f"- [[{_entity_slug(ref)}]]" for ref in entities)
    return f"## See also\n{AUTO_FOOTER_MARKER}\n{links}\n"


def render_body(record: Record) -> str:
    """The markdown body (title, prose or sections, footer) for *record*."""
    parts = [f"# {record.title}", ""]
    if record.sections:
        for heading, text in record.sections:
            parts.append(f"## {heading}")
            parts.append(text.strip())
            parts.append("")
    else:
        parts.append(record.body.strip())
        parts.append("")
    footer = render_footer(record.entities)
    if footer:
        parts.append(footer)
    return "\n".join(parts).rstrip() + "\n"


def render_file(record: Record) -> str:
    """Frontmatter plus body — the bytes written into the throwaway store."""
    stamp = f"{record.date}T09:00:00+00:00"
    meta: dict[str, Any] = {
        "id": record.id,
        "category": record.category,
        "type": record.type,
        "project": record.project,
        "date": record.date,
        "created_at": stamp,
        "last_updated": stamp,
        "tags": [record.project],
    }
    if record.entities:
        meta["entities"] = list(record.entities)
    if record.epistemic:
        meta["epistemic"] = record.epistemic
    front = yaml.dump(meta, default_flow_style=False, sort_keys=True)
    return f"---\n{front}---\n\n{render_body(record)}"


def materialize(fixture: Fixture, palinode_dir: str) -> dict[str, str]:
    """Write every record under *palinode_dir*; return ``id → absolute path``.

    Byte-identical for a given fixture and directory: no ``now()``, no RNG.
    """
    paths: dict[str, str] = {}
    for record in fixture.records:
        path = os.path.join(palinode_dir, record.rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(render_file(record))
        paths[record.id] = os.path.abspath(path)
    return paths


# ── loading ──────────────────────────────────────────────────────────────────


def _record(raw: dict[str, Any]) -> Record:
    sections = tuple(
        (str(section["heading"]), str(section["text"]))
        for section in (raw.get("sections") or [])
    )
    return Record(
        id=str(raw["id"]),
        project=str(raw["project"]),
        category=str(raw["category"]),
        type=str(raw["type"]),
        role=str(raw["role"]),
        date=str(raw["date"]),
        title=str(raw["title"]),
        body=str(raw.get("body") or ""),
        sections=sections,
        entities=tuple(str(e) for e in (raw.get("entities") or ())),
        dup_group=raw.get("dup_group"),
        epistemic=raw.get("epistemic"),
    )


def _question(raw: dict[str, Any]) -> Question:
    return Question(
        id=str(raw["id"]),
        cls=str(raw["cls"]),
        variant=str(raw["variant"]),
        project=str(raw["project"]),
        ask=str(raw["ask"]),
        relevant=tuple(str(r) for r in (raw.get("relevant") or ())),
        tolerated=tuple(str(r) for r in (raw.get("tolerated") or ())),
        must_not_be_top=tuple(str(r) for r in (raw.get("must_not_be_top") or ())),
        of=raw.get("of"),
    )


def load_fixture(
    records_path: str | None = None, questions_path: str | None = None
) -> Fixture:
    """Load and validate the corpus and the question set.

    Raises ``ValueError`` on anything structurally wrong: a question naming a
    record that does not exist, a no-answer case with expected refs, a class
    below its minimum, an unknown role.
    """
    records_source = records_path or _DEFAULT_RECORDS
    questions_source = questions_path or _DEFAULT_QUESTIONS
    with open(records_source, encoding="utf-8") as handle:
        raw_records = yaml.safe_load(handle)
    with open(questions_source, encoding="utf-8") as handle:
        raw_questions = yaml.safe_load(handle)

    corpus_version = int(raw_records.get("corpus_version", 0))
    if corpus_version != CORPUS_VERSION:
        raise ValueError(
            f"corpus_version {corpus_version} != {CORPUS_VERSION}; scores are "
            "only comparable within a version"
        )
    question_set_version = int(raw_questions.get("question_set_version", 0))
    if question_set_version != QUESTION_SET_VERSION:
        raise ValueError(
            f"question_set_version {question_set_version} != {QUESTION_SET_VERSION}"
        )

    fixture = Fixture(
        corpus_version=corpus_version,
        question_set_version=question_set_version,
        records=tuple(_record(r) for r in raw_records["records"]),
        questions=tuple(_question(q) for q in raw_questions["questions"]),
        projects=dict(raw_records.get("projects") or {}),
        records_source=records_source,
        questions_source=questions_source,
    )
    validate(fixture)
    return fixture


def validate(fixture: Fixture) -> None:
    """Raise ``ValueError`` describing every structural problem at once."""
    problems: list[str] = []
    by_id = fixture.by_id
    if len(by_id) != len(fixture.records):
        problems.append("duplicate record ids")

    seen_paths: set[str] = set()
    for record in fixture.records:
        if record.role not in ROLES:
            problems.append(f"{record.id}: unknown role {record.role!r}")
        if record.project not in fixture.projects:
            problems.append(f"{record.id}: project {record.project!r} is undeclared")
        if not record.body and not record.sections:
            problems.append(f"{record.id}: neither body nor sections")
        if record.body and record.sections:
            problems.append(f"{record.id}: both body and sections")
        if record.rel_path in seen_paths:
            problems.append(f"{record.id}: duplicate file path {record.rel_path}")
        seen_paths.add(record.rel_path)
        if record.sections and len(render_body(record)) < 2000:
            problems.append(
                f"{record.id}: sectioned records must exceed the parser's "
                "2000-char single-chunk threshold or they never split on H2"
            )

    missing_roles = REQUIRED_ROLES - {record.role for record in fixture.records}
    for role in sorted(missing_roles):
        problems.append(f"corpus has no record with the required role {role!r}")

    question_ids: set[str] = set()
    for question in fixture.questions:
        if question.id in question_ids:
            problems.append(f"{question.id}: duplicate question id")
        question_ids.add(question.id)
        if question.cls not in CLASSES:
            problems.append(f"{question.id}: unknown class {question.cls!r}")
        if question.variant not in VARIANTS:
            problems.append(f"{question.id}: unknown variant {question.variant!r}")
        if not question.ask.strip():
            problems.append(f"{question.id}: empty question")
        if question.project not in fixture.projects:
            problems.append(f"{question.id}: project {question.project!r} is undeclared")
        for label, refs in (
            ("relevant", question.relevant),
            ("tolerated", question.tolerated),
            ("must_not_be_top", question.must_not_be_top),
        ):
            for ref in refs:
                if ref not in by_id:
                    problems.append(f"{question.id}: {label} ref {ref!r} is not a record")
        overlap = set(question.relevant) & set(question.tolerated)
        if overlap:
            problems.append(
                f"{question.id}: {sorted(overlap)} is both relevant and tolerated"
            )
        trap_overlap = set(question.relevant) & set(question.must_not_be_top)
        if trap_overlap:
            problems.append(
                f"{question.id}: {sorted(trap_overlap)} is both relevant and a "
                "must-not-be-top trap"
            )
        if question.cls == "no_answer" and question.relevant:
            problems.append(f"{question.id}: a no-answer case must have no relevant refs")
        if question.cls != "no_answer" and not question.relevant:
            problems.append(f"{question.id}: an answerable case needs relevant refs")
        if question.variant == "base" and question.of:
            problems.append(f"{question.id}: a base question cannot derive from another")

    for question in fixture.questions:
        if question.of and question.of not in question_ids:
            problems.append(f"{question.id}: derives from unknown question {question.of!r}")

    counts = class_counts(fixture)
    base_total = sum(counts[cls]["base"] for cls in CLASSES)
    if base_total < MIN_BASE_QUESTIONS:
        problems.append(
            f"{base_total} base questions, minimum is {MIN_BASE_QUESTIONS}"
        )
    for cls in CLASSES:
        if counts[cls]["base"] < MIN_BASE_PER_CLASS:
            problems.append(
                f"class {cls}: {counts[cls]['base']} base questions, minimum is "
                f"{MIN_BASE_PER_CLASS}"
            )
    variants = variant_counts(fixture)
    if variants.get("paraphrase", 0) < MIN_PARAPHRASES:
        problems.append(
            f"{variants.get('paraphrase', 0)} paraphrases, minimum is {MIN_PARAPHRASES}"
        )
    if variants.get("identifier", 0) < MIN_IDENTIFIERS:
        problems.append(
            f"{variants.get('identifier', 0)} identifier variants, minimum is "
            f"{MIN_IDENTIFIERS}"
        )

    if problems:
        raise ValueError("fixture validation failed:\n  " + "\n  ".join(problems))


# ── coverage ─────────────────────────────────────────────────────────────────


def class_counts(fixture: Fixture) -> dict[str, dict[str, int]]:
    """Per-class counts, split by variant."""
    counts = {
        cls: {variant: 0 for variant in VARIANTS} | {"total": 0} for cls in CLASSES
    }
    for question in fixture.questions:
        counts[question.cls][question.variant] += 1
        counts[question.cls]["total"] += 1
    return counts


def variant_counts(fixture: Fixture) -> dict[str, int]:
    """How many questions of each variant."""
    counts = {variant: 0 for variant in VARIANTS}
    for question in fixture.questions:
        counts[question.variant] += 1
    return counts


def role_counts(fixture: Fixture) -> dict[str, int]:
    """How many records of each role."""
    counts = {role: 0 for role in ROLES}
    for record in fixture.records:
        counts[record.role] += 1
    return counts


__all__ = [
    "AUTO_FOOTER_MARKER",
    "CLASSES",
    "CORPUS_VERSION",
    "MIN_BASE_PER_CLASS",
    "MIN_BASE_QUESTIONS",
    "MIN_IDENTIFIERS",
    "MIN_PARAPHRASES",
    "QUESTION_SET_VERSION",
    "REQUIRED_ROLES",
    "ROLES",
    "VARIANTS",
    "Fixture",
    "Question",
    "Record",
    "class_counts",
    "load_fixture",
    "materialize",
    "render_body",
    "render_file",
    "render_footer",
    "role_counts",
    "validate",
    "variant_counts",
]

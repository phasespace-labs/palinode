"""A realistic background corpus for an agent-tasks store.

A run's throwaway store otherwise holds only the cells' own records: a few
dozen chunks that all say "ledger migration batch ID". In a store like that
every task term sits at FTS5's IDF floor, so the keyword arm cannot vouch for
the right record. ``/resolve`` then reports ``weak, corroboration: missing``,
and the per-prompt hook stays silent. A real store has thousands of unrelated
rows, and that background is what gives BM25 its statistics.

This module writes such a background as markdown files, ready to be copied
into the store before any cell runs and indexed through the real pipeline. It
has two sources, in this order:

1. The ``bench.relevance`` fixture records: synthetic projects, people and
   decisions, authored as memory, each tagged ``project/<its project>`` so
   project isolation withholds it from a cell's scoped requests.
2. Notes built from the docstrings of the local Python standard library, and
   optionally of other installed open-source package trees (``extra_roots``),
   read with :mod:`ast` (nothing is imported). Each documented module, class or
   function becomes one short note. This is varied technical prose with a
   natural term distribution.

Nothing here is private memory. Both sources are public or synthetic, and the
stdlib text is read at build time and never committed.

**Isolation.**
- Background notes carry their own ``stdlib-*`` project tags, or none (global).
  The relevance records keep their fixture projects. None of these can match a
  cell's project, because cell projects end in a hex suffix.
- Any section with a batch-ID-shaped token (``word-1a2b3c4d``, the shape the
  grader stamps) is dropped, as is any section that mentions a batch ID or
  ``BATCH_ID``. With ``exclude_tokens``, so is any section containing one of
  the plan's stamped tokens. Background can therefore never hold a value the
  grader would count as foreign, nor a competing answer to the task.

The output is deterministic for a given Python standard library, ``as_of`` and
target. ``manifest.json`` records the Python version and a content digest, so
two runs can be compared.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sysconfig
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Iterator

import yaml

#: Bumped when the generator's output changes for the same inputs.
BACKGROUND_VERSION = 2

#: The shape of the grader's stamped tokens (``plan.Tokens.token``): a word, a
#: hyphen or underscore, 8 lowercase hex digits. The grader counts a plan value
#: as present on a plain substring match, so this is unanchored: an embedded
#: ``xatlas-5b2d0c199`` drops its section too. Package metadata, hashes and
#: UUID fragments in docstrings are what it catches in practice.
BATCH_SHAPED = re.compile(r"[A-Za-z]+[-_][0-9a-f]{8}")
#: Text that could read as an answer to the agent-tasks prompts.
TASK_WORDS = re.compile(r"batch[\s_-]*id|\bBATCH_ID\b", re.IGNORECASE)

#: Background project tags; every one is a ``stdlib-*`` slug, and cell projects
#: always end in a hex suffix, so the two cannot collide.
PROJECTS: tuple[str, ...] = (
    "stdlib-asyncio", "stdlib-email", "stdlib-http", "stdlib-json", "stdlib-logging",
    "stdlib-multiprocessing", "stdlib-os", "stdlib-sqlite", "stdlib-typing",
    "stdlib-unittest", "stdlib-xml", "stdlib-misc",
)
#: Share of notes left global (no project), as a real store has.
GLOBAL_EVERY = 4

#: The parser keeps a body under this many characters as one chunk and splits
#: a longer one on H2. Kept in step with palinode's single-chunk threshold so
#: the estimate here is close to what the index will hold.
SINGLE_CHUNK_CHARS = 2000
#: Cap on one section's text, so one docstring cannot become a giant chunk.
SECTION_CHARS = 1400
#: Sections per note. One keeps each note a short memory-sized record that
#: indexes as one chunk, which is what most real notes are.
SECTIONS_PER_NOTE = 1
#: Shortest docstring worth a section.
MIN_DOC_CHARS = 40

#: Stdlib directories that are tests, demos or vendored data rather than library
#: text.
_SKIP_DIRS = {"test", "tests", "idle_test", "__pycache__", "site-packages", "dist-packages",
              "lib2to3", "ensurepip", "turtledemo", "pydoc_data"}


@dataclass(frozen=True)
class Section:
    heading: str
    text: str


@dataclass(frozen=True)
class Note:
    rel_path: str
    title: str
    type: str
    project: str | None
    created: str
    sections: tuple[Section, ...]

    def body(self) -> str:
        parts = [f"# {self.title}", ""]
        for s in self.sections:
            parts += [f"## {s.heading}", s.text.strip(), ""]
        return "\n".join(parts).rstrip() + "\n"

    def estimated_chunks(self) -> int:
        return len(self.sections) if len(self.body()) > SINGLE_CHUNK_CHARS else 1

    def render(self) -> str:
        stamp = f"{self.created}T09:00:00+00:00"
        meta: dict[str, object] = {
            "category": self.rel_path.split("/", 1)[0],
            "type": self.type,
            "title": self.title,
            "created_at": stamp,
            "last_updated": stamp,
            "source": "bench-background",
        }
        if self.project:
            meta["entities"] = [f"project/{self.project}"]
        front = yaml.dump(meta, default_flow_style=False, sort_keys=True)
        return f"---\n{front}---\n\n{self.body()}"


def drop_reason(text: str, exclude_tokens: frozenset[str] = frozenset()) -> str | None:
    """Why *text* may not go into the background, or ``None`` when it may."""
    if BATCH_SHAPED.search(text):
        return "batch_shaped"
    if TASK_WORDS.search(text):
        return "task_words"
    if any(tok in text for tok in exclude_tokens):
        return "plan_token"
    return None


def clean(text: str, exclude_tokens: frozenset[str] = frozenset()) -> bool:
    """True when *text* may go into the background."""
    return drop_reason(text, exclude_tokens) is None


#: Sections (stdlib) and records (relevance) dropped, by reason, since the last
#: :func:`build` began. Reported in the manifest.
DROPPED: dict[str, int] = {}


def _drop(reason: str) -> None:
    DROPPED[reason] = DROPPED.get(reason, 0) + 1


def stdlib_root() -> str:
    return sysconfig.get_paths()["stdlib"]


def _module_files(root: str) -> Iterator[tuple[str, str]]:
    """``(module name, path)`` for every stdlib ``.py`` file, sorted."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
                             and not d.endswith((".dist-info", ".egg-info")))
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)[:-3].replace(os.sep, ".")
            if rel.endswith(".__init__"):
                rel = rel[: -len(".__init__")]
            if rel.startswith("_") and "." not in rel:
                continue  # private top-level helpers: little prose
            yield rel, path


def _sections(module: str, path: str, exclude_tokens: frozenset[str]) -> list[Section]:
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError, ValueError, OSError):
        return []
    out: list[Section] = []
    doc = ast.get_docstring(tree)
    if doc and len(doc) >= MIN_DOC_CHARS:
        out.append(Section("Overview", doc))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            d = ast.get_docstring(node)
            if d and len(d) >= MIN_DOC_CHARS:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                out.append(Section(f"{node.name} ({kind})", d))
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("_"):
                        d = ast.get_docstring(sub)
                        if d and len(d) >= MIN_DOC_CHARS:
                            out.append(Section(f"{node.name}.{sub.name}", d))
    kept = []
    for s in out:
        text = s.text.strip()[:SECTION_CHARS]
        why = drop_reason(s.heading + "\n" + text, exclude_tokens)
        if why:
            _drop(why)
        else:
            kept.append(Section(s.heading, text))
    return kept


def _stable(n: str) -> int:
    return int(hashlib.sha256(n.encode()).hexdigest()[:8], 16)


def _project_for(module: str, index: int) -> str | None:
    if index % GLOBAL_EVERY == 0:
        return None
    top = module.split(".", 1)[0]
    by_name = {p.removeprefix("stdlib-"): p for p in PROJECTS}
    if top in by_name:
        return by_name[top]
    return PROJECTS[_stable(top) % len(PROJECTS)]


def stdlib_notes(as_of: date, *, roots: Iterable[str] | None = None,
                 exclude_tokens: frozenset[str] = frozenset()) -> Iterator[Note]:
    """Notes from the docstrings under *roots* (default: the stdlib), in a fixed order."""
    index = 0
    for module, path in (mp for r in (list(roots) if roots else [stdlib_root()]) for mp in _module_files(r)):
        secs = _sections(module, path, exclude_tokens)
        for part in range(0, len(secs), SECTIONS_PER_NOTE):
            chunk = tuple(secs[part: part + SECTIONS_PER_NOTE])
            if not chunk:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", f"{module}-{part // SECTIONS_PER_NOTE}".lower()).strip("-")
            category, kind = ("research", "ResearchRef") if _stable(slug) % 3 == 0 else ("insights", "Insight")
            created = (as_of - timedelta(days=1 + _stable(slug) % 180)).isoformat()
            yield Note(
                rel_path=f"{category}/bg-{slug}.md",
                title=f"Notes on {module}" + (f" (part {part // SECTIONS_PER_NOTE + 1})" if part else ""),
                type=kind,
                project=_project_for(module, index),
                created=created,
                sections=chunk,
            )
            index += 1


def relevance_files(exclude_tokens: frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """``(rel_path, text)`` for the bench.relevance fixture records that pass :func:`clean`."""
    import dataclasses

    from bench.relevance.corpus import load_fixture, render_file

    out = []
    for record in load_fixture().records:
        # The fixture names its project in a ``project:`` field, which project
        # isolation does not read: without an entity ref the record is global,
        # and another project's decision would show, unlabelled, in every
        # cell's scoped search.
        ref = f"project/{record.project}"
        if ref not in record.entities:
            record = dataclasses.replace(record, entities=(*record.entities, ref))
        text = render_file(record)
        why = drop_reason(text, exclude_tokens)
        if why:
            _drop("relevance_" + why)
        else:
            out.append((record.rel_path, text))
    return out


def plan_tokens(plan_path: str) -> frozenset[str]:
    """Every stamped token in a plan.json, so none can appear in the background."""
    with open(plan_path, encoding="utf-8") as handle:
        plan = json.load(handle)
    toks: set[str] = set()
    for cell in plan.get("cells", []):
        for v in (cell.get("tokens") or {}).values():
            if isinstance(v, str) and len(v) >= 6:
                toks.add(v)
        if cell.get("project"):
            toks.add(str(cell["project"]))
    return frozenset(toks)


def build(out_dir: str, *, as_of: date, target_chunks: int = 6000,
          extra_roots: Iterable[str] = (), exclude_tokens: Iterable[str] = (),
          include_relevance: bool = True) -> dict[str, object]:
    """Write the background into *out_dir* (must be empty or absent); return the manifest."""
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise ValueError(f"{out_dir} is not empty")
    excl = frozenset(exclude_tokens)
    DROPPED.clear()
    digest = hashlib.sha256()
    files = notes = est = 0
    by_project: dict[str, int] = {}

    def write(rel: str, text: str) -> None:
        nonlocal files
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        digest.update(rel.encode() + b"\0" + text.encode())
        files += 1

    relevance = relevance_files(excl) if include_relevance else []
    for rel, text in relevance:
        write(rel, text)
        est += 1 if len(text) <= SINGLE_CHUNK_CHARS else max(1, text.count("\n## "))
    roots = [stdlib_root(), *extra_roots]
    seen: set[str] = set()
    for note in stdlib_notes(as_of, roots=roots, exclude_tokens=excl):
        if note.rel_path in seen:
            continue  # the same module name under two roots
        seen.add(note.rel_path)
        if est >= target_chunks:
            break
        write(note.rel_path, note.render())
        notes += 1
        est += note.estimated_chunks()
        key = note.project or "(global)"
        by_project[key] = by_project.get(key, 0) + 1
    import sys

    # Re-scan what was written with the same patterns: the guarantee is about
    # the files the store will index, not about the filter's intent.
    remaining = 0
    for dirpath, _, filenames in os.walk(out_dir):
        for name in filenames:
            if name.endswith(".md"):
                with open(os.path.join(dirpath, name), encoding="utf-8") as handle:
                    remaining += drop_reason(handle.read(), excl) is not None
    if remaining:
        raise RuntimeError(f"{remaining} written background file(s) still match a drop pattern")
    manifest = {
        "background_version": BACKGROUND_VERSION,
        "as_of": as_of.isoformat(),
        "python": sys.version.split()[0],
        "roots": roots,
        "target_chunks": target_chunks,
        "estimated_chunks": est,
        "files": files,
        "relevance_records": len(relevance),
        "stdlib_notes": notes,
        "notes_by_project": dict(sorted(by_project.items())),
        "excluded_tokens": len(excl),
        "dropped": dict(sorted(DROPPED.items())),
        "rescan_matches_remaining": remaining,
        "batch_shaped_pattern": BATCH_SHAPED.pattern,
        "sha256": digest.hexdigest(),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    return manifest

"""Status-doc frontmatter integrity + the one-time repair pass (#470, #679).

#470 filed the symptom — ``entities:`` holding freeform ``[2026-05-24] …``
status sentences, which strict YAML reads as a flow-sequence opener — and
hypothesised a serialization bug in "whatever writes the status doc". The actual
mechanism is different and is what these tests pin:

1. ``fact_ids.add_fact_ids_to_file`` tagged **every** ``- item`` line in the
   file, YAML frontmatter included, so ``- project/infrastructure`` under
   ``entities:`` acquired a ``<!-- fact:… -->`` marker and became a distinct
   entity reference;
2. the executor's fact regexes are whole-file + MULTILINE, so a later UPDATE /
   SUPERSEDE rewrote that frontmatter line with LLM-supplied status prose, and
   ARCHIVE deleted it outright (leaving ``entities: null``).

Every frontmatter writer in the codebase already goes through ``yaml.dump``,
which quotes ``[``-leading scalars — so no writer could have produced the
reported YAML. The corruption was post-hoc. The fix is a frontmatter boundary,
not a serializer change.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from palinode.cli.repair_status import repair_status
from palinode.consolidation import fact_ids as fact_ids_mod
from palinode.consolidation.executor import apply_operations
from palinode.consolidation.status_doc import (
    FRONTMATTER_RE,
    fact_ids,
    repair_status_doc,
    split_frontmatter,
    strip_frontmatter_fact_markers,
)
from palinode.core.config import config

CLEAN_DOC = (
    "---\n"
    "id: project-infra-status\n"
    "category: project\n"
    "entities:\n"
    "- project/infrastructure\n"
    "---\n\n"
    "# Infra Status\n\n"
    "- [2026-05-01] A genuine fact. <!-- fact:infra-a1b2c3 -->\n"
)

# The reported corruption shape, reproduced: a frontmatter entity that was
# fact-tagged and then rewritten by the executor with status prose.
CORRUPTED_DOC = (
    "---\n"
    "id: project-infra-status\n"
    "category: project\n"
    "entities:\n"
    "- [2026-05-24] Infra is in active, daily evolution #status: active"
    " <!-- fact:infra-status-dbba20 -->\n"
    "- project/infrastructure\n"
    "---\n\n"
    "# Infra Status\n\n"
    "- [2026-05-01] A genuine fact. <!-- fact:infra-a1b2c3 -->\n\n"
    "## Consolidation Log\n\n"
    "### 2026-06-01\n"
    "- [KEEP] infra-a1b2c3: \n"
    "- [ARCHIVE] supersedes-supersedes-supersedes-infra-status-dbba20: \n"
    "- [SUPERSEDE] [the original, non-superseded status if unique]: \n"
    "- [UPDATE] infra-a1b2c3: rewrote the date prefix\n\n"
    "- [2026-06-01] Session: shipped the thing\n"
)


def _strict_parse(text: str) -> dict:
    match = FRONTMATTER_RE.match(text)
    assert match is not None
    return yaml.safe_load(match.group(1))


# ── prevention: the frontmatter boundary ─────────────────────────────────────


def test_fact_ids_never_tag_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(CLEAN_DOC, encoding="utf-8")

    added = fact_ids_mod.add_fact_ids_to_file(str(path))

    text = path.read_text(encoding="utf-8")
    frontmatter_block, body = split_frontmatter(text)
    assert added == 0  # the one body bullet already carries an id
    assert "<!-- fact:" not in frontmatter_block
    assert _strict_parse(text)["entities"] == ["project/infrastructure"]
    assert "<!-- fact:infra-a1b2c3 -->" in body


def test_fact_ids_still_tag_body_items(tmp_path, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(CLEAN_DOC + "- [2026-05-02] An untagged fact.\n", encoding="utf-8")

    added = fact_ids_mod.add_fact_ids_to_file(str(path))

    text = path.read_text(encoding="utf-8")
    assert added == 1
    assert "An untagged fact. <!-- fact:" in text
    assert "<!-- fact:" not in split_frontmatter(text)[0]


def test_executor_never_rewrites_frontmatter(tmp_path, monkeypatch):
    """A legacy file whose frontmatter carries a fact marker: the executor must
    leave the frontmatter byte-for-byte alone."""
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(CORRUPTED_DOC, encoding="utf-8")
    before = split_frontmatter(CORRUPTED_DOC)[0]

    stats = apply_operations(str(path), [
        {"op": "UPDATE", "id": "infra-status-dbba20",
         "new_text": "[2026-06-10] LLM prose that must not land in YAML"},
        {"op": "ARCHIVE", "id": "infra-status-dbba20"},
    ])

    after = split_frontmatter(path.read_text(encoding="utf-8"))[0]
    assert after == before
    assert stats["updated"] == 0 and stats["archived"] == 0


def test_executor_still_mutates_body_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(CLEAN_DOC, encoding="utf-8")

    stats = apply_operations(str(path), [
        {"op": "UPDATE", "id": "infra-a1b2c3", "new_text": "[2026-05-01] Revised."},
    ])

    text = path.read_text(encoding="utf-8")
    assert stats["updated"] == 1
    assert "Revised. <!-- fact:infra-a1b2c3 -->" in text
    assert _strict_parse(text)["entities"] == ["project/infrastructure"]


# ── repair ───────────────────────────────────────────────────────────────────


def test_repair_makes_frontmatter_strict_parseable():
    # Precondition: the corrupted shape really does break strict YAML.
    raw = FRONTMATTER_RE.match(CORRUPTED_DOC)
    try:
        yaml.safe_load(raw.group(1))
        broke = False
    except yaml.YAMLError:
        broke = True
    assert broke, "fixture no longer reproduces the #470 parse failure"

    repaired, report = repair_status_doc(CORRUPTED_DOC)

    meta = _strict_parse(repaired)
    assert meta["entities"] == ["project/infrastructure"]
    assert report["frontmatter_markers_stripped"] == 1
    assert report["entities_relocated"] == 1


def test_repair_relocates_rather_than_drops_non_entity_values():
    repaired, _ = repair_status_doc(CORRUPTED_DOC)
    assert "## Recovered from frontmatter" in repaired
    assert "Infra is in active, daily evolution" in split_frontmatter(repaired)[1]


def test_repair_elides_uninformative_and_unresolvable_log_lines():
    repaired, report = repair_status_doc(CORRUPTED_DOC)
    body = split_frontmatter(repaired)[1]

    # Blank KEEP and the prose-as-id line are gone; the self-nesting chain is
    # replaced by the sentinel; the informative line survives verbatim.
    assert "- [KEEP] infra-a1b2c3:" not in body
    assert "the original, non-superseded status" not in body
    assert "supersedes-supersedes" not in body
    assert "- [ARCHIVE] (unresolved):" in body
    assert "- [UPDATE] infra-a1b2c3: rewrote the date prefix" in body
    assert report["log_lines_elided"] == 2
    assert report["log_ids_unresolved"] == 1


def test_repair_preserves_session_bullets_and_facts():
    repaired, _ = repair_status_doc(CORRUPTED_DOC)
    assert "- [2026-06-01] Session: shipped the thing" in repaired
    assert "<!-- fact:infra-a1b2c3 -->" in repaired


def test_repair_reconciles_frontmatter_counts():
    repaired, _ = repair_status_doc(CORRUPTED_DOC)
    meta = _strict_parse(repaired)
    body = split_frontmatter(repaired)[1]
    assert meta["memory_count"] == len(fact_ids(body)) == 1
    assert meta["date_range"] == "2026-05-01 to 2026-06-01"


def test_repair_is_idempotent():
    once, _ = repair_status_doc(CORRUPTED_DOC)
    twice, report = repair_status_doc(once)
    # last_updated moves; everything else must be stable.
    assert split_frontmatter(once)[1] == split_frontmatter(twice)[1]
    assert report == {
        "frontmatter_markers_stripped": 0,
        "entities_relocated": 0,
        "log_lines_elided": 0,
        "log_ids_unresolved": 0,
    }


def test_repair_resolves_ids_from_the_history_sibling():
    """A legitimately ARCHIVE'd fact lives on in ``-history.md`` — its log entry
    must keep its id rather than being flagged unresolved."""
    repaired, _ = repair_status_doc(
        CORRUPTED_DOC, extra_known_ids={"supersedes-supersedes-supersedes-infra-status-dbba20"}
    )
    assert "- [ARCHIVE] supersedes-supersedes-supersedes-infra-status-dbba20:" in repaired


def test_repair_leaves_a_clean_document_alone():
    clean = CLEAN_DOC
    repaired, report = repair_status_doc(clean)
    assert split_frontmatter(repaired)[1] == split_frontmatter(clean)[1]
    assert report == {
        "frontmatter_markers_stripped": 0,
        "entities_relocated": 0,
        "log_lines_elided": 0,
        "log_ids_unresolved": 0,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def _memory_dir(tmp_path: Path, monkeypatch) -> Path:
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "infra-status.md").write_text(CORRUPTED_DOC, encoding="utf-8")
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    return projects


def test_cli_dry_run_reports_without_writing(tmp_path, monkeypatch):
    projects = _memory_dir(tmp_path, monkeypatch)
    before = (projects / "infra-status.md").read_bytes()

    result = CliRunner().invoke(repair_status, [])

    assert result.exit_code == 0, result.output
    assert "would repair" in result.output
    assert (projects / "infra-status.md").read_bytes() == before


def test_cli_execute_writes_repaired_file(tmp_path, monkeypatch):
    projects = _memory_dir(tmp_path, monkeypatch)

    result = CliRunner().invoke(repair_status, ["--execute"])

    assert result.exit_code == 0, result.output
    text = (projects / "infra-status.md").read_text(encoding="utf-8")
    assert _strict_parse(text)["entities"] == ["project/infrastructure"]


# ── store-wide marker strip (--scope all) ────────────────────────────────────

# `bootstrap_all_fact_ids` walks people/ projects/ decisions/ insights/, so a
# curated non-status memory could be tagged too. A marker lands *inside* the
# entity ref's string value, which forks it into its own entity-graph node.
TAGGED_PERSON = (
    "---\n"
    "id: person-alice\n"
    "category: people\n"
    "entities:\n"
    "- person/alice <!-- fact:alice-bio-048fc0 -->\n"
    "source_agents:\n"
    "- claude <!-- fact:alice-bio-9a1b2c -->\n"
    "---\n\n"
    "# Alice\n\n"
    "- [2026-05-01] A curated biographical fact. <!-- fact:alice-bio-abc123 -->\n"
)


def test_strip_frontmatter_fact_markers_is_surgical():
    """Only the marker text changes — the parsed structure is otherwise equal."""
    before = _strict_parse(TAGGED_PERSON)
    stripped, count = strip_frontmatter_fact_markers(TAGGED_PERSON)
    after = _strict_parse(stripped)

    assert count == 2
    assert after == {
        "id": "person-alice",
        "category": "people",
        "entities": ["person/alice"],
        "source_agents": ["claude"],
    }
    assert set(before) == set(after)
    # The body — including its legitimate fact marker — is untouched.
    assert split_frontmatter(stripped)[1] == split_frontmatter(TAGGED_PERSON)[1]
    assert "<!-- fact:alice-bio-abc123 -->" in stripped


def test_strip_frontmatter_fact_markers_is_idempotent_and_inert_when_clean():
    once, first = strip_frontmatter_fact_markers(TAGGED_PERSON)
    twice, second = strip_frontmatter_fact_markers(once)
    assert second == 0 and twice == once
    assert strip_frontmatter_fact_markers(CLEAN_DOC) == (CLEAN_DOC, 0)


def test_strip_frontmatter_drops_a_list_entry_that_was_only_a_marker():
    """Stripping must not leave a `- ` line that parses as a null list member."""
    doc = (
        "---\nid: x\nentities:\n- person/alice <!-- fact:a1 -->\n"
        "- <!-- fact:a2 -->\n---\n\n# X\n"
    )
    stripped, count = strip_frontmatter_fact_markers(doc)
    assert count == 2
    assert _strict_parse(stripped)["entities"] == ["person/alice"]


def _mixed_store(tmp_path: Path, monkeypatch) -> Path:
    for directory in ("people", "projects", "decisions", "insights"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "infra-status.md").write_text(
        CORRUPTED_DOC, encoding="utf-8")
    (tmp_path / "people" / "alice.md").write_text(TAGGED_PERSON, encoding="utf-8")
    (tmp_path / "decisions" / "d1.md").write_text(TAGGED_PERSON, encoding="utf-8")
    (tmp_path / "insights" / "i1.md").write_text(CLEAN_DOC, encoding="utf-8")
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    return tmp_path


def test_default_scope_leaves_non_status_files_alone(tmp_path, monkeypatch):
    """The narrow default must not touch curated memories."""
    store = _mixed_store(tmp_path, monkeypatch)
    before = (store / "people" / "alice.md").read_bytes()

    result = CliRunner().invoke(repair_status, ["--execute"])

    assert result.exit_code == 0, result.output
    assert (store / "people" / "alice.md").read_bytes() == before


def test_scope_all_strips_markers_across_every_memory_dir(tmp_path, monkeypatch):
    store = _mixed_store(tmp_path, monkeypatch)

    result = CliRunner().invoke(repair_status, ["--scope", "all", "--execute"])

    assert result.exit_code == 0, result.output
    for rel in ("people/alice.md", "decisions/d1.md"):
        text = (store / rel).read_text(encoding="utf-8")
        assert "<!-- fact:" not in split_frontmatter(text)[0]
        assert _strict_parse(text)["entities"] == ["person/alice"]
        # markers-only: the body keeps its own fact marker verbatim
        assert "<!-- fact:alice-bio-abc123 -->" in text
    assert "markers only" in result.output


def test_scope_all_does_not_rewrite_non_status_bodies(tmp_path, monkeypatch):
    """Non-status files get the marker strip and nothing else — no re-dump, no
    log rewrite, no entity relocation."""
    store = _mixed_store(tmp_path, monkeypatch)
    body_before = split_frontmatter(
        (store / "people" / "alice.md").read_text(encoding="utf-8"))[1]

    CliRunner().invoke(repair_status, ["--scope", "all", "--execute"])

    text = (store / "people" / "alice.md").read_text(encoding="utf-8")
    assert split_frontmatter(text)[1] == body_before
    # Key order preserved (no yaml re-dump reordering).
    assert list(_strict_parse(text)) == ["id", "category", "entities", "source_agents"]


def test_scope_all_dry_run_writes_nothing(tmp_path, monkeypatch):
    store = _mixed_store(tmp_path, monkeypatch)
    snapshot = {
        p: p.read_bytes() for p in store.rglob("*.md")
    }

    result = CliRunner().invoke(repair_status, ["--scope", "all"])

    assert result.exit_code == 0, result.output
    assert all(p.read_bytes() == blob for p, blob in snapshot.items())
    assert "would repair" in result.output


def test_set_entities_for_path_replaces_rather_than_accumulates(tmp_path, monkeypatch):
    """A corrected entity ref must remove the old row, not add a second one.

    `upsert_entities` only ever INSERTs, so a changed ref writes a new row and
    orphans the old one (#699). The stale node then survives both the file
    repair and a full `palinode reindex`.
    """
    import os

    from palinode.core import store

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    (tmp_path / "projects").mkdir(exist_ok=True)
    fp = str(tmp_path / "projects" / "thing.md")

    # Seed the index the way the old tagger left it: a polluted ref.
    store.upsert_entities(fp, {"category": "projects",
                               "entities": ["person/alice <!-- fact:thing-abc123 -->"]})
    db = store.get_db()
    before = [r[0] for r in db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (fp,)).fetchall()]
    db.close()
    assert before == ["person/alice <!-- fact:thing-abc123 -->"]

    # Repair corrects the ref.
    store.set_entities_for_path(fp, ["person/alice"])

    db = store.get_db()
    after = [r[0] for r in db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (fp,)).fetchall()]
    db.close()
    assert after == ["person/alice"], f"stale ref survived: {after}"


def test_repair_execute_repoints_the_index(tmp_path, monkeypatch):
    """`--execute` must leave the index agreeing with the file it just fixed."""
    from click.testing import CliRunner

    from palinode.cli.repair_status import repair_status
    from palinode.core import store

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    d = tmp_path / "insights"
    d.mkdir()
    fp = d / "note.md"
    fp.write_text(
        "---\nid: insights-note\ncategory: insights\nentities:\n"
        "- person/alice <!-- fact:note-048fc0 -->\n---\n\n- A fact.\n",
        encoding="utf-8",
    )
    store.upsert_entities(str(fp), {"category": "insights",
                                    "entities": ["person/alice <!-- fact:note-048fc0 -->"]})

    res = CliRunner().invoke(repair_status, ["--scope", "all", "--execute"])
    assert res.exit_code == 0, res.output

    assert "fact:" not in fp.read_text(encoding="utf-8")
    # Scoped to this file: the entities table is shared across the suite, so a
    # table-wide assertion would read other tests' rows.
    db = store.get_db()
    refs = [r[0] for r in db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (str(fp),)).fetchall()]
    db.close()
    assert refs == ["person/alice"], f"index not repointed: {refs}"

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from palinode.cli.lint import api_client, lint as lint_command
from palinode.core.config import config
from palinode.core.lint import check_relative_dates, run_lint_pass


@pytest.mark.parametrize(
    "expression",
    [
        "yesterday",
        "today",
        "tomorrow",
        "last week",
        "last month",
        "last year",
        "next week",
        "next month",
        "next year",
        "this week",
        "this month",
        "recently",
        "lately",
        "currently",
        "right now",
        "these days",
    ]
    + [
        f"{number} {unit} ago"
        for number in [
            "3",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        ]
        for unit in ["days", "weeks", "months", "years"]
    ],
)
def test_check_relative_dates_matches_specified_expressions(expression):
    body = f"First line.\nWe wrote {expression} in a memory."

    assert check_relative_dates(body) == [{"line": "2", "expression": expression}]


def test_check_relative_dates_is_case_insensitive_and_flags_quoted_text():
    body = 'She said, "YESTERDAY we decided it."\nRIGHT NOW it still applies.'

    assert check_relative_dates(body) == [
        {"line": "1", "expression": "YESTERDAY"},
        {"line": "2", "expression": "RIGHT NOW"},
    ]


@pytest.mark.parametrize(
    "body",
    [
        "The decision was recorded on 2026-08-03.",
        "The launch month is August.",
        "TodaysStatus is a legacy identifier.",
    ],
)
def test_check_relative_dates_ignores_out_of_scope_text(body):
    assert check_relative_dates(body) == []


def test_lint_relative_dates_exempts_daily_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    daily_dir = tmp_path / "daily"
    insights_dir = tmp_path / "insights"
    daily_dir.mkdir()
    insights_dir.mkdir()

    (daily_dir / "2026-08-11.md").write_text(
        "Yesterday we shipped the release.\n", encoding="utf-8"
    )
    (insights_dir / "drifting.md").write_text(
        "---\nid: insights-drifting\ncategory: insights\ntype: Insight\n---\n"
        "Stable first line.\nTomorrow we will revisit it.\n",
        encoding="utf-8",
    )

    result = run_lint_pass()

    assert result["relative_dates"] == [
        {
            "file": "insights/drifting.md",
            "matches": [{"line": "2", "expression": "Tomorrow"}],
        }
    ]


def test_lint_cli_renders_relative_dates(monkeypatch):
    import importlib

    from click.testing import CliRunner

    lint_module = importlib.import_module("palinode.cli.lint")

    data = {
        "missing_fields": [],
        "orphaned_files": [],
        "stale_files": [],
        "contradictions": [],
        "missing_entities": [],
        "missing_descriptions": [],
        "relative_dates": [
            {
                "file": "decisions/launch.md",
                "matches": [{"line": "4", "expression": "next month"}],
            }
        ],
    }
    monkeypatch.setattr(lint_module.api_client, "lint", lambda: data)

    result = CliRunner().invoke(lint_module.lint, ["--format", "text"])

    assert result.exit_code == 0
    assert "Relative Dates (1)" in result.output
    assert "decisions/launch.md:4: next month" in result.output


def _run_lint_text(monkeypatch):
    monkeypatch.setattr(api_client, "lint", run_lint_pass)
    result = CliRunner().invoke(lint_command, ["--format", "text"])
    assert result.exit_code == 0, result.output
    return result.output


def test_lint_pass(tmp_path, monkeypatch):
    """Test the lint logic with simulated memory files."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    
    people_dir = tmp_path / "people"
    people_dir.mkdir(parents=True)
    
    # 1. Missing Fields (No id, type, category)
    missing1 = people_dir / "missing1.md"
    missing1.write_text("---\nstatus: active\n---\nBody", encoding="utf-8")
    
    # 2. Stale Files (Active and older than 90 days)
    stale1 = people_dir / "stale1.md"
    old_date = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale1.write_text(f"---\nid: people-stale1\ncategory: people\ntype: Person\nstatus: active\ncreated_at: {old_date}\n---\nBody", encoding="utf-8")
    
    # 3. Healthy file referencing 'people/stale1' (meaning stale1 is not an orphan)
    healthy1 = people_dir / "healthy1.md"
    healthy1.write_text(f"---\nid: people-healthy1\ncategory: people\ntype: Person\nstatus: active\ncreated_at: {(datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')}\nentities:\n  - people/stale1\n---\nBody", encoding="utf-8")
    
    # 4. Orphaned file (No entities, and nobody references it)
    orphan1 = people_dir / "orphan1.md"
    orphan1.write_text("---\nid: people-orphan1\ncategory: people\ntype: Person\nstatus: active\n---\nBody", encoding="utf-8")
    
    # 5. Contradiction file 1
    contra1 = people_dir / "contra.md"
    contra1.write_text("---\nid: people-contra\ncategory: people\ntype: Person\nstatus: active\n---\nBody", encoding="utf-8")
    
    # 6. Contradiction file 2 (duplicate entity slug, both active)
    contra2 = tmp_path / "insights"
    contra2.mkdir(exist_ok=True)
    # Actually wait, our contradiction logic creates entity ref like f"{cat}/{slug}".
    # Let's put it in the same category!
    contra_dup = people_dir / "contra-status.md"
    contra_dup.write_text("---\nid: people-contra-dup\ncategory: people\ntype: Person\nstatus: active\n---\nBody", encoding="utf-8")

    result = run_lint_pass()

    # Validate missing fields
    assert any(mf["file"].endswith("missing1.md") and "id" in mf["missing"] for mf in result["missing_fields"])

    # Validate stale
    assert any(sf["file"].endswith("stale1.md") for sf in result["stale_files"])

    # Validate orphan
    assert any(of.endswith("orphan1.md") for of in result["orphaned_files"])
    assert not any(of.endswith("stale1.md") for of in result["orphaned_files"])  # referenced!

    # Validate contradictions
    assert any(ct["entity"] == "people/contra" for ct in result["contradictions"])

    # M0: Validate new keys exist
    assert "missing_entities" in result
    assert "missing_descriptions" in result
    assert "missing_priority" in result
    assert "core_count" in result


def test_lint_missing_entities(tmp_path, monkeypatch):
    """Files without entities should be flagged."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    people_dir = tmp_path / "people"
    people_dir.mkdir()

    # File with no entities
    (people_dir / "lonely.md").write_text(
        "---\nid: people-lonely\ncategory: people\ntype: Person\n---\nBody"
    )
    # File with entities
    (people_dir / "linked.md").write_text(
        "---\nid: people-linked\ncategory: people\ntype: Person\nentities:\n  - project/foo\n---\nBody"
    )

    result = run_lint_pass()
    assert any("lonely.md" in f for f in result["missing_entities"])
    assert not any("linked.md" in f for f in result["missing_entities"])


def test_lint_missing_descriptions(tmp_path, monkeypatch):
    """Files without description should be flagged."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    insights_dir = tmp_path / "insights"
    insights_dir.mkdir()

    # No description
    (insights_dir / "nodesc.md").write_text(
        "---\nid: insights-nodesc\ncategory: insights\ntype: Insight\n---\nBody"
    )
    # Has description
    (insights_dir / "hasdesc.md").write_text(
        "---\nid: insights-hasdesc\ncategory: insights\ntype: Insight\ndescription: A useful insight\n---\nBody"
    )

    result = run_lint_pass()
    assert any("nodesc.md" in f for f in result["missing_descriptions"])
    assert not any("hasdesc.md" in f for f in result["missing_descriptions"])


def test_lint_core_count(tmp_path, monkeypatch):
    """Core count should tally files with core: true."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Core file
    (projects_dir / "important.md").write_text(
        "---\nid: projects-important\ncategory: projects\ntype: ProjectSnapshot\ncore: true\n---\nBody"
    )
    # Non-core file
    (projects_dir / "normal.md").write_text(
        "---\nid: projects-normal\ncategory: projects\ntype: ProjectSnapshot\n---\nBody"
    )

    result = run_lint_pass()
    assert result["core_count"] == 1


def test_lint_missing_priority_for_core_and_decisions_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    insights_dir = tmp_path / "insights"
    decisions_dir = tmp_path / "decisions"
    projects_dir = tmp_path / "projects"
    insights_dir.mkdir()
    decisions_dir.mkdir()
    projects_dir.mkdir()

    (decisions_dir / "decision.md").write_text(
        "---\nid: decisions-decision\ncategory: decisions\ntype: Decision\n---\nBody"
    )
    (projects_dir / "core.md").write_text(
        "---\nid: projects-core\ncategory: projects\ntype: ProjectSnapshot\ncore: true\n---\nBody"
    )
    (insights_dir / "insight.md").write_text(
        "---\nid: insights-insight\ncategory: insights\ntype: Insight\n---\nBody"
    )
    (decisions_dir / "prioritized.md").write_text(
        "---\nid: decisions-prioritized\ncategory: decisions\ntype: Decision\npriority: 4\n---\nBody"
    )

    result = run_lint_pass()
    assert any("decision.md" in f for f in result["missing_priority"])
    assert any("core.md" in f for f in result["missing_priority"])
    assert not any("insight.md" in f for f in result["missing_priority"])
    assert not any("prioritized.md" in f for f in result["missing_priority"])


def test_lint_missing_fields_exempts_daily_logs(tmp_path, monkeypatch):
    """`daily/` is the structural log tier — never flagged for missing fields.

    See PROGRAM.md § File tiers: a daily note is an append-only log holding N
    sessions, whose content session-end already persists separately as typed
    memories. Flagging it here also made the report internally inconsistent —
    `total_files` (the denominator) has always excluded `daily/`.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    daily_dir = tmp_path / "daily"
    insights_dir = tmp_path / "insights"
    daily_dir.mkdir()
    insights_dir.mkdir()

    # A real session-end note: appended, so it never has frontmatter.
    (daily_dir / "2026-04-19.md").write_text(
        "## Session End — 2026-04-19T10:00:00Z\n\n**Summary:** shipped it\n",
        encoding="utf-8",
    )
    (insights_dir / "conformant.md").write_text(
        "---\nid: insights-conformant\ncategory: insights\ntype: Insight\n---\nBody",
        encoding="utf-8",
    )
    (insights_dir / "broken.md").write_text("No frontmatter at all\n", encoding="utf-8")

    result = run_lint_pass()

    flagged = [mf["file"] for mf in result["missing_fields"]]
    assert not any(f.startswith("daily/") for f in flagged)
    # Non-daily files are still flagged — the exemption is scoped, not a mute.
    assert any("broken.md" in f for f in flagged)


def test_lint_missing_fields_numerator_matches_total_files_denominator(tmp_path, monkeypatch):
    """Nothing counted in `missing_fields` may be outside `total_files`.

    The invariant the daily/ exemption restores: a corpus of nothing but daily
    logs has a zero denominator, so it must also report zero violations.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    for day in ("2026-04-19", "2026-04-20", "2026-04-21"):
        (daily_dir / f"{day}.md").write_text(f"## Session End — {day}\n", encoding="utf-8")

    result = run_lint_pass()

    assert result["total_files"] == 0
    assert result["missing_fields"] == []


def test_lint_wiki_drift_exempts_daily_logs(tmp_path, monkeypatch):
    """`daily/` is exempt from wiki drift too — the second outlier.

    `check_wiki_drift` measures agreement between frontmatter `entities:` and
    body `[[wikilinks]]`. A daily log is a structural tier with no `entities:`
    contract (PROGRAM.md § File tiers), so a wikilink written in a session
    summary was reported as drifting from a list the file will never carry —
    a disagreement between two halves of a contract the file isn't party to.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    daily_dir = tmp_path / "daily"
    decisions_dir = tmp_path / "decisions"
    daily_dir.mkdir()
    decisions_dir.mkdir()

    # A session summary that mentions an entity in prose, as they routinely do.
    (daily_dir / "2026-04-19.md").write_text(
        "## Session End — 2026-04-19T10:00:00Z\n\n"
        "**Summary:** paired with [[person/alice]] on the executor\n",
        encoding="utf-8",
    )
    # A real memory whose body links an entity absent from its frontmatter —
    # genuine drift, and it must still be reported.
    (decisions_dir / "drifted.md").write_text(
        "---\nid: decisions-drifted\ncategory: decisions\ntype: Decision\n"
        "entities:\n- project/palinode\n---\n"
        "Decided with [[person/bob]], who is not in the frontmatter.\n",
        encoding="utf-8",
    )

    result = run_lint_pass()

    drifted = [w["file"] for w in result["wiki_drift"]]
    assert not any(f.startswith("daily/") for f in drifted)
    # Scoped, not a mute: real drift in a real memory is still caught.
    assert any("drifted.md" in f for f in drifted)


def test_lint_text_reports_wiki_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    (decisions_dir / "drifter.md").write_text(
        "---\nid: decisions-drifter\ncategory: decisions\ntype: Decision\n"
        "priority: 3\ndescription: A drifting memory.\n"
        "entities:\n- project/alpha\n- person/casey\n---\n"
        "This body contains no wikilinks.\n",
        encoding="utf-8",
    )

    output = _run_lint_text(monkeypatch)

    assert "Wiki Drift (1)" in output
    assert "drifter.md: [frontmatter_not_in_body]" in output
    assert "project/alpha" in output
    assert "person/casey" in output


def test_lint_text_reports_clean_wiki_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    insights_dir = tmp_path / "insights"
    insights_dir.mkdir()
    (insights_dir / "aligned.md").write_text(
        "---\nid: insights-aligned\ncategory: insights\ntype: Insight\n"
        "description: An aligned memory.\nentities:\n- project/alpha\n---\n"
        "This body links [[project/alpha]].\n",
        encoding="utf-8",
    )

    output = _run_lint_text(monkeypatch)

    assert "No wiki drift" in output


def test_lint_text_reports_missing_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    (decisions_dir / "unranked.md").write_text(
        "---\nid: decisions-unranked\ncategory: decisions\ntype: Decision\n"
        "description: A decision without priority.\n"
        "entities:\n- project/alpha\n---\n"
        "This body links [[project/alpha]].\n",
        encoding="utf-8",
    )

    output = _run_lint_text(monkeypatch)

    assert "Missing Priority (1)" in output
    assert "unranked.md" in output


def test_lint_text_reports_clean_priority_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    (decisions_dir / "ranked.md").write_text(
        "---\nid: decisions-ranked\ncategory: decisions\ntype: Decision\n"
        "priority: 4\ndescription: A prioritized decision.\n"
        "entities:\n- project/alpha\n---\n"
        "This body links [[project/alpha]].\n",
        encoding="utf-8",
    )

    output = _run_lint_text(monkeypatch)

    assert "All core and Decision memories have priority" in output


def test_lint_text_represents_every_lint_result_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    data = run_lint_pass()
    output = _run_lint_text(monkeypatch)
    output_markers = {
        "total_files": "Files scanned: 0",
        "orphaned_files": "No orphaned files",
        "stale_files": "No stale active files",
        "missing_fields": "No files missing frontmatter",
        "contradictions": "No contradictions detected",
        "missing_entities": "All files have entity refs",
        "missing_descriptions": "All files have descriptions",
        "missing_priority": "All core and Decision memories have priority",
        "wiki_drift": "No wiki drift",
        "relative_dates": "No relative dates",
        "source_anchor_issues": "No source-anchor issues",
        "claim_anchor_issues": "No claim-anchor issues",
        "stale_open_questions": "No stale open questions",
        "open_contradictions": "No open contradictions",
        "entity_aliases": "No entity-alias candidates",
        "core_count": "No core files found",
    }

    assert set(data) == set(output_markers)
    for key, marker in output_markers.items():
        assert marker in output, f"{key} is absent from text output"

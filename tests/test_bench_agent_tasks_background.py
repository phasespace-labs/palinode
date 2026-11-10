"""bench.agent_tasks.background — the realistic background corpus."""
from __future__ import annotations

import json
import os
import re
from datetime import date

import pytest

from bench.agent_tasks import __main__ as cli
from bench.agent_tasks import background as bg

AS_OF = date(2026, 11, 3)


def _fake_root(tmp_path, files: dict[str, str]) -> str:
    root = tmp_path / "lib"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return str(root)


DOC = "Return the configured value for the given key, falling back to the default when absent."


def test_clean_rejects_batch_shaped_tokens_and_task_words():
    assert bg.clean("an ordinary paragraph about sockets")
    assert not bg.clean("we used atlas-5b2d0c19 for it")
    assert not bg.clean("the kestrel_ed218837 value")
    assert not bg.clean("set the batch id first")
    assert not bg.clean("BATCH_ID must be set")
    assert not bg.clean("mentions ledger-9c3e57 here", frozenset({"ledger-9c3e57"}))
    # Unanchored, as the grader's substring match is: an embedded token counts.
    assert not bg.clean("id=xatlas-5b2d0c199 in a hash")
    assert not bg.clean("deadbeef-cafebabe")
    assert bg.drop_reason("uuid 0123-4567") is None
    assert bg.drop_reason("we used atlas-5b2d0c19") == "batch_shaped"
    assert bg.drop_reason("the batch id") == "task_words"
    assert bg.drop_reason("x ledger-9c3e57", frozenset({"ledger-9c3e57"})) == "plan_token"


def test_sections_skip_private_names_short_docs_and_dirty_text(tmp_path):
    root = _fake_root(tmp_path, {
        "alpha.py": (
            f'"""{DOC}"""\n'
            f'def public():\n    """{DOC}"""\n'
            f'def _private():\n    """{DOC}"""\n'
            'def short():\n    """Too short."""\n'
            'def dirty():\n    """Pin the batch to atlas-5b2d0c19 before the run starts, always."""\n'
            f'class Thing:\n    """{DOC}"""\n    def method(self):\n        """{DOC}"""\n'
        ),
    })
    secs = bg._sections("alpha", os.path.join(root, "alpha.py"), frozenset())
    assert [s.heading for s in secs] == ["Overview", "public (function)", "Thing (class)", "Thing.method"]


def test_module_files_skip_tests_and_private_top_level(tmp_path):
    root = _fake_root(tmp_path, {
        "beta.py": f'"""{DOC}"""\n', "_gamma.py": f'"""{DOC}"""\n',
        "test/test_beta.py": f'"""{DOC}"""\n', "pkg/__init__.py": f'"""{DOC}"""\n',
        "pkg/sub.py": f'"""{DOC}"""\n', "pkg-1.0.dist-info/x.py": f'"""{DOC}"""\n',
    })
    assert [m for m, _ in bg._module_files(root)] == ["beta", "pkg", "pkg.sub"]


def test_build_is_deterministic_isolated_and_capped(tmp_path, monkeypatch):
    root = _fake_root(tmp_path, {f"mod{i}.py": f'"""{DOC} Module {i}."""\n' for i in range(20)})
    monkeypatch.setattr(bg, "stdlib_root", lambda: root)
    a = bg.build(str(tmp_path / "a"), as_of=AS_OF, target_chunks=12, include_relevance=False)
    b = bg.build(str(tmp_path / "b"), as_of=AS_OF, target_chunks=12, include_relevance=False)
    assert a["sha256"] == b["sha256"]
    assert a["stdlib_notes"] == 12 and a["estimated_chunks"] == 12
    texts = [open(os.path.join(dp, f), encoding="utf-8").read()
             for dp, _, fs in os.walk(tmp_path / "a") for f in fs if f.endswith(".md")]
    assert len(texts) == 12
    tags = {t for x in texts for t in re.findall(r"project/([A-Za-z0-9._-]+)", x)}
    assert tags and all(t in {p.removeprefix("project/") for p in (f"project/{q}" for q in bg.PROJECTS)} for t in tags)
    assert any("entities:" not in x for x in texts), "some notes must be global"
    assert not any(bg.BATCH_SHAPED.search(x) for x in texts)
    dates = {re.search(r"created_at: '(\d{4}-\d\d-\d\d)", x).group(1) for x in texts}
    assert all(d < AS_OF.isoformat() for d in dates)


def test_build_refuses_a_non_empty_dir(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "keep.md").write_text("x")
    with pytest.raises(ValueError):
        bg.build(str(tmp_path / "x"), as_of=AS_OF, target_chunks=1)


def test_plan_tokens_and_cli_exclusion(tmp_path, monkeypatch, capsys):
    root = _fake_root(tmp_path, {
        "keep.py": f'"""{DOC}"""\n',
        "drop.py": '"""Notes about the ledger-9c3e57 workspace and how its config is loaded at start."""\n',
    })
    monkeypatch.setattr(bg, "stdlib_root", lambda: root)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"cells": [{"project": "ledger-9c3e57",
                                            "tokens": {"batch": "atlas-5b2d0c19"}}]}))
    assert bg.plan_tokens(str(plan)) == {"ledger-9c3e57", "atlas-5b2d0c19"}
    out = tmp_path / "out"
    rc = cli.main(["background", "--out", str(out), "--as-of", "2026-11-03",
                   "--target-chunks", "10", "--plan", str(plan), "--no-relevance"])
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["stdlib_notes"] == 1 and manifest["excluded_tokens"] == 2
    assert (out / "manifest.json").is_file()


def test_relevance_records_are_included_and_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "stdlib_root", lambda: _fake_root(tmp_path, {}))
    m = bg.build(str(tmp_path / "r"), as_of=AS_OF, target_chunks=1000)
    assert m["relevance_records"] > 0 and m["stdlib_notes"] == 0


def test_manifest_records_drops_and_a_clean_rescan(tmp_path, monkeypatch):
    root = _fake_root(tmp_path, {
        "ok.py": f'"""{DOC}"""\n',
        "meta.py": '"""Package metadata: release sha deadbeef-0123abcd is pinned for reproducible builds."""\n',
    })
    monkeypatch.setattr(bg, "stdlib_root", lambda: root)
    m = bg.build(str(tmp_path / "m"), as_of=AS_OF, target_chunks=100, include_relevance=False)
    assert m["stdlib_notes"] == 1
    assert m["dropped"] == {"batch_shaped": 1}
    assert m["rescan_matches_remaining"] == 0
    assert m["batch_shaped_pattern"] == bg.BATCH_SHAPED.pattern


def test_relevance_records_carry_their_project_entity(tmp_path, monkeypatch):
    """Isolation reads entities, not the fixture's ``project:`` field."""
    monkeypatch.setattr(bg, "stdlib_root", lambda: _fake_root(tmp_path, {}))
    out = tmp_path / "e"
    bg.build(str(out), as_of=AS_OF, target_chunks=1000)
    import yaml
    seen = 0
    for dp, _, fs in os.walk(out):
        for f in fs:
            if f.endswith(".md"):
                meta = yaml.safe_load(open(os.path.join(dp, f), encoding="utf-8").read().split("---")[1])
                assert f"project/{meta['project']}" in meta["entities"]
                seen += 1
    assert seen > 0

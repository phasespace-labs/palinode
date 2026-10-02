"""The agent-task harness: plan determinism, corpus integrity, grading, report.

No client runs here — that is the driver's job. What CI protects is the part
that makes a live run's numbers mean anything: the plan is reproducible from
its seed, every cell carries what the driver needs, tokens are unique and
unguessable, the arms get equal memory budgets, each task repo fails before
the task and passes after a correct change, and the grader turns hand-built
evidence into the right verdicts — including NOT RUN for missing evidence,
never a fail.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from bench.agent_tasks import __main__ as cli
from bench.agent_tasks import grade, plan, report
from bench.agent_tasks.corpus import ROWS, derive_held_out, load_corpus, p1_variant_count

SEED = 1449
AS_OF = "2026-09-26"
TOKEN_RE = re.compile(r"^[a-z]+[-_][0-9a-f]{8}$")


def _make_plan(out, seed=SEED, **overrides):
    kwargs = dict(rows=[1, 2, 7], split="dev", clients=list(plan.CLIENTS),
                  arms=list(plan.ARMS), repeats=1, seed=seed, as_of=AS_OF)
    kwargs.update(overrides)
    built = plan.build_plan(load_corpus(), **kwargs)
    plan.write_plan(built, str(out))
    return plan.load_plan(str(out))


@pytest.fixture(scope="module")
def planned(tmp_path_factory):
    out = tmp_path_factory.mktemp("plan")
    return out, _make_plan(out)


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    """Every row, both splits, one repeat (the P1 shape at a third of its size)."""
    out = tmp_path_factory.mktemp("full")
    return out, _make_plan(out, rows=list(range(1, 19)), split="dev,heldout")


def _cell(p, cell_id):
    return next(c for c in p["cells"] if c["cell_id"] == cell_id)


def _tree(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                out[os.path.relpath(path, root)] = handle.read()
    return out


# ── corpus and design ───────────────────────────────────────────────────────


def test_design_has_all_eighteen_rows_and_p1_count():
    assert [r.row for r in ROWS] == list(range(1, 19))
    # 13 rows with a negative control, 4 positive-only, row 7 its own control.
    assert p1_variant_count() == 13 * 2 + 4 + 1
    counts = report.p1_cell_counts()
    assert counts["planned"] == 31 * 2 * 3 * 8
    # N/A: Codex native everywhere (186); row 6 claude native (1 variant × 2
    # splits × 3 = 6); row 17 Codex none/file/palinode (3 × 12) and claude
    # native (12).
    assert counts["not_applicable"] == 186 + 6 + 36 + 12
    assert counts["total"] == counts["planned"] - counts["not_applicable"] == 1248
    assert counts["cells"][("codex", "native")] == 0


def _measured(claude_mean, claude_max):
    return {("claude-code", a): {"cost_mean": claude_mean, "cost_max": claude_max,
                                 "tokens_mean": None, "wall_mean": 30.0, "n": 5}
            for a in plan.ARMS}


def test_cap_simulation_cuts_whole_late_rows_first():
    cells = report.p1_cells()
    claude = [c for c in cells if c["client"] == "claude-code" and c["status"] == "run"]
    assert len(claude) == 31 * 2 * 3 * 4 - 6 - 12
    ok = report.simulate_cap(cells, _measured(0.05, 0.11))
    assert ok["stopped_at"] is None and ok["unconstrained"] == pytest.approx(0.05 * len(claude))
    tight = report.simulate_cap(cells, _measured(0.10, 0.20))
    assert tight["stopped_at"] is not None and tight["spent"] <= report.P1_CAP_USD
    cut = tight["cut"]
    # everything after the stop is cut, and the stop falls after repeat 1
    first = cut[0]
    assert first["repeat"] >= 2
    assert all((c["repeat"], c["row"]) >= (first["repeat"], first["row"]) for c in cut)
    assert report.simulate_cap(cells, {})["ok"] is False


def test_every_design_row_has_its_controls():
    corpus = load_corpus()
    assert corpus.rows() == tuple(range(1, 19))
    for row in ROWS:
        assert {s.control for s in corpus.for_row(row.row)} == set(row.controls)
    assert len(corpus.scenarios) == p1_variant_count()


def test_rows_outside_the_design_are_refused(tmp_path):
    with pytest.raises(ValueError, match="not in the design"):
        _make_plan(tmp_path / "p", rows=[19])
    assert plan.parse_rows("1-3,7") == [1, 2, 3, 7]


def test_v1_scenarios_draw_the_same_stimuli_as_the_p0_plan(planned):
    """P0 plans stay valid: a v1 dev cell is drawn exactly as it was in P0."""
    _, p = planned
    c = _cell(p, "r01-pos-claude-code-file-1")
    assert c["project"] == "corvane-62b902"
    assert c["tokens"]["keep"] == "godwit_94252af8"
    assert c["tokens"]["incident"] == "yellowlegs-5e34c4bb"


# ── plan ────────────────────────────────────────────────────────────────────


def test_plan_is_byte_identical_for_the_same_seed(tmp_path):
    _make_plan(tmp_path / "a")
    _make_plan(tmp_path / "b")
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
    _make_plan(tmp_path / "c", seed=SEED + 1)
    assert _tree(tmp_path / "a")["plan.json"] != _tree(tmp_path / "c")["plan.json"]


def test_plan_refuses_a_non_empty_directory(tmp_path):
    _make_plan(tmp_path / "a")
    with pytest.raises(FileExistsError):
        _make_plan(tmp_path / "a")


def test_cell_matrix_and_not_applicable(planned):
    _, p = planned
    assert p["schema"] == "agent_tasks.plan/2"
    assert len(p["cells"]) == 5 * 2 * 4
    na = [c for c in p["cells"] if c["status"] == "not_applicable"]
    assert {(c["client"], c["arm"]) for c in na} == {("codex", "native")}
    assert len(na) == 5
    assert all("Codex memories are off by default" in c["reason"] for c in na)
    assert _cell(p, "r01-pos-claude-code-palinode-1")["control"] == "positive"
    assert _cell(p, "r07-ctl-codex-none-1")["control"] == "control"


def test_every_cell_has_the_contract_files(planned):
    out, p = planned
    projects = set()
    for c in p["cells"]:
        base = out / "cells" / c["cell_id"]
        assert (base / "prompt.txt").is_file()
        assert (base / "repo").is_dir() and (base / "memory").is_dir()
        assert (base / "repo" / ".gitignore").is_file()
        assert c["test_cmd"] == "python3 -m unittest -q"
        assert c["limits"] == {"max_turns": 20, "wall_s": 420}
        assert c["project"] not in projects
        projects.add(c["project"])
        memory = sorted(os.listdir(base / "memory"))
        if c["arm"] == "none":
            assert c["memory"] == {} and memory == []
        elif c["arm"] == "file":
            expected = {"claude-code": "CLAUDE.md", "codex": "AGENTS.md"}[c["client"]]
            assert c["memory"] == {"file_path": expected} and memory == [expected]
        elif c["arm"] == "native":
            assert c["memory"] == {"native_path": "MEMORY.md"} and memory == ["MEMORY.md"]
        else:
            ops = c["memory"]["palinode_ops"]
            assert ops and memory == ["palinode_ops.json"]
            assert json.loads((base / "memory" / "palinode_ops.json").read_text()) == ops
            for op in ops:
                if op["op"] == "save":
                    assert set(op) == {"op", "type", "title", "body", "entities", "date", "slug"}
                    assert op["entities"] == [f"project/{c['project']}"]
                else:
                    assert set(op) == {"op", "target_slug", "new_text", "reason"}
    assert len(p["probes"]) == 1
    probe = p["probes"][0]
    pdir = out / "probes" / probe["probe_id"]
    assert probe["tokens"]["probe"] in (pdir / "memory" / "MEMORY.md").read_text()
    assert probe["tokens"]["probe"] not in (pdir / "prompt.txt").read_text()


def test_full_plan_matrix_and_not_applicable_rules(full):
    _, p = full
    cells = p["cells"]
    assert len(cells) == 31 * 2 * 2 * 4
    na = {(c["scenario"], c["client"], c["arm"]) for c in cells if c["status"] == "not_applicable"}
    for c in cells:
        key = (c["scenario"], c["client"], c["arm"])
        expect_na = (
            (c["client"] == "codex" and c["arm"] == "native")
            or (c["row"] == 6 and c["arm"] == "native")
            or (c["row"] == 17 and (c["client"] == "codex" or c["arm"] == "native"))
        )
        assert (key in na) == expect_na, key
    assert all(c["reason"] for c in cells if c["status"] == "not_applicable")


def test_cells_are_emitted_in_run_order(tmp_path):
    p = _make_plan(tmp_path / "p", rows=[2, 1], repeats=2)
    keys = [(c["repeat"], c["row"], c["control"]) for c in p["cells"]]
    # repeat-major, rows in the order asked, positive before negative
    assert keys[0] == (1, 2, "positive") and keys[-1] == (2, 1, "negative")
    assert keys == sorted(keys, key=lambda k: (k[0], [2, 1].index(k[1]), k[2] != "positive"))


def test_v2_ops_have_the_contract_shapes(full):
    out, p = full
    shapes = {
        "save": {"op", "type", "title", "body", "entities", "date", "slug"},
        "correct": {"op", "target_slug", "new_text", "reason"},
        "consolidate": {"op"}, "reindex": {"op"},
        "restart": {"op", "units"},
        "archive": {"op", "target_slug"}, "restore": {"op", "target_slug"},
        "transcript_capture": {"op", "client", "transcript", "confirm"},
        "agent_session": {"op", "client", "prompt", "capture"},
        "session_end": {"op", "summary", "project"},
    }
    seen = set()
    for c in p["cells"]:
        if c["arm"] != "palinode":
            continue
        base = out / "cells" / c["cell_id"]
        for op in c["memory"]["palinode_ops"]:
            seen.add(op["op"])
            extra = ({"project", "metadata"} if op["op"] == "save"
                     else {"decisions"} if op["op"] == "session_end"
                     else {"on_refused"} if op["op"] == "correct" else set())
            assert "${" not in json.dumps(op), op
            assert shapes[op["op"]] <= set(op) <= shapes[op["op"]] | extra, op
            for field in ("transcript", "prompt"):
                if field in op and op["op"] != "save":
                    assert (base / op[field]).is_file(), (c["cell_id"], op)
            if op["op"] == "agent_session":
                other = next(x for x in plan.CLIENTS if x != c["client"])
                assert op["client"] == (other if c["row"] == 6 else c["client"])
            if op["op"] == "transcript_capture":
                for line in (base / op["transcript"]).read_text().splitlines():
                    rec = json.loads(line)
                    assert rec["type"] == rec["message"]["role"] and "timestamp" not in rec
                    assert rec["cwd"].endswith("/" + c["project"])
    assert seen == set(shapes)


def test_row15_saves_under_another_project(full):
    _, p = full
    c = _cell(p, "r15-pos-codex-palinode-1")
    save = [o for o in c["memory"]["palinode_ops"] if o.get("project")]
    assert len(save) == 1 and save[0]["project"] != c["project"]
    assert save[0]["entities"] == [f"project/{save[0]['project']}"]
    neg = _cell(p, "r15-neg-codex-palinode-1")
    assert not any(o.get("project") for o in neg["memory"]["palinode_ops"])


def test_row17_native_seed_placement(full):
    _, p = full
    pos = _cell(p, "r17-pos-claude-code-palinode-1")
    t = pos["tokens"]
    assert t["old"] in pos["memory"]["native_seed"] and t["new"] not in pos["memory"]["native_seed"]
    assert "native_seed" in _cell(p, "r17-pos-claude-code-file-1")["memory"]
    assert "native_seed" not in _cell(p, "r17-pos-claude-code-none-1")["memory"]


def test_file_log_renders_each_ops_net_effect(full):
    out, p = full
    log = (out / "cells" / "r10-pos-claude-code-file-1" / "memory" / "CLAUDE.md").read_text()
    assert "retired; no longer applies" in log and "restored; applies again" in log
    log9 = (out / "cells" / "r09-neg-claude-code-file-1" / "memory" / "CLAUDE.md").read_text()
    assert "retired" not in log9 and "restart" not in log9.lower()
    c = _cell(p, "r04-pos-codex-file-1")
    log4 = (out / "cells" / c["cell_id"] / "memory" / "AGENTS.md").read_text()
    assert "(changed in chat)" in log4 and c["tokens"]["change_ref"] in log4
    c = _cell(p, "r06-pos-claude-code-file-1")
    assert "(in codex)" in (out / "cells" / c["cell_id"] / "memory" / "CLAUDE.md").read_text()


def test_row2_positive_seeds_a_save_then_a_correction(planned):
    _, p = planned
    ops = _cell(p, "r02-pos-codex-palinode-1")["memory"]["palinode_ops"]
    saves = [o for o in ops if o["op"] == "save"]
    assert ops[-1]["op"] == "correct"
    assert ops[-1]["target_slug"] in {o["slug"] for o in saves}
    assert all(o["body"].startswith(f"[{o['date']}] ") for o in saves)
    assert ops[-1]["new_text"].startswith("[2026-09-19] ")  # as_of - 7 days
    neg = _cell(p, "r02-neg-codex-palinode-1")["memory"]["palinode_ops"]
    assert all(o["op"] == "save" for o in neg)


def test_tokens_are_unique_random_and_placed_correctly(full):
    out, p = full
    values = [v for c in p["cells"] for v in c["tokens"].values()]
    values += [v for pr in p["probes"] for v in pr["tokens"].values()]
    assert len(values) == len(set(values))
    assert all(TOKEN_RE.match(v) for v in values)
    for c in p["cells"]:
        words = [re.split(r"[-_]", v)[0] for v in c["tokens"].values()]
        assert len(words) == len(set(words)), c["cell_id"]
        base = out / "cells" / c["cell_id"]
        repo = "".join(str(k) + v.decode() for k, v in _tree(base / "repo").items())
        prompt = (base / "prompt.txt").read_text()
        for name in c["grading"]["memory_tokens"]:
            assert c["tokens"][name] not in repo, (c["cell_id"], name)
        for value in c["tokens"].values():
            assert value not in prompt
        memory_text = "".join(v.decode() for v in _tree(base / "memory").values())
        if c["arm"] != "none":
            for name in c["grading"]["memory_tokens"]:
                assert c["tokens"][name] in memory_text, (c["cell_id"], name)


def test_prompts_are_identical_across_arms_and_never_name_memory(full):
    out, p = full
    by_scenario: dict[str, set[str]] = {}
    for c in p["cells"]:
        text = (out / "cells" / c["cell_id"] / "prompt.txt").read_text()
        by_scenario.setdefault(c["scenario"], set()).add(text)
        lowered = text.lower()
        for word in ("memory", "palinode", "claude.md", "agents.md", "notes", "remember"):
            assert word not in lowered, (c["cell_id"], word)
    assert all(len(texts) == 1 for texts in by_scenario.values())


def test_option_order_is_not_a_tell(planned):
    """The favoured option is listed first in some cells and second in others."""
    out, p = planned
    firsts = []
    for c in p["cells"]:
        if c["scenario"] != "r01-pos":
            continue
        registry = (out / "cells" / c["cell_id"] / "repo" / "ingest" / "backends" / "__init__.py").read_text()
        firsts.append(registry.index(c["tokens"]["keep"]) < registry.index(c["tokens"]["reject"]))
    assert True in firsts and False in firsts


def test_file_native_and_palinode_arms_have_equal_budget(full):
    """Where every op is text the user wrote down (save/correct) the arms carry
    the same budget. Other ops are measured and reported, not held to it:
    archive/restore are state changes the file arm can only express as extra
    lines, and what a captured transcript or prior session leaves behind is
    the thing under test."""
    _, p = full
    corpus = load_corpus()
    free = {sc.id for sc in corpus.scenarios
            if {e["op"] for e in sc.events} - {"save", "correct", "consolidate", "restart", "reindex"}}
    budgets: dict[tuple, dict[str, int]] = {}
    for c in p["cells"]:
        if c["arm"] == "none":
            assert c["memory_budget"]["bytes"] == 0
            continue
        assert c["memory_budget"]["bytes"] > 0
        if c["scenario"].removesuffix("-heldout") in free:
            continue
        budgets.setdefault((c["scenario"], c["client"], c["repeat"]), {})[c["arm"]] = c["memory_budget"]["bytes"]
    for key, arms in budgets.items():
        pal = arms["palinode"]
        for arm, n in arms.items():
            assert 0.8 <= n / pal <= 1.3, (key, arm, n, pal)


# ── the task repos themselves ───────────────────────────────────────────────


def _unittest(repo):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q"], cwd=repo,
        capture_output=True, text=True, timeout=60,
    )


def _right_value(c):
    """The token a correct change writes into the repo, per the cell's oracle."""
    expect = c["grading"]["expect"]
    for key in ("diff_uses", "diff_uses_any"):
        if expect.get(key):
            return c["tokens"][expect[key][0]]
    repo = c["grading"]["repo_tokens"]
    return c["tokens"][repo[0]] if repo else "batch-unknown"


def _solve(repo, c):
    """A correct change for each repo template, using the right option."""
    t = c["tokens"]
    value = _right_value(c)
    if c["family"] == "house_rule":
        name = t["prefix"] + "_normalize" if c["control"] == "positive" else "normalize"
        (repo / "labels" / f"{name}.py").write_text(
            'def normalize(text):\n    return "-".join(text.lower().split())\n')
        (repo / "labels" / "__init__.py").write_text(f"from .{name} import normalize\n")
    elif (repo / "migrate").is_dir():
        path = repo / "migrate" / "config.py"
        path.write_text(path.read_text().replace("BATCH_ID = None", f"BATCH_ID = {value!r}"))
    elif (repo / "notify" / "deadletter.py").is_file():
        path = repo / "notify" / "config.py"
        path.write_text(path.read_text().replace("DEAD_LETTER_QUEUE = None", f"DEAD_LETTER_QUEUE = {value!r}"))
    elif (repo / "notify").is_dir():
        path = repo / "notify" / "dispatch.py"
        src = path.read_text()
        src = src.replace('"""\n', '"""\nfrom notify import transports\n', 1)
        src = src.replace("self.transport = None", f"self.transport = transports.REGISTRY[{value!r}]()")
        path.write_text(src)
    elif c["family"] == "rejected_approach":
        path = repo / "ingest" / "pipeline.py"
        src = path.read_text()
        src = src.replace('"""Record ingest pipeline."""\n', '"""Record ingest pipeline."""\nfrom ingest import backends\n')
        src = src.replace("        self.loader = loader\n",
                          f"        self.loader = loader\n        self.cache = backends.REGISTRY[{t['keep']!r}]()\n")
        src = src.replace(
            "        return normalize(self.loader(key))\n",
            "        hit = self.cache.get(key)\n        if hit is not None:\n            return hit\n"
            "        value = normalize(self.loader(key))\n        self.cache.set(key, value)\n        return value\n",
        )
        path.write_text(src)
    else:
        path = repo / "service" / "banner.py"
        src = path.read_text()
        src = src.replace('"""Startup banner."""\n', '"""Startup banner."""\nfrom service import config\n')
        src = src.replace('return f"{name} ready"', 'return f"{name} ready ({config.BUILD_TAG})"')
        path.write_text(src)


def test_each_repo_fails_before_the_task_and_passes_after(full, tmp_path):
    import shutil

    out, p = full
    for scenario in sorted({c["scenario"] for c in p["cells"]}):
        c = next(x for x in p["cells"] if x["scenario"] == scenario)
        repo = tmp_path / scenario
        shutil.copytree(out / "cells" / c["cell_id"] / "repo", repo)
        before = _unittest(repo)
        assert before.returncode != 0, scenario
        assert "ImportError" not in before.stderr and "SyntaxError" not in before.stderr, before.stderr
        _solve(repo, c)
        after = _unittest(repo)
        assert after.returncode == 0, (scenario, after.stderr)


# ── grading on hand-built run directories ───────────────────────────────────


def _patch(added: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        f"@@ -1,1 +1,{len(added) + 1} @@\n context line\n{body}"
    )


def _write_cell(run_dir, cell_id, *, answer="", added=(), passed=True, delivery=None,
                usage=None, skip=()):
    base = run_dir / "cells" / cell_id
    base.mkdir(parents=True)
    files = {
        "events.jsonl": json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__palinode__palinode_search"},
            {"type": "tool_use", "id": "t2", "name": "Edit"},
        ]}}) + "\n",
        "answer.txt": answer,
        "diff.patch": _patch(list(added)),
        "tests.json": json.dumps({"ran": True, "passed": passed, "rc": 0 if passed else 1, "output": ""}),
        "delivery.json": json.dumps(delivery or {"retrieval_rows": [], "hook_context": None, "tool_calls": []}),
        "usage.json": json.dumps(usage or {
            "cost_usd": 0.04, "input_tokens": 20000, "cached_input_tokens": 15000,
            "output_tokens": 900, "num_turns": 6, "wall_s": 55.0, "timed_out": False,
            "rc": 0, "hit_max_turns": False,
        }),
    }
    for name, text in files.items():
        if name not in skip:
            (base / name).write_text(text)


RUN_JSON = {
    "palinode_commit": "abc1234", "palinode_config_sha256": "f" * 64,
    "clients": {"claude-code": "2.9.0", "codex": "0.156.1"},
    "models": {"claude-code": "claude-haiku-4-5", "codex": "gpt-6-luna"},
    "corpus_version": 1, "seed": SEED,
    "store": {"palinode_dir": "/tmp/p1449-run/store", "original_palinode_dir": "/home/someone/palinode",
              "proof": {"empty_before_seeding": True}},
    "started": "2026-09-26T10:00:00Z", "ended": "2026-09-26T11:00:00Z",
    "not_run": [{"cell_id": "r07-ctl-codex-file-1", "reason": "client crashed before first turn"}],
}


@pytest.fixture()
def graded(planned, tmp_path):
    out, p = planned
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps(RUN_JSON))

    c = _cell(p, "r01-pos-claude-code-palinode-1")
    t = c["tokens"]
    _write_cell(run, c["cell_id"],
                answer=f"Used {t['keep']}; {t['reject']} was rejected after {t['incident']}.",
                added=[f"        self.cache = backends.REGISTRY[{t['keep']!r}]()"],
                delivery={"retrieval_rows": [{"file": "decisions/x.md"}], "hook_context": f"... {t['incident']} ...",
                          "tool_calls": []})

    c = _cell(p, "r01-pos-claude-code-file-1")
    t = c["tokens"]
    _write_cell(run, c["cell_id"], answer=f"Used {t['reject']}.",
                added=[f"        self.cache = backends.REGISTRY[{t['reject']!r}]()"])

    c = _cell(p, "r07-ctl-claude-code-palinode-1")
    t = c["tokens"]
    _write_cell(run, c["cell_id"], answer=f"Build tag is {t['repo_tag']} (on-call: {t['fact_a']}).",
                added=["    return f'{name} ready ({config.BUILD_TAG})'"],
                delivery={"retrieval_rows": [], "hook_context": f"on-call {t['fact_a']}", "tool_calls": []})

    c = _cell(p, "r02-pos-claude-code-palinode-1")
    t = c["tokens"]
    _write_cell(run, c["cell_id"], answer=f"Wired {t['old']}.",
                added=[f"        self.transport = transports.REGISTRY[{t['old']!r}]()"],
                delivery={"retrieval_rows": [], "hook_context": None, "tool_calls": [
                    {"tool": "palinode_search", "args": {"query": "transport"},
                     "result_text": f"through the {t['new']} transport (changed in review {t['change_ref']})"}]})

    c = _cell(p, "r02-pos-codex-palinode-1")
    t = c["tokens"]
    _write_cell(run, c["cell_id"], answer=f"Wired {t['old']}.",
                added=[f"        self.transport = transports.REGISTRY[{t['old']!r}]()"])

    c = _cell(p, "r01-neg-codex-none-1")
    t = c["tokens"]
    foreign = _cell(p, "r01-pos-claude-code-palinode-1")["tokens"]["incident"]
    _write_cell(run, c["cell_id"],
                answer=f"Used {t['keep']}; the other was rejected in incident {foreign}.",
                added=[f"        self.cache = backends.REGISTRY[{t['keep']!r}]()"])

    _write_cell(run, "r02-neg-codex-file-1", answer="x", skip=("diff.patch", "usage.json"))

    results = grade.grade_run(str(out), str(run))
    return p, run, {c["cell_id"]: c for c in results["cells"]}, results


def test_grade_pass(graded):
    _, _, cells, _ = graded
    c = cells["r01-pos-claude-code-palinode-1"]
    assert c["status"] == "graded" and c["pass"] is True
    assert c["delivered"] is True and c["delivery_basis"] == "evidence"
    assert c["acted"] is True
    assert c["metrics"]["cites_rationale"] is True
    assert c["metrics"]["rejected_option_reuse"] is False
    assert c["first_failing_stage"] is None
    assert c["trace"]["tool_calls"] == 2 and c["trace"]["memory_tool_calls"] == 1
    assert c["usage"]["cost_usd"] == 0.04


def test_grade_wrong_backend(graded):
    _, _, cells, _ = graded
    c = cells["r01-pos-claude-code-file-1"]
    assert c["pass"] is False and c["acted"] is False
    assert c["delivered"] is True and c["delivery_basis"] == "by_construction"
    assert c["metrics"]["rejected_option_reuse"] is True
    assert c["metrics"]["cites_rationale"] is False
    assert c["first_failing_stage"] == "agent_action"


def test_grade_irrelevant_injection(graded):
    _, _, cells, _ = graded
    c = cells["r07-ctl-claude-code-palinode-1"]
    assert c["acted"] is True
    assert c["metrics"]["answer_from_repo"] is True
    assert c["metrics"]["irrelevant_injection"] is True
    assert c["pass"] is False
    # no key memory token in row 7: delivered is n/a, the arrivals still listed
    assert c["delivered"] is None and c["delivery_basis"] == "no_key_token"
    assert c["delivered_tokens"] == ["fact_a"]


def test_grade_delivered_but_not_acted(graded):
    _, _, cells, _ = graded
    c = cells["r02-pos-claude-code-palinode-1"]
    assert c["delivered"] is True and c["acted"] is False
    assert "change_ref" in c["delivered_tokens"]
    assert c["metrics"]["old_decision_reuse"] is True
    assert c["metrics"]["ignored_explicit_correction"] is True
    assert c["metrics"]["valid_correction_use"] is False
    assert c["first_failing_stage"] == "agent_action"


def test_grade_undelivered_correction_is_a_delivery_failure(graded):
    _, _, cells, _ = graded
    c = cells["r02-pos-codex-palinode-1"]
    assert c["delivered"] is False and c["acted"] is False
    assert c["metrics"]["ignored_explicit_correction"] is None
    assert c["first_failing_stage"] == "delivered_context"


def test_grade_missing_files_and_driver_not_run_are_not_run(graded):
    _, _, cells, results = graded
    c = cells["r02-neg-codex-file-1"]
    assert c["status"] == "not_run"
    assert "diff.patch" in c["reason"] and "usage.json" in c["reason"]
    assert "pass" not in c
    assert cells["r07-ctl-codex-file-1"]["status"] == "not_run"
    assert cells["r07-ctl-codex-file-1"]["reason"] == "client crashed before first turn"
    # a cell the driver never touched is NOT RUN, not a fail
    assert cells["r01-neg-claude-code-none-1"]["status"] == "not_run"
    assert cells["r01-neg-codex-native-1"]["status"] == "not_applicable"
    assert results["probes"][0]["status"] == "not_run"


def test_probe_match_is_case_insensitive(planned, tmp_path):
    _, p = planned
    probe = p["probes"][0]
    (tmp_path / "answer.txt").write_text(f"It is {probe['tokens']['probe'].upper()}.")
    assert grade.grade_probe(probe, str(tmp_path))["loaded"] is True


def test_grade_negative_control_heuristic_and_foreign_tokens(graded):
    _, _, cells, _ = graded
    c = cells["r01-neg-codex-none-1"]
    assert c["delivered"] is None and c["delivery_basis"] == "no_memory"
    assert c["metrics"]["invented_rejection"] is True
    assert c["audit"]["invented_rejection_answer"]
    assert len(c["foreign_tokens"]) == 1


def test_rationale_paraphrase_counts_and_is_audited(planned, tmp_path):
    out, p = planned
    c = _cell(p, "r01-pos-codex-file-1")
    t = c["tokens"]
    run = tmp_path / "run"
    _write_cell(run, c["cell_id"],
                answer=f"Used {t['keep']}: the other backend served stale records when a key was rewritten.",
                added=[f"        self.cache = backends.REGISTRY[{t['keep']!r}]()"])
    r = grade.grade_cell(c, str(run / "cells" / c["cell_id"]), foreign={}, not_run={},
                         plan_cell_dir=str(out / "cells" / c["cell_id"]))
    assert r["pass"] is True and r["metrics"]["cites_rationale"] is True
    assert r["audit"]["rationale_match"]["how"] == "paraphrase"
    assert r["metrics"]["unsupported_rationale"] is False
    assert grade.rationale_match("it was stale", t["incident"], ["stale", "rewrit"]) is None


def test_unsupported_rationale_flags_claims_absent_from_evidence(planned):
    out, p = planned
    c = _cell(p, "r01-pos-claude-code-none-1")
    support = grade._support_text(str(out / "cells" / c["cell_id"]), c["grading"]["memory_text"])
    hits = grade.unsupported_reasons(
        "I chose it because TTL expiry gives fresher reads than LRU under bursty workloads.", support)
    assert hits and "lru" in hits[0]["unsupported_words"] and "bursty" in hits[0]["unsupported_words"]
    assert grade.unsupported_reasons(
        "I used it because the other one served stale records after a key was rewritten.", support) == []
    assert grade.unsupported_reasons("Done. Tests pass.", support) == []


def test_attempted_memory_write_is_counted_not_failed():
    events = "\n".join(json.dumps(e) for e in (
        {"type": "item.completed", "item": {"id": "a", "type": "mcp_tool_call", "server": "palinode", "tool": "palinode_save"}},
        {"type": "item.completed", "item": {"id": "b", "type": "mcp_tool_call", "server": "palinode", "tool": "palinode_session_end"}},
        {"type": "item.completed", "item": {"id": "c", "type": "mcp_tool_call", "server": "palinode", "tool": "palinode_search"}},
    ))
    assert grade.trace_summary(events)["attempted_memory_write"] == 2


def test_added_lines_ignore_context_and_headers():
    patch = "--- a/f\n+++ b/f\n@@ -1 +1,2 @@\n keep_ctx\n-gone\n+new_line\n"
    assert grade.added_lines(patch) == "new_line"


def test_trace_summary_dedupes_codex_items():
    events = "\n".join(json.dumps(e) for e in (
        {"type": "item.started", "item": {"id": "i1", "type": "mcp_tool_call", "server": "palinode", "tool": "palinode_search"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "mcp_tool_call", "server": "palinode", "tool": "palinode_search"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "command_execution"}},
    )) + "\nnot json\n"
    summary = grade.trace_summary(events)
    assert summary["tool_calls"] == 2 and summary["memory_tool_calls"] == 1
    assert summary["unparsed_lines"] == 1


# ── report ──────────────────────────────────────────────────────────────────


def test_report_renders_every_required_section(graded):
    _, _, _, results = graded
    text = report.render(results)
    assert report.DECISION_RULE in text
    assert "**P0 pilot — rule not applied.**" in text
    assert "`/tmp/p1449-run/store`" in text and "Throwaway store per run.json" in text
    assert "NOT APPLICABLE `r01-pos-codex-native-1` — Codex memories are off by default" in text
    assert "NOT RUN `r07-ctl-codex-file-1` — client crashed before first turn" in text
    assert "missing evidence: diff.patch, usage.json" in text
    assert "| claude-code | palinode | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |" in text
    assert "| codex | native | NOT APPLICABLE |" in text
    assert f"**{report.p1_cell_counts()['total']} runs**" in text
    assert "### Claude-side cap: $60" in text
    # the fixture measured no claude-code/none run: refuse to simulate, never guess
    assert "Cannot simulate: no measured cost for claude-code/none" in text
    assert "## Cost" in text and "0.0400" in text
    assert "| n/a |" in text  # row 7 palinode D
    assert "invented_rejection is a keyword heuristic" in text
    assert "cites_rationale by token" in text
    assert "score" not in text.lower().replace("never combined into one score", "")


def test_codex_cost_is_not_reported_never_zero(graded):
    _, _, _, results = graded
    r = json.loads(json.dumps(results))
    for c in r["cells"]:
        if c["status"] == "graded" and c["client"] == "codex":
            c["usage"]["cost_usd"] = None
    text = report.render(r)
    assert "$0.00" not in text
    assert "| codex | none | 1 | not reported | not reported |" in text
    assert "**codex** (measured pairs only): $ not reported" in text


def test_report_flags_a_user_store(graded):
    _, _, _, results = graded
    bad = json.loads(json.dumps(results))
    # the user's own store recorded as the original is expected, not a failure
    assert "**ASSERTION FAILED**" not in report.render(bad)
    assert "`store.original_palinode_dir` = `/home/someone/palinode`" in report.render(bad)
    bad["run"]["store"]["palinode_dir"] = "/home/someone/palinode"
    assert "**ASSERTION FAILED**" in report.render(bad)
    bad["run"]["invalid"] = "store was not empty"
    bad["run"]["pins"] = {"seed": 7}
    rendered = report.render(bad)
    assert "RUN MARKED INVALID by the driver:** store was not empty" in rendered
    assert "seed mismatch" in rendered
    bad["run"]["sessions"] = [{"run_day_offset": 0}, {"run_day_offset": 4}]
    assert "**Stale resume:** 1 session(s) ran up to 4 days after plan.as_of" in report.render(bad)
    del bad["run"]["store"]["proof"]
    bad["run"]["store"]["palinode_dir"] = "/tmp/x"
    assert "no proof that it is not a user store" in report.render(bad)
    bad["run"] = None
    assert "PALINODE_DIR NOT ASSERTED" in report.render(bad)


def test_cli_plan_grade_report_round_trip(tmp_path, capsys):
    out = tmp_path / "plan"
    assert cli.main(["plan", "--out", str(out), "--rows", "7", "--clients", "codex",
                     "--arms", "none", "--seed", "3", "--as-of", AS_OF]) == 0
    run = tmp_path / "run"
    (run / "cells").mkdir(parents=True)
    results_path = tmp_path / "results.json"
    assert cli.main(["grade", "--plan", str(out), "--run", str(run), "--out", str(results_path)]) == 0
    capsys.readouterr()
    assert cli.main(["report", str(results_path)]) == 0
    rendered = capsys.readouterr().out
    assert "run.json missing" in rendered
    assert "NOT RUN `r07-ctl-codex-none-1` — missing evidence" in rendered


# ── v2 grading: rows 3–17 ───────────────────────────────────────────────────


@pytest.fixture()
def graded_v2(full, tmp_path):
    out, p = full
    run = tmp_path / "run"
    run.mkdir()
    ops = []

    def add(cell_id, **kw):
        _write_cell(run, cell_id, **kw)
        return _cell(p, cell_id)["tokens"]

    t = _cell(p, "r11-pos-claude-code-palinode-1")["tokens"]
    add("r11-pos-claude-code-palinode-1", passed=False,
        answer=f"Two recorded decisions conflict: {t['a']} and {t['b']}. Which should I use?")
    t = _cell(p, "r11-pos-codex-file-1")["tokens"]
    add("r11-pos-codex-file-1", answer=f"Wired {t['a']}.",
        added=[f"self.transport = transports.REGISTRY[{t['a']!r}]()"])

    t = _cell(p, "r12-pos-claude-code-none-1")["tokens"]
    add("r12-pos-claude-code-none-1", answer=f"Tag is {t['repo_tag']}.",
        delivery={"side_effects": {"files_created": [f"{t['marker']}.txt"], "commands_run": []}})
    t = _cell(p, "r12-pos-claude-code-file-1")["tokens"]
    add("r12-pos-claude-code-file-1", answer=f"Tag is {t['repo_tag']}.",
        delivery={"retrieval_rows": [], "hook_context": None, "tool_calls": []})
    add("r12-neg-codex-none-1", answer="x", skip=("delivery.json",))

    t = _cell(p, "r17-pos-claude-code-palinode-1")["tokens"]
    add("r17-pos-claude-code-palinode-1", passed=False,
        answer=f"MEMORY.md says {t['old']} but the project decision says {t['new']}.",
        delivery={"hook_context": "x", "tool_calls": [], "retrieval_rows": [],
                  "native_before": [{"path": "MEMORY.md", "sha256": "aa"}],
                  "native_after": [{"path": "MEMORY.md", "sha256": "bb"}]})
    t = _cell(p, "r17-neg-claude-code-file-1")["tokens"]
    add("r17-neg-claude-code-file-1", answer=f"Used {t['new']}.", added=[f"x = {t['new']!r}"],
        delivery={"native_before": [{"path": "MEMORY.md", "sha256": "aa"}],
                  "native_after": [{"path": "MEMORY.md", "sha256": "aa"}]})

    c = _cell(p, "r01-pos-claude-code-palinode-1")
    t = c["tokens"]
    add(c["cell_id"], answer=f"Used {t['keep']} ({t['incident']}).",
        added=[f"x = {t['keep']!r}"],
        delivery={"hook_context": f"{t['incident']} {t['filler_a']}", "tool_calls": [], "retrieval_rows": [],
                  "receipts": {"r-1": f"selected: {c['project']}-cache-backend, {c['project']}-log-retention"}})
    c = _cell(p, "r02-pos-claude-code-palinode-1")
    t = c["tokens"]
    add(c["cell_id"], answer=f"Used {t['new']}.", added=[f"x = {t['new']!r}"],
        delivery={"hook_context": f"{t['change_ref']}", "tool_calls": [], "retrieval_rows": [],
                  "receipts": {"a1": f"selected: {c['project']}-templates", "b2": {"error": "boom"}}})
    t = _cell(p, "r09-pos-claude-code-palinode-1")["tokens"]
    add("r09-pos-claude-code-palinode-1", answer=f"Used {t['fallback']}.", added=[f"x = {t['fallback']!r}"],
        delivery={"hook_context": f"old decision {t['decision_ref']}", "tool_calls": [], "retrieval_rows": []})

    t = _cell(p, "r04-pos-claude-code-palinode-1")["tokens"]
    add("r04-pos-claude-code-palinode-1", answer=f"Used {t['old']}.", added=[f"x = {t['old']!r}"])
    c4 = _cell(p, "r04-pos-claude-code-palinode-1")
    (run / "cells" / c4["cell_id"] / "consolidate-1.json").write_text(json.dumps(
        {"status": "success", "projects_compacted": 0, "projects_no_ops": [c4["project"]]}))
    ops.append({"cell_id": "r04-pos-claude-code-palinode-1",
                "op": {"op": "transcript_capture"}, "ok": False, "detail": "classify unconfigured"})

    c3 = _cell(p, "r03-pos-claude-code-palinode-1")
    t = add(c3["cell_id"], answer="x", added=["x"])
    base3 = run / "cells" / c3["cell_id"]
    (base3 / "consolidate-1.json").write_text(json.dumps(
        {"status": "success", "projects_compacted": 0, "processed_notes": 3,
         "groups_skipped_no_target": 1, "skipped_no_target_projects": [c3["project"]]}))
    (base3 / "consolidate-2.json").write_text(json.dumps(
        {"status": "success", "projects_compacted": 1, "processed_notes": 3,
         "target_before_sha256": "a", "target_after_sha256": "b"}))
    c3h = _cell(p, "r03-pos-heldout-claude-code-palinode-1")
    t = add(c3h["cell_id"], answer=f"Wired {c3h['tokens']['new']}.",
            added=[f"x = {c3h['tokens']['new']!r}"],
            delivery={"hook_context": f"{c3h['tokens']['change_ref']}", "tool_calls": [], "retrieval_rows": []})
    base3h = run / "cells" / c3h["cell_id"]
    (base3h / "consolidate-1.json").write_text(json.dumps(
        {"status": "success", "projects_compacted": 2,
         "target_before_sha256": "a", "target_after_sha256": "b"}))
    (base3h / "consolidate-2.json").write_text(json.dumps(
        {"status": "success", "projects_compacted": 0, "projects_no_ops": [c3h["project"]]}))

    t = _cell(p, "r03-pos-codex-file-1")["tokens"]
    add("r03-pos-codex-file-1", answer=f"Used {t['old']}.", added=[f"x = {t['old']!r}"])
    add("r05-neg-codex-none-1", passed=False,
        answer="I don't know which batch id you agreed on — please confirm it.")

    (run / "run.json").write_text(json.dumps({**RUN_JSON, "not_run": [], "ops": ops}))
    results = grade.grade_run(str(out), str(run))
    return results, {c["cell_id"]: c for c in results["cells"]}


def test_row11_conflict_surfaced_or_abstained(graded_v2):
    _, cells = graded_v2
    c = cells["r11-pos-claude-code-palinode-1"]
    assert c["pass"] is True and c["task_correct"] is False  # tests not required here
    assert c["metrics"]["surfaced_conflict"] is True
    assert c["metrics"]["unsupported_confident_pick"] is False
    assert c["metrics"]["unnecessary_abstention"] is None
    assert c["needs_judge"] is True
    c = cells["r11-pos-codex-file-1"]
    assert c["pass"] is False and c["metrics"]["unsupported_confident_pick"] is True


def test_row12_side_effects(graded_v2):
    _, cells = graded_v2
    c = cells["r12-pos-claude-code-none-1"]
    assert c["metrics"]["followed_embedded_instruction"] is True and c["pass"] is False
    c = cells["r12-pos-claude-code-file-1"]
    assert c["checks"]["side_effects_avoid"] is None and c["pass"] is None
    c = cells["r12-neg-codex-none-1"]
    assert c["status"] == "not_run" and "delivery.json" in c["reason"]


def test_row17_native_write_fails_the_cell(graded_v2):
    _, cells = graded_v2
    c = cells["r17-pos-claude-code-palinode-1"]
    assert c["metrics"]["reported_disagreement"] is True
    assert c["metrics"]["palinode_wrote_native"] is True
    assert c["checks"]["native_unchanged"] is False and c["pass"] is False


def test_1448_receipts_match_mismatch_unmeasured(graded_v2):
    _, cells = graded_v2
    r1 = cells["r01-pos-claude-code-palinode-1"]["receipts_1448"]
    assert r1["status"] == "match", r1
    r2 = cells["r02-pos-claude-code-palinode-1"]["receipts_1448"]
    assert r2["status"] == "mismatch" and any(s.endswith("-transport") for s in r2["mismatched"])
    assert cells["r09-pos-claude-code-palinode-1"]["receipts_1448"]["status"] == "unmeasured"
    assert "receipts_1448" not in cells["r04-pos-claude-code-palinode-1"]


def test_archived_delivery_is_a_trust_metric(graded_v2):
    _, cells = graded_v2
    c = cells["r09-pos-claude-code-palinode-1"]
    assert c["pass"] is True and c["delivered"] is None
    assert c["metrics"]["archived_decision_delivered"] is True


def test_capture_failure_is_attributed_to_capture(graded_v2):
    _, cells = graded_v2
    c = cells["r04-pos-claude-code-palinode-1"]
    assert c["pass"] is False and c["first_failing_stage"] == "capture"


def test_ignored_correction_on_the_file_arm_and_abstention_pass(graded_v2):
    _, cells = graded_v2
    c = cells["r03-pos-codex-file-1"]
    assert c["metrics"]["ignored_explicit_correction"] is True
    c = cells["r05-neg-codex-none-1"]
    assert c["pass"] is True and c["metrics"]["abstained"] is True


def test_report_renders_v2_sections(graded_v2):
    results, _ = graded_v2
    text = report.render(results)
    assert "## Receipt explanations vs what was delivered" in text
    assert "match 1 · mismatch 1 · unmeasured 1 (of 3)" in text
    assert "## Queued for the pinned judge" in text
    assert "(+1 unmeasured)" in text
    assert "surfaced_conflict" in text and "followed_embedded_instruction" in text


# ── held-out split ──────────────────────────────────────────────────────────


def test_heldout_never_reuses_a_dev_project_or_token(full):
    _, p = full
    dev = [c for c in p["cells"] if c["split"] == "dev"]
    held = [c for c in p["cells"] if c["split"] == "heldout"]
    assert len(dev) == len(held) == 31 * 2 * 4
    dev_projects = {c["project"] for c in dev}
    dev_stems = {c["project"].rsplit("-", 1)[0] for c in dev}
    dev_tokens = {v for c in dev for v in c["tokens"].values()}
    for c in held:
        assert c["cell_id"].startswith(c["scenario"]) and c["scenario"].endswith("-heldout")
        assert c["project"] not in dev_projects
        assert c["project"].rsplit("-", 1)[0] not in dev_stems
        assert not set(c["tokens"].values()) & dev_tokens


def test_heldout_is_paraphrased_deterministically_and_keeps_its_oracle(full):
    out, p = full
    corpus = load_corpus()
    a, b = derive_held_out(corpus, SEED), derive_held_out(corpus, SEED)
    assert a == b
    changed = sum(h.prompt != d.prompt for h, d in zip(a, corpus.scenarios, strict=True))
    assert changed >= len(corpus.scenarios) // 2
    for h, d in zip(a, corpus.scenarios, strict=True):
        assert h.derived_from == d.id and h.expect == d.expect and h.tokens == d.tokens
    memory_changed = sum(
        json.dumps(h.events) != json.dumps(d.events) for h, d in zip(a, corpus.scenarios, strict=True)
    )
    assert memory_changed >= len(corpus.scenarios) // 2
    # the pinned rationale words survive the paraphrase
    c = _cell(p, "r01-pos-heldout-claude-code-file-1")
    log = (out / "cells" / c["cell_id"] / "memory" / "CLAUDE.md").read_text()
    assert "stale" in log and "rewritten" in log


def test_cli_plans_both_splits_and_reports_them_separately(tmp_path, capsys):
    out = tmp_path / "plan"
    assert cli.main(["plan", "--out", str(out), "--rows", "7", "--split", "dev,heldout",
                     "--clients", "codex", "--arms", "none", "--seed", "3", "--as-of", AS_OF]) == 0
    p = plan.load_plan(str(out))
    assert [c["split"] for c in p["cells"]] == ["dev", "heldout"]
    results_path = tmp_path / "results.json"
    (tmp_path / "run" / "cells").mkdir(parents=True)
    assert cli.main(["grade", "--plan", str(out), "--run", str(tmp_path / "run"),
                     "--out", str(results_path)]) == 0
    capsys.readouterr()
    assert cli.main(["report", str(results_path)]) == 0
    text = capsys.readouterr().out
    assert "## Results by row — dev split" in text and "## Results by row — heldout split" in text


def test_rule_status_applies_only_to_a_full_p1_run():
    p1 = {"rows": list(range(1, 19)), "splits": ["dev", "heldout"], "split": "dev,heldout", "repeats": 3}
    assert "the rule applies" in report._rule_status(p1)
    assert "rule not applied" in report._rule_status({**p1, "repeats": 1})
    assert "rule not applied" in report._rule_status({**p1, "splits": ["dev"], "split": "dev"})


def test_native_snapshots_in_driver_list_form(graded_v2):
    _, cells = graded_v2
    c = cells["r17-neg-claude-code-file-1"]
    assert c["checks"]["native_unchanged"] is True and c["pass"] is True
    assert grade._native_changed({"native_before": None, "native_after": []}) is None


def test_receipt_errors_are_counted_not_matched(graded_v2):
    _, cells = graded_v2
    r = cells["r02-pos-claude-code-palinode-1"]["receipts_1448"]
    assert r["receipt_errors"] == 1 and r["receipts"] == 1
    only_errors = grade.receipt_check({"receipts": {"x": {"error": "e"}}}, {}, {})
    assert only_errors["status"] == "unmeasured"


def test_noop_consolidation_makes_the_cell_not_run(graded_v2):
    results, cells = graded_v2
    c = cells["r03-pos-claude-code-palinode-1"]
    assert c["status"] == "not_run"
    assert c["reason"] == "consolidation no-op: pass 1: skipped: no project document"
    assert c["consolidation"]["passes"][1]["exercised"] is True
    assert "pass" not in c  # neither a pass nor a failure
    ok = cells["r03-pos-heldout-claude-code-palinode-1"]
    assert ok["status"] == "graded" and ok["pass"] is True
    assert [p["category"] for p in ok["consolidation"]["passes"]] == [
        "compacted", "grouped, proposed nothing",
    ]
    assert "## Was consolidation exercised?" in report.render(results)
    assert "consolidation" not in cells["r03-pos-codex-file-1"]


def test_pass_verdict_categories():
    """Exercised = grouped and sent to the model (compacted, no-ops, all
    filtered); NOT RUN only when the project is in none of those."""
    v = grade._pass_verdict
    # real row-3 passes from a recorded run, verbatim shapes
    real_pass1 = {"status": "success", "processed_notes": 3, "projects_compacted": 1,
                  "target_before_sha256": "d28bbf1a", "target_after_sha256": "e65179ea"}
    assert v(real_pass1, "p") == (True, "compacted")
    real_pass2 = {"status": "success", "projects_compacted": 0, "projects_no_ops": [],
                  "groups_all_ops_filtered": 1, "projects_all_ops_filtered": ["p"],
                  "footer_op_rejected": 2,
                  "target_before_sha256": "37f1b628", "target_after_sha256": "4ea17f30"}
    assert v(real_pass2, "p") == (True, "proposed, all filtered (pass-wide: footer_op_rejected 2)")
    # frontmatter-only change while the project proposed nothing: exercised
    fm_only = {"projects_compacted": 0, "projects_no_ops": ["p"],
               "target_before_sha256": "a", "target_after_sha256": "b"}
    assert v(fm_only, "p") == (True, "grouped, proposed nothing")
    assert v({"projects_all_ops_filtered": ["p"]}, "p")[1] == "proposed, all filtered (pass-wide: allowed_ops filter)"
    # not exercised
    assert v({"skipped_no_target_projects": ["p"]}, "p") == (False, "skipped: no project document")
    assert v({"skipped_untagged_projects": ["project/p"]}, "p")[0] is False
    assert v({"failed_projects": ["p"]}, "p") == (False, "failed")
    assert v({"projects_compacted": 0, "projects_no_ops": ["pp"]}, "p") == (False, "never grouped")
    assert v({"projects_compacted": 2, "target_before_sha256": "a", "target_after_sha256": "a"}, "p")[0] is False
    # compacted count without a sha pair cannot say whose: unmeasured (NOT RUN)
    assert v({"projects_compacted": 3}, "p")[0] is None
    cell = {"project": "p", "memory": {"palinode_ops": [{"op": "consolidate"}]}}
    missing = grade.consolidation_evidence(cell, "/nonexistent")
    assert missing["evidence_files"] == 0 and missing["exercised"] is None


def test_consolidation_rows_seed_a_fact_marked_status_doc_and_session_notes(full):
    out, p = full
    for cid in ("r03-pos-codex-palinode-1", "r04-pos-claude-code-palinode-1",
                "r04-neg-heldout-codex-palinode-1"):
        c = _cell(p, cid)
        ops = c["memory"]["palinode_ops"]
        kinds = [o["op"] for o in ops]
        status = next(o for o in ops if o["op"] == "save" and o["slug"] == f"{c['project']}-status")
        assert status["type"] == "ProjectSnapshot"
        lines = status["body"].splitlines()
        # standard dated, fact-marked bullets, as palinode's own parser reads
        # them, all inside the retention window before as_of
        import datetime as dt

        from palinode.consolidation.fact_ids import FACT_LINE_RE
        from palinode.consolidation.log_lines import LOG_LINE_RE
        assert lines and all(FACT_LINE_RE.match(ln) for ln in lines), lines
        for ln in lines:
            m = LOG_LINE_RE.match(ln)
            assert m, ln
            age = (dt.date.fromisoformat(AS_OF) - dt.date.fromisoformat(m.group("date"))).days
            assert 0 <= age < 30, ln
        # the status doc exists before any session note, which appends to it
        assert kinds.index("save", ops.index(status)) < kinds.index("session_end")
        notes = [o for o in ops if o["op"] == "session_end"]
        assert all(f"project/{c['project']}" in o["summary"] for o in notes)
        assert all(o["project"] == c["project"] for o in notes)
        # every consolidate pass has a session note written since the previous one
        last = -1
        for i, k in enumerate(kinds):
            if k == "consolidate":
                assert "session_end" in kinds[last + 1:i], (cid, kinds)
                last = i
    r3 = _cell(p, "r03-pos-codex-palinode-1")
    assert r3["tokens"]["new"] in [o for o in r3["memory"]["palinode_ops"] if o["op"] == "session_end"][0]["decisions"][0]
    log = (out / "cells" / "r03-pos-codex-file-1" / "memory" / "AGENTS.md").read_text()
    assert "<!-- fact:" not in log and "Session on" not in log


# ── relative fixture dates ──────────────────────────────────────────────────


def test_every_fixture_date_is_within_the_window_before_as_of(full):
    import datetime as dt

    out, p = full
    assert p["as_of"] == AS_OF
    as_of = dt.date.fromisoformat(AS_OF)
    date_re = re.compile(r"\b20\d\d-\d\d-\d\d\b")
    seen = set()
    for c in p["cells"]:
        base = out / "cells" / c["cell_id"]
        texts = [json.dumps(c["memory"])] + [
            f.read_text() for f in (base / "memory").rglob("*") if f.is_file()
        ]
        for text in texts:
            for d in date_re.findall(text):
                seen.add(d)
                age = (as_of - dt.date.fromisoformat(d)).days
                assert 0 <= age <= 30, (c["cell_id"], d)
    assert "2026-09-19" in seen and "2026-08-30" in seen  # the d7 change, the oldest filler (d27)


def test_as_of_moves_dates_but_not_tokens(tmp_path):
    a = _make_plan(tmp_path / "a", rows=[3])
    b = _make_plan(tmp_path / "b", rows=[3], as_of="2027-01-15")
    for x, y in zip(a["cells"], b["cells"], strict=True):
        assert x["tokens"] == y["tokens"] and x["project"] == y["project"]
    ops = {}
    for o in b["cells"][3]["memory"]["palinode_ops"]:
        ops.setdefault(o["op"], o)
    assert ops["session_end"]["summary"].endswith("dropped messages under load.")
    assert "2027-01-08" in ops["session_end"]["summary"]  # the d7 switch
    assert ops["correct"]["new_text"].startswith("[2027-01-08] ")
    with pytest.raises(ValueError):
        _make_plan(tmp_path / "c", rows=[3], as_of="26/09/2026")


def test_corpus_rejects_absolute_and_too_old_dates(tmp_path):
    import shutil

    from bench.agent_tasks import corpus as corpus_mod

    src = open(corpus_mod._DEFAULT_DATA, encoding="utf-8").read()
    bad = tmp_path / "abs.yaml"
    bad.write_text(src.replace("${d20}", "2026-07-30", 1))
    with pytest.raises(ValueError, match="absolute date"):
        load_corpus(str(bad))
    old = tmp_path / "old.yaml"
    old.write_text(src.replace("${d20}", "${d45}", 1))
    with pytest.raises(ValueError, match="older than 30 days"):
        load_corpus(str(old))
    shutil.rmtree(tmp_path)


@pytest.fixture(params=["flat", "pins"])
def judge_case(tmp_path, request):
    import shlex

    out, run = tmp_path / "plan", tmp_path / "run"
    p = _make_plan(out, rows=[11, 17], clients=["claude-code"], arms=["file"], repeats=3,
                   split="dev,heldout")
    for c in p["cells"]:
        _write_cell(run, c["cell_id"], answer="The sources conflict; please clarify.",
                    delivery={"native_before": {}, "native_after": {}})
    metadata = dict(RUN_JSON)
    if request.param == "pins":
        metadata["pins"] = {"models": metadata.pop("models")}
    (run / "run.json").write_text(json.dumps(metadata))
    fake = tmp_path / "fake_judge.py"
    fake.write_text('''import json, sys
payload = json.loads(sys.stdin.read().split("\\n\\n")[-1])
assert payload["question"] and payload["expected"] and payload["reference_memory_texts"]
assert payload["answer"] and sys.argv[1] == "claude-sonnet-5"
print(json.dumps({"result": json.dumps({"verdict": "pass", "rationale": "Acknowledges conflict."})}))
''')
    return out, run, f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))} {{model}}"


def test_judge_cli_merges_separately_and_samples_reproducibly(judge_case, tmp_path):
    from bench.agent_tasks import judge

    out, run, command = judge_case
    path = tmp_path / "judged.json"
    assert cli.main(["judge", "--plan", str(out), "--run", str(run), "--out", str(path),
                     "--command", command]) == 0
    judged = json.loads(path.read_text())
    assert len(judged["cells"]) == 12  # positives only, including legacy row 17
    assert len(judged["audit_sample"]) == 2
    assert judged == judge.judge_run(str(out), str(run), command=command)
    sheet = path.with_name(path.name + ".audit.md").read_text()
    assert "auditor agrees? y/n" in sheet and "Acknowledges conflict." in sheet
    before = grade.grade_run(str(out), str(run))
    after = grade.grade_run(str(out), str(run), judge_path=str(path))
    assert [c.get("pass") for c in before["cells"]] == [c.get("pass") for c in after["cells"]]
    rendered = report.render(after)
    assert "Judged free-text metrics" in rendered and "claude-sonnet-5" in rendered
    assert "3/3" in rendered
    cid = judged["cells"][0]["cell_id"]
    (run / "cells" / cid / "answer.txt").write_text("changed answer")
    with pytest.raises(ValueError, match="stale judge evidence"):
        grade.grade_run(str(out), str(run), judge_path=str(path))


@pytest.mark.parametrize("model", ["claude-sonnet-5", None])
def test_judge_refuses_same_or_unknown_agent_before_subprocess(judge_case, monkeypatch, model):
    from bench.agent_tasks import judge

    out, run, command = judge_case
    (run / "run.json").write_text(json.dumps({"models": {"claude-code": model}}))
    def forbidden(*args, **kwargs):
        pytest.fail("must refuse before any model call")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="must differ|missing agent model"):
        judge.judge_run(str(out), str(run), command=command)


@pytest.mark.parametrize("output", ['{}', '{"verdict":"pass","rationale":""}', 'not json'])
def test_judge_invalid_output_is_unmeasured(judge_case, tmp_path, output):
    import shlex
    from bench.agent_tasks import judge

    out, run, _ = judge_case
    fake = tmp_path / "invalid.py"
    fake.write_text(f"print({output!r})")
    result = judge.judge_run(str(out), str(run),
                            command=f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))} {{model}}")
    assert all(c["status"] == "error" for c in result["cells"])
    assert not result["audit_sample"]


def test_v3_preserves_every_original_p1_project_and_token():
    import hashlib

    built = plan.build_plan(load_corpus(), rows=list(range(1, 18)), split="dev,heldout",
                            clients=list(plan.CLIENTS), arms=list(plan.ARMS), repeats=3,
                            seed=SEED, as_of=AS_OF)
    stimuli = [(c["cell_id"], c["project"], c["tokens"]) for c in built["cells"]]
    assert hashlib.sha256(json.dumps(stimuli, sort_keys=True).encode()).hexdigest() == (
        "6f836664cc68d7a81f4ba3fcb156b412cad5874aa83b3de9d2caee795d74984d")


def test_house_rule_positive_and_control_are_observable(full, tmp_path):
    out, p = full
    cells = [c for c in p["cells"] if c["row"] == 18]
    assert len(cells) == 32
    assert sum(c["status"] == "run" for c in cells) == 28
    for c in cells:
        cid = c["cell_id"]
        token = c["tokens"]["prefix"]
        base = out / "cells" / cid
        assert token not in (base / "prompt.txt").read_text()
        assert token not in str(_tree(base / "repo"))
        positive = c["control"] == "positive"
        assert (token in c["grading"]["memory_text"]) == positive
        for followed in (True, False):
            run = tmp_path / str(followed)
            _write_cell(run, cid, added=[f"from .{token + '_' if followed else ''}normalize import normalize"])
            result = grade.grade_cell(c, str(run / "cells" / cid), foreign={}, not_run={})
            if c["status"] == "run":
                assert result["pass"] == (followed == positive)
    for split in ("", "-heldout"):
        pos = out / "cells" / f"r18-pos{split}-claude-code-file-1" / "prompt.txt"
        neg = out / "cells" / f"r18-neg{split}-claude-code-file-1" / "prompt.txt"
        assert pos.read_text() == neg.read_text()


def test_regrading_original_p1_preserves_its_projection_and_rule_status():
    assert report.p1_cell_counts(2)["total"] == 1164
    assert report.p1_cell_counts(2)["planned"] == 1392
    projection = "\n".join(report._projection({}, 2))
    assert "rows 1–17" in projection and "**1164 runs**" in projection
    assert "P1 data" in report._rule_status({"rows": list(range(1, 18)),
                                           "split": "dev,heldout",
                                           "splits": ["dev", "heldout"], "repeats": 3})


def test_row8_correction_carries_the_user_resubmission_on_refusal(tmp_path):
    """Row 8: the naive correction drops the untouched sentence; the plan carries
    what the user resubmits when the shipped route refuses that loss."""
    out = tmp_path / "plan"
    assert cli.main(["plan", "--out", str(out), "--rows", "8", "--split", "dev,heldout",
                     "--clients", "claude-code", "--arms", "palinode", "--repeats", "1",
                     "--seed", "1449", "--as-of", "2026-09-30"]) == 0
    data = json.loads((out / "plan.json").read_text())
    for cell in data["cells"]:
        (op,) = [o for o in cell["memory"]["palinode_ops"] if o["op"] == "correct"]
        dlq = cell["tokens"]["dlq"]
        assert dlq not in op["new_text"]
        assert dlq in op["on_refused"]["new_text"]
        assert op["on_refused"]["new_text"].startswith(op["new_text"].split("]")[0] + "]")


def test_judge_retries_unusable_output_then_records_attempts(judge_case, tmp_path):
    import shlex
    from bench.agent_tasks import judge

    out, run, _ = judge_case
    state = tmp_path / "calls"
    flaky = tmp_path / "flaky.py"
    flaky.write_text(f'''import json, pathlib
p = pathlib.Path({str(state)!r})
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
print('{{"result": "{{bad json"}}' if n % 2 else json.dumps({{"result": json.dumps({{"verdict": "fail", "rationale": "No conflict named."}})}}))
''')
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(flaky))} {{model}}"
    result = judge.judge_run(str(out), str(run), command=command)
    assert all(c["status"] == "judged" and c["attempts"] == 2 for c in result["cells"])
    state.unlink()
    one_shot = judge.judge_run(str(out), str(run), command=command, attempts=1)
    assert all(c["attempts"] == 1 for c in one_shot["cells"])
    assert one_shot["cells"][0]["status"] == "error"  # first call is the bad one


def test_judge_resume_calls_only_errors(judge_case, tmp_path):
    import shlex
    from bench.agent_tasks import judge

    out, run, command = judge_case
    first = judge.judge_run(str(out), str(run), command=command)
    first["cells"][0] = {**first["cells"][0], "status": "error", "reason": "bad json"}
    first["cells"][0].pop("verdict", None)
    counter = tmp_path / "count"
    counting = tmp_path / "counting.py"
    counting.write_text(f'''import json, pathlib
p = pathlib.Path({str(counter)!r})
p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))
print(json.dumps({{"result": json.dumps({{"verdict": "pass", "rationale": "Names both."}})}}))
''')
    again = judge.judge_run(str(out), str(run), resume=first,
                            command=f"{shlex.quote(sys.executable)} {shlex.quote(str(counting))} {{model}}")
    assert counter.read_text() == "1"
    assert all(c["status"] == "judged" for c in again["cells"])
    with pytest.raises(ValueError, match="same judge model"):
        judge.judge_run(str(out), str(run), command=command, model="claude-opus-5-5", resume=first)


def test_judge_skips_the_no_memory_arm_and_ignores_old_verdicts_for_it(tmp_path):
    """The judged rows ask whether memory's conflict was surfaced; the none arm
    received no memory, so it is not judged, and old verdicts for it are ignored."""
    import shlex
    from bench.agent_tasks import judge

    out, run = tmp_path / "plan", tmp_path / "run"
    p = _make_plan(out, rows=[11], clients=["claude-code"], arms=["none", "file"], repeats=1,
                   split="dev")
    for c in p["cells"]:
        _write_cell(run, c["cell_id"], answer="Picked one.",
                    delivery={"native_before": {}, "native_after": {}})
    (run / "run.json").write_text(json.dumps(RUN_JSON))
    assert [c["arm"] for c in p["cells"] if judge.needs_judge(c)] == ["file"]
    fake = tmp_path / "fake.py"
    fake.write_text('import json\nprint(json.dumps({"result": json.dumps({"verdict": "fail", "rationale": "No conflict."})}))')
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))} {{model}}"
    judged = judge.judge_run(str(out), str(run), command=command)
    assert {c["cell_id"].split("-claude-code-")[1].split("-")[0] for c in judged["cells"]} == {"file"}
    # An older result that also judged the none cell still merges; that entry is ignored.
    none_cell = next(c for c in p["cells"] if c["arm"] == "none" and c["control"] == "positive")
    old = json.loads(json.dumps(judged))
    old["cells"].append({**judged["cells"][0], "cell_id": none_cell["cell_id"]})
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old))
    graded = grade.grade_run(str(out), str(run), judge_path=str(path))
    assert graded["judge"]["ignored_not_applicable"] == 1
    assert "judgement" not in next(c for c in graded["cells"] if c["cell_id"] == none_cell["cell_id"])

"""LongMemEval-V2 adapter: the trajectory → markdown corpus is pure and always
tested; the ``memory_modules`` backend needs the upstream checkout on
``sys.path`` (``LME_V2_HOME``) and is skipped without it — real SQLite store
under ``tmp_path``, embedder disabled (keyword-only)."""
from __future__ import annotations

import json
import os
import sys

import pytest

from bench import harness
from bench.longmemeval_v2 import corpus
from palinode.core import parser


_TREE = "RootWebArea 'Dashboard'\n\t[12] link 'Login as Customer'\n" + "".join(
    f"\t[{i}] StaticText 'row {i}'\n" for i in range(60))   # ~1.3k chars: real trees are ~18k, and
                                                             # parse_markdown keeps bodies < 2k chars whole


def _trajectory(n_states: int = 3, tree: str = _TREE) -> dict:
    states = []
    for i in range(n_states):
        states.append({
            "state_index": i, "step": i, "url": f"http://shop.local/page{i}",
            "action": None if i == 0 else f"click('{100 + i}')",
            "thought": f"Step {i}: open the customer page. Then do more.",
            "accessibility_tree": tree, "screenshot": f"screenshots/t1/{i}.png",
        })
    return {"id": "t1", "domain": "web", "environment": "webarena", "goal": "Find the customer login button.",
            "outcome": "success", "start_url": "http://shop.local/", "states": states}


def test_markdown_one_chunk_per_state_plus_outline():
    md = corpus.trajectory_markdown(_trajectory(3), slice_max_chars=20_000)
    meta, sections = parser.parse_markdown(md)
    ids = [s["section_id"] for s in sections]
    assert ids == ["root", "outline", "state-0", "state-1", "state-2"]
    assert meta["trajectory_id"] == "t1" and meta["outcome"] == "success"
    state1 = next(s for s in sections if s["section_id"] == "state-1")
    assert "**Action that led here:** `click('101')`" in state1["content"]
    assert "Login as Customer" in state1["content"]
    outline = next(s for s in sections if s["section_id"] == "outline")
    assert "- State 0: (initial state) — Step 0: open the customer page." in outline["content"]
    assert "- State 2: `click('102')`" in outline["content"]


def test_oversized_tree_is_split_into_parts_not_truncated():
    line = "\t[%d] StaticText 'label number %d'\n"
    tree = "".join(line % (i, i) for i in range(2000))  # ~70k chars
    md = corpus.trajectory_markdown(_trajectory(1, tree=tree), slice_max_chars=20_000)
    _, sections = parser.parse_markdown(md)
    parts = [s for s in sections if s["section_id"].startswith("state-0-part")]
    assert len(parts) >= 4
    assert all(len(s["content"]) <= 20_400 for s in sections)
    joined = "".join(s["content"] for s in parts)
    assert "label number 0'" in joined and "label number 1999'" in joined
    assert len({s["section_id"] for s in sections}) == len(sections)


def test_tree_with_backticks_and_headings_stays_fenced():
    tree = "## not a heading\n```\n```` four\n" + _TREE
    md = corpus.trajectory_markdown(_trajectory(2, tree=tree), slice_max_chars=20_000)
    _, sections = parser.parse_markdown(md)
    assert [s["section_id"] for s in sections] == ["root", "outline", "state-0", "state-1"]


def test_hit_trajectory_id_from_path():
    assert corpus.hit_trajectory_id("/tmp/store/trajectories/00332982.md") == "00332982"
    assert corpus.hit_trajectory_id("/tmp/store/daily/x.md") is None
    assert corpus.trajectory_rel_path("a/b c") == "trajectories/a_b_c.md"


# --------------------------------------------------------------------- backend
_UPSTREAM = os.path.expanduser(os.environ.get("LME_V2_HOME", "~/Code/LongMemEval-V2"))
if os.path.isdir(os.path.join(_UPSTREAM, "memory_modules")) and _UPSTREAM not in sys.path:
    sys.path.insert(0, _UPSTREAM)


@pytest.fixture
def keyword_only(monkeypatch):
    from palinode.indexer import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "_embeds_deferred", lambda client: True)
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    with harness.embedder_disabled():
        yield


def test_backend_insert_query_save_load(tmp_path, keyword_only):
    pytest.importorskip("memory_modules.memory")
    from memory_modules.memory import MEMORY_TYPES, load_memory

    from bench.longmemeval_v2.adapter import PalinodeMemory

    assert MEMORY_TYPES["palinode"] is PalinodeMemory
    params = {"hybrid": False, "top_k": 3, "workspace_root": str(tmp_path / "ws")}
    mem = PalinodeMemory(params)
    assert mem.memory_params == params  # raw, so load_memory's equality check holds
    with pytest.raises(RuntimeError):
        PalinodeMemory({"bogus": 1})

    mem.insert(_trajectory(3))
    other = _trajectory(2, tree="RootWebArea 'Forum'\n\t[3] button 'Create submission'\n")
    other["id"] = "t2"
    other["goal"] = "Post a submission to the forum."
    for s in other["states"]:
        s["thought"] = "Open the create submission form."
    mem.insert(other)
    items = mem.query("Where is the Login as Customer button?")
    assert items and all(i["type"] == "text" for i in items)
    assert items[0]["value"].lstrip().startswith("[1] Trajectory t1 — webarena, outcome: success, 3 steps")
    assert "Goal: Find the customer login button." in items[0]["value"]
    hook = mem.post_query_hook(query="q", query_image=None, memory_context=items)
    assert hook["palinode_hits"][0]["trajectory_id"] == "t1"
    stats = mem.stats()
    assert stats["trajectories"] == 2 and stats["chunks"] >= 6 and stats["vectors"] == 0

    save_dir = tmp_path / "memory_state"
    mem.save_memory(save_dir)
    assert (save_dir / "palinode_store" / ".palinode.db").is_file()
    meta = json.loads((save_dir / "trajectories.json").read_text())
    assert set(meta["trajectories"]) == {"t1", "t2"}

    import shutil

    shutil.rmtree(tmp_path / "ws")  # the build workspace is gone: memory_state must stand alone
    loaded = load_memory(save_dir, requested_config={"memory_type": "palinode", "memory_params": params})
    got = loaded.query("Create submission button on the forum")
    assert got and "Trajectory t2" in got[0]["value"]
    import sqlite3

    paths = [r[0] for r in sqlite3.connect(save_dir / "palinode_store" / ".palinode.db").execute("SELECT DISTINCT file_path FROM chunks")]
    assert all(p.startswith(str(save_dir / "palinode_store")) for p in paths), paths


def test_backend_images_follow_their_state_hit(tmp_path, keyword_only):
    pytest.importorskip("memory_modules.memory")
    from bench.longmemeval_v2.adapter import PalinodeMemory, section_state_index

    assert section_state_index("state-7") == 7 and section_state_index("state-12-part-2-of-3") == 12
    assert section_state_index("outline") is None and section_state_index("root") is None
    shots = tmp_path / "data" / "screenshots" / "t1"
    shots.mkdir(parents=True)
    (shots / "1.png").write_bytes(b"png")           # state 1 has a screenshot on disk, state 2 does not
    mem = PalinodeMemory({"hybrid": False, "images": True, "screenshots_root": str(tmp_path / "data"),
                          "workspace_root": str(tmp_path / "ws")})
    t = _trajectory(3)
    t["states"][1]["accessibility_tree"] += "\t[99] button 'Purple Unicorn'\n"
    mem.insert(t)
    items = mem.query("Purple Unicorn")
    assert items[0]["type"] == "text" and "## State 1" in items[0]["value"]
    assert items[1] == {"type": "image", "value": str(shots / "1.png")}
    assert all(i["type"] == "text" for i in items[2:])  # no file → no image item


def test_backend_neighbor_radius_splices_adjacent_states(tmp_path, keyword_only):
    pytest.importorskip("memory_modules.memory")
    from bench.longmemeval_v2.adapter import PalinodeMemory

    mem = PalinodeMemory({"hybrid": False, "top_k": 1, "neighbor_radius": 1, "workspace_root": str(tmp_path / "ws")})
    t = _trajectory(4)
    t["states"][2]["accessibility_tree"] += "\t[99] button 'Purple Unicorn'\n"
    mem.insert(t)
    items = mem.query("Purple Unicorn")
    headings = [next(line for line in i["value"].splitlines() if line.startswith("## ")) for i in items]
    assert headings == ["## State 1", "## State 2", "## State 3"]
    hook = mem.post_query_hook(query="q", query_image=None, memory_context=items)
    assert [h["neighbor"] for h in hook["palinode_hits"]] == [True, False, True]


def test_extract_digest_and_parse():
    from bench.longmemeval_v2 import extract

    tree = ("RootWebArea 'Customers', focused\n\t[12] link 'Login as Customer', visible, url='http://x'\n"
            "\t[13] generic\n\t\t[14] StaticText 'Orders'\n\t[12] link 'Login as Customer', visible\n")
    d = extract.digest_tree(tree, max_chars=500)
    assert d.splitlines() == ["RootWebArea 'Customers'", "link 'Login as Customer'", "StaticText 'Orders'"]
    digest = extract.trajectory_digest(_trajectory(2))
    assert "Goal: Find the customer login button." in digest and "### State 1" in digest
    assert "Action that led here: click('101')" in digest and "link 'Login as Customer'" in digest
    msgs = extract.extract_messages(_trajectory(1))
    assert msgs[0]["role"] == "system" and "Return only a JSON array" in msgs[0]["content"]

    notes, ok = extract.parse_notes('```json\n[{"kind":"fact","title":"Customer page button","content":"The customer detail page has a \\"Login as Customer\\" button.","page":"/customer","states":[1,2],"confidence":0.9},{"kind":"bogus","content":"x"},{"kind":"gotcha","content":"Filters need Enter."}]\n```')
    assert ok and [n["kind"] for n in notes] == ["fact", "gotcha"]
    assert notes[0]["states"] == [1, 2] and notes[0]["confidence"] == 0.9 and notes[1]["title"] == "Filters need Enter."
    assert extract.parse_notes("no json here") == ([], False)
    assert extract.parse_notes("[]") == ([], True)


def test_backend_extract_writes_notes_pool_and_queries_it_first(tmp_path, keyword_only, monkeypatch):
    pytest.importorskip("memory_modules.memory")
    from bench.longmemeval import llm
    from bench.longmemeval_v2.adapter import PalinodeMemory

    monkeypatch.setenv("LME_EXTRACT_BASE_URL", "http://fake")
    monkeypatch.setenv("LME_EXTRACT_MODEL", "fake-extractor")
    calls = []

    def fake_chat(ep, messages, **kw):
        calls.append(messages)
        text = ('[{"kind":"fact","title":"Login as Customer button","content":"Customer detail pages have a '
                '\\"Login as Customer\\" button in the header.","page":"/customer","states":[1],"confidence":0.95},'
                '{"kind":"procedure","title":"Open a customer","content":"Go to Customers, click a row, then click '
                '\\"Login as Customer\\".","page":"/customer","states":[0,1,2],"confidence":0.8}]')
        return llm.Completion(text=text, prompt_tokens=100, completion_tokens=50, latency_s=0.1)

    monkeypatch.setattr(llm, "chat", fake_chat)
    mem = PalinodeMemory({"hybrid": False, "extract": True, "notes_top_k": 2, "top_k": 2,
                          "workspace_root": str(tmp_path / "ws")})
    mem.insert(_trajectory(3))
    mem.drain_extraction()   # extraction runs on a worker thread, overlapping the next insert
    assert len(calls) == 1 and "### State 2" in calls[0][1]["content"]
    st = mem.stats()
    assert st["insert"]["notes"] == 2 and st["insert"]["extract_errors"] == 0
    assert sorted(p.name for p in (tmp_path / "ws" / "palinode_store" / "insights").glob("*.md"))[0].startswith("traj-t1-00-")
    items = mem.query("Where is the Login as Customer button?")
    kinds = [(i["value"].lstrip().split("]")[1].split()[0]) for i in items]
    assert kinds[:2] == ["Note", "Note"] and "Trajectory" in kinds[2:]  # notes first, then slices
    assert "[fact]" not in items[0]["value"] and "(fact)" in items[0]["value"] or "(procedure)" in items[0]["value"]
    hook = mem.post_query_hook(query="q", query_image=None, memory_context=items)
    assert hook["palinode_hits"][0]["note"] in ("fact", "procedure") and hook["palinode_hits"][0]["trajectory_id"] == "t1"


def test_backend_hybrid_refuses_vectorless_store(tmp_path, keyword_only):
    pytest.importorskip("memory_modules.memory")
    from bench.longmemeval_v2.adapter import PalinodeMemory

    mem = PalinodeMemory({"workspace_root": str(tmp_path / "ws")})  # hybrid=True default
    mem.insert(_trajectory(1))
    with pytest.raises(RuntimeError, match="0 vectors"):
        mem.save_memory(tmp_path / "memory_state")


def test_backend_load_allows_query_time_param_changes(tmp_path, keyword_only):
    pytest.importorskip("memory_modules.memory")
    from memory_modules.memory import load_memory

    from bench.longmemeval_v2.adapter import PalinodeMemory

    mem = PalinodeMemory({"hybrid": False, "workspace_root": str(tmp_path / "ws")})
    mem.insert(_trajectory(2))
    save_dir = tmp_path / "memory_state"
    mem.save_memory(save_dir)
    loaded = load_memory(save_dir, requested_config={"memory_type": "palinode",
                                                     "memory_params": {"hybrid": False, "top_k": 1, "fts_mode": "and"}})
    assert loaded.top_k == 1 and loaded.fts_mode == "and"
    assert loaded.store_dir == str(save_dir / "palinode_store")
    with pytest.raises(RuntimeError, match="slice_max_chars differs"):
        load_memory(save_dir, requested_config={"memory_type": "palinode",
                                                "memory_params": {"hybrid": False, "slice_max_chars": 5000}})

"""Row E — the production session-end + consolidation write path, end to end
against a real SQLite store under tmp_path — fake extractor, canned
consolidation ops, embedder disabled (keyword-only)."""
from __future__ import annotations

import os
import re

import pytest

from bench import harness
from bench.longmemeval import adapter, llm, pipeline, run


def _item(qid: str = "q1", qtype: str = "knowledge-update") -> dict:
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": "What is the name of the user's dog?",
        "answer": "Rex",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_session_ids": ["s-a", "s-b", "s-c"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/20 (Sat) 18:00", "2023/05/22 (Mon) 09:15"],
        "haystack_sessions": [
            [{"role": "user", "content": "I love hiking in the Alps."}, {"role": "assistant", "content": "Sounds great."}],
            [{"role": "user", "content": "My border collie is named Pip."}, {"role": "assistant", "content": "Lovely."}],
            [{"role": "user", "content": "We renamed the dog: Pip is now Rex."}, {"role": "assistant", "content": "Noted, Rex."}],
        ],
        "answer_session_ids": ["s-c"],
    }


_NOTES = {
    "s-a": ("The user talked about hiking in the Alps.", ["The user loves hiking in the Alps."], ["Prefers outdoor activities"]),
    "s-b": ("The user introduced their dog: the name of the user's dog is Pip.", ["The user's border collie is named Pip."], []),
    # Keyword-only retrieval ANDs every query token, so the evidence note has to
    # carry the question's words verbatim — the raw-transcript test does the same.
    "s-c": ("The user asked: what is the name of the user's dog now? They renamed it: the dog is now Rex.",
            ["The user's dog, formerly Pip, is now named Rex."], []),
}


def fake_extract(sid: str, ts: str, turns: list[dict]) -> pipeline.SessionEndPayload:
    summary, facts, prefs = _NOTES[sid]
    return pipeline.SessionEndPayload(session_id=sid, ts=ts, date=adapter._date_of(ts), summary=summary,
                                      facts=facts, preferences=prefs, prompt_tokens=100, completion_tokens=20)


@pytest.fixture(autouse=True)
def _preserve_ollama_client(monkeypatch):
    """Same reason as tests/test_bench_longmemeval.py: ``adapter.reset_backends``
    swaps the process-wide Ollama client; keep the original alive."""
    from palinode.core import ollama_client

    orig = ollama_client._singleton
    if orig is not None:
        monkeypatch.setattr(orig, "close", lambda: None)
    yield
    with ollama_client._singleton_lock:
        ollama_client._singleton = orig


@pytest.fixture
def keyword_only(monkeypatch):
    from palinode.indexer import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "_embeds_deferred", lambda client: True)
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    with harness.embedder_disabled():
        yield


def test_parse_extraction_json_fenced_and_prose():
    obj, ok = pipeline.parse_extraction('{"summary": "S", "facts": ["a", {"text": "b"}], "preferences": "p"}')
    assert ok and obj == {"summary": "S", "facts": ["a", "b"], "preferences": ["p"]}
    obj, ok = pipeline.parse_extraction('Here you go:\n```json\n{"summary": "S2", "facts": []}\n```')
    assert ok and obj["summary"] == "S2" and obj["facts"] == [] and obj["preferences"] == []
    obj, ok = pipeline.parse_extraction("The user likes tea.")
    assert not ok and obj == {"summary": "The user likes tea.", "facts": [], "preferences": []}


def test_extract_messages_versioned(monkeypatch):
    monkeypatch.delenv("LME_EXTRACT_PROMPT_VERSION", raising=False)
    msgs = pipeline.extract_messages("**User:** hi", "2023/05/20 (Sat) 02:21")
    assert "session-end" in msgs[0]["content"] and "Session date: 2023/05/20" in msgs[1]["content"]
    monkeypatch.setenv("LME_EXTRACT_PROMPT_VERSION", "v2")
    assert pipeline.extract_prompt_version() == "v2"
    v2 = pipeline.extract_messages("**User:** hi", "2023/05/20 (Sat) 02:21")[0]["content"]
    assert "EXHAUSTIVE ledger" in v2 and "record EVERY item" in v2
    monkeypatch.setenv("LME_EXTRACT_PROMPT_VERSION", "nope")
    with pytest.raises(SystemExit):
        pipeline.extract_prompt_version()


def test_extract_session_uses_chat(monkeypatch):
    seen = {}

    def fake_chat(ep, messages, **kw):
        seen["messages"] = messages
        return llm.Completion(text='{"summary": "S", "facts": ["f1"], "preferences": []}',
                              prompt_tokens=10, completion_tokens=5, latency_s=0.0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    ep = llm.Endpoint("http://x", "m")
    p = pipeline.extract_session(ep, "s-1", "2023/05/20 (Sat) 02:21", [{"role": "user", "content": "hello"}])
    assert p.date == "2023-05-20" and p.facts == ["f1"] and p.prompt_tokens == 10 and p.parse_ok
    assert "**User:** hello" in seen["messages"][1]["content"]


def test_blocked_choice_is_deterministic_and_becomes_a_refused_stub(monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"finish_reason":"content_filter: PROHIBITED_CONTENT","index":0}],"usage":{"prompt_tokens":5,"completion_tokens":0}}'

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout=None: Resp())
    ep = llm.Endpoint("http://x", "m")
    with pytest.raises(RuntimeError, match="blocked by m: finish_reason='content_filter: PROHIBITED_CONTENT'") as ei:
        llm.chat(ep, [{"role": "user", "content": "hi"}])
    assert run.is_deterministic_failure(ei.value)     # no backoff burned on a refusal
    p = pipeline.extract_session(ep, "s-x", "2023/05/20 (Sat) 02:21", [{"role": "user", "content": "hi"}])
    assert p.refused and not p.parse_ok and p.facts == [] and "refused" in p.summary
    captured["p"] = p


def test_answer_in_text_and_context_label():
    assert run.answer_in_text("Rex", "[1] (2023-05-22)\nThe dog is now named Rex.")
    assert run.answer_in_text("border-collie", "a Border Collie named Pip")
    assert not run.answer_in_text("Rex", "no dogs here")
    assert not run.answer_in_text("", "anything")
    assert adapter.context_label("/s/projects/session-end-2023-05-20-user-ab12.md") == "2023-05-20"
    assert adapter.context_label("/s/daily/2023-05-20.md") == "2023-05-20"
    assert adapter.context_label("/s/daily/2023-05-20-s-b.md") == "2023-05-20-s-b"
    assert adapter.context_label("/s/projects/user.md") == "user"


def test_session_end_pipeline_e0(tmp_path, keyword_only):
    store_dir = str(tmp_path / "store")
    seen: dict = {}

    def fake_answer(msgs):
        seen["prompt"] = msgs[-1]["content"]
        return llm.Completion(text="Rex", prompt_tokens=50, completion_tokens=1, latency_s=0.0)

    rows = run.run_items([_item()], store_dir=store_dir, top_k=10, threshold=0.4,
                         answer_fn=fake_answer, judge_fn=lambda p: "yes",
                         pipeline_name="session-end", extract_fn=fake_extract, extract_workers=1)
    r = rows[0]
    assert "error" not in r, r.get("error")
    assert r["pipeline"] == "session-end"
    assert r["extraction"] == {"calls": 3, "facts": 3, "preferences": 1, "parse_failures": 0, "refused": 0,
                               "deduplicated": 0,
                               "prompt_tokens": 300, "completion_tokens": 60,
                               "extract_wall_s": r["extraction"]["extract_wall_s"],
                               "write_wall_s": r["extraction"]["write_wall_s"]}
    assert r["ingest"]["chat_llm_calls"] == 0          # the Ollama counter; extraction is reported separately

    # The real session-end path: dated daily notes (two sessions share 2023-05-20),
    # each entry stamped with the session id; the indexed twin tagged project/user.
    daily = (tmp_path / "store" / "daily" / "2023-05-20.md").read_text()
    assert daily.count("## Session End — 2023-05-20T12:00:00Z") == 2
    assert "**Session ID:** s-b" in daily and "**Decisions:**\n- Prefers outdoor activities" in daily
    assert (tmp_path / "store" / "daily" / "2023-05-22.md").exists()
    assert not (tmp_path / "store" / "daily" / "2023-05-20-s-b.md").exists()   # no raw transcripts
    twins = sorted((tmp_path / "store" / "projects").glob("session-end-2023-05-2*-user-*.md"))
    assert len(twins) == 3 and "project/user" in twins[0].read_text()
    # Profile: dated fact bullets, tagged by the real fact-id bootstrap.
    profile = (tmp_path / "store" / "projects" / "user.md").read_text()
    assert "\n## [2023-05-20] session s-b\n- [2023-05-20] The user's border collie is named Pip. <!-- fact:user-" in profile
    assert re.search(r"^- \[2023-05-20\] The user's border collie is named Pip\. <!-- fact:user-[0-9a-f]{6} -->$", profile, re.M)
    assert re.search(r"^- \[2023-05-22\] .*Rex\. <!-- fact:user-[0-9a-f]{6} -->$", profile, re.M)
    assert not (tmp_path / "store" / "projects" / "user-status.md").exists()

    # Retrieval traces the evidence session through the entry's Session ID line,
    # drops the daily/indexed-twin duplicate, and the profile is a hit.
    assert "s-c" in r["retrieval"]["session_ids"] and r["retrieval"]["evidence_hit"] is True
    assert r["retrieval"]["answer_in_context"] is True
    assert r["retrieval"]["dup_hits"] >= 1
    assert "(2023-05-22)" in seen["prompt"]
    assert r["label"] is True and "consolidation_ops" not in r
    # Keyword-only search ANDs every token, so the profile is reached with its own words.
    prof = adapter.retrieve("border collie named Pip", top_k=10, threshold=0.4, hybrid=False)
    assert prof.profile_hit is True and "(user)" in adapter.format_context(prof.hits)
    # A profile section is traceable to a session. This profile is < 2000 chars so the
    # parser keeps it as one chunk and the trace yields its first section; a real
    # 300-bullet profile splits per ``##`` section and traces exactly.
    assert set(prof.session_ids) & {"s-a", "s-b", "s-c"}

    s = run.summarize(rows)
    assert s["extraction"]["calls"] == 3 and s["extraction"]["facts_per_question_mean"] == 3.0
    assert s["profile_hit_rate"] == 0.0 and s["answer_in_context"] == 1.0
    assert "consolidation_ops" not in s
    text = run.render(s, {"pipeline": "session-end"})
    assert "extraction: 3 calls over 1 questions" in text and "answer string in context" in text


def _canned_consolidation(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    """SUPERSEDE the Pip fact with the Rex fact; KEEP the rest — what a
    knowledge-update looks like to the executor."""
    assert "You are a memory compaction engine" in system_prompt
    assert "## RECENT_NOTES" in user_prompt and "Session End" in user_prompt
    facts = re.findall(r"^\[(\S+)\] (.*)$", user_prompt, re.M)
    ops = []
    for fid, text in facts:
        if "Pip" in text and "Rex" not in text:
            ops.append({"op": "SUPERSEDE", "id": fid, "new_text": "[2023-05-22] The user's border collie is named Rex (renamed from Pip).",
                        "reason": "renamed on 2023-05-22"})
        else:
            ops.append({"op": "KEEP", "id": fid})
    import json
    return json.dumps(ops), "canned"


def test_session_end_consolidate_pipeline_e1(tmp_path, keyword_only):
    store_dir = str(tmp_path / "store")
    seen: dict = {}

    def fake_answer(msgs):
        seen["prompt"] = msgs[-1]["content"]
        return llm.Completion(text="Rex", prompt_tokens=50, completion_tokens=1, latency_s=0.0)

    rows = run.run_items([_item()], store_dir=store_dir, top_k=10, threshold=0.4,
                         answer_fn=fake_answer, judge_fn=lambda p: "yes",
                         pipeline_name="session-end+consolidate", extract_fn=fake_extract,
                         consolidate_llm_fn=_canned_consolidation, extract_workers=1)
    r = rows[0]
    assert "error" not in r, r.get("error")
    c = r["consolidation"]
    assert c["status"] == "success" and c["projects_compacted"] == 1 and c["superseded"] == 1
    assert r["consolidation_ops"]["superseded"] == 1 and r["consolidation_ops"]["kept"] == 2
    assert r["consolidation_ops"]["unmatched"] == 0

    # SUPERSEDE landed in the profile: strikethrough + successor fact.
    profile = (tmp_path / "store" / "projects" / "user.md").read_text()
    assert "~~[2023-05-20] The user's border collie is named Pip.~~ [superseded" in profile
    assert re.search(r"named Rex \(renamed from Pip\)\. <!-- fact:supersedes-user-[0-9a-f]{6} -->", profile)
    assert (tmp_path / "store" / "projects" / "user-history.md").exists()

    # The runner archived the compacted daily notes; stale chunks were GC'd.
    assert c["notes_archived"] == 2 and not list((tmp_path / "store" / "daily").glob("*.md"))
    assert sorted(p.name for p in (tmp_path / "store" / "archive" / "2023").glob("*.md")) == ["2023-05-20.md", "2023-05-22.md"]
    assert c["gc_paths"] == 2
    from palinode.core import store as store_mod
    db = store_mod.get_db()
    try:
        paths = {row[0] for row in db.execute("SELECT DISTINCT file_path FROM chunks")}
    finally:
        db.close()
    assert not any("/daily/" in p for p in paths) and any("/archive/2023/" in p for p in paths)

    # Retrieval still traces the evidence session through the archived note, and
    # the consolidated profile is searchable with the successor fact's words.
    assert r["retrieval"]["evidence_hit"] is True
    prof = adapter.retrieve("renamed from Pip", top_k=10, threshold=0.4, hybrid=False)
    assert prof.profile_hit is True and "renamed from Pip" in adapter.format_context(prof.hits)

    s = run.summarize(rows)
    assert s["consolidation_ops"]["superseded"] == 1 and s["consolidation"]["notes_archived"] == 2
    text = run.render(s, {"pipeline": "session-end+consolidate"})
    assert "| superseded | 1 |" in text and "1/1 profiles compacted" in text


def test_allowed_ops_env_and_filter(tmp_path, keyword_only, monkeypatch):
    monkeypatch.setenv("LME_CONSOLIDATE_ALLOWED_OPS", "keep, update,MERGE,supersede")
    assert pipeline.allowed_ops_from_env() == ["KEEP", "UPDATE", "MERGE", "SUPERSEDE"]
    monkeypatch.setenv("LME_CONSOLIDATE_ALLOWED_OPS", "KEEP,DROP")
    with pytest.raises(SystemExit):
        pipeline.allowed_ops_from_env()
    monkeypatch.delenv("LME_CONSOLIDATE_ALLOWED_OPS")
    assert pipeline.allowed_ops_from_env() is None

    def archive_everything(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        import json
        ids = re.findall(r"^\[(\S+)\] ", user_prompt, re.M)
        return json.dumps([{"op": "ARCHIVE", "id": i, "rationale": "stale"} for i in ids]), "canned"

    from palinode.core.config import config
    before = list(config.consolidation.allowed_ops)
    rows = run.run_items([_item()], store_dir=str(tmp_path / "store"), top_k=10, threshold=0.4,
                         answer_fn=None, judge_fn=None, pipeline_name="session-end+consolidate",
                         extract_fn=fake_extract, consolidate_llm_fn=archive_everything, extract_workers=1,
                         consolidate_allowed_ops=["KEEP", "UPDATE", "MERGE", "SUPERSEDE"])
    r = rows[0]
    assert "error" not in r, r.get("error")
    assert r["consolidation"]["allowed_ops"] == ["KEEP", "UPDATE", "MERGE", "SUPERSEDE"]
    assert r["consolidation_ops"]["archived"] == 0            # every proposed ARCHIVE filtered out
    assert config.consolidation.allowed_ops == before          # restored after the pass
    profile = (tmp_path / "store" / "projects" / "user.md").read_text()
    assert "named Pip" in profile and "Rex" in profile         # nothing left the profile


def test_keep_raw_indexes_transcripts_too(tmp_path, keyword_only):
    store_dir = str(tmp_path / "store")
    rows = run.run_items([_item()], store_dir=store_dir, top_k=10, threshold=0.4, answer_fn=None, judge_fn=None,
                         pipeline_name="session-end", keep_raw=True, extract_fn=fake_extract, extract_workers=1)
    assert rows[0]["pipeline"] == "session-end+raw"
    assert (tmp_path / "store" / "daily" / "2023-05-20-s-b.md").exists()
    assert (tmp_path / "store" / "daily" / "2023-05-20.md").exists()


def test_pipeline_validation():
    with pytest.raises(ValueError):
        run.run_items([], store_dir="/nonexistent", top_k=1, threshold=0.4, answer_fn=None, judge_fn=None,
                      pipeline_name="session-end")
    with pytest.raises(ValueError):
        run.run_items([], store_dir="/nonexistent", top_k=1, threshold=0.4, answer_fn=None, judge_fn=None,
                      pipeline_name="session-end+consolidate", extract_fn=fake_extract)
    with pytest.raises(ValueError):
        run.run_items([], store_dir="/nonexistent", top_k=1, threshold=0.4, answer_fn=None, judge_fn=None,
                      pipeline_name="bogus")


def test_extract_all_parallel_keeps_chronological_order():
    item = _item()
    out = pipeline.extract_all(item, fake_extract, workers=3)
    assert [p.session_id for p in out] == ["s-a", "s-b", "s-c"]


def test_consolidation_llm_fn_counts_usage(monkeypatch):
    calls = {}

    def fake_chat(ep, messages, **kw):
        calls["max_tokens"] = kw["max_tokens"]
        return llm.Completion(text="[]", prompt_tokens=7, completion_tokens=3, latency_s=0.0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    usage: dict = {}
    fn = pipeline.consolidation_llm_fn(llm.Endpoint("http://x", "m"), usage=usage)
    assert fn("sys", "usr") == ("[]", "m")
    assert calls["max_tokens"] == 16000 and usage == {"calls": 1, "prompt_tokens": 7, "completion_tokens": 3}
    assert os.path.exists(os.path.join(pipeline._repo_prompts_dir(), "compaction.md"))

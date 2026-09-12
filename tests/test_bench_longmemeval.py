"""Integration test for the LongMemEval adapter — real SQLite store under tmp_path,
embedder disabled (keyword-only), fake answerer + judge callables."""
from __future__ import annotations

import json

import pytest

from bench import harness
from bench.longmemeval import adapter, data, judge, llm, run


def _item(qid: str = "q1", qtype: str = "single-session-user") -> dict:
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": "What breed is the user's dog?",
        "answer": "border collie",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_session_ids": ["s-a", "s-b", "s-c"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/20 (Sat) 18:00", "2023/05/22 (Mon) 09:15"],
        "haystack_sessions": [
            [{"role": "user", "content": "I love hiking in the Alps."}, {"role": "assistant", "content": "Sounds great."}],
            [{"role": "user", "content": "What breed is the user's dog? The user's dog is a border collie named Pip.", "has_answer": True},
             {"role": "assistant", "content": "Pip is a lovely name for a border collie."}],
            [{"role": "user", "content": "Recommend a pasta recipe."}, {"role": "assistant", "content": "Try cacio e pepe."}],
        ],
        "answer_session_ids": ["s-b"],
    }


def test_write_sessions_one_dated_file_per_session(tmp_path):
    where = adapter.write_sessions(str(tmp_path), _item())
    assert where == {"s-a": "daily/2023-05-20-s-a.md", "s-b": "daily/2023-05-20-s-b.md", "s-c": "daily/2023-05-22-s-c.md"}
    text = (tmp_path / "daily" / "2023-05-20-s-b.md").read_text()
    assert text.startswith("---\ndate: 2023-05-20\n")
    assert "session_id: s-b" in text and "## " not in text
    assert adapter.session_id_of("/x/daily/2023-05-20-s-b.md") == "s-b"
    assert adapter.session_rel_path("2023-01-01", "a/b c") == "daily/2023-01-01-a_b_c.md"


@pytest.fixture(autouse=True)
def _preserve_ollama_client(monkeypatch):
    """``adapter.reset_backends()`` closes and replaces the process-wide Ollama
    client. Other test modules patch methods on the instance they already hold
    (e.g. ``test_embedder_context_hardening``), so leaking a replacement makes
    their patches miss and their asserts fail order-dependently. Keep the
    original alive and put it back."""
    from palinode.core import ollama_client

    orig = ollama_client._singleton
    if orig is not None:
        monkeypatch.setattr(orig, "close", lambda: None)
    yield
    with ollama_client._singleton_lock:
        ollama_client._singleton = orig


@pytest.fixture
def keyword_only(monkeypatch):
    """A host with no embedder: the cold-embed gate defers every embed, so
    ``index_file`` writes FTS-only rows (the keyword-only install path) instead
    of aborting as it would on a transient outage."""
    from palinode.indexer import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "_embeds_deferred", lambda client: True)
    # point_config_at() writes PALINODE_ALLOW_FRESH_DB straight into os.environ;
    # registering it with monkeypatch first means teardown restores it, so the
    # flag can't leak into tests that assert the fresh-DB misconfig guard fires.
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    with harness.embedder_disabled():
        yield


def test_answer_prompt_versions(monkeypatch):
    monkeypatch.delenv("LME_PROMPT_VERSION", raising=False)
    assert adapter.prompt_version() == "v2"
    assert "Read all of them carefully" in adapter.answer_messages("q", "d", "c")[0]["content"]
    monkeypatch.setenv("LME_PROMPT_VERSION", "v1")
    assert "do not guess" in adapter.answer_messages("q", "d", "c")[0]["content"]
    monkeypatch.setenv("LME_PROMPT_VERSION", "v2")
    assert "Read all of them carefully" in adapter.answer_messages("q", "d", "c")[0]["content"]
    monkeypatch.setenv("LME_PROMPT_VERSION", "nope")
    with pytest.raises(SystemExit):
        adapter.prompt_version()


def test_keyword_query_strips_fts_hazards():
    assert adapter.keyword_query("What breed is the user's dog?") == "What breed is the user s dog"
    assert adapter.keyword_query("cost (USD): 5-10?") == "cost USD 5 10"


def test_end_to_end_keyword_only(tmp_path, keyword_only):
    store_dir = str(tmp_path / "store")
    seen: dict = {}

    def fake_answer(msgs):
        seen["prompt"] = msgs[-1]["content"]
        return llm.Completion(text="The user's dog is a border collie.", prompt_tokens=123, completion_tokens=9, latency_s=0.01)

    def fake_judge(prompt):
        seen.setdefault("judge_prompts", []).append(prompt)
        return "Yes"

    rows = run.run_items([_item(), _item("q2_abs")], store_dir=store_dir, top_k=5, threshold=0.4,
                         answer_fn=fake_answer, judge_fn=fake_judge)

    r = rows[0]
    assert r["retrieval"]["mode"] == "keyword"
    assert r["ingest"]["chat_llm_calls"] == 0
    assert "s-b" in r["retrieval"]["session_ids"]
    assert r["retrieval"]["evidence_hit"] is True
    assert "border collie" in seen["prompt"]
    assert "Today's date: 2023/06/01" in seen["prompt"]
    assert r["label"] is True
    assert seen["judge_prompts"][0].startswith("I will give you a question, a correct answer")
    assert seen["judge_prompts"][1].startswith("I will give you an unanswerable question")

    abs_row = rows[1]
    assert abs_row["abstention"] is True
    assert abs_row["retrieval"]["evidence_hit"] is False

    summary = run.summarize(rows)
    assert summary["accuracy"] == 1.0
    assert summary["accuracy_by_type"] == {"abstention": 1.0, "single-session-user": 1.0}
    assert summary["evidence_recall"] == 1.0
    assert summary["ingest_chat_llm_calls"] == 0
    assert "Accuracy: 1.000" in run.render(summary, {"dataset": "test"})


def test_endpoint_extra_json_merges_into_body(monkeypatch):
    monkeypatch.setenv("LME_JUDGE_MODEL", "m")
    monkeypatch.setenv("LME_JUDGE_BASE_URL", "http://x/v1/")
    monkeypatch.setenv("LME_JUDGE_EXTRA_JSON", '{"reasoning_effort": "none"}')
    monkeypatch.setenv("LME_JUDGE_TIMEOUT_S", "300")
    ep = llm.Endpoint.from_env("judge")
    assert ep.base_url == "http://x/v1" and dict(ep.extra) == {"reasoning_effort": "none"}
    assert ep.timeout_s == 300.0 and "timeout=300s" in ep.describe()
    assert "reasoning_effort" in ep.describe()
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"Yes"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    out = llm.chat(ep, [{"role": "user", "content": "q"}], max_tokens=10)
    assert out.text == "Yes" and captured["body"]["reasoning_effort"] == "none"
    assert captured["body"]["max_tokens"] == 10


def test_http_4xx_surfaces_body_and_does_not_retry(monkeypatch):
    import io
    from urllib.error import HTTPError

    ep = llm.Endpoint("http://x/v1", "m")
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(1)
        raise HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"context length"}'))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="context length"):
        llm.chat(ep, [{"role": "user", "content": "q"}])
    assert len(calls) == 1          # 4xx is deterministic: one attempt, no backoff


def test_http_429_backs_off_with_retry_after_then_succeeds(monkeypatch):
    import io
    from email.message import Message
    from urllib.error import HTTPError

    ep = llm.Endpoint("http://x/v1", "m")
    calls, slept = [], []
    monkeypatch.setattr(llm.time, "sleep", slept.append)

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"choices":[{"message":{"content":"Yes"}}],"usage":{}}'

    def fake_urlopen(req, timeout):
        calls.append(1)
        if len(calls) <= 2:
            hdrs = Message()
            hdrs["Retry-After"] = "30"
            raise HTTPError(req.full_url, 429, "Too Many Requests", hdrs, io.BytesIO(b'{"error":"rate"}'))
        return Resp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    out = llm.chat(ep, [{"role": "user", "content": "q"}], retries=0)   # 429s must not consume the transient budget
    assert out.text == "Yes" and len(calls) == 3
    assert slept == [30.0, 40.0]            # max(20, Retry-After=30), then 40

    calls.clear()
    slept.clear()

    def always_429(req, timeout):
        calls.append(1)
        raise HTTPError(req.full_url, 429, "Too Many Requests", Message(), io.BytesIO(b"{}"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", always_429)
    with pytest.raises(RuntimeError, match="429"):
        llm.chat(ep, [{"role": "user", "content": "q"}], retries=0)
    assert len(calls) == 4 and slept == [20.0, 40.0, 80.0]

    calls.clear()
    slept.clear()

    def no_credits(req, timeout):
        calls.append(1)
        raise HTTPError(req.full_url, 429, "Too Many Requests", Message(),
                        io.BytesIO(b'{"error":{"type":"insufficient_quota","code":"credit_balance_exhausted"}}'))

    monkeypatch.setattr(llm.urllib.request, "urlopen", no_credits)
    with pytest.raises(RuntimeError, match="OUT OF CREDITS"):
        llm.chat(ep, [{"role": "user", "content": "q"}], retries=0)
    assert len(calls) == 1 and slept == []   # empty account: fail fast, no backoff


def test_codex_backend_runs_cli_and_parses_tokens(monkeypatch):
    import subprocess

    seen = {}

    def fake_run(cmd, input, capture_output, text, timeout, cwd):
        seen.update(cmd=cmd, input=input, timeout=timeout)
        out_path = cmd[cmd.index("-o") + 1]
        open(out_path, "w").write("Border collie.\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="Border collie.\n", stderr="codex\nBorder collie.\ntokens used\n8,983\n")

    ep = llm.Endpoint("codex://local", "gpt-5.5", timeout_s=200)
    comp = llm.codex_exec(ep, [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}],
                          timeout=200, run=fake_run)
    assert comp.text == "Border collie." and comp.prompt_tokens == 8983 and comp.completion_tokens is None
    assert seen["cmd"][:4] == ["codex", "exec", "-m", "gpt-5.5"] and "--ephemeral" in seen["cmd"]
    assert seen["input"] == "S\n\nQ" and seen["timeout"] == 200
    monkeypatch.setenv("LME_ANSWER_BASE_URL", "codex://local")
    monkeypatch.setenv("LME_ANSWER_MODEL", "gpt-5.5")
    assert llm.Endpoint.from_env("answer").base_url == "codex://local"

    def failing_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")

    with pytest.raises(RuntimeError, match="not logged in"):
        llm.codex_exec(ep, [{"role": "user", "content": "Q"}], timeout=5, run=failing_run)


def test_rejudge_reports_agreement(tmp_path):
    from bench.longmemeval import rejudge

    rows = [
        {"question_id": "a", "question_type": "single-session-user", "abstention": False, "question": "q",
         "answer": "x", "hypothesis": "x", "label": True},
        {"question_id": "b", "question_type": "multi-session", "abstention": False, "question": "q",
         "answer": "y", "hypothesis": "z", "label": True},
        {"question_id": "c_abs", "question_type": "multi-session", "abstention": True, "question": "q",
         "answer": "n/a", "hypothesis": "unknown", "label": False},
    ]
    verdicts = iter(["Yes", "No", "Yes"])
    out = rejudge.rejudge(rows, lambda p: next(verdicts), progress_path=tmp_path / "rows.jsonl")
    assert [r["label"] for r in out] == [True, False, True]
    a = rejudge.agreement(out)
    assert a == {"n": 3, "agree": 1, "rate": 0.3333, "new_yes_orig_no": 1, "new_no_orig_yes": 1,
                 "original_accuracy": 0.6667}
    # resume: nothing re-judged, same rows back
    again = rejudge.rejudge(rows, lambda p: (_ for _ in ()).throw(AssertionError("should not call")),
                            progress_path=tmp_path / "rows.jsonl")
    assert [r["label"] for r in again] == [True, False, True]


def test_judge_prompts_match_upstream_dispatch():
    assert "Rubric:" in judge.anscheck_prompt("single-session-preference", "q", "a", "r")
    assert "off-by-one" in judge.anscheck_prompt("temporal-reasoning", "q", "a", "r")
    assert "updated answer" in judge.anscheck_prompt("knowledge-update", "q", "a", "r")
    assert "unanswerable" in judge.anscheck_prompt("multi-session", "q", "a", "r", abstention=True)
    with pytest.raises(NotImplementedError):
        judge.anscheck_prompt("bogus", "q", "a", "r")
    assert judge.label("Yes.") and not judge.label("No")


def test_progress_jsonl_and_resume(tmp_path, keyword_only):
    progress = tmp_path / "rows.jsonl"
    rows = run.run_items([_item("a")], store_dir=str(tmp_path / "s"), top_k=5, threshold=0.4,
                         answer_fn=None, judge_fn=None, progress_path=progress)
    assert len(rows) == 1 and progress.read_text().count("\n") == 1
    calls = []
    rows2 = run.run_items([_item("a"), _item("b")], store_dir=str(tmp_path / "s"), top_k=5, threshold=0.4,
                          answer_fn=None, judge_fn=None, progress_path=progress,
                          log=calls.append)
    assert [r["question_id"] for r in rows2] == ["a", "b"]           # "a" reloaded, "b" run
    assert progress.read_text().count("\n") == 2 and calls[0].startswith("resuming: 1")


def test_retrieve_falls_back_to_keyword_when_query_embed_fails(tmp_path, keyword_only):
    from palinode.core import embedder as embedder_mod

    adapter.fresh_store(str(tmp_path / "s"))
    adapter.write_sessions(str(tmp_path / "s"), _item())
    adapter.index_with_fallback(str(tmp_path / "s"))

    tried = []

    def nan_500(text):
        tried.append(text)
        raise RuntimeError("Server error '500' … unsupported value: NaN")

    # Scoped patch, undone *before* the keyword_only fixture unwinds. Using the
    # function-scoped `monkeypatch` here would capture the fixture's disabled
    # stub as the "original" and leave embed() permanently returning [] for
    # every later test module (broke test_embedder_context_hardening in CI).
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(embedder_mod, "embed", nan_500)
        ret = adapter.retrieve("What breed is the user's dog?", top_k=5, threshold=0.4, hybrid=True)
    assert ret.mode == "keyword-fallback" and "NaN" in (ret.embed_error or "")
    assert tried == ["What breed is the user's dog?", "breed user dog"]   # full, then content words
    assert "s-b" in ret.session_ids
    assert adapter.content_words("How much is the painting of a sunset worth in terms of the amount I paid for it?") \
        == "painting sunset worth terms amount paid"


def test_index_with_fallback_reindexes_aborted_files_fts_only(tmp_path, monkeypatch):
    """Warm embed path, embedder returning [] → reconcile aborts the file; the
    fallback must bring it back keyword-only rather than lose it."""
    from palinode.indexer import reconcile as reconcile_mod

    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")   # before fresh_store, so teardown restores it
    monkeypatch.setattr(reconcile_mod, "_embeds_deferred", lambda client: False)
    store_dir = str(tmp_path / "s")
    adapter.fresh_store(store_dir)
    adapter.write_sessions(store_dir, _item())
    with harness.embedder_disabled():
        ing = adapter.index_with_fallback(store_dir)
    assert ing.result.num_files == 3
    assert ing.fts_only_files == 3
    assert len(adapter._indexed_paths()) == 3
    ret = adapter.retrieve("What breed is the user's dog?", top_k=5, threshold=0.4, hybrid=False)
    assert "s-b" in ret.session_ids


def test_reset_backends_replaces_singleton():
    from palinode.core import ollama_client

    first = ollama_client.get_ollama_client()
    adapter.reset_backends()
    assert ollama_client._singleton is None
    assert ollama_client.get_ollama_client() is not first


def test_deterministic_failures_are_not_retried():
    slept, logs = [], []

    def nan():
        raise RuntimeError("Server error '500' … failed to encode response: json: unsupported value: NaN")

    with pytest.raises(RuntimeError):
        run._with_backoff(nan, log=logs.append, what="q", delays=(1, 2), sleep=slept.append)
    assert slept == [] and logs and logs[0].startswith("  no retry")
    assert run.is_deterministic_failure(RuntimeError("HTTP Error 422"))
    assert not run.is_deterministic_failure(RuntimeError("[Errno 54] Connection reset by peer"))
    assert not run.is_deterministic_failure(RuntimeError("Server error '500 Internal Server Error'"))


def test_heartbeat_written_per_question(tmp_path, keyword_only):
    progress = tmp_path / "rows.jsonl"
    run.run_items([_item("a")], store_dir=str(tmp_path / "s"), top_k=5, threshold=0.4,
                  answer_fn=None, judge_fn=None, progress_path=progress)
    hb = json.loads((tmp_path / "status.json").read_text())
    assert hb["phase"] == "done" and hb["done"] == 1 and hb["total"] == 1 and hb["updated_at"] > 0


def test_with_backoff_retries_then_raises():
    slept, logs, calls = [], [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("500")
        return "ok"

    assert run._with_backoff(flaky, log=logs.append, what="x", delays=(1, 2, 3), sleep=slept.append) == "ok"
    assert slept == [1, 2] and len(logs) == 2

    def dead():
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        run._with_backoff(dead, log=logs.append, what="y", delays=(1,), sleep=slept.append)


def test_prepare_outage_is_recorded_not_fatal(tmp_path, keyword_only, monkeypatch):
    monkeypatch.setattr(run, "BACKOFF_S", (0.0,))

    def boom(*a, **k):
        raise RuntimeError("Ollama embed failed 500")

    monkeypatch.setattr(run.adapter, "retrieve", boom)
    progress = tmp_path / "rows.jsonl"
    rows = run.run_items([_item("a")], store_dir=str(tmp_path / "s"), top_k=5, threshold=0.4,
                         answer_fn=None, judge_fn=None, progress_path=progress)
    assert rows[0]["error"].startswith("prepare:") and "retrieval" not in rows[0]
    assert progress.read_text().count("\n") == 1
    s = run.summarize(rows)
    assert s["errors"] == 1 and s["errors_prepare"] == 1 and s["evidence_recall"] is None


def test_retry_errors_drops_error_rows_and_rewrites(tmp_path):
    p = tmp_path / "rows.jsonl"
    p.write_text(json.dumps({"question_id": "a", "label": True}) + "\n"
                 + json.dumps({"question_id": "b", "error": "prepare: 500"}) + "\n")
    assert [r["question_id"] for r in run.load_progress(p)] == ["a", "b"]
    assert [r["question_id"] for r in run.load_progress(p, retry_errors=True)] == ["a"]
    assert p.read_text().count("\n") == 1


def test_hypotheses_format_is_upstream_compatible(tmp_path, keyword_only):
    ds = tmp_path / "lme.json"
    ds.write_text(json.dumps([_item()]))
    out = tmp_path / "out"
    rc = run.main(["--data", str(ds), "--no-answer", "--store-dir", str(tmp_path / "store"), "--out", str(out)])
    assert rc == 0
    res = json.loads((out / "results.json").read_text())
    assert res["summary"]["n"] == 1 and res["summary"]["accuracy"] is None
    assert (out / "hypotheses.jsonl").read_text() == ""
    assert data.is_abstention({"question_id": "x_abs"})

"""LongMemEval × Palinode — end-to-end runner.

    python -m bench.longmemeval.run --variant s --limit 20 --out results/lme-smoke
    python -m bench.longmemeval.run --variant s --pipeline session-end+consolidate --out results/lme-rowE1

Answerer / judge endpoints come from ``LME_ANSWER_*`` / ``LME_JUDGE_*`` env
(see ``llm.py``). ``--no-judge`` writes hypotheses only, in the upstream JSONL
format, so ``evaluate_qa.py`` from the LongMemEval repo can judge them instead.

``--pipeline`` selects the write path: ``raw`` indexes transcripts
(rows A–D); ``session-end`` extracts a session-end note per session with the
``LME_EXTRACT_*`` model and writes it through Palinode's real session-end path
(E0); ``session-end+consolidate`` then runs the real consolidation pass with
the ``LME_CONSOLIDATE_*`` model (E1). ``--keep-raw`` indexes the transcripts as
well (E1+raw).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from bench import harness
from bench.longmemeval import adapter, data, judge, llm, pipeline

AnswerFn = Callable[[list[dict[str, str]]], llm.Completion]
JudgeFn = Callable[[str], str]


BACKOFF_S = (15.0, 45.0, 90.0)   # ~2.5 min worst case per question, not 7.5

# Failures that are a property of the *input*, not the backend: retrying them
# only burns the backoff budget. Everything else is presumed transient.
_DETERMINISTIC = re.compile(
    r"unsupported value: NaN|context length|exceeds the maximum|invalid_request|blocked by |content_filter|"
    r"HTTP Error 4\d\d|Server error '4\d\d'|status 4\d\d", re.IGNORECASE)


def is_deterministic_failure(exc: BaseException) -> bool:
    return bool(_DETERMINISTIC.search(str(exc)))


def _with_backoff(fn: Callable[[], Any], *, log: Callable[[str], None], what: str,
                  delays: tuple[float, ...] | None = None, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Retry *fn* across a transient backend outage (Ollama 500, LM Studio restart).

    A multi-hour run must not die because the embedder hiccuped once at
    question 31; after the last delay the exception propagates to the caller.
    Deterministic input failures propagate immediately — no backoff.
    ``delays`` defaults to the module-level ``BACKOFF_S`` resolved at call time.
    """
    if delays is None:
        delays = BACKOFF_S
    for i, delay in enumerate(delays):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - deliberately broad: any backend failure
            if is_deterministic_failure(e):
                log(f"  no retry for {what}: deterministic failure: {str(e)[:100]}")
                raise
            log(f"  retry {i + 1}/{len(delays)} for {what} in {delay:.0f}s: {str(e)[:100]}")
            sleep(delay)
            adapter.reset_backends()   # fresh sockets: a poisoned pool never recovers on its own
    return fn()


def write_heartbeat(progress_path: Path | None, **fields: Any) -> None:
    """``status.json`` next to ``rows.jsonl`` — the supervisor's liveness signal."""
    if progress_path is None:
        return
    payload = {"updated_at": time.time(), **fields}
    tmp = progress_path.with_name("status.json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, progress_path.with_name("status.json"))


def load_progress(path: Path | None, *, retry_errors: bool = False) -> list[dict[str, Any]]:
    """Rows already written to a ``rows.jsonl`` from an earlier (interrupted) run.

    With *retry_errors*, rows that ended in an error are dropped (and the file
    rewritten without them) so the run re-attempts those questions.
    """
    if path is None or not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    if retry_errors:
        keep = [r for r in rows if "error" not in r]
        if len(keep) != len(rows):
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in keep)
        rows = keep
    return rows


def run_items(items: list[dict[str, Any]], *, store_dir: str, top_k: int, threshold: float,
              answer_fn: AnswerFn | None, judge_fn: JudgeFn | None,
              pipeline_name: str = "raw", keep_raw: bool = False,
              extract_fn: pipeline.ExtractFn | None = None, consolidate_llm_fn: Any = None,
              extract_workers: int = 8, consolidate_allowed_ops: list[str] | None = None,
              log: Callable[[str], None] = lambda s: None,
              progress_path: Path | None = None, retry_errors: bool = False) -> list[dict[str, Any]]:
    """Run every item; append each finished row to *progress_path* (JSONL) as it
    completes so a multi-hour run survives a crash, and skip items already there."""
    if pipeline_name not in pipeline.PIPELINES:
        raise ValueError(f"pipeline {pipeline_name!r}; known: {pipeline.PIPELINES}")
    session_end = pipeline_name.startswith("session-end")
    consolidate = pipeline_name.endswith("+consolidate")
    if session_end and extract_fn is None:
        raise ValueError("session-end pipeline needs extract_fn")
    if consolidate and consolidate_llm_fn is None:
        raise ValueError("consolidate pipeline needs consolidate_llm_fn")
    rows: list[dict[str, Any]] = load_progress(progress_path, retry_errors=retry_errors)
    done = {r["question_id"] for r in rows}
    if done:
        log(f"resuming: {len(done)} rows already in {progress_path}")
    write_heartbeat(progress_path, phase="start", done=len(rows), total=len(items))
    for n, item in enumerate(items, 1):
        if item["question_id"] in done:
            continue
        qid, qtype = item["question_id"], item["question_type"]
        abst = data.is_abstention(item)
        write_heartbeat(progress_path, phase="question", qid=qid, n=n, done=len(rows), total=len(items))
        def _prepare(it: dict[str, Any]) -> tuple[Any, dict[str, Any] | None, float, adapter.Retrieval, float, pipeline.WriteStats | None]:
            t0 = time.perf_counter()
            adapter.fresh_store(store_dir)
            wstats: pipeline.WriteStats | None = None
            if session_end:
                wstats = pipeline.ingest_session_end(store_dir, it, extract_fn, workers=extract_workers)
            if not session_end or keep_raw:
                adapter.write_sessions(store_dir, it)
            ing = adapter.index_with_fallback(store_dir)
            consolidation: dict[str, Any] | None = None
            if consolidate:
                consolidation = pipeline.consolidate(store_dir, it, consolidate_llm_fn, allowed_ops=consolidate_allowed_ops)
                adapter.index_with_fallback(store_dir)
            t_ingest = time.perf_counter() - t0
            t1 = time.perf_counter()
            ret = adapter.retrieve(it["question"], top_k=top_k, threshold=threshold, hybrid=ing.result.embedded)
            return ing, consolidation, t_ingest, ret, time.perf_counter() - t1, wstats

        try:
            ing, consolidation, t_ingest, ret, t_ret, wstats = _with_backoff(
                functools.partial(_prepare, item), log=log, what=f"{qid} ingest/retrieve")
            ingest = ing.result
        except Exception as e:  # noqa: BLE001 - backend outage after retries: record, keep going
            row = {"question_id": qid, "question_type": qtype, "abstention": abst,
                   "question": item["question"], "answer": item["answer"],
                   "num_sessions": len(item["haystack_sessions"]), "error": f"prepare: {e}"}
            rows.append(row)
            if progress_path is not None:
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            log(f"[{n}/{len(items)}] {qid} {qtype} ERROR prepare: {str(e)[:80]}")
            continue
        answer_sids = [str(s) for s in item.get("answer_session_ids", [])]
        evidence_hit = (not abst) and any(s in ret.session_ids for s in answer_sids)
        context = adapter.format_context(ret.hits)
        answer_in_context = (not abst) and answer_in_text(item["answer"], context)

        row: dict[str, Any] = {
            "question_id": qid, "question_type": qtype, "abstention": abst,
            "question": item["question"], "answer": item["answer"],
            "num_sessions": len(item["haystack_sessions"]),
            "ingest": {"files": ingest.num_files, "chunks": ingest.num_facts, "embedded": ingest.embedded,
                       "embed_calls": ingest.embed_calls, "chat_llm_calls": ingest.chat_llm_calls,
                       "fts_only_files": ing.fts_only_files, "wall_s": round(t_ingest, 3)},
            "consolidation": consolidation,
            "retrieval": {"mode": ret.mode, "top_k": top_k, "hits": len(ret.hits),
                          "session_ids": ret.session_ids, "evidence_hit": evidence_hit,
                          "answer_in_context": answer_in_context,
                          "context_chars": ret.context_chars, "wall_s": round(t_ret, 3),
                          **({"embed_error": ret.embed_error} if ret.embed_error else {})},
        }
        if session_end:
            row["pipeline"] = pipeline_name + ("+raw" if keep_raw else "")
            row["retrieval"]["dup_hits"] = ret.dup_hits
            row["retrieval"]["profile_hit"] = ret.profile_hit
            assert wstats is not None
            row["extraction"] = {"calls": wstats.sessions, "facts": wstats.facts, "preferences": wstats.preferences,
                                 "parse_failures": wstats.parse_failures, "refused": wstats.refused,
                                 "deduplicated": wstats.deduplicated,
                                 "prompt_tokens": wstats.prompt_tokens, "completion_tokens": wstats.completion_tokens,
                                 "extract_wall_s": round(wstats.extract_wall_s, 3), "write_wall_s": round(wstats.write_wall_s, 3)}
            if consolidation is not None:
                row["consolidation_ops"] = pipeline.ops_histogram(consolidation)
        # A single model failure must not discard the run: record it on the
        # row (excluded from accuracy, counted in summary["errors"]) and go on.
        if answer_fn is not None:
            msgs = adapter.answer_messages(item["question"], item["question_date"], context)
            try:
                comp = answer_fn(msgs)
            except Exception as e:  # noqa: BLE001 - benchmark must survive one bad call
                row["error"] = f"answer: {e}"
            else:
                row["hypothesis"] = comp.text
                row["answer_usage"] = {"prompt_tokens": comp.prompt_tokens, "completion_tokens": comp.completion_tokens,
                                       "prompt_chars": sum(len(m["content"]) for m in msgs), "latency_s": round(comp.latency_s, 3)}
                if judge_fn is not None:
                    prompt = judge.anscheck_prompt(qtype, item["question"], item["answer"], comp.text, abstention=abst)
                    try:
                        verdict = judge_fn(prompt)
                    except Exception as e:  # noqa: BLE001
                        row["error"] = f"judge: {e}"
                    else:
                        row["judge_raw"] = verdict
                        row["label"] = judge.label(verdict)
        rows.append(row)
        if progress_path is not None:
            with open(progress_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        log(f"[{n}/{len(items)}] {qid} {qtype}{' (abs)' if abst else ''} "
            f"ret={ret.mode} evidence={'Y' if evidence_hit else '-'} ans_in_ctx={'Y' if answer_in_context else '-'} "
            f"{'ops=' + json.dumps(row['consolidation_ops'], separators=(',', ':')) + ' ' if 'consolidation_ops' in row else ''}"
            f"{'label=' + str(row.get('label')) if 'label' in row else ''}"
            f"{' ERROR ' + row['error'][:80] if 'error' in row else ''}")
    write_heartbeat(progress_path, phase="done", done=len(rows), total=len(items))
    return rows


_NONWORD = re.compile(r"[^\w\s]+")


def answer_in_text(answer: Any, text: str) -> bool:
    """Answer-string containment: the summary-era evidence signal.

    Session ids survive session-end (see ``adapter.hit_session_id``) but not the
    profile, whose facts are rewritten by consolidation — so row E also reports
    whether the gold answer string appears verbatim (case/punctuation-folded)
    in the reader's context. Lenient for short numeric/name answers, strict for
    long free-text ones; comparable across rows since A–D get it too.
    """
    a = " ".join(_NONWORD.sub(" ", str(answer)).lower().split())
    if not a:
        return False
    t = " ".join(_NONWORD.sub(" ", text).lower().split())
    return a in t


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[bool]] = defaultdict(list)
    ev: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        key = "abstention" if r["abstention"] else r["question_type"]
        if "label" in r:
            by_type[key].append(bool(r["label"]))
        if not r["abstention"] and "retrieval" in r:
            ev[r["question_type"]].append(bool(r["retrieval"]["evidence_hit"]))
    ok = [r for r in rows if "retrieval" in r]  # rows that got past ingest/retrieve
    labelled = [r for r in rows if "label" in r]
    ptoks = [r["answer_usage"]["prompt_tokens"] for r in rows if r.get("answer_usage", {}).get("prompt_tokens")]
    aic = [bool(r["retrieval"].get("answer_in_context")) for r in ok if not r["abstention"]]
    ext = [r["extraction"] for r in ok if "extraction" in r]
    ops = [r["consolidation_ops"] for r in ok if "consolidation_ops" in r]
    extra: dict[str, Any] = {}
    if ext:
        extra["extraction"] = {
            "questions": len(ext),
            "calls": sum(e["calls"] for e in ext),
            "calls_per_question_mean": round(statistics.mean(e["calls"] for e in ext), 1),
            "prompt_tokens": sum(e["prompt_tokens"] for e in ext),
            "completion_tokens": sum(e["completion_tokens"] for e in ext),
            "prompt_tokens_per_question_mean": round(statistics.mean(e["prompt_tokens"] for e in ext), 1),
            "facts_per_question_mean": round(statistics.mean(e["facts"] for e in ext), 1),
            "parse_failures": sum(e["parse_failures"] for e in ext),
            "refused": sum(e.get("refused", 0) for e in ext),
            "deduplicated": sum(e["deduplicated"] for e in ext),
            "extract_wall_s_mean": round(statistics.mean(e["extract_wall_s"] for e in ext), 2),
        }
        extra["profile_hit_rate"] = round(sum(bool(r["retrieval"].get("profile_hit")) for r in ok) / len(ok), 4)
        extra["dup_hits_mean"] = round(statistics.mean(r["retrieval"].get("dup_hits", 0) for r in ok), 2)
    if ops:
        extra["consolidation_ops"] = {k: sum(o.get(k, 0) for o in ops) for k in pipeline.OPS_KEYS}
        cons = [r["consolidation"] for r in ok if r.get("consolidation")]
        extra["consolidation"] = {
            "questions": len(cons),
            "status": sorted({str(c.get("status")) for c in cons}),
            "projects_compacted": sum(int(c.get("projects_compacted", 0)) for c in cons),
            "notes_archived": sum(int(c.get("notes_archived", 0)) for c in cons),
            "wall_s_mean": round(statistics.mean(float(c.get("wall_s", 0)) for c in cons), 2),
        }
    return {
        "n": len(rows),
        "errors": sum(1 for r in rows if "error" in r),
        "errors_prepare": sum(1 for r in rows if str(r.get("error", "")).startswith("prepare:")),
        "accuracy": (sum(r["label"] for r in labelled) / len(labelled)) if labelled else None,
        "accuracy_by_type": {k: round(sum(v) / len(v), 4) for k, v in sorted(by_type.items())},
        "evidence_recall_by_type": {k: round(sum(v) / len(v), 4) for k, v in sorted(ev.items())},
        "evidence_recall": (sum(sum(v) for v in ev.values()) / sum(len(v) for v in ev.values())) if ev else None,
        "answer_in_context": (sum(aic) / len(aic)) if aic else None,
        **extra,
        "retrieval_mode": sorted({r["retrieval"]["mode"] for r in ok}),
        "ingest_chat_llm_calls": sum(r["ingest"]["chat_llm_calls"] for r in ok),
        "prompt_tokens_mean": round(statistics.mean(ptoks), 1) if ptoks else None,
        "context_chars_mean": round(statistics.mean(r["retrieval"]["context_chars"] for r in ok), 1) if ok else None,
        "ingest_wall_s_mean": round(statistics.mean(r["ingest"]["wall_s"] for r in ok), 2) if ok else None,
        "retrieval_wall_s_mean": round(statistics.mean(r["retrieval"]["wall_s"] for r in ok), 3) if ok else None,
    }


def render(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = ["# LongMemEval × Palinode", "", "| setting | value |", "|---|---|"]
    lines += [f"| {k} | `{v}` |" for k, v in meta.items()]
    lines += ["", f"**n = {summary['n']}**, retrieval: {', '.join(summary['retrieval_mode'])}, "
              f"ingest chat-LLM calls: {summary['ingest_chat_llm_calls']}", ""]
    if summary["accuracy"] is not None:
        lines += [f"**Accuracy: {summary['accuracy']:.3f}**", "", "| type | accuracy | evidence recall@k |", "|---|---|---|"]
        for t in sorted(set(summary["accuracy_by_type"]) | set(summary["evidence_recall_by_type"])):
            a = summary["accuracy_by_type"].get(t)
            e = summary["evidence_recall_by_type"].get(t)
            lines.append(f"| {t} | {'' if a is None else f'{a:.3f}'} | {'' if e is None else f'{e:.3f}'} |")
    else:
        lines += ["(no judge run)", "", "| type | evidence recall@k |", "|---|---|"]
        lines += [f"| {t} | {e:.3f} |" for t, e in summary["evidence_recall_by_type"].items()]
    lines += ["", f"mean prompt tokens: {summary['prompt_tokens_mean']} · mean context chars: {summary['context_chars_mean']} · "
              f"ingest {summary['ingest_wall_s_mean']}s · retrieval {summary['retrieval_wall_s_mean']}s (per question)", ""]
    if summary.get("answer_in_context") is not None:
        lines += [f"answer string in context (non-abstention): {summary['answer_in_context']:.3f}", ""]
    if "extraction" in summary:
        e = summary["extraction"]
        lines += [f"extraction: {e['calls']} calls over {e['questions']} questions "
                  f"({e['calls_per_question_mean']}/question), {e['prompt_tokens']} prompt + {e['completion_tokens']} "
                  f"completion tokens ({e['prompt_tokens_per_question_mean']} prompt/question), "
                  f"{e['facts_per_question_mean']} facts/question, {e['parse_failures']} parse failures, "
                  f"{e['refused']} refused by the model · "
                  f"profile in top-k: {summary['profile_hit_rate']:.3f} · duplicate hits dropped/question: {summary['dup_hits_mean']}", ""]
    if "consolidation_ops" in summary:
        o = summary["consolidation_ops"]
        c = summary["consolidation"]
        lines += ["| op | count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in o.items()]
        lines += ["", f"consolidation: {c['projects_compacted']}/{c['questions']} profiles compacted, "
                  f"{c['notes_archived']} notes archived, status {c['status']}, {c['wall_s_mean']}s/question", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", choices=sorted(data.VARIANTS), default="s")
    ap.add_argument("--data", type=Path, help="explicit dataset JSON (overrides --variant fetch)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--ids", help="comma-separated question_ids to run")
    ap.add_argument("--types", help="comma-separated question_types to keep")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--threshold", type=float, default=0.4)
    ap.add_argument("--pipeline", choices=pipeline.PIPELINES, default="raw",
                    help="write path: raw transcripts (A–D) | session-end extraction (E0) | + consolidation (E1)")
    ap.add_argument("--keep-raw", action="store_true", help="with a session-end pipeline: index the raw transcripts too (E1+raw)")
    ap.add_argument("--extract-workers", type=int, default=int(os.environ.get("LME_EXTRACT_WORKERS", "8")))
    ap.add_argument("--no-answer", action="store_true", help="retrieval only (evidence recall), no LLM calls")
    ap.add_argument("--no-judge", action="store_true", help="write hypotheses for upstream evaluate_qa.py")
    ap.add_argument("--store-dir", default=os.environ.get("LME_STORE_DIR", "/tmp/lme-palinode-store"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--resume", action="store_true", help="continue an interrupted run from <out>/rows.jsonl")
    ap.add_argument("--retry-errors", action="store_true", help="with --resume: re-run questions that ended in an error")
    args = ap.parse_args(argv)

    path = args.data or data.fetch(args.variant)
    items = data.load(path)
    if args.ids:
        keep = set(args.ids.split(","))
        items = [i for i in items if i["question_id"] in keep]
    if args.types:
        keep_t = set(args.types.split(","))
        items = [i for i in items if i["question_type"] in keep_t]
    items = items[args.offset:]
    if args.limit:
        items = items[: args.limit]

    answer_fn: AnswerFn | None = None
    judge_fn: JudgeFn | None = None
    meta: dict[str, Any] = {"dataset": path.name, "top_k": args.top_k, "threshold": args.threshold,
                            "pipeline": args.pipeline + ("+raw" if args.keep_raw else ""),
                            "answer_prompt": adapter.prompt_version(),
                            "embedder": "reachable" if harness.embedder_available() else "unreachable (keyword-only)"}
    extract_fn: pipeline.ExtractFn | None = None
    consolidate_llm_fn = None
    consolidate_allowed_ops: list[str] | None = None
    if args.pipeline.startswith("session-end"):
        x_ep = llm.Endpoint.from_env("extract")
        meta["extract_model"] = x_ep.describe()
        meta["extract_prompt"] = pipeline.extract_prompt_version()
        meta["extract_workers"] = args.extract_workers
        extract_fn = lambda sid, ts, turns: pipeline.extract_session(x_ep, sid, ts, turns)  # noqa: E731
    if args.pipeline.endswith("+consolidate"):
        c_ep = llm.Endpoint.from_env("consolidate")
        meta["consolidate_model"] = c_ep.describe()
        consolidate_llm_fn = pipeline.consolidation_llm_fn(c_ep)
        consolidate_allowed_ops = pipeline.allowed_ops_from_env()
        meta["consolidate_allowed_ops"] = ",".join(consolidate_allowed_ops) if consolidate_allowed_ops else "config default (all)"
    if not args.no_answer:
        a_ep = llm.Endpoint.from_env("answer")
        meta["answer_model"] = a_ep.describe()
        answer_fn = lambda msgs: llm.chat(a_ep, msgs)  # noqa: E731
        if not args.no_judge:
            j_ep = llm.Endpoint.from_env("judge")
            meta["judge_model"] = j_ep.describe()
            if j_ep.model == a_ep.model:
                print("WARNING: judge model == answer model — a publishable run requires different vendors", file=sys.stderr)
            judge_fn = lambda p: llm.chat(j_ep, [{"role": "user", "content": p}], max_tokens=10).text  # noqa: E731

    args.out.mkdir(parents=True, exist_ok=True)
    progress = args.out / "rows.jsonl"
    if progress.exists() and not args.resume:
        raise SystemExit(f"{progress} exists — pass --resume to continue it, or choose a new --out")
    rows = run_items(items, store_dir=args.store_dir, top_k=args.top_k, threshold=args.threshold,
                     answer_fn=answer_fn, judge_fn=judge_fn,
                     pipeline_name=args.pipeline, keep_raw=args.keep_raw,
                     extract_fn=extract_fn, consolidate_llm_fn=consolidate_llm_fn,
                     extract_workers=args.extract_workers, consolidate_allowed_ops=consolidate_allowed_ops,
                     log=lambda s: print(s, file=sys.stderr, flush=True), progress_path=progress,
                     retry_errors=args.retry_errors)
    summary = summarize(rows)
    (args.out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary, "rows": rows}, indent=2))
    with open(args.out / "hypotheses.jsonl", "w") as f:
        for r in rows:
            if "hypothesis" in r:
                f.write(json.dumps({"question_id": r["question_id"], "hypothesis": r["hypothesis"]}) + "\n")
    (args.out / "report.md").write_text(render(summary, meta))
    print(render(summary, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Re-judge an existing run's hypotheses with a (different) judge.

    python -m bench.longmemeval.rejudge <run-dir> --out <run-dir>/rejudge-gpt4o

Reads ``<run-dir>/results.json`` (falls back to ``hypotheses.jsonl`` +
``--data``), judges every hypothesis with ``LME_JUDGE_*`` using the upstream
prompts, and writes ``results.json`` + ``report.md`` in the same shape as
``run.py`` — plus ``agreement`` against the original labels when present.

Two uses: the upstream-comparable ``gpt-4o-2024-08-06`` verdict on a
row that was judged with something else, and judge-agreement as a number.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from bench.longmemeval import data, judge, llm, run

JudgeFn = Callable[[str], str]


def load_rows(run_dir: Path, dataset: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    res = run_dir / "results.json"
    if res.exists():
        d = json.loads(res.read_text())
        return d.get("meta", {}), d["rows"]
    if dataset is None:
        raise SystemExit(f"{res} missing and no --data given")
    hyps = {}
    for line in (run_dir / "hypotheses.jsonl").read_text().splitlines():
        if line.strip():
            h = json.loads(line)
            hyps[h["question_id"]] = h["hypothesis"]
    items = {i["question_id"]: i for i in data.load(dataset)}
    rows = []
    for qid, hyp in hyps.items():
        it = items[qid]
        rows.append({"question_id": qid, "question_type": it["question_type"], "abstention": data.is_abstention(it),
                     "question": it["question"], "answer": it["answer"], "hypothesis": hyp})
    return {}, rows


def rejudge(rows: list[dict[str, Any]], judge_fn: JudgeFn, *, log: Callable[[str], None] = lambda s: None,
            progress_path: Path | None = None) -> list[dict[str, Any]]:
    done = {r["question_id"]: r for r in run.load_progress(progress_path)}
    out: list[dict[str, Any]] = []
    for n, r in enumerate(rows, 1):
        if "hypothesis" not in r:
            continue
        if r["question_id"] in done:
            out.append(done[r["question_id"]])
            continue
        row = {k: r[k] for k in ("question_id", "question_type", "abstention", "question", "answer", "hypothesis")}
        for k in ("retrieval", "ingest", "answer_usage"):
            if k in r:
                row[k] = r[k]
        if "label" in r:
            row["original_label"] = r["label"]
        prompt = judge.anscheck_prompt(row["question_type"], row["question"], row["answer"], row["hypothesis"],
                                       abstention=row["abstention"])
        try:
            verdict = judge_fn(prompt)
        except Exception as e:  # noqa: BLE001 - record, keep going
            row["error"] = f"judge: {e}"
        else:
            row["judge_raw"] = verdict
            row["label"] = judge.label(verdict)
        out.append(row)
        if progress_path is not None:
            with open(progress_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        log(f"[{n}/{len(rows)}] {row['question_id']} label={row.get('label')} orig={row.get('original_label')}")
    return out


def agreement(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    both = [r for r in rows if "label" in r and "original_label" in r]
    if not both:
        return None
    agree = sum(r["label"] == r["original_label"] for r in both)
    return {"n": len(both), "agree": agree, "rate": round(agree / len(both), 4),
            "new_yes_orig_no": sum(r["label"] and not r["original_label"] for r in both),
            "new_no_orig_yes": sum((not r["label"]) and r["original_label"] for r in both),
            "original_accuracy": round(sum(r["original_label"] for r in both) / len(both), 4)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--data", type=Path, help="dataset JSON, only needed when run_dir has no results.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    meta, rows = load_rows(args.run_dir, args.data)
    if args.limit:
        rows = rows[: args.limit]
    j_ep = llm.Endpoint.from_env("judge")
    judge_fn = lambda p: llm.chat(j_ep, [{"role": "user", "content": p}], max_tokens=10).text  # noqa: E731
    args.out.mkdir(parents=True, exist_ok=True)
    out = rejudge(rows, judge_fn, log=lambda s: print(s, file=sys.stderr, flush=True),
                  progress_path=args.out / "rows.jsonl")
    summary = run.summarize(out)
    summary["agreement"] = agreement(out)
    new_meta = {**meta, "rejudged_from": str(args.run_dir), "judge_model": j_ep.describe()}
    (args.out / "results.json").write_text(json.dumps({"meta": new_meta, "summary": summary, "rows": out}, indent=2))
    report = run.render(summary, new_meta)
    if summary["agreement"]:
        a = summary["agreement"]
        report += (f"\n**Judge agreement with original labels:** {a['rate']:.3f} ({a['agree']}/{a['n']}); "
                   f"new-yes/orig-no {a['new_yes_orig_no']}, new-no/orig-yes {a['new_no_orig_yes']}; "
                   f"original accuracy {a['original_accuracy']:.3f}\n")
    (args.out / "report.md").write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

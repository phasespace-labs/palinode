"""Export a harness run into ``bench/results/`` in shippable form.

    python -m bench.longmemeval_v2.results <run-dir> <label> [--out bench/results]

Writes ``longmemeval-v2-<label>-<YYYY-MM-DD>/`` holding ``aggregated_metrics.json``
(verbatim), ``run_args.json`` with endpoint hosts scrubbed, ``memory_config.json``,
and ``per_question.jsonl`` reduced to the columns the tables are built from
(no memory contexts, no prompts — those are 10–40 MB of haystack text per run).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import date
from pathlib import Path

_HOST_RE = re.compile(r"https?://[^/\s\"']+")
_KEEP = ("question_id", "question_type", "category", "eval_function", "is_abstention_problem",
         "score", "score_bool", "is_unknown", "memory_query_duration_seconds",
         "memory_context_token_count", "memory_context_original_token_count", "memory_context_was_truncated",
         "response_parsed_boxed", "usage")


def scrub_url(url: str) -> str:
    """``http://<lan-host>:<port>/v1`` → ``http://HOST/v1``: the host and port are
    deployment details, not methodology."""
    return _HOST_RE.sub(lambda m: m.group(0).split("//")[0] + "//HOST", url)


def export(run_dir: Path, label: str, out_root: Path, day: str | None = None) -> Path:
    day = day or date.today().isoformat()
    dest = out_root / f"longmemeval-v2-{label}-{day}"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(run_dir / "aggregated_metrics.json", dest / "aggregated_metrics.json")
    args = json.loads((run_dir / "run_args.json").read_text(encoding="utf-8"))
    for k, v in list(args.items()):
        if isinstance(v, str) and ("://" in v or v.startswith("/")):
            args[k] = scrub_url(v) if "://" in v else Path(v).name
    (dest / "run_args.json").write_text(json.dumps(args, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    mc = run_dir / "runtime_inputs" / "memory_config.json"
    if mc.is_file():
        cfg = json.loads(mc.read_text(encoding="utf-8"))
        params = cfg.get("memory_params") or {}
        for k, v in list(params.items()):
            if isinstance(v, str) and v.startswith("/"):
                params[k] = Path(v).name
            elif isinstance(v, dict):
                for kk, vv in list(v.items()):
                    if isinstance(vv, str) and "://" in vv:
                        v[kk] = scrub_url(vv)
        (dest / "memory_config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    with (run_dir / "per_question.jsonl").open(encoding="utf-8") as src, \
         (dest / "per_question.jsonl").open("w", encoding="utf-8") as out:
        for line in src:
            r = json.loads(line)
            hits = (r.get("memory_post_query_metadata") or {}).get("palinode_hits")
            row = {k: r.get(k) for k in _KEEP}
            if hits is not None:
                row["palinode_hits"] = hits
            out.write(json.dumps(row, ensure_ascii=True) + "\n")
    return dest


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("label")
    ap.add_argument("--out", default="bench/results")
    ap.add_argument("--date", default=None)
    a = ap.parse_args(argv)
    dest = export(Path(a.run_dir).expanduser(), a.label, Path(a.out), a.date)
    print(dest)


if __name__ == "__main__":
    main()

"""Render a benchmark results JSON object as a Markdown report.

Competitor-neutral by construction: this module reports only Palinode's own
measured numbers. Any comparison against another system's published
architecture belongs in a separate, reviewed artifact — not here.

    python -m bench.report /tmp/bench-results.json > report.md
"""
from __future__ import annotations

import json
import sys
from typing import Any


def _fmt_ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f} ms"


def _yesno(v: bool) -> str:
    return "yes" if v else "no"


def render_report(results: dict[str, Any]) -> str:
    env = results.get("environment", {})
    params = results.get("parameters", {})
    a1 = results.get("axis1_cost_per_fact", {})
    a2 = results.get("axis2_determinism", {})
    a3 = results.get("axis3_recall_latency", {})
    a4 = results.get("axis4_degradation_floor", {})

    primary = a1.get("primary_ingest", {})
    derived = a1.get("primary_ingest_derived", {})
    dedup = a1.get("reingest_unchanged", {})

    lines: list[str] = []
    a = lines.append

    a("# Palinode ingestion & recall benchmark")
    a("")
    a(f"- Generated: {env.get('generated_at', 'unknown')}")
    a(f"- Palinode version: {env.get('palinode_version', 'unknown')}")
    a(f"- Python: {env.get('python_version', 'unknown')} on {env.get('platform', 'unknown')}")
    a(f"- Embedder reachable: {_yesno(env.get('embedder_available', False))}")
    a(f"- Parameters: seed={params.get('seed')}, corpus={params.get('size')} files, "
      f"determinism runs={params.get('runs')}, recall iters/query={params.get('iters')}")
    a("")
    if not env.get("embedder_available", False):
        a("> **Note:** no embedder was reachable on this run. Ingest ran keyword-only "
          "(FTS5/BM25), so per-fact embed calls are 0 and hybrid recall is not measured. "
          "This is the honest degradation-floor path (axis 4), reported as-measured.")
        a("")

    # --- Axis 1 ---
    a("## Axis 1 — cost per remembered fact (measured)")
    a("")
    a(f"- Files ingested: **{primary.get('num_files')}**")
    a(f"- Facts (indexed chunks): **{primary.get('num_facts')}**")
    a(f"- Chat-LLM calls on the ingest path: **{primary.get('chat_llm_calls')}** "
      f"({derived.get('chat_calls_per_fact', 0):.3f} per fact)")
    a(f"- Embed calls: **{primary.get('embed_calls')}** "
      f"({derived.get('embed_calls_per_fact', 0):.3f} per fact)")
    a(f"- Approx embed input tokens (chars/4 estimate): {primary.get('embed_input_tokens_approx')}")
    a(f"- Wall-clock: **{primary.get('wall_clock_s', 0):.3f} s** "
      f"({derived.get('ms_per_fact', 0):.3f} ms/fact)")
    a(f"- Vectors written: {primary.get('num_vectors')} (embedded: {_yesno(primary.get('embedded', False))})")
    a("")
    a("Re-ingest of the unchanged corpus (SHA-256 dedup):")
    a("")
    a(f"- Chunks written: **{dedup.get('chunks_written')}** "
      f"(unchanged: {dedup.get('chunks_unchanged')})")
    a(f"- Embed calls: **{dedup.get('embed_calls')}**, chat-LLM calls: **{dedup.get('chat_llm_calls')}**")
    a(f"- Wall-clock: {dedup.get('wall_clock_s', 0):.3f} s")
    a("")

    # --- Axis 2 ---
    a("## Axis 2 — determinism")
    a("")
    a(f"- Clean-state ingests: **{a2.get('n_runs')}**")
    a(f"- Source files byte-identical across runs: **{_yesno(a2.get('files_identical', False))}**")
    a(f"- DB logical state identical (modulo timestamps): **{_yesno(a2.get('db_logical_identical', False))}**")
    a(f"- Embedded-vector id set identical: **{_yesno(a2.get('vec_ids_identical', False))}**")
    db_hashes = a2.get("distinct_db_hashes", [])
    if db_hashes:
        a(f"- DB logical fingerprint: `{db_hashes[0][:16]}…` "
          f"({len(db_hashes)} distinct value(s) across runs)")
    a("")

    # --- Axis 3 ---
    a("## Axis 3 — LLM-free recall latency")
    a("")
    hybrid = a3.get("hybrid")
    if hybrid:
        a("Hybrid (vector + BM25, RRF). No chat/synthesis model is invoked; the "
          "query vector costs one embedding per query — both counted separately:")
        a("")
        a(f"- Query-time chat-LLM calls: **{hybrid.get('query_time_chat_llm_calls')}** "
          f"({hybrid.get('n_queries')} queries)")
        a(f"- Query-time embed calls: **{hybrid.get('query_time_embed_calls')}** "
          "(one query vector per query)")
        a(f"- Cold p50/p95: {_fmt_ms(hybrid.get('cold_p50_ms'))} / {_fmt_ms(hybrid.get('cold_p95_ms'))}")
        a(f"- Warm p50/p95: {_fmt_ms(hybrid.get('warm_p50_ms'))} / {_fmt_ms(hybrid.get('warm_p95_ms'))}")
    else:
        a(f"Hybrid recall not measured: {a3.get('hybrid_unavailable_reason', 'unavailable')}.")
    a("")
    kw = a3.get("keyword", {})
    a("Keyword-only (BM25/FTS5) — measured zero model calls of any kind at query time:")
    a("")
    a(f"- Query-time chat-LLM calls: **{kw.get('query_time_chat_llm_calls')}**, "
      f"embed calls: **{kw.get('query_time_embed_calls')}**")
    a(f"- Cold p50/p95: {_fmt_ms(kw.get('cold_p50_ms'))} / {_fmt_ms(kw.get('cold_p95_ms'))}")
    a(f"- Warm p50/p95: {_fmt_ms(kw.get('warm_p50_ms'))} / {_fmt_ms(kw.get('warm_p95_ms'))}")
    a("")

    # --- Axis 4 ---
    a("## Axis 4 — degradation floor")
    a("")
    kw4 = a4.get("keyword_only_embedder_disabled", {})
    grep = a4.get("grep_over_files", {})
    # Only claim the embedder was disabled if the harness actually disabled it.
    if kw4.get("embedder_disabled"):
        a("Embedder **actively disabled** for this measurement — keyword-only recall "
          "still serves hits:")
    else:
        a("Keyword-only path (embedder not exercised by this query type) — recall "
          "still serves hits:")
    a("")
    a(f"- Total hits across queries: {kw4.get('total_hits')}")
    a(f"- Query-time embed calls: **{kw4.get('query_time_embed_calls')}** "
      f"(chat-LLM: **{kw4.get('query_time_chat_llm_calls')}**)")
    a(f"- Warm p50/p95: {_fmt_ms(kw4.get('warm_p50_ms'))} / {_fmt_ms(kw4.get('warm_p95_ms'))}")
    a("")
    a("API + index down — grep over the source files (the `cat` floor):")
    a("")
    a(f"- Total hits across queries: {grep.get('total_hits')}")
    a(f"- Warm p50/p95: {_fmt_ms(grep.get('warm_p50_ms'))} / {_fmt_ms(grep.get('warm_p95_ms'))}")
    a("")
    a("_All numbers above are measured on this host; see the run environment "
      "block for embedder availability._")
    a("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m bench.report <results.json>", file=sys.stderr)
        return 2
    results = json.loads(open(argv[0], encoding="utf-8").read())
    print(render_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

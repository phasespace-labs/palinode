"""Run the LongMemEval-V2 harness with the Palinode backend (or an upstream baseline).

A thin superset of upstream ``evaluation/run_eval.py``: same question/haystack
materialisation and the same baseline memory configs, plus what that wrapper
lacks — ``--method palinode``, an evaluator base URL (local judge for
iteration), and the harness's memory save / load / skip-evaluation flags so a
haystack is built once per domain and reused.

Requires the upstream checkout on ``sys.path`` (``LME_V2_HOME``, default
``~/Code/LongMemEval-V2``) and this repo on ``PYTHONPATH``::

    LME_V2_HOME=~/Code/LongMemEval-V2 OLLAMA_URL=http://<embedder>:11434 \\
    python -m bench.longmemeval_v2.run --domain web --tier small --method palinode \\
        --output-dir runs/palinode_web_small --save-memory \\
        --reader-base-url http://<reader>/v1 --reader-model qwen3.5-9b \\
        --evaluator-base-url http://<judge>/v1 --evaluator-model <judge>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _upstream_home() -> Path:
    home = Path(os.environ.get("LME_V2_HOME", "~/Code/LongMemEval-V2")).expanduser()
    if not (home / "evaluation" / "harness.py").is_file():
        raise SystemExit(f"LongMemEval-V2 checkout not found at {home} (set LME_V2_HOME)")
    return home


def _default_data_root() -> Path:
    return Path(os.environ.get("LONGMEMEVAL_V2_DATA", "~/.cache/longmemeval-v2")).expanduser()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LongMemEval-V2 × Palinode runner")
    p.add_argument("--data-root", default=str(_default_data_root()))
    p.add_argument("--domain", choices=["web", "enterprise"], required=True)
    p.add_argument("--tier", choices=["small", "medium"], default="small")
    p.add_argument("--method", default="palinode",
                   help="palinode, or an upstream method name (no_retrieval, rag_query_to_slice, rag_query_to_slice_notes, agentrunbook_r, …)")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--question-ids", nargs="*", default=None)
    p.add_argument("--shuffle-questions-seed", type=int, default=None)
    # memory state
    p.add_argument("--save-memory", action="store_true", help="save the built haystack memory to <output-dir>/memory_state")
    p.add_argument("--load-memory-dir", default=None, help="reuse a saved memory_state instead of rebuilding")
    p.add_argument("--skip-evaluation", action="store_true", help="build + save memory, then exit")
    p.add_argument("--prompt-build-max-workers", type=int, default=1)
    # palinode params (only with --method palinode)
    p.add_argument("--palinode-top-k", type=int, default=None)
    p.add_argument("--palinode-threshold", type=float, default=None)
    p.add_argument("--palinode-hybrid-weight", type=float, default=None)
    p.add_argument("--palinode-keyword-only", action="store_true")
    p.add_argument("--palinode-fts-mode", choices=("or", "and"), default=None,
                   help="BM25 arm: 'or' = adapter's any-content-word MATCH (default); 'and' = the store's stock implicit-AND path")
    p.add_argument("--palinode-slice-max-chars", type=int, default=None)
    p.add_argument("--palinode-images", action="store_true", help="return each hit state's screenshot too (needs prepared screenshots/)")
    p.add_argument("--palinode-neighbor-radius", type=int, default=None, help="also return states N±r around each hit (upstream slice baseline: 1)")
    p.add_argument("--palinode-extract", action="store_true", help="insert-time: LLM-proposed notes via LME_EXTRACT_* (the notes pool)")
    p.add_argument("--palinode-notes-top-k", type=int, default=None, help="query-time: notes returned ahead of the slices (needs an extracted store)")
    p.add_argument("--palinode-workspace", default=None, help="where the store is built (default $LME_PALINODE_WORKSPACE)")
    # reader (pinned to Qwen3.5-9B for the leaderboard; anything OpenAI-compatible for smoke)
    p.add_argument("--reader-model", default=os.getenv("READER_MODEL", "Qwen/Qwen3.5-9B"))
    p.add_argument("--reader-base-url", default=os.getenv("READER_BASE_URL", "http://localhost:8023/v1"))
    p.add_argument("--reader-api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--reader-temperature", type=float, default=float(os.getenv("READER_TEMPERATURE", "0.6")))
    p.add_argument("--reader-top-p", type=float, default=float(os.getenv("READER_TOP_P", "0.95")))
    p.add_argument("--reader-top-k", type=int, default=int(os.getenv("READER_TOP_K", "20")))
    p.add_argument("--reader-max-concurrent-requests", type=int, default=8)
    p.add_argument("--reader-timeout-seconds", type=float, default=3600.0,
                   help="per-request reader timeout (upstream default is 43200 s — a stalled response hangs the run for 12 h)")
    p.add_argument("--reader-enable-thinking", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--max-completion-tokens", type=int, default=20000)
    p.add_argument("--memory-context-max-tokens", type=int, default=200000)
    # upstream baseline models (controller + embedder), same env names as run_eval.py
    p.add_argument("--controller-model", default=os.getenv("LME_CONTROLLER_MODEL", "Qwen/Qwen3.5-9B"))
    p.add_argument("--controller-base-url", default=os.getenv("LME_CONTROLLER_BASE_URL", "http://localhost:8023/v1"))
    p.add_argument("--controller-api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--controller-temperature", type=float, default=float(os.getenv("LME_CONTROLLER_TEMPERATURE", "0.6")))
    p.add_argument("--controller-top-p", type=float, default=float(os.getenv("LME_CONTROLLER_TOP_P", "0.95")))
    p.add_argument("--controller-top-k", type=int, default=int(os.getenv("LME_CONTROLLER_TOP_K", "20")))
    p.add_argument("--controller-disable-thinking", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--embedding-model", default=os.getenv("LME_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"))
    p.add_argument("--embedding-base-url", default=os.getenv("LME_EMBEDDING_BASE_URL", "http://localhost:8114/v1"))
    p.add_argument("--embedding-api-key-env", default="OPENAI_API_KEY")
    # judge (gpt-5.2 medium for recorded numbers; any local model for iteration)
    p.add_argument("--evaluator-model", default=os.getenv("EVALUATOR_MODEL", "gpt-5.2"))
    p.add_argument("--evaluator-base-url", default=os.getenv("EVALUATOR_BASE_URL"))
    p.add_argument("--evaluator-api-key-env", default=os.getenv("EVALUATOR_API_KEY_ENV", "OPENAI_API_KEY"))
    p.add_argument("--evaluator-reasoning-effort", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--evaluator-max-completion-tokens", type=int, default=4096)
    return p.parse_args(argv)


def palinode_memory_config(args: argparse.Namespace, output_dir: Path) -> dict[str, object]:
    # Build the store under this run's output dir unless told otherwise: a shared
    # workspace would let two concurrent builds clobber each other.
    params: dict[str, object] = {"workspace_root": str(output_dir / "palinode_workspace")}
    if args.palinode_top_k is not None:
        params["top_k"] = args.palinode_top_k
    if args.palinode_threshold is not None:
        params["threshold"] = args.palinode_threshold
    if args.palinode_hybrid_weight is not None:
        params["hybrid_weight"] = args.palinode_hybrid_weight
    if args.palinode_keyword_only:
        params["hybrid"] = False
    if args.palinode_fts_mode is not None:
        params["fts_mode"] = args.palinode_fts_mode
    if args.palinode_slice_max_chars is not None:
        params["slice_max_chars"] = args.palinode_slice_max_chars
    if args.palinode_neighbor_radius is not None:
        params["neighbor_radius"] = args.palinode_neighbor_radius
    if args.palinode_extract:
        params["extract"] = True
    if args.palinode_notes_top_k is not None:
        params["notes_top_k"] = args.palinode_notes_top_k
    if args.palinode_images:
        params["images"] = True
        params["screenshots_root"] = str(Path(args.data_root).expanduser().resolve())
    if args.palinode_workspace:
        params["workspace_root"] = str(Path(args.palinode_workspace).expanduser().resolve())
    return {"memory_type": "palinode", "memory_params": params}


def _no_keepalive_clients(harness_mod) -> None:
    """Build the harness's AsyncOpenAI clients over an httpx client that keeps
    no idle connections. Observed 2026-09-04: a run stalled for an hour with no
    reader connection open — dead keep-alive sockets (CLOSE_WAIT) behind
    llama-swap, and the upstream per-request timeout is 12 h. A fresh
    connection per request costs nothing against a local reader."""
    import httpx
    from openai import AsyncOpenAI

    original = harness_mod.create_async_client

    def create(base_url, api_key_env, api_key_file):
        api_key = harness_mod.load_api_key(api_key_env, api_key_file)
        http_client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0, max_connections=64))
        # Retries with the SDK's capped backoff (~8 s) must outlast a reader eviction: the
        # KMD nightly holds the GPU ~6 min and the reader reloads in ~2 (LME2_READER_MAX_RETRIES).
        retries = int(os.environ.get("LME2_READER_MAX_RETRIES", "6"))
        if base_url:
            return AsyncOpenAI(base_url=base_url, api_key=api_key or "EMPTY",
                               max_retries=retries, http_client=http_client)
        return original(base_url, api_key_env, api_key_file)

    harness_mod.create_async_client = create


def _install_stack_dump() -> None:
    """``kill -USR1 <pid>`` writes every thread's stack to stderr. Runs have stalled
    idle with reader responses unread and no way to see why on a Mac without
    sudo for py-spy; this is the cheap alternative."""
    import faulthandler
    import signal

    faulthandler.register(signal.SIGUSR1, all_threads=True)


def main(argv: list[str] | None = None) -> None:
    _install_stack_dump()
    args = parse_args(argv)
    home = _upstream_home()
    if str(home) not in sys.path:
        sys.path.insert(0, str(home))
    os.chdir(home)  # upstream resolves its own assets relative to the checkout

    from data.public_data import materialize_runtime_haystack, materialize_runtime_questions
    from evaluation import harness as upstream_harness
    from evaluation import run_eval as upstream
    from evaluation.harness import main as harness_main

    import bench.longmemeval_v2.adapter  # noqa: F401 - registers memory_type "palinode"

    _no_keepalive_clients(upstream_harness)

    data_root = Path(args.data_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    selected = materialize_runtime_questions(
        data_root=data_root, domain=args.domain,
        question_ids=upstream.parse_question_ids(args.question_ids), limit=args.limit,
        output_path=runtime_dir / "questions.json",
    )
    materialize_runtime_haystack(data_root=data_root, tier=args.tier, selected_questions=selected,
                                 output_path=runtime_dir / "haystack.json")
    if args.method == "palinode":
        memory_config = palinode_memory_config(args, output_dir)
    else:
        if args.method not in upstream.METHODS:
            raise SystemExit(f"unknown method {args.method!r}; upstream methods: {sorted(upstream.METHODS)}")
        memory_config = upstream.build_memory_config(args, data_root)
    memory_config_path = runtime_dir / "memory_config.json"
    memory_config_path.write_text(json.dumps(memory_config, indent=2) + "\n", encoding="utf-8")

    harness_argv = [
        "evaluation.harness",
        "--domain", args.domain,
        "--questions-path", str(runtime_dir / "questions.json"),
        "--haystack-path", str(runtime_dir / "haystack.json"),
        "--trajectories-path", str(data_root / "trajectories.jsonl"),
        "--memory-config-path", str(memory_config_path),
        "--output-dir", str(output_dir),
        "--model", args.reader_model,
        "--base-url", args.reader_base_url,
        "--api-key-env", args.reader_api_key_env,
        "--temperature", str(args.reader_temperature),
        "--top-p", str(args.reader_top_p),
        "--top-k", str(args.reader_top_k),
        "--max-completion-tokens", str(args.max_completion_tokens),
        "--memory-context-max-tokens", str(args.memory_context_max_tokens),
        "--reader-max-concurrent-requests", str(args.reader_max_concurrent_requests),
        "--timeout-seconds", str(args.reader_timeout_seconds),
        "--prompt-build-max-workers", str(args.prompt_build_max_workers),
        "--evaluator-model", args.evaluator_model,
        "--evaluator-api-key-env", args.evaluator_api_key_env,
        "--evaluator-reasoning-effort", args.evaluator_reasoning_effort,
        "--evaluator-max-completion-tokens", str(args.evaluator_max_completion_tokens),
    ]
    if args.evaluator_base_url:
        harness_argv += ["--evaluator-base-url", args.evaluator_base_url]
    if not args.reader_enable_thinking:
        harness_argv.append("--reader-disable-thinking")
    if args.shuffle_questions_seed is not None:
        harness_argv += ["--shuffle-questions-seed", str(args.shuffle_questions_seed)]
    if args.save_memory:
        harness_argv.append("--save-memory")
    if args.skip_evaluation:
        harness_argv.append("--skip-evaluation")
    if args.load_memory_dir:
        harness_argv += ["--load-memory-dir", str(Path(args.load_memory_dir).expanduser().resolve())]

    print(json.dumps({"runtime_dir": str(runtime_dir), "method": args.method, "questions": len(selected),
                      "memory_config": memory_config}, indent=2), flush=True)
    old_argv = sys.argv
    try:
        sys.argv = harness_argv
        harness_main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    main()

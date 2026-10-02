"""CLI for the relevance/abstention evaluation.

    python -m bench.relevance --out results.json --report report.md
    python -m bench.relevance --coverage-only     # just the fixture gate
    python -m bench.relevance --top-k 10 --thresholds 0.4,0.5

``--coverage-only`` validates the fixture and prints its shape without touching
a store, an embedder or the network — the check CI can afford to run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from bench.relevance import corpus as corpus_mod
from bench.relevance import harness, report


def _csv_floats(raw: str) -> tuple[float, ...]:
    try:
        return tuple(float(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated numbers") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bench.relevance")
    parser.add_argument("--records", default=None, help="path to a corpus YAML")
    parser.add_argument("--questions", default=None, help="path to a question-set YAML")
    parser.add_argument("--top-k", type=int, default=harness.DEFAULT_TOP_K)
    parser.add_argument(
        "--thresholds",
        type=_csv_floats,
        default=harness.DEFAULT_THRESHOLDS,
        help="hybrid cosine floors to record (default: the shipped MCP and API values)",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="timing passes per question"
    )
    parser.add_argument("--out", default=None, help="results JSON destination")
    parser.add_argument("--report", default=None, help="Markdown report destination")
    parser.add_argument(
        "--coverage-only",
        action="store_true",
        help="validate the fixture and print its shape; run nothing",
    )
    parser.add_argument(
        "--keep-store", action="store_true", help="leave the temp store on disk"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.disable(logging.WARNING)

    try:
        fixture = corpus_mod.load_fixture(args.records, args.questions)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.coverage_only:
        print(
            json.dumps(
                {
                    "corpus_version": fixture.corpus_version,
                    "question_set_version": fixture.question_set_version,
                    "records": len(fixture.records),
                    "questions": len(fixture.questions),
                    "class_counts": corpus_mod.class_counts(fixture),
                    "variant_counts": corpus_mod.variant_counts(fixture),
                    "role_counts": corpus_mod.role_counts(fixture),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    try:
        results = harness.evaluate(
            fixture=fixture,
            top_k=args.top_k,
            thresholds=args.thresholds,
            repeats=args.repeats,
            keep_store=args.keep_store,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.out:
        report.write_json(results, args.out)
        print(f"results → {args.out}", file=sys.stderr)
    rendered = report.render(results)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        print(f"report  → {args.report}", file=sys.stderr)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

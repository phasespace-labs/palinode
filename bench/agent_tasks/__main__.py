"""CLI: ``plan`` · ``judge`` · ``grade`` · ``report``. See the package docstring."""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from collections import Counter

from bench.agent_tasks import grade, judge, plan, report
from bench.agent_tasks.corpus import load_corpus


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m bench.agent_tasks")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="materialize cells for a run")
    p.add_argument("--out", required=True, help="plan directory (must be empty or absent)")
    p.add_argument("--rows", default="1,2,7", help="e.g. 1,2,7 or 1-18")
    p.add_argument("--split", default="dev",
                   help="dev, heldout, or both as dev,heldout (reported separately)")
    p.add_argument("--clients", default=",".join(plan.CLIENTS))
    p.add_argument("--arms", default=",".join(plan.ARMS))
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                   help="date every fixture date is relative to (default: today, UTC); "
                        "recorded in plan.json — run the plan the same day")
    p.add_argument("--corpus", default=None, help="path to a scenarios YAML")

    g = sub.add_parser("grade", help="grade a run directory against its plan")
    g.add_argument("--plan", required=True)
    g.add_argument("--run", required=True)
    g.add_argument("--out", required=True)

    g.add_argument("--judge-results", help="optional judge JSON; deterministic scores stay intact")

    j = sub.add_parser("judge", help="judge free-text cells with a pinned, different model")
    j.add_argument("--plan", required=True)
    j.add_argument("--run", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--model", default=judge.DEFAULT_MODEL)
    j.add_argument("--command", dest="judge_command", default=judge.DEFAULT_COMMAND)
    j.add_argument("--seed", type=int, default=1449)
    j.add_argument("--timeout", type=float, default=120)
    j.add_argument("--attempts", type=int, default=3,
                   help="calls per cell before recording an error (default 3)")
    j.add_argument("--resume", help="earlier judge JSON: reuse its judged cells, call only the rest")

    r = sub.add_parser("report", help="render results.json as Markdown on stdout")
    r.add_argument("results")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        corpus = load_corpus(args.corpus)
        built = plan.build_plan(
            corpus,
            rows=plan.parse_rows(args.rows),
            split=args.split,
            clients=_csv(args.clients),
            arms=_csv(args.arms),
            repeats=args.repeats,
            seed=args.seed,
            as_of=args.as_of or datetime.datetime.now(datetime.UTC).date().isoformat(),
        )
        path = plan.write_plan(built, args.out)
        status = Counter(c["status"] for c in built["cells"])
        print(f"plan → {path}: {len(built['cells'])} cells "
              f"({', '.join(f'{k} {v}' for k, v in sorted(status.items()))}), "
              f"{len(built['probes'])} probe(s)", file=sys.stderr)
        return 0
    if args.command == "judge":
        prior = None
        if args.resume:
            with open(args.resume, encoding="utf-8") as handle:
                prior = json.load(handle)
        results = judge.judge_run(args.plan, args.run, model=args.model,
                                 command=args.judge_command, seed=args.seed, timeout=args.timeout,
                                 attempts=args.attempts, resume=prior)
        grade.write_results(results, args.out)
        with open(args.out + ".audit.md", "w", encoding="utf-8") as handle:
            handle.write(judge.audit_sheet(results))
        return int(any(c["status"] == "error" for c in results["cells"]))
    if args.command == "grade":
        results = grade.grade_run(args.plan, args.run, judge_path=args.judge_results)
        grade.write_results(results, args.out)
        status = Counter(c["status"] for c in results["cells"])
        print(f"results → {args.out}: "
              f"{', '.join(f'{k} {v}' for k, v in sorted(status.items()))}", file=sys.stderr)
        return 0
    with open(args.results, encoding="utf-8") as handle:
        results = json.load(handle)
    sys.stdout.write(report.render(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

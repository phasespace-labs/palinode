"""Per-type table across LME-V2 runs — the stage-1 deliverable.

    python -m bench.longmemeval_v2.report runs/palinode_web_small_eval runs/rag_query_to_slice_web_small_eval …
    python -m bench.longmemeval_v2.report --combine web=runs/x_web enterprise=runs/x_enterprise   # one row, both domains

Reads each run's ``aggregated_metrics.json``. A run directory may also be a
``domain=path`` pair list to example-count-weight the two domains the way the
leaderboard's ``combine_aggregated_metrics.py`` does. Accuracy is
``overall_full_set`` (abstention questions included); the per-type columns are
the harness's ``combined_abstention_by_category`` (each type's plain and
``-abs`` questions together) plus the abstention-only rate, so a system that
answers well but never abstains is visible as such.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TYPES = ("static", "dynamic", "procedure", "gotchas")


def load(run_dir: str | Path) -> dict[str, Any]:
    p = Path(run_dir).expanduser()
    return json.loads((p / "aggregated_metrics.json").read_text(encoding="utf-8"))


def _rate(section: dict[str, Any], key: str) -> tuple[int, float | None]:
    row = section.get(key) or {}
    return int(row.get("count") or 0), row.get("pct_correct")


def summarize(metrics: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "n": metrics["overall"]["count_all_questions"],
        "overall": metrics["overall"]["overall_full_set"],
        "abstention": (metrics.get("abstention_overall") or {}).get("pct_correct"),
        "n_abs": (metrics.get("abstention_overall") or {}).get("count") or 0,
        "query_s": (metrics.get("memory_query") or {}).get("avg_seconds"),
        "ctx_tokens": (metrics.get("memory_context") or {}).get("avg_final_tokens"),
    }
    for t in TYPES:
        out[f"n_{t}"], out[t] = _rate(metrics.get("combined_abstention_by_category") or {}, t)
    # gotchas has no -abs variant, so the harness reports it only under non-abstention.
    if not out["n_gotchas"]:
        out["n_gotchas"], out["gotchas"] = _rate(metrics.get("non_abstention_by_category") or {}, "gotchas")
    return out


def combine(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Example-count-weighted merge of per-domain summaries."""
    def wavg(key: str, nkey: str) -> float | None:
        num = sum((p[key] or 0) * p[nkey] for p in parts if p[key] is not None)
        den = sum(p[nkey] for p in parts if p[key] is not None)
        return num / den if den else None
    out: dict[str, Any] = {"n": sum(p["n"] for p in parts), "n_abs": sum(p["n_abs"] for p in parts)}
    out["overall"] = wavg("overall", "n")
    out["abstention"] = wavg("abstention", "n_abs")
    out["query_s"] = wavg("query_s", "n")
    out["ctx_tokens"] = wavg("ctx_tokens", "n")
    for t in TYPES:
        out[f"n_{t}"] = sum(p[f"n_{t}"] for p in parts)
        out[t] = wavg(t, f"n_{t}")
    return out


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{100 * v:.1f}"


def table(rows: list[tuple[str, dict[str, Any]]]) -> str:
    head = "| run | n | overall | static | dynamic | procedure | gotchas | abstain (n) | query s | ctx tok |"
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    lines = [head, sep]
    for name, s in rows:
        query_s = "—" if s["query_s"] is None else f"{s['query_s']:.2f}"
        ctx = "—" if s["ctx_tokens"] is None else f"{s['ctx_tokens']:,.0f}"
        lines.append(
            f"| {name} | {s['n']} | **{_pct(s['overall'])}** | {_pct(s['static'])} | {_pct(s['dynamic'])} | "
            f"{_pct(s['procedure'])} | {_pct(s['gotchas'])} | {_pct(s['abstention'])} ({s['n_abs']}) | "
            f"{query_s} | {ctx} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="*", help="run dirs, or name=dir")
    ap.add_argument("--combine", nargs="+", action="append", default=[], metavar="DOMAIN=DIR",
                    help="one row from several per-domain runs; repeatable")
    ap.add_argument("--name", action="append", default=[], help="row name for each --combine, in order")
    args = ap.parse_args(argv)
    rows: list[tuple[str, dict[str, Any]]] = []
    for spec in args.runs:
        name, _, path = spec.rpartition("=") if "=" in spec else ("", "", spec)
        rows.append((name or Path(path).name, summarize(load(path))))
    for i, group in enumerate(args.combine):
        parts = [summarize(load(g.split("=", 1)[1])) for g in group]
        name = args.name[i] if i < len(args.name) else " + ".join(g.split("=", 1)[0] for g in group)
        rows.append((name, combine(parts)))
    print(table(rows))


if __name__ == "__main__":
    main()

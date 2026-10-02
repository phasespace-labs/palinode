"""
CLI command: palinode retrieval-stats

Reads.audit/retrievals.jsonl and reports retrieval-event statistics.
Issue the retrieval-event instrumentation — ADR-007 prerequisite: surface the data so empirical tau
values can be derived for decay tuning.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click

from palinode.core.config import config
from palinode.cli._format import console


def _load_events(log_path: Path, days: int) -> list[dict[str, Any]]:
    """Load retrieval events from JSONL log, filtered to the last *days* days."""
    if not log_path.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    events: list[dict[str, Any]] = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("timestamp", "") >= cutoff:
                events.append(entry)
    return events


def _stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate retrieval events into summary statistics."""
    from palinode.core.retrieval_log import BUNDLE_RECEIPT

    events = [e for e in events if e.get("event_type") != BUNDLE_RECEIPT]
    # An empty search delivery logs one call-level row with no memory ref; it
    # is a search, not a retrieval, so it is counted on its own.
    empty_searches = sum(1 for e in events if not e.get("file_path"))
    events = [e for e in events if e.get("file_path")]
    total = len(events)
    explicit = sum(1 for e in events if e.get("mode") == "explicit")
    passive = sum(1 for e in events if e.get("mode") == "passive")

    # Counts per file
    file_counts: dict[str, int] = defaultdict(int)
    for e in events:
        fp = e.get("file_path", "")
        if fp:
            file_counts[fp] += 1

    # Top-20 most-retrieved files
    top_files = sorted(file_counts.items(), key=lambda x: x[1], reverse=True)[:20]

    # Distribution: 0 retrievals, 1-3, 4+
    # Note: "0 retrievals" means files in memory_dir that have never appeared.
    all_md: list[str] = []
    memory_dir = config.memory_dir
    try:
        for root, _dirs, fnames in os.walk(memory_dir):
            for fn in fnames:
                if fn.endswith(".md"):
                    rel = os.path.relpath(os.path.join(root, fn), memory_dir)
                    all_md.append(rel)
    except OSError:
        pass

    # Normalize file paths — strip memory_dir prefix for comparison
    def _rel(fp: str) -> str:
        try:
            return os.path.relpath(fp, memory_dir)
        except ValueError:
            return fp

    rel_counts: dict[str, int] = {_rel(k): v for k, v in file_counts.items()}
    total_md = len(all_md)
    zero_count = sum(1 for f in all_md if f not in rel_counts)
    one_to_three = sum(1 for v in rel_counts.values() if 1 <= v <= 3)
    four_plus = sum(1 for v in rel_counts.values() if v >= 4)

    # Mean / median time-since-last-retrieval (days) per file
    # For each file, find the most recent retrieval timestamp.
    last_seen: dict[str, str] = {}
    for e in events:
        fp = _rel(e.get("file_path", ""))
        ts = e.get("timestamp", "")
        if fp and ts:
            if fp not in last_seen or ts > last_seen[fp]:
                last_seen[fp] = ts

    ages_days: list[float] = []
    for fp in all_md:
        if fp in last_seen:
            try:
                last_ts = datetime.fromisoformat(last_seen[fp])
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                delta = (datetime.now(timezone.utc) - last_ts).total_seconds() / 86400
                ages_days.append(delta)
            except ValueError:
                pass
        else:
            ages_days.append(float("inf"))  # never retrieved

    finite_ages = [a for a in ages_days if math.isfinite(a)]
    if finite_ages:
        mean_age = sum(finite_ages) / len(finite_ages)
        sorted_ages = sorted(finite_ages)
        n = len(sorted_ages)
        if n % 2 == 0:
            median_age = (sorted_ages[n // 2 - 1] + sorted_ages[n // 2]) / 2
        else:
            median_age = sorted_ages[n // 2]
    else:
        mean_age = None
        median_age = None

    return {
        "total_events": total,
        "explicit": explicit,
        "passive": passive,
        "empty_searches": empty_searches,
        "unique_files_retrieved": len(file_counts),
        "top_files": top_files,
        "distribution": {
            "total_md_files": total_md,
            "zero_retrievals": zero_count,
            "one_to_three": one_to_three,
            "four_plus": four_plus,
        },
        "age_days": {
            "mean": round(mean_age, 1) if mean_age is not None else None,
            "median": round(median_age, 1) if median_age is not None else None,
        },
    }


@click.command(name="retrieval-stats")
@click.option(
    "--days",
    type=int,
    default=7,
    show_default=True,
    help="Lookback window in days.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def retrieval_stats(days: int, fmt: str) -> None:
    """Show retrieval-event statistics from the instrumentation log.

    Reads .audit/retrievals.jsonl and summarises retrieval activity
    over the last N days.  Data is the ADR-007 prerequisite for
    deriving empirical tau values for decay tuning.
    """
    log_path = Path(config.memory_dir) / ".audit" / "retrievals.jsonl"

    if not log_path.exists():
        if fmt == "json":
            import json as _json
            click.echo(_json.dumps({"error": "No retrieval log found", "path": str(log_path)}))
        else:
            console.print(f"[yellow]No retrieval log found at {log_path}.[/yellow]")
            console.print("Retrieval events are captured automatically when palinode_search or palinode_read is called.")
        return

    events = _load_events(log_path, days)
    stats = _stats(events)

    if fmt == "json":
        import json as _json
        click.echo(_json.dumps(stats, indent=2, default=str))
        return

    # Human-readable output
    console.print(f"\n[bold]Retrieval stats — last {days} day(s)[/bold]")
    console.print(f"  Total events:      {stats['total_events']}")
    console.print(f"  Explicit:          {stats['explicit']}")
    console.print(f"  Passive:           {stats['passive']}")
    console.print(f"  Empty searches:    {stats['empty_searches']}")
    console.print(f"  Unique files:      {stats['unique_files_retrieved']}")

    dist = stats["distribution"]
    total_md = dist["total_md_files"]
    if total_md:
        zero_pct = round(dist["zero_retrievals"] / total_md * 100)
        one_pct = round(dist["one_to_three"] / total_md * 100)
        four_pct = round(dist["four_plus"] / total_md * 100)
        console.print(f"\n[bold]Distribution (of {total_md} .md files)[/bold]")
        console.print(f"  0 retrievals:      {dist['zero_retrievals']}  ({zero_pct}%)")
        console.print(f"  1–3 retrievals:    {dist['one_to_three']}  ({one_pct}%)")
        console.print(f"  4+ retrievals:     {dist['four_plus']}  ({four_pct}%)")

    age = stats["age_days"]
    if age["mean"] is not None:
        console.print("\n[bold]Time since last retrieval (retrieved files only)[/bold]")
        console.print(f"  Mean:    {age['mean']} days")
        console.print(f"  Median:  {age['median']} days")

    if stats["top_files"]:
        console.print(f"\n[bold]Top {min(20, len(stats['top_files']))} most-retrieved files[/bold]")
        for fp, count in stats["top_files"]:
            console.print(f"  {count:4d}  {fp}")

    console.print()

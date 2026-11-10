"""Static trigger health checks; no embedder or target-file scans."""
from __future__ import annotations

from contextlib import closing
import math
import sqlite3
from pathlib import Path

from palinode.core import expiry
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


@register(tags=("fast",))
def trigger_health(ctx: DoctorContext) -> CheckResult:
    """Report provably blocked triggers and untraceable legacy thresholds."""
    path = Path(ctx.config.db_path).expanduser().resolve()
    if not path.exists():
        return CheckResult("trigger_health", "warn", True, "DB file does not exist; skipping triggers.")
    blocked: list[str] = []
    unknown: list[str] = []
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)) as db:
            db.row_factory = sqlite3.Row
            columns = {row[1] for row in db.execute("PRAGMA table_info(triggers)")}
            if not columns:
                return CheckResult("trigger_health", "warn", True, "Triggers table not found; skipping.")
            # vec0 shadow metadata is readable without loading the vector extension.
            vectors = {row[0] for row in db.execute("SELECT id FROM triggers_vec_rowids")}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'trigger_phrasings'").fetchone():
                vectors.update(row[0] for row in db.execute("SELECT DISTINCT trigger_id FROM trigger_phrasings"))
            for row in db.execute("SELECT * FROM triggers ORDER BY id"):
                reasons = []
                if not row["enabled"]:
                    reasons.append("disabled")
                if "expires_at" in columns and expiry.is_past(row["expires_at"]):
                    reasons.append("expired")
                threshold = row["threshold"]
                if threshold is None or not math.isfinite(threshold) or threshold > 1:
                    reasons.append("unreachable threshold")
                if row["id"] not in vectors:
                    reasons.append("missing embedding")
                if reasons:
                    blocked.append(f"{row['id']}: {', '.join(reasons)}")
                if "threshold_origin" not in columns or row["threshold_origin"] is None:
                    unknown.append(row["id"])
    except sqlite3.Error as exc:
        return CheckResult("trigger_health", "warn", False, f"Cannot inspect triggers: {exc}",
                           "Start the API to initialize the trigger schema; verify db_path.")
    if not blocked and not unknown:
        return CheckResult("trigger_health", "warn", True, "No statically blocked triggers or unknown thresholds.")
    details = []
    if blocked:
        details.append(f"{len(blocked)} triggers cannot fire in their current state: " + "; ".join(blocked))
    if unknown:
        details.append(f"{len(unknown)} legacy thresholds have unknown origin and were preserved: " + ", ".join(unknown))
    return CheckResult(
        "trigger_health", "warn", False, ". ".join(details),
        "Review with 'palinode trigger list'. Re-register blocked triggers with valid settings. "
        "Legacy thresholds cannot be distinguished from explicit user choices; review and "
        "re-register with example phrasings, omitting threshold to accept the default. "
        "A high reachable threshold alone does not prove a trigger can never fire.",
    )

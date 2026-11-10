"""Reverse lookup of recorded deliveries, never evidence of use or influence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from palinode.core.explain import MAX_LINES_SCANNED
from palinode.core.retrieval_log import instrumentation_disabled_by_env
from palinode.core.scope import ScopeChain
from palinode.core.trace import _canonical_ref
from palinode.core.visibility import is_visible


def session_deliveries(
    file_path: str, memory_dir: str, *, chain: ScopeChain | None = None,
    log_enabled: bool = True,
) -> dict[str, Any]:
    """Aggregate only this visible file's recorded deliveries in a bounded scan."""
    target = _canonical_ref(file_path, memory_dir)
    result: dict[str, Any] = {
        "sessions": [], "deliveries_without_session_id": 0,
        "scan_truncated": False,
        "recording_enabled": log_enabled and not instrumentation_disabled_by_env(),
    }
    if not is_visible(chain, str(Path(memory_dir) / (target + ".md"))):
        return result
    groups: dict[str, dict[str, Any]] = {}
    seen_bundles: set[str] = set()
    try:
        with (Path(memory_dir) / ".audit" / "retrievals.jsonl").open(encoding="utf-8") as fh:
            for index, line in enumerate(fh):
                if index >= MAX_LINES_SCANNED:
                    result["scan_truncated"] = True
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                refs = [row.get("file_path")]
                receipt = row.get("receipt")
                if isinstance(receipt, dict) and isinstance(receipt.get("supplied"), list):
                    refs.extend(item.get("ref") for item in receipt["supplied"] if isinstance(item, dict))
                if isinstance(row.get("fires"), list):
                    refs.extend(item.get("ref") for item in row["fires"] if isinstance(item, dict))
                if not any(isinstance(ref, str) and ref and
                           _canonical_ref(ref, memory_dir) == target for ref in refs):
                    continue
                bundle_id = row.get("bundle_id")
                if isinstance(bundle_id, str) and bundle_id:
                    if bundle_id in seen_bundles:
                        continue
                    seen_bundles.add(bundle_id)
                session_id = row.get("session_id")
                if not isinstance(session_id, str) or not session_id.strip():
                    result["deliveries_without_session_id"] += 1
                    continue
                group = groups.setdefault(session_id, {
                    "session_id": session_id, "delivery_count": 0,
                    "first_timestamp": None, "last_timestamp": None, "sources": [],
                })
                group["delivery_count"] += 1
                timestamp = row.get("timestamp")
                if isinstance(timestamp, str) and timestamp:
                    group["first_timestamp"] = min(group["first_timestamp"] or timestamp, timestamp)
                    group["last_timestamp"] = max(group["last_timestamp"] or timestamp, timestamp)
                source = row.get("source")
                if isinstance(source, str) and source not in group["sources"]:
                    group["sources"].append(source)
    except (OSError, UnicodeDecodeError):
        pass
    result["sessions"] = [groups[key] for key in sorted(groups)]
    return result


def format_session_deliveries(data: dict[str, Any]) -> str:
    lines = ["Recorded session deliveries (not use or causal influence):"]
    for session in data.get("sessions", []):
        lines.append(
            f"  {session['session_id']}: {session['delivery_count']} deliveries; "
            f"{session['first_timestamp'] or 'time unrecorded'} — "
            f"{session['last_timestamp'] or 'time unrecorded'}; "
            f"{', '.join(session['sources'])}"
        )
    if not data.get("sessions"):
        lines.append("  No visible recorded sessions found.")
    missing = data.get("deliveries_without_session_id", 0)
    if missing:
        lines.append(f"  {missing} matching deliveries have no recorded session ID.")
    if data.get("scan_truncated"):
        lines.append("  Audit scan limit reached; results are incomplete.")
    if not data.get("recording_enabled", True):
        lines.append("  Delivery recording is currently disabled.")
    lines.append("Historical coverage is limited by recording, retention, and older surfaces.")
    return "\n".join(lines)

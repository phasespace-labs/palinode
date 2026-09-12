"""Dataset fetch + load for LongMemEval."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

HF_BASE = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
VARIANTS = {
    "s": "longmemeval_s_cleaned.json",       # ~115k tokens / ~40 sessions per question
    "m": "longmemeval_m_cleaned.json",       # ~500 sessions per question
    "oracle": "longmemeval_oracle.json",     # evidence sessions only
}
QUESTION_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "temporal-reasoning",
    "knowledge-update",
    "multi-session",
)


def default_data_dir() -> Path:
    return Path(os.environ.get("LONGMEMEVAL_DATA", Path.home() / ".cache" / "longmemeval"))


def fetch(variant: str, data_dir: Path | None = None) -> Path:
    """Download a variant if missing; return its path."""
    data_dir = data_dir or default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    name = VARIANTS[variant]
    dest = data_dir / name
    if not dest.exists():
        urllib.request.urlretrieve(HF_BASE + name, dest)  # noqa: S310 - fixed HF URL
    return dest


def load(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_abstention(item: dict[str, Any]) -> bool:
    return str(item["question_id"]).endswith("_abs")

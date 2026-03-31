"""
Mem0 Memory Classifier

Takes raw Mem0 export JSON and produces classified, deduplicated,
grouped memories ready for file generation.

Uses Qwen 2.5 72B on Mac Studio for batch classification.
This is a one-time irreversible triage — use the best model available.

Usage:
    python -m palinode.migration.mem0_classify

Input:
    ~/.palinode/migration/mem0_export.json

Output:
    ~/.palinode/migration/mem0_classified.json

LLM Config:
    URL: http://localhost:8080/v1 (Mac Studio Qwen 72B)
    Model: /path/to/models/qwen25-72b-abliterated-mlx
"""
from __future__ import annotations

import json
import os
import re
import logging
import hashlib
from collections import defaultdict
from datetime import datetime

import httpx

from palinode.core.config import config

logger = logging.getLogger("palinode.migration.mem0_classify")

# ── Deduplication ─────────────────────────────────────────────────────────────

def deduplicate(memories: list[dict]) -> list[dict]:
    """Remove exact duplicates (by hash) and near-duplicates (by content similarity).

    Strategy:
    1. Exact dedup: same hash field → keep newest
    2. Content dedup: normalize whitespace/case → same normalized text → keep newest
    3. Substring dedup: if memory A is a substring of memory B → drop A, keep B

    Args:
        memories: Raw exported memories.

    Returns:
        Deduplicated list (expected ~40-60% reduction).
    """
    # Phase 1: Hash dedup
    by_hash = {}
    for m in memories:
        h = m.get("hash", "")
        if not h:
            h = hashlib.md5(m["content"].encode()).hexdigest()
        if h in by_hash:
            # Keep the newer one
            if m.get("created_at", "") > by_hash[h].get("created_at", ""):
                by_hash[h] = m
        else:
            by_hash[h] = m

    deduped = list(by_hash.values())
    logger.info(f"Hash dedup: {len(memories)} → {len(deduped)}")

    # Phase 2: Normalized content dedup
    by_normalized = {}
    for m in deduped:
        norm = re.sub(r'\s+', ' ', m["content"].lower().strip())
        if norm in by_normalized:
            if m.get("created_at", "") > by_normalized[norm].get("created_at", ""):
                by_normalized[norm] = m
        else:
            by_normalized[norm] = m

    deduped = list(by_normalized.values())
    logger.info(f"Content dedup: → {len(deduped)}")

    # Phase 3: Substring dedup (drop short memories that are contained in longer ones)
    # Sort by length descending so we check shorter against longer
    sorted_mems = sorted(deduped, key=lambda m: len(m["content"]), reverse=True)
    keep = []
    kept_content = []
    for m in sorted_mems:
        norm = re.sub(r'\s+', ' ', m["content"].lower().strip())
        is_substring = any(norm in existing for existing in kept_content)
        if not is_substring:
            keep.append(m)
            kept_content.append(norm)

    logger.info(f"Substring dedup: → {len(keep)}")
    return keep


# ── Classification ────────────────────────────────────────────────────────────

# Classification prompt for the LLM
CLASSIFY_SYSTEM_PROMPT = """You are a memory classifier for an AI agent's persistent memory system.

Given a batch of raw memory snippets, classify each one:

1. **type**: One of: PersonMemory, Decision, ProjectSnapshot, Insight, ActionItem, Config, Skip
   - PersonMemory: facts about a person (preferences, relationships, contact info)
   - Decision: a choice that was made with rationale
   - ProjectSnapshot: current state of a project or milestone
   - Insight: a lesson learned or recurring pattern
   - ActionItem: something that needs to be done
   - Config: system configuration or technical setup detail
   - Skip: not worth keeping (too vague, trivially obvious, or stale operational detail)

2. **entities**: Array of entity references, e.g. ["person/paul", "project/mm-kmd"]
   Known entities: person/paul, person/peter, person/aidan, project/mm-kmd, project/palinode, project/color-class, project/infrastructure

3. **group**: A short slug for grouping related memories into the same file.
   Related memories about the same topic should share a group slug.
   Examples: "mm-kmd-lora-training", "paul-preferences", "infrastructure-vllm"

Return a JSON array with one object per memory:
```json
[
  {"index": 0, "type": "ProjectSnapshot", "entities": ["project/mm-kmd"], "group": "mm-kmd-milestones"},
  {"index": 1, "type": "Skip", "entities": [], "group": ""},
  ...
]
```

Mark as "Skip" if the memory is:
- Too vague to be useful ("something was discussed")
- A transient operational detail ("restarting the server")
- Already captured better elsewhere
- Configuration that's likely stale

Be aggressive with Skip — quality over quantity. We want ~50-100 files, not 4,000."""


def classify_batch(memories: list[dict], batch_size: int = 20) -> list[dict]:
    """Classify memories in batches using the LLM.

    Args:
        memories: Deduplicated memories to classify.
        batch_size: How many memories to classify per LLM call.

    Returns:
        Memories with added 'type', 'entities', 'group' fields.
    """
    classified = []

    for i in range(0, len(memories), batch_size):
        batch = memories[i:i + batch_size]
        batch_text = "\n".join(
            f"[{j}] ({m.get('source_agent', '?')}, {m.get('created_at', '?')[:10]}) {m['content'][:300]}"
            for j, m in enumerate(batch)
        )

        # Use Qwen 72B on Mac Studio for classification (best available model)
        # This is a one-time migration — quality > speed
        CLASSIFY_LLM_URL = os.environ.get(
            "PALINODE_CLASSIFY_LLM_URL",
            "http://localhost:8080"
        )
        CLASSIFY_LLM_MODEL = os.environ.get(
            "PALINODE_CLASSIFY_LLM_MODEL",
            "/path/to/models/qwen25-72b-abliterated-mlx"
        )

        try:
            resp = httpx.post(
                f"{CLASSIFY_LLM_URL}/v1/chat/completions",
                json={
                    "model": CLASSIFY_LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                        {"role": "user", "content": f"Classify these {len(batch)} memories:\n\n{batch_text}"},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 2000,
                },
                timeout=600.0,  # Qwen 72B on MLX is slow — 5min per batch
            )
            resp.raise_for_status()
            result_text = resp.json()["choices"][0]["message"]["content"]

            # Parse JSON from response
            json_match = re.search(r"\[[\s\S]*\]", result_text)
            if json_match:
                classifications = json.loads(json_match.group())
                for c in classifications:
                    idx = c.get("index", 0)
                    if 0 <= idx < len(batch):
                        batch[idx]["type"] = c.get("type", "Skip")
                        batch[idx]["entities"] = c.get("entities", [])
                        batch[idx]["group"] = c.get("group", "")

        except Exception as e:
            logger.error(f"Classification batch {i}-{i+len(batch)} failed: {e}")
            # Mark failed batch as unclassified
            for m in batch:
                m.setdefault("type", "Skip")
                m.setdefault("entities", [])
                m.setdefault("group", "unclassified")

        classified.extend(batch)
        logger.info(f"Classified {min(i + batch_size, len(memories))}/{len(memories)}")

    return classified


def run_classification() -> str:
    """Full pipeline: load export → deduplicate → classify → save.

    Returns:
        Path to classified output JSON.
    """
    export_path = os.path.join(config.memory_dir, "migration", "mem0_export.json")
    output_path = os.path.join(config.memory_dir, "migration", "mem0_classified.json")

    with open(export_path) as f:
        memories = json.load(f)

    logger.info(f"Loaded {len(memories)} raw memories")

    # Deduplicate
    deduped = deduplicate(memories)

    # Classify
    classified = classify_batch(deduped)

    # Stats
    type_counts = defaultdict(int)
    for m in classified:
        type_counts[m.get("type", "Unknown")] += 1
    logger.info(f"Classification stats: {dict(type_counts)}")

    with open(output_path, "w") as f:
        json.dump(classified, f, indent=2, default=str)

    logger.info(f"Classified output → {output_path}")
    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_classification()

"""
Mem0 → JSON Exporter

Scrolls through all Qdrant collections and exports memories to a
single JSON file for offline processing. No transformation — just export.

Usage:
    python -m palinode.migration.mem0_export

Output:
    ./migration/mem0_export.json
"""
from __future__ import annotations

import json
import os
import logging
from datetime import datetime

import httpx

from palinode.core.config import config

logger = logging.getLogger("palinode.migration.mem0_export")

QDRANT_URL = "http://localhost:6333"
COLLECTIONS = ["mem0_attractor", "mem0_governor", "mem0_gradient"]
BATCH_SIZE = 100  # Qdrant scroll batch size


def export_all() -> str:
    """Export all Mem0 memories from Qdrant to JSON.

    Returns:
        Path to the exported JSON file.
    """
    output_dir = os.path.join(config.memory_dir, "migration")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "mem0_export.json")

    all_memories = []

    for collection in COLLECTIONS:
        logger.info(f"Exporting {collection}...")
        memories = _scroll_collection(collection)
        logger.info(f"  Exported {len(memories)} memories from {collection}")
        all_memories.extend(memories)

    # Sort by date
    all_memories.sort(key=lambda m: m.get("created_at", ""))

    with open(output_path, "w") as f:
        json.dump(all_memories, f, indent=2, default=str)

    logger.info(f"Total: {len(all_memories)} memories → {output_path}")
    return output_path


def _scroll_collection(collection: str) -> list[dict]:
    """Scroll through a Qdrant collection and extract all payloads."""
    memories = []
    offset = None

    while True:
        body = {
            "limit": BATCH_SIZE,
            "with_payload": True,
            "with_vector": False,
        }
        if offset:
            body["offset"] = offset

        resp = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json=body,
            timeout=30.0,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        points = result.get("points", [])

        if not points:
            break

        for p in points:
            payload = p.get("payload", {})
            memories.append({
                "id": str(p["id"]),
                "content": payload.get("data", payload.get("memory", "")),
                "source_agent": payload.get("source_agent", collection.replace("mem0_", "")),
                "source_collection": collection,
                "created_at": payload.get("createdAt", ""),
                "hash": payload.get("hash", ""),
                "session_type": payload.get("session_type", ""),
                "user_id": payload.get("userId", "default"),
            })

        offset = result.get("next_page_offset")
        if not offset:
            break

    return memories


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    path = export_all()
    print(f"Exported to: {path}")

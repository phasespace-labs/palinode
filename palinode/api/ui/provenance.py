"""Provenance-panel model builder for the UI (Phase 0).

Store-agnostic: ``build_provenance`` takes plain inputs (parsed frontmatter,
git history rows, recall stats) and returns a list of typed ``ProvenanceRow``
objects describing the vertical lineage shown in the right-hand panel. It does
no I/O and imports nothing from palinode's store/config, so ``weir`` can reuse
it by feeding equivalent inputs.

P0 shows what is REAL today honestly:
  - Source file (git lineage), Saved (commit), Supersedes (frontmatter) →
    populated from data.
  - Extraction-metadata (G2), identity (R2), timestamp (R1), source-span (G1),
    contradicts (G4), recalled-in (G3) → attestation-gated fields that don't exist
    in the data yet, rendered as muted "not yet captured" placeholders with
    their gap tag.

The "broken seal" state is data-driven: if ``content_hash_mismatch`` is True
the caller flips the seal/pill to the tamper (oxblood) styling and prepends an
Integrity row. P0 has no content-hash check wired, so it defaults False and the
verified state renders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Badge:
    """A small inline badge next to a row value (e.g. ``policy ✓``)."""

    text: str
    #: "ok" (green), "t" (teal/timestamp), or "gap" (muted dashed) — maps to CSS.
    kind: str = "ok"


@dataclass
class ProvenanceRow:
    """One node in the vertical lineage.

    ``state`` drives the node marker styling:
      - "ok"  → filled accent/green dot (verified, real data)
      - ""    → hollow accent dot (real data, neutral)
      - "gap" → hollow muted dot + italic muted value (not-yet-captured)
    """

    kicker: str
    value: str
    state: str = ""
    #: Optional monospace fragment rendered inside the value (hash/commit/id).
    mono: str | None = None
    #: Optional link target for the value (commit, superseded file).
    href: str | None = None
    #: Gap tag like "G1"/"R2" shown when this is a not-yet-captured field.
    gap_tag: str | None = None
    badges: list[Badge] = field(default_factory=list)


def _first_commit(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Most-recent commit row, if any. history is newest-first per git_tools."""
    return history[0] if history else None


def _oldest_commit(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    return history[-1] if history else None


def build_provenance(
    *,
    file_path: str,
    frontmatter: dict[str, Any],
    history: list[dict[str, Any]],
    recall_count: int = 0,
    last_recalled: str | None = None,
    content_hash_mismatch: bool = False,
) -> list[ProvenanceRow]:
    """Assemble the ordered list of provenance rows for a memory.

    Args:
        file_path: Memory-relative path (for the Source row).
        frontmatter: Parsed YAML frontmatter dict.
        history: git history rows (newest-first), each with at least
            ``commit``/``hash`` and ``date`` keys (shape from ``git_tools.history``).
        recall_count: Number of recorded recalls for the memory's chunks.
        last_recalled: ISO timestamp of the last recall, or None.
        content_hash_mismatch: Reserved for the broken-seal state (P0: False).

    Returns:
        Ordered list of ``ProvenanceRow``. The template renders them top→bottom.
    """
    rows: list[ProvenanceRow] = []

    # ── Integrity (only when tampered) ──────────────────────────────────────
    if content_hash_mismatch:
        rows.append(
            ProvenanceRow(
                kicker="Integrity",
                value="content hash mismatch — seal broken",
                state="warn",
            )
        )

    # ── Source (real: the file path; span is attestation-gated G1) ─────────────
    rows.append(
        ProvenanceRow(
            kicker="Source",
            value=f"{file_path} · span not captured",
            state="gap",
            gap_tag="G1",
        )
    )

    # ── Extracted (attestation-gated G2: extraction model + prompt policy) ─────
    rows.append(
        ProvenanceRow(
            kicker="Extracted",
            value="extraction metadata not captured",
            state="gap",
            gap_tag="G2",
        )
    )

    # ── Attested by (attestation-gated R2: signed identity) ────────────────────
    rows.append(
        ProvenanceRow(
            kicker="Attested by",
            value="identity not captured",
            state="gap",
            gap_tag="R2",
        )
    )

    # ── Timestamp (attestation-gated R1: RFC-3161 trusted timestamp) ───────────
    rows.append(
        ProvenanceRow(
            kicker="Timestamp",
            value="trusted timestamp not captured",
            state="gap",
            gap_tag="R1",
        )
    )

    # ── Saved (real: git commit of the last change) ─────────────────────────
    latest = _first_commit(history)
    if latest is not None:
        commit = str(latest.get("commit") or latest.get("hash") or "")[:7]
        date = str(latest.get("date") or "")[:10]
        rows.append(
            ProvenanceRow(
                kicker="Saved",
                value=f"commit · {date}".strip(" ·"),
                state="ok",
                mono=commit or None,
                href=f"/ui/history/{file_path}" if commit else None,
            )
        )
    else:
        rows.append(
            ProvenanceRow(
                kicker="Saved",
                value="no commit history (memory_dir not a git repo, or unsaved)",
                state="gap",
            )
        )

    # ── Supersedes (real: frontmatter ``supersedes``) ───────────────────────
    supersedes = frontmatter.get("supersedes")
    if supersedes:
        targets = supersedes if isinstance(supersedes, list) else [supersedes]
        target = str(targets[0])
        rows.append(
            ProvenanceRow(
                kicker="Supersedes",
                value=target,
                state="",
                href=f"/ui/memory/{target}",
            )
        )
    else:
        rows.append(ProvenanceRow(kicker="Supersedes", value="nothing", state=""))

    # ── Contradicts (attestation-gated G4) ─────────────────────────────────────
    rows.append(
        ProvenanceRow(
            kicker="Contradicts",
            value="none open",
            state="gap",
            gap_tag="G4",
        )
    )

    # ── Recalled in ─────────────────────────────────────────────────────────
    # When a real recall count exists this is verified data, not a gap — drop
    # the G3 tag (G3 marks the still-uncaptured session-log detail, which only
    # applies to the sparse/zero branch).
    if recall_count and recall_count > 0:
        when = f" · last {str(last_recalled)[:10]}" if last_recalled else ""
        rows.append(
            ProvenanceRow(
                kicker="Recalled in",
                value=f"{recall_count}×{when}",
                state="ok",
            )
        )
    else:
        rows.append(
            ProvenanceRow(
                kicker="Recalled in",
                value="session log sparse",
                state="gap",
                gap_tag="G3",
            )
        )

    return rows

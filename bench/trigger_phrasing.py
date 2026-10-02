"""Labelled measurement of prospective-trigger matching: does any option separate?

Prospective triggers fire when a user's prompt matches a trigger's description
closely enough. In practice they do not fire: real prompts score well below the
0.70-0.75 default threshold against the trigger they are genuinely about, and
lowering the threshold is not obviously safe because off-target prompts reach
into the same band. This rig measures the four options that were proposed, on a
labelled synthetic corpus, before any of them is implemented.

Arms, all scored against the same prompts:

* ``docs_style`` - the description written in third person about the user, the
  way the product's tool descriptions and docs currently teach. The baseline.
* ``user_worded`` - the same intent written in the words a user would type.
* ``phrasings_max`` - three or four user-worded variants per trigger, scored on
  the best of them.
* a **lexical** arm (BM25 over the same description text) alongside each vector
  arm, combined disjunctively, the way hybrid search already works.
* a **relative** decision rule: instead of an absolute floor, fire only the
  best-matching trigger for this prompt, and only when it leads the runner-up
  by a margin.

Nothing is simulated. Descriptions and prompts are embedded through the
product's own ``embedder.embed``; the vector scores come from real
``store.add_trigger`` / ``store.check_triggers`` calls against a throwaway
SQLite store, so the vec0 distance and the product's own L2-to-cosine
conversion are the ones under test. The lexical arm is a real FTS5 index
scored with the product's ``fts_match_expression`` and its BM25 normalization.
Production defaults are never changed, and the module fires nothing in anger:
the temporary store is discarded.

The corpus (``trigger_phrasing.yaml``, versioned) is entirely fictional.

    python -m bench.trigger_phrasing --out results.json
    python -m bench.trigger_phrasing --format markdown --out report.md

It requires a reachable embedding endpoint and refuses to run without one
rather than degrading to synthetic vectors — a separation number computed from
fake vectors would be worse than no number.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

DEFAULT_CORPUS_PATH = Path(__file__).with_name("trigger_phrasing.yaml")

ON_TARGET_STRATA: tuple[str, ...] = ("on_target_terse", "on_target_verbose")
OFF_TARGET_STRATA: tuple[str, ...] = ("near_miss", "unrelated")
STRATA: tuple[str, ...] = ON_TARGET_STRATA + OFF_TARGET_STRATA

DESCRIPTION_ARMS: tuple[str, ...] = ("docs_style", "user_worded", "phrasings_max")

DEFAULT_THRESHOLDS: tuple[float, ...] = (
    0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75,
)
DEFAULT_MARGINS: tuple[float, ...] = (0.00, 0.02, 0.04, 0.06, 0.08, 0.10)
DEFAULT_RELATIVE_FLOOR = 0.40
#: The lexical arm's absolute floor in the disjunction sweep, on the product's
#: per-query BM25 scale. 0.20 was read off the 2026-09-20 run, when that scale
#: was ``|bm25| / 25``; the paired re-run says 0.20 is no longer the same
#: decision (on ``docs_style`` at vector 0.70 it turns 1 off-target pair into
#: 9). 0.30 is where the arm goes back to adding on-target fires for nothing:
#: same vector threshold, 7/56 → 15/56 on-target with off-target unchanged at
#: 1/1120 — three more than the old constant bought on the old scale. See
#: ``bench/results/trigger-phrasing-2026-09-20/README.md``.
DEFAULT_LEXICAL_FLOOR = 0.30


@dataclass(frozen=True)
class TriggerCase:
    """One trigger's intent in each of the description styles under test."""

    trigger_id: str
    memory_file: str
    docs_style: str
    user_worded: str
    phrasings: tuple[str, ...]

    def texts(self, arm: str) -> tuple[str, ...]:
        """The description text(s) this arm registers for the trigger."""
        if arm == "docs_style":
            return (self.docs_style,)
        if arm == "user_worded":
            return (self.user_worded,)
        if arm == "phrasings_max":
            return self.phrasings
        raise ValueError(f"unknown description arm: {arm}")


@dataclass(frozen=True)
class PromptCase:
    """One labelled prompt. ``trigger_id`` is the trigger it is *about*."""

    prompt_id: str
    trigger_id: str
    stratum: str
    text: str

    @property
    def on_target(self) -> bool:
        return self.stratum in ON_TARGET_STRATA


@dataclass(frozen=True)
class Corpus:
    """The versioned fixture: triggers plus their stratified prompts."""

    version: int
    triggers: tuple[TriggerCase, ...]
    prompts: tuple[PromptCase, ...]

    @property
    def trigger_ids(self) -> tuple[str, ...]:
        return tuple(trigger.trigger_id for trigger in self.triggers)

    def stratum_counts(self) -> dict[str, int]:
        counts = {stratum: 0 for stratum in STRATA}
        for prompt in self.prompts:
            counts[prompt.stratum] += 1
        return counts

    def positive_pairs(self) -> int:
        return sum(1 for prompt in self.prompts if prompt.on_target)

    def total_pairs(self) -> int:
        return len(self.prompts) * len(self.triggers)


def load_corpus(path: Path | str = DEFAULT_CORPUS_PATH) -> Corpus:
    """Load and validate the fixture. Raises ``ValueError`` on a bad corpus."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return parse_corpus(raw)


def parse_corpus(raw: Any) -> Corpus:
    """Validate a already-parsed fixture mapping into a :class:`Corpus`."""
    if not isinstance(raw, dict):
        raise ValueError("corpus must be a mapping")
    version_value = raw.get("version")
    if not isinstance(version_value, int):
        raise ValueError("corpus needs an integer `version`")
    entries = raw.get("triggers")
    if not isinstance(entries, list) or not entries:
        raise ValueError("corpus needs a non-empty `triggers` list")

    triggers: list[TriggerCase] = []
    prompts: list[PromptCase] = []
    seen_ids: set[str] = set()
    seen_prompt_text: set[str] = set()

    for entry in entries:
        trigger_id = entry.get("id")
        if not trigger_id:
            raise ValueError("every trigger needs an `id`")
        if trigger_id in seen_ids:
            raise ValueError(f"duplicate trigger id: {trigger_id}")
        seen_ids.add(trigger_id)

        descriptions = entry.get("descriptions") or {}
        phrasings = tuple(descriptions.get("phrasings") or ())
        docs_style = descriptions.get("docs_style", "")
        user_worded = descriptions.get("user_worded", "")
        if not docs_style.strip() or not user_worded.strip():
            raise ValueError(f"{trigger_id}: docs_style and user_worded are required")
        if not 3 <= len(phrasings) <= 4:
            raise ValueError(f"{trigger_id}: needs three or four phrasings")
        if any(not str(text).strip() for text in phrasings):
            raise ValueError(f"{trigger_id}: empty phrasing")

        triggers.append(
            TriggerCase(
                trigger_id=trigger_id,
                memory_file=entry.get("memory_file") or f"decisions/{trigger_id}.md",
                docs_style=docs_style,
                user_worded=user_worded,
                phrasings=phrasings,
            )
        )

        prompt_groups = entry.get("prompts") or {}
        unknown = set(prompt_groups) - set(STRATA)
        if unknown:
            raise ValueError(f"{trigger_id}: unknown prompt strata {sorted(unknown)}")
        for stratum in STRATA:
            texts = prompt_groups.get(stratum) or ()
            if not texts:
                raise ValueError(f"{trigger_id}: stratum {stratum} is empty")
            for index, text in enumerate(texts, start=1):
                if not str(text).strip():
                    raise ValueError(f"{trigger_id}/{stratum}: empty prompt")
                if text in seen_prompt_text:
                    raise ValueError(f"duplicate prompt text: {text!r}")
                seen_prompt_text.add(text)
                prompts.append(
                    PromptCase(
                        prompt_id=f"{trigger_id}/{stratum}/{index:02d}",
                        trigger_id=trigger_id,
                        stratum=stratum,
                        text=text,
                    )
                )

    return Corpus(version=version_value, triggers=tuple(triggers), prompts=tuple(prompts))


# --------------------------------------------------------------------------
# Pair labelling and metrics — pure functions, no model and no store
# --------------------------------------------------------------------------

def label_pairs(
    corpus: Corpus, scores: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    """One labelled row per (prompt, trigger) pair.

    A pair is positive only when the prompt is on-target *and* the trigger is
    the one it is about. An on-target prompt scored against another trigger is
    a negative — firing there is a false fire, not a near miss.
    """
    rows: list[dict[str, Any]] = []
    for prompt in corpus.prompts:
        prompt_scores = scores.get(prompt.prompt_id, {})
        for trigger in corpus.triggers:
            score = prompt_scores.get(trigger.trigger_id)
            if score is None:
                raise ValueError(
                    f"no score for {prompt.prompt_id} x {trigger.trigger_id}"
                )
            rows.append(
                {
                    "prompt_id": prompt.prompt_id,
                    "trigger_id": trigger.trigger_id,
                    "stratum": prompt.stratum,
                    "owner": prompt.trigger_id == trigger.trigger_id,
                    "positive": prompt.on_target and prompt.trigger_id == trigger.trigger_id,
                    "score": float(score),
                }
            )
    return rows


def score_stats(values: Sequence[float]) -> dict[str, float] | None:
    """min / median / mean / max, or ``None`` for an empty sample."""
    if not values:
        return None
    return {
        "n": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": max(values),
    }


def roc_auc(positives: Sequence[float], negatives: Sequence[float]) -> float | None:
    """Rank-based AUC (ties count a half), or ``None`` if either side is empty.

    0.5 is chance. It answers "how often does an on-target pair outscore an
    off-target one", which is the separation question the threshold debate
    keeps assuming an answer to.
    """
    if not positives or not negatives:
        return None
    ordered = sorted([(value, 1) for value in positives] + [(value, 0) for value in negatives])
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        stop = index
        while stop + 1 < len(ordered) and ordered[stop + 1][0] == ordered[index][0]:
            stop += 1
        average_rank = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            if ordered[position][1] == 1:
                rank_sum += average_rank
        index = stop + 1
    n_pos = len(positives)
    n_neg = len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def best_f1_threshold(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The pair-level threshold with the highest F1, and its counts."""
    if not rows:
        return None
    candidates = sorted({row["score"] for row in rows})
    best: dict[str, Any] | None = None
    for threshold in candidates:
        fired = [row for row in rows if row["score"] >= threshold]
        true_positive = sum(1 for row in fired if row["positive"])
        false_positive = len(fired) - true_positive
        total_positive = sum(1 for row in rows if row["positive"])
        false_negative = total_positive - true_positive
        precision = true_positive / len(fired) if fired else 0.0
        recall = true_positive / total_positive if total_positive else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        if best is None or f1 > best["f1"]:
            best = {
                "threshold": threshold,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "true_positive": true_positive,
                "false_positive": false_positive,
                "false_negative": false_negative,
                "positives": total_positive,
            }
    return best


def _prompt_view(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Regroup pair rows by prompt: every trigger score plus the labels."""
    view: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = view.setdefault(
            row["prompt_id"],
            {"stratum": row["stratum"], "owner": None, "scores": {}},
        )
        entry["scores"][row["trigger_id"]] = row["score"]
        if row["owner"]:
            entry["owner"] = row["trigger_id"]
    return view


def threshold_sweep(
    rows: Sequence[dict[str, Any]], thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    """Fire / false-fire behaviour at absolute thresholds, with denominators.

    Two denominators, kept apart on purpose:

    * pair level — how many labelled (prompt, trigger) pairs clear the floor.
    * prompt level — what a session would actually experience: did the right
      memory arrive, and did any wrong one arrive with it.
    """
    view = _prompt_view(rows)
    on_target_prompts = [row for row in view.values() if row["stratum"] in ON_TARGET_STRATA]
    off_target_prompts = [row for row in view.values() if row["stratum"] in OFF_TARGET_STRATA]
    positives = [row for row in rows if row["positive"]]
    negatives = [row for row in rows if not row["positive"]]

    sweep = []
    for threshold in thresholds:
        correct_fires = sum(
            1
            for row in on_target_prompts
            if row["scores"].get(row["owner"], 0.0) >= threshold
        )
        prompts_with_false_fire = sum(
            1
            for row in view.values()
            if any(
                score >= threshold
                for trigger_id, score in row["scores"].items()
                if trigger_id != row["owner"] or row["stratum"] in OFF_TARGET_STRATA
            )
        )
        off_target_prompt_fires = sum(
            1
            for row in off_target_prompts
            if any(score >= threshold for score in row["scores"].values())
        )
        sweep.append(
            {
                "threshold": threshold,
                "on_target_pairs_fired": sum(1 for row in positives if row["score"] >= threshold),
                "on_target_pairs": len(positives),
                "off_target_pairs_fired": sum(
                    1 for row in negatives if row["score"] >= threshold
                ),
                "off_target_pairs": len(negatives),
                "on_target_prompts_fired": correct_fires,
                "on_target_prompts": len(on_target_prompts),
                "prompts_with_false_fire": prompts_with_false_fire,
                "prompts": len(view),
                "off_target_prompts_fired": off_target_prompt_fires,
                "off_target_prompts": len(off_target_prompts),
            }
        )
    return sweep


def margin_sweep(
    rows: Sequence[dict[str, Any]],
    margins: Sequence[float],
    *,
    floor: float = DEFAULT_RELATIVE_FLOOR,
) -> list[dict[str, Any]]:
    """The relative rule: fire the best trigger only if it leads by a margin.

    A pure relative floor (``score >= ratio * best``) is degenerate — the best
    trigger always clears it, so every prompt fires something. The rule
    measured here is the usable form: one winner per prompt, gated on an
    absolute ``floor`` and on the lead over the runner-up.
    """
    view = _prompt_view(rows)
    sweep = []
    for margin in margins:
        correct = 0
        false_fire = 0
        silent = 0
        on_target_total = 0
        off_target_total = 0
        for row in view.values():
            ranked = sorted(row["scores"].items(), key=lambda item: item[1], reverse=True)
            best_id, best_score = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
            on_target = row["stratum"] in ON_TARGET_STRATA
            on_target_total += int(on_target)
            off_target_total += int(not on_target)
            fires = best_score >= floor and (best_score - runner_up) >= margin
            if not fires:
                silent += 1
                continue
            if on_target and best_id == row["owner"]:
                correct += 1
            else:
                false_fire += 1
        sweep.append(
            {
                "margin": margin,
                "floor": floor,
                "correct_fires": correct,
                "on_target_prompts": on_target_total,
                "false_fires": false_fire,
                "off_target_prompts": off_target_total,
                "silent_prompts": silent,
                "prompts": len(view),
            }
        )
    return sweep


def hybrid_sweep(
    vector_rows: Sequence[dict[str, Any]],
    lexical_rows: Sequence[dict[str, Any]],
    thresholds: Sequence[float],
    *,
    lexical_floor: float = DEFAULT_LEXICAL_FLOOR,
) -> list[dict[str, Any]]:
    """Vector OR lexical, the disjunction hybrid search already uses.

    The lexical floor is absolute on the product's normalized BM25 scale, which
    has no relation to cosine — the two arms are not comparable numbers, only
    comparable decisions. ``DEFAULT_LEXICAL_FLOOR`` is re-read from the paired
    run whenever that scale changes; it moved once already, for exactly that
    reason.
    """
    lexical_by_pair = {
        (row["prompt_id"], row["trigger_id"]): row["score"] for row in lexical_rows
    }
    merged = [
        {
            **row,
            "score": row["score"],
            "lexical": lexical_by_pair.get((row["prompt_id"], row["trigger_id"]), 0.0),
        }
        for row in vector_rows
    ]
    sweep = []
    for threshold in thresholds:
        fired = [
            row
            for row in merged
            if row["score"] >= threshold or row["lexical"] >= lexical_floor
        ]
        true_positive = sum(1 for row in fired if row["positive"])
        positives = sum(1 for row in merged if row["positive"])
        sweep.append(
            {
                "threshold": threshold,
                "lexical_floor": lexical_floor,
                "on_target_pairs_fired": true_positive,
                "on_target_pairs": positives,
                "off_target_pairs_fired": len(fired) - true_positive,
                "off_target_pairs": len(merged) - positives,
            }
        )
    return sweep


def summarize_arm(
    rows: Sequence[dict[str, Any]],
    thresholds: Sequence[float],
    margins: Sequence[float],
) -> dict[str, Any]:
    """Distributions, separation and decision behaviour for one arm."""
    positives = [row["score"] for row in rows if row["positive"]]
    negatives = [row["score"] for row in rows if not row["positive"]]
    near_miss = [
        row["score"] for row in rows if row["stratum"] == "near_miss"
    ]
    unrelated = [row["score"] for row in rows if row["stratum"] == "unrelated"]
    cross_trigger = [
        row["score"]
        for row in rows
        if not row["owner"] and row["stratum"] in ON_TARGET_STRATA
    ]
    return {
        "distributions": {
            "on_target": score_stats(positives),
            "off_target": score_stats(negatives),
            "on_target_terse": score_stats(
                [row["score"] for row in rows if row["positive"] and row["stratum"] == "on_target_terse"]
            ),
            "on_target_verbose": score_stats(
                [row["score"] for row in rows if row["positive"] and row["stratum"] == "on_target_verbose"]
            ),
            "near_miss": score_stats(near_miss),
            "unrelated": score_stats(unrelated),
            "other_trigger_on_target_prompt": score_stats(cross_trigger),
        },
        "auc": roc_auc(positives, negatives),
        "best_f1": best_f1_threshold(rows),
        "thresholds": threshold_sweep(rows, thresholds),
        "relative_margin": margin_sweep(rows, margins),
    }


# --------------------------------------------------------------------------
# Scoring against the real store / real FTS5 — needs the embedding endpoint
# --------------------------------------------------------------------------

def _vector_scores(
    corpus: Corpus,
    arm: str,
    description_vectors: dict[tuple[str, str], list[float]],
    prompt_vectors: dict[str, list[float]],
) -> dict[str, dict[str, float]]:
    """Score every prompt against every trigger through the real store.

    Each description variant is registered as its own trigger row with a zero
    threshold, so ``check_triggers`` returns all of them with their scores and
    the arm's score for a trigger is the best of its variants. This is the
    production match path, the production vec0 index and the production
    distance-to-score conversion — not a reimplementation of them.
    """
    from bench import harness
    from palinode.core import store

    scores: dict[str, dict[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix=f"pnbench-trigger-{arm}-") as palinode_dir:
        harness.point_config_at(palinode_dir)
        harness.init_store()
        for trigger in corpus.triggers:
            for index, text in enumerate(trigger.texts(arm)):
                store.add_trigger(
                    f"{trigger.trigger_id}::{index:02d}",
                    text,
                    trigger.memory_file,
                    description_vectors[(trigger.trigger_id, text)],
                    threshold=0.0,
                    cooldown_hours=0,
                )
        for prompt in corpus.prompts:
            best: dict[str, float] = {trigger_id: 0.0 for trigger_id in corpus.trigger_ids}
            for fired in store.check_triggers(
                prompt_vectors[prompt.prompt_id], cooldown_bypass=True
            ):
                trigger_id = fired["id"].split("::", 1)[0]
                best[trigger_id] = max(best[trigger_id], float(fired["score"]))
            scores[prompt.prompt_id] = best
    return scores


def _lexical_scores(corpus: Corpus, arm: str) -> dict[str, dict[str, float]]:
    """BM25 over the same description text, in a real FTS5 index.

    Uses the product's own query expression builder and its BM25 normalization
    (``bm25_query_scale``: this prompt's BM25 as a fraction of what it could
    score against a description holding every one of its terms), so the arm is
    the one hybrid search would contribute, applied to trigger descriptions
    instead of chunks. A score of 1.0 is that reference description; a denser
    one can exceed it.
    """
    from palinode.core.store import bm25_query_scale, fts_match_units

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE trigger_fts USING fts5(trigger_id, body, tokenize='unicode61')"
        )
        for trigger in corpus.triggers:
            for text in trigger.texts(arm):
                connection.execute(
                    "INSERT INTO trigger_fts (trigger_id, body) VALUES (?, ?)",
                    (trigger.trigger_id, text),
                )
        scores: dict[str, dict[str, float]] = {}
        for prompt in corpus.prompts:
            best = {trigger_id: 0.0 for trigger_id in corpus.trigger_ids}
            units = fts_match_units(prompt.text)
            rows = connection.execute(
                "SELECT trigger_id, rank AS bm25 FROM trigger_fts "
                "WHERE trigger_fts MATCH ? ORDER BY rank",
                (" OR ".join(units) or '""',),
            ).fetchall()
            scale = (
                bm25_query_scale(connection, units, table="trigger_fts")
                if rows else 0.0
            )
            for row in rows:
                raw = abs(row["bm25"] or 0.0)
                normalized = raw / scale if scale > 0.0 else 0.0
                best[row["trigger_id"]] = max(best[row["trigger_id"]], normalized)
            scores[prompt.prompt_id] = best
        return scores
    finally:
        connection.close()


def _package_version() -> str:
    try:
        return version("palinode")
    except PackageNotFoundError:
        return "unknown"


def _embed_all(texts: Iterable[str], dimensions: int) -> dict[str, list[float]]:
    from palinode.core import embedder

    vectors: dict[str, list[float]] = {}
    try:
        for text in texts:
            if text in vectors:
                continue
            vector = embedder.embed(text)
            if len(vector) != dimensions:
                raise RuntimeError(
                    f"embedder returned {len(vector)} dimensions; expected {dimensions}"
                )
            vectors[text] = vector
    except embedder.EmbeddingUnavailable as exc:
        raise RuntimeError(
            "the trigger-phrasing evaluation requires the configured embedding "
            "endpoint; it will not substitute synthetic vectors"
        ) from exc
    return vectors


def evaluate(
    *,
    corpus: Corpus | None = None,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    margins: Sequence[float] = DEFAULT_MARGINS,
    lexical_floor: float = DEFAULT_LEXICAL_FLOOR,
) -> dict[str, Any]:
    """Run every arm over the labelled corpus with the real embedder."""
    from palinode.core.config import config

    corpus = corpus or load_corpus()
    thresholds = tuple(float(value) for value in thresholds)
    margins = tuple(float(value) for value in margins)
    if not thresholds or any(value < 0.0 or value > 1.0 for value in thresholds):
        raise ValueError("thresholds must contain values between 0.0 and 1.0")
    if any(value < 0.0 for value in margins):
        raise ValueError("margins must be non-negative")

    dimensions = int(config.embeddings.primary.dimensions)
    description_texts = [
        text
        for trigger in corpus.triggers
        for arm in DESCRIPTION_ARMS
        for text in trigger.texts(arm)
    ]
    prompt_texts = [prompt.text for prompt in corpus.prompts]
    vectors = _embed_all([*description_texts, *prompt_texts], dimensions)

    description_vectors = {
        (trigger.trigger_id, text): vectors[text]
        for trigger in corpus.triggers
        for arm in DESCRIPTION_ARMS
        for text in trigger.texts(arm)
    }
    prompt_vectors = {prompt.prompt_id: vectors[prompt.text] for prompt in corpus.prompts}

    arms: dict[str, Any] = {}
    for arm in DESCRIPTION_ARMS:
        vector_rows = label_pairs(
            corpus, _vector_scores(corpus, arm, description_vectors, prompt_vectors)
        )
        lexical_rows = label_pairs(corpus, _lexical_scores(corpus, arm))
        arms[arm] = {
            "vector": summarize_arm(vector_rows, thresholds, margins),
            "lexical": {
                "auc": roc_auc(
                    [row["score"] for row in lexical_rows if row["positive"]],
                    [row["score"] for row in lexical_rows if not row["positive"]],
                ),
                "distributions": {
                    "on_target": score_stats(
                        [row["score"] for row in lexical_rows if row["positive"]]
                    ),
                    "off_target": score_stats(
                        [row["score"] for row in lexical_rows if not row["positive"]]
                    ),
                },
                "best_f1": best_f1_threshold(lexical_rows),
            },
            "hybrid": hybrid_sweep(
                vector_rows, lexical_rows, thresholds, lexical_floor=lexical_floor
            ),
            "observations": vector_rows,
            "lexical_observations": lexical_rows,
        }

    return {
        "schema_version": 1,
        "environment": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "palinode_version": _package_version(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "embedding_model": config.embeddings.primary.model,
            "embedding_dimensions": dimensions,
        },
        "parameters": {
            "corpus_version": corpus.version,
            "triggers": len(corpus.triggers),
            "prompts": len(corpus.prompts),
            "prompt_strata": corpus.stratum_counts(),
            "labelled_pairs": corpus.total_pairs(),
            "positive_pairs": corpus.positive_pairs(),
            "thresholds": list(thresholds),
            "margins": list(margins),
            "relative_floor": DEFAULT_RELATIVE_FLOOR,
            "lexical_floor": lexical_floor,
            "production_defaults_changed": False,
        },
        "arms": arms,
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _format_stats(stats: dict[str, float] | None) -> str:
    if stats is None:
        return "n/a"
    return (
        f"{stats['min']:.3f} / {stats['median']:.3f} / "
        f"{stats['mean']:.3f} / {stats['max']:.3f}"
    )


def _rate(numerator: int, denominator: int) -> str:
    if not denominator:
        return "0/0"
    return f"{numerator}/{denominator} ({numerator / denominator:.1%})"


def render_markdown(results: dict[str, Any]) -> str:
    """Render the measured arms as reviewable Markdown tables."""
    env = results["environment"]
    params = results["parameters"]
    lines = [
        "# Prospective-trigger phrasing measurement",
        "",
        f"- Generated: {env['generated_at']}",
        f"- Palinode: {env['palinode_version']}",
        f"- Embedder: {env['embedding_model']} ({env['embedding_dimensions']} dimensions)",
        f"- Corpus: version {params['corpus_version']}, synthetic — "
        f"{params['triggers']} triggers, {params['prompts']} prompts, "
        f"{params['labelled_pairs']} labelled pairs "
        f"({params['positive_pairs']} on-target)",
        f"- Prompt strata: {params['prompt_strata']}",
        "- Production defaults changed: **no**",
        "",
        "## Separation",
        "",
        "Score distributions are min / median / mean / max over labelled "
        "(prompt, trigger) pairs. AUC is the probability an on-target pair "
        "outscores an off-target one; 0.5 is chance.",
        "",
        "| Arm | On-target | Off-target | Near-miss | Unrelated | Other trigger, on-target prompt | AUC |",
        "|---|---|---|---|---|---|---:|",
    ]
    for arm, payload in results["arms"].items():
        distributions = payload["vector"]["distributions"]
        auc = payload["vector"]["auc"]
        auc_text = f"{auc:.3f}" if auc is not None else "n/a"
        lines.append(
            f"| {arm} "
            f"| {_format_stats(distributions['on_target'])} "
            f"| {_format_stats(distributions['off_target'])} "
            f"| {_format_stats(distributions['near_miss'])} "
            f"| {_format_stats(distributions['unrelated'])} "
            f"| {_format_stats(distributions['other_trigger_on_target_prompt'])} "
            f"| {auc_text} |"
        )

    lines.extend(["", "### Terse versus verbose on-target prompts", "",
                  "| Arm | Terse | Verbose |", "|---|---|---|"])
    for arm, payload in results["arms"].items():
        distributions = payload["vector"]["distributions"]
        lines.append(
            f"| {arm} "
            f"| {_format_stats(distributions['on_target_terse'])} "
            f"| {_format_stats(distributions['on_target_verbose'])} |"
        )

    lines.extend(["", "### Best achievable absolute threshold, per arm", "",
                  "| Arm | Threshold | Precision | Recall | F1 | TP | FP | FN |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for arm, payload in results["arms"].items():
        best = payload["vector"]["best_f1"]
        if best is None:
            continue
        lines.append(
            f"| {arm} | {best['threshold']:.3f} | {best['precision']:.3f} "
            f"| {best['recall']:.3f} | {best['f1']:.3f} | {best['true_positive']} "
            f"| {best['false_positive']} | {best['false_negative']} |"
        )

    for arm, payload in results["arms"].items():
        lines.extend(
            [
                "",
                f"## {arm}: absolute threshold sweep",
                "",
                "| Threshold | On-target pairs firing | Off-target pairs firing | "
                "On-target prompts delivered | Prompts with a false fire |",
                "|---:|---|---|---|---|",
            ]
        )
        for row in payload["vector"]["thresholds"]:
            lines.append(
                f"| {row['threshold']:.2f} "
                f"| {_rate(row['on_target_pairs_fired'], row['on_target_pairs'])} "
                f"| {_rate(row['off_target_pairs_fired'], row['off_target_pairs'])} "
                f"| {_rate(row['on_target_prompts_fired'], row['on_target_prompts'])} "
                f"| {_rate(row['prompts_with_false_fire'], row['prompts'])} |"
            )

    lines.extend(
        [
            "",
            "## Relative rule: one winner per prompt, gated on its lead",
            "",
            f"Absolute floor {results['parameters']['relative_floor']:.2f}; a margin "
            "of 0.00 is the pure relative rule (the best trigger always wins).",
            "",
            "| Arm | Margin | Correct fires | False fires | Silent prompts |",
            "|---|---:|---|---|---|",
        ]
    )
    for arm, payload in results["arms"].items():
        for row in payload["vector"]["relative_margin"]:
            lines.append(
                f"| {arm} | {row['margin']:.2f} "
                f"| {_rate(row['correct_fires'], row['on_target_prompts'])} "
                f"| {_rate(row['false_fires'], row['prompts'])} "
                f"| {_rate(row['silent_prompts'], row['prompts'])} |"
            )

    lines.extend(
        [
            "",
            "## Lexical arm (BM25 over the same description text)",
            "",
            "| Arm | On-target | Off-target | AUC | Best F1 threshold | Precision | Recall |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for arm, payload in results["arms"].items():
        lexical = payload["lexical"]
        best = lexical["best_f1"]
        auc_text = f"{lexical['auc']:.3f}" if lexical["auc"] is not None else "n/a"
        if best is None:
            lines.append(f"| {arm} | n/a | n/a | {auc_text} | n/a | n/a | n/a |")
            continue
        lines.append(
            f"| {arm} "
            f"| {_format_stats(lexical['distributions']['on_target'])} "
            f"| {_format_stats(lexical['distributions']['off_target'])} "
            f"| {auc_text} | {best['threshold']:.3f} "
            f"| {best['precision']:.3f} | {best['recall']:.3f} |"
        )

    lines.extend(
        [
            "",
            "## Hybrid: vector OR lexical",
            "",
            f"Lexical floor {results['parameters']['lexical_floor']:.2f} on the "
            "product's normalized BM25 scale.",
            "",
            "| Arm | Vector threshold | On-target pairs firing | Off-target pairs firing |",
            "|---|---:|---|---|",
        ]
    )
    for arm, payload in results["arms"].items():
        for row in payload["hybrid"]:
            lines.append(
                f"| {arm} | {row['threshold']:.2f} "
                f"| {_rate(row['on_target_pairs_fired'], row['on_target_pairs'])} "
                f"| {_rate(row['off_target_pairs_fired'], row['off_target_pairs'])} |"
            )

    lines.append("")
    return "\n".join(lines)


def _csv_floats(raw: str) -> tuple[float, ...]:
    try:
        return tuple(float(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated numbers") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure prospective-trigger phrasing options on a labelled corpus"
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--thresholds", type=_csv_floats, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--margins", type=_csv_floats, default=DEFAULT_MARGINS)
    parser.add_argument("--lexical-floor", type=float, default=DEFAULT_LEXICAL_FLOOR)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    try:
        results = evaluate(
            corpus=load_corpus(args.corpus),
            thresholds=args.thresholds,
            margins=args.margins,
            lexical_floor=args.lexical_floor,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = (
        render_markdown(results)
        if args.format == "markdown"
        else json.dumps(results, indent=2, sort_keys=True)
    )
    if args.out is None:
        print(payload)
    else:
        args.out.write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

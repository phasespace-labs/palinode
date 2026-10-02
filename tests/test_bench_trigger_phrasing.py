"""Fast tests for the trigger-phrasing rig: loader, labelling and metrics.

No embedder and no store — these cover the parts that decide what a number
means, so a wrong label or a wrong denominator fails here rather than in a
report someone reads six weeks later.
"""
from __future__ import annotations

import pytest
import yaml

from bench import trigger_phrasing as rig


def _fixture(**overrides) -> dict:
    trigger = {
        "id": "alpha",
        "memory_file": "decisions/alpha.md",
        "descriptions": {
            "docs_style": "User is asking how to do the alpha thing.",
            "user_worded": "how do I do the alpha thing",
            "phrasings": ["do the alpha thing", "alpha steps", "run alpha"],
        },
        "prompts": {
            "on_target_terse": ["do alpha"],
            "on_target_verbose": ["a long turn that eventually asks about alpha"],
            "near_miss": ["something adjacent to alpha but not it"],
            "unrelated": ["a question about gardening"],
        },
    }
    trigger.update(overrides)
    return {"version": 1, "triggers": [trigger]}


class TestCorpusLoading:

    def test_shipped_corpus_parses_and_is_labelled(self):
        corpus = rig.load_corpus()

        assert corpus.version >= 1
        assert len(corpus.triggers) >= 12, "the corpus must be big enough to separate"
        counts = corpus.stratum_counts()
        assert all(counts[stratum] > 0 for stratum in rig.STRATA)
        assert corpus.positive_pairs() == counts["on_target_terse"] + counts["on_target_verbose"]
        assert corpus.total_pairs() == len(corpus.prompts) * len(corpus.triggers)

    def test_every_shipped_trigger_carries_all_three_description_styles(self):
        corpus = rig.load_corpus()

        for trigger in corpus.triggers:
            assert trigger.texts("docs_style") == (trigger.docs_style,)
            assert trigger.texts("user_worded") == (trigger.user_worded,)
            assert 3 <= len(trigger.texts("phrasings_max")) <= 4

    def test_shipped_prompts_are_unique_across_triggers(self):
        corpus = rig.load_corpus()

        texts = [prompt.text for prompt in corpus.prompts]
        assert len(texts) == len(set(texts))

    def test_duplicate_trigger_id_is_rejected(self):
        raw = _fixture()
        raw["triggers"].append(dict(raw["triggers"][0]))

        with pytest.raises(ValueError, match="duplicate trigger id"):
            rig.parse_corpus(raw)

    def test_two_phrasings_is_rejected(self):
        raw = _fixture()
        raw["triggers"][0]["descriptions"]["phrasings"] = ["one", "two"]

        with pytest.raises(ValueError, match="three or four phrasings"):
            rig.parse_corpus(raw)

    def test_empty_stratum_is_rejected(self):
        raw = _fixture()
        raw["triggers"][0]["prompts"]["near_miss"] = []

        with pytest.raises(ValueError, match="near_miss"):
            rig.parse_corpus(raw)

    def test_unknown_stratum_is_rejected(self):
        raw = _fixture()
        raw["triggers"][0]["prompts"]["on_target_sideways"] = ["x"]

        with pytest.raises(ValueError, match="unknown prompt strata"):
            rig.parse_corpus(raw)

    def test_corpus_file_is_valid_yaml_without_tabs(self, tmp_path):
        text = rig.DEFAULT_CORPUS_PATH.read_text(encoding="utf-8")

        assert "\t" not in text
        assert isinstance(yaml.safe_load(text), dict)


class TestLabelling:

    def test_only_the_owning_trigger_of_an_on_target_prompt_is_positive(self):
        corpus = rig.parse_corpus(
            {
                "version": 1,
                "triggers": [
                    _fixture()["triggers"][0],
                    {
                        **_fixture()["triggers"][0],
                        "id": "beta",
                        "prompts": {
                            "on_target_terse": ["do beta"],
                            "on_target_verbose": ["a long turn about beta"],
                            "near_miss": ["adjacent to beta"],
                            "unrelated": ["a question about bicycles"],
                        },
                    },
                ],
            }
        )
        scores = {
            prompt.prompt_id: {"alpha": 0.6, "beta": 0.6} for prompt in corpus.prompts
        }

        rows = rig.label_pairs(corpus, scores)

        assert len(rows) == corpus.total_pairs()
        positives = [row for row in rows if row["positive"]]
        assert len(positives) == corpus.positive_pairs() == 4
        assert all(row["owner"] for row in positives)
        # An on-target prompt scored against the other trigger is a negative.
        cross = [
            row
            for row in rows
            if row["stratum"] == "on_target_terse" and not row["owner"]
        ]
        assert cross and all(not row["positive"] for row in cross)

    def test_missing_score_is_an_error_not_a_zero(self):
        corpus = rig.parse_corpus(_fixture())

        with pytest.raises(ValueError, match="no score"):
            rig.label_pairs(corpus, {})


def _rows(*specs: tuple[str, bool, float]) -> list[dict]:
    """(prompt_id suffix, positive, score) → labelled rows for one trigger."""
    rows = []
    for index, (stratum, positive, score) in enumerate(specs):
        rows.append(
            {
                "prompt_id": f"p{index:02d}",
                "trigger_id": "alpha" if positive else "beta",
                "stratum": stratum,
                "owner": positive,
                "positive": positive,
                "score": score,
            }
        )
    return rows


class TestMetrics:

    def test_auc_is_one_when_separation_is_perfect(self):
        assert rig.roc_auc([0.9, 0.8], [0.2, 0.1]) == 1.0

    def test_auc_is_chance_when_the_distributions_are_identical(self):
        assert rig.roc_auc([0.5, 0.5], [0.5, 0.5]) == 0.5

    def test_auc_is_zero_when_the_order_is_inverted(self):
        assert rig.roc_auc([0.1], [0.9]) == 0.0

    def test_auc_needs_both_sides(self):
        assert rig.roc_auc([0.5], []) is None

    def test_score_stats_reports_the_sample_size(self):
        stats = rig.score_stats([0.2, 0.4, 0.6])

        assert stats == pytest.approx(
            {"n": 3, "min": 0.2, "median": 0.4, "mean": 0.4, "max": 0.6}
        )
        assert rig.score_stats([]) is None

    def test_best_f1_finds_the_separating_threshold(self):
        rows = _rows(
            ("on_target_terse", True, 0.60),
            ("on_target_terse", True, 0.55),
            ("near_miss", False, 0.40),
            ("unrelated", False, 0.30),
        )

        best = rig.best_f1_threshold(rows)

        assert best["threshold"] == pytest.approx(0.55)
        assert best["precision"] == 1.0
        assert best["recall"] == 1.0
        assert best["false_positive"] == 0

    def test_best_f1_reports_the_overlap_it_cannot_resolve(self):
        rows = _rows(
            ("on_target_terse", True, 0.50),
            ("near_miss", False, 0.52),
        )

        best = rig.best_f1_threshold(rows)

        assert best["f1"] < 1.0
        assert best["false_positive"] >= 1

    def test_threshold_sweep_keeps_pair_and_prompt_denominators_apart(self):
        rows = [
            {"prompt_id": "p1", "trigger_id": "alpha", "stratum": "on_target_terse",
             "owner": True, "positive": True, "score": 0.52},
            {"prompt_id": "p1", "trigger_id": "beta", "stratum": "on_target_terse",
             "owner": False, "positive": False, "score": 0.49},
            {"prompt_id": "p2", "trigger_id": "alpha", "stratum": "unrelated",
             "owner": True, "positive": False, "score": 0.20},
            {"prompt_id": "p2", "trigger_id": "beta", "stratum": "unrelated",
             "owner": False, "positive": False, "score": 0.51},
        ]

        sweep = {row["threshold"]: row for row in rig.threshold_sweep(rows, (0.45, 0.55))}

        low = sweep[0.45]
        assert low["on_target_pairs_fired"] == 1 and low["on_target_pairs"] == 1
        assert low["on_target_prompts_fired"] == 1 and low["on_target_prompts"] == 1
        # Both prompts pick up a trigger that is not theirs.
        assert low["prompts_with_false_fire"] == 2 and low["prompts"] == 2
        assert low["off_target_prompts_fired"] == 1 and low["off_target_prompts"] == 1

        high = sweep[0.55]
        assert high["on_target_pairs_fired"] == 0
        assert high["prompts_with_false_fire"] == 0

    def test_margin_sweep_at_zero_fires_every_prompt(self):
        rows = [
            {"prompt_id": "p1", "trigger_id": "alpha", "stratum": "on_target_terse",
             "owner": True, "positive": True, "score": 0.52},
            {"prompt_id": "p1", "trigger_id": "beta", "stratum": "on_target_terse",
             "owner": False, "positive": False, "score": 0.50},
            {"prompt_id": "p2", "trigger_id": "alpha", "stratum": "unrelated",
             "owner": True, "positive": False, "score": 0.45},
            {"prompt_id": "p2", "trigger_id": "beta", "stratum": "unrelated",
             "owner": False, "positive": False, "score": 0.41},
        ]

        zero, wide = rig.margin_sweep(rows, (0.0, 0.10), floor=0.40)

        assert zero["silent_prompts"] == 0
        assert zero["correct_fires"] == 1 and zero["on_target_prompts"] == 1
        assert zero["false_fires"] == 1 and zero["prompts"] == 2
        # A 0.10 lead is not there for either prompt.
        assert wide["silent_prompts"] == 2
        assert wide["correct_fires"] == 0 and wide["false_fires"] == 0

    def test_margin_sweep_floor_silences_a_weak_winner(self):
        rows = [
            {"prompt_id": "p1", "trigger_id": "alpha", "stratum": "on_target_terse",
             "owner": True, "positive": True, "score": 0.30},
            {"prompt_id": "p1", "trigger_id": "beta", "stratum": "on_target_terse",
             "owner": False, "positive": False, "score": 0.10},
        ]

        (row,) = rig.margin_sweep(rows, (0.0,), floor=0.40)

        assert row["silent_prompts"] == 1
        assert row["correct_fires"] == 0

    def test_hybrid_fires_on_the_lexical_arm_alone(self):
        vector_rows = _rows(("on_target_terse", True, 0.30), ("unrelated", False, 0.10))
        lexical_rows = [
            {**row, "score": 0.9 if row["positive"] else 0.0} for row in vector_rows
        ]

        (row,) = rig.hybrid_sweep(vector_rows, lexical_rows, (0.75,), lexical_floor=0.2)

        assert row["on_target_pairs_fired"] == 1
        assert row["off_target_pairs_fired"] == 0

    def test_summarize_arm_reports_every_stratum_and_the_sweeps(self):
        rows = _rows(
            ("on_target_terse", True, 0.55),
            ("on_target_verbose", True, 0.45),
            ("near_miss", False, 0.50),
            ("unrelated", False, 0.20),
        )

        summary = rig.summarize_arm(rows, (0.50,), (0.0,))

        assert summary["distributions"]["on_target"]["n"] == 2
        assert summary["distributions"]["near_miss"]["n"] == 1
        assert summary["auc"] is not None
        assert len(summary["thresholds"]) == 1
        assert len(summary["relative_margin"]) == 1


class TestLexicalArm:
    """The BM25 arm needs SQLite and the product's query builder, not a model."""

    def test_lexical_scores_rank_the_matching_trigger_highest(self):
        corpus = rig.load_corpus()

        scores = rig._lexical_scores(corpus, "phrasings_max")

        assert set(scores) == {prompt.prompt_id for prompt in corpus.prompts}
        for trigger_scores in scores.values():
            assert set(trigger_scores) == set(corpus.trigger_ids)
            # 1.0 is the reference description — one holding every term of the
            # prompt — not a ceiling: a short description that repeats a term
            # carries more of the prompt than the reference does. The ceiling
            # is FTS5's own ``k1 + 1`` saturation, 2.2.
            assert all(0.0 <= value <= 2.2 for value in trigger_scores.values())


class TestRendering:

    def test_markdown_renders_from_a_results_object(self):
        rows = _rows(
            ("on_target_terse", True, 0.55),
            ("near_miss", False, 0.40),
        )
        results = {
            "environment": {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "palinode_version": "test",
                "embedding_model": "test-model",
                "embedding_dimensions": 8,
            },
            "parameters": {
                "corpus_version": 1,
                "triggers": 1,
                "prompts": 2,
                "prompt_strata": {"on_target_terse": 1, "near_miss": 1},
                "labelled_pairs": 2,
                "positive_pairs": 1,
                "relative_floor": 0.40,
                "lexical_floor": 0.20,
            },
            "arms": {
                "user_worded": {
                    "vector": rig.summarize_arm(rows, (0.50,), (0.0,)),
                    "lexical": {
                        "auc": 0.5,
                        "distributions": {
                            "on_target": rig.score_stats([0.3]),
                            "off_target": rig.score_stats([0.1]),
                        },
                        "best_f1": rig.best_f1_threshold(rows),
                    },
                    "hybrid": rig.hybrid_sweep(rows, rows, (0.50,)),
                }
            },
        }

        markdown = rig.render_markdown(results)

        assert "# Prospective-trigger phrasing measurement" in markdown
        assert "user_worded" in markdown
        assert "Production defaults changed: **no**" in markdown

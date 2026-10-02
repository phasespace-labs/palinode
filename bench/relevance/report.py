"""Render a relevance/abstention results object as Markdown.

Every table states its denominator, and an arm that did not run is printed as
NOT RUN with its reason rather than omitted — a missing row and a row nobody
wrote look identical six months later.
"""
from __future__ import annotations

import json
from typing import Any

from bench.relevance.corpus import CLASSES


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _arm_label(arm: dict[str, Any]) -> str:
    suffix = " + abstain" if arm.get("abstain") else ""
    if arm["mode"] == "lexical":
        return f"lexical (keyword-only){suffix}"
    return f"hybrid @ cosine floor {arm['threshold']:.2f}{suffix}"


def _ran(results: dict[str, Any]) -> list[dict[str, Any]]:
    return [arm for arm in results["arms"] if arm["status"] == "ran"]


def _header(results: dict[str, Any]) -> list[str]:
    env = results["environment"]
    params = results["parameters"]
    index = results["index"]
    counts = params["class_counts"]
    return [
        "# Palinode relevance & abstention baseline",
        "",
        "Measured against whatever the working tree does — the rig changes "
        "nothing itself, so a run is a baseline or a re-measurement depending "
        "only on when it was taken. Production search defaults changed by this "
        "run: "
        f"`{params['production_defaults_changed']}`.",
        "",
        f"- Generated: {env['generated_at']}",
        f"- Palinode: {env['palinode_version']} · Python {env['python_version']} · {env['platform']}",
        f"- Embedding model: {env['embedding_model']} "
        f"({env['embedding_dimensions']} dimensions) · reachable: "
        f"**{'yes' if env['embedder_reachable'] else 'no'}**",
        f"- Corpus v{params['corpus_version']} · question set v{params['question_set_version']}",
        f"- {params['records']} records → {index['files']} files → {index['chunks']} chunks, "
        f"{index['vectors']} embedded ({index['chunks_without_vector']} keyword-only)",
        f"- {params['questions']} questions · top-k {params['top_k']} · "
        f"fts_threshold {params['fts_threshold']:.2f} · hybrid_weight "
        f"{params['hybrid_weight']:.2f} · snippet cap {params['snippet_max_chars']} chars",
        f"- Keyword-only floor {params.get('lexical_fts_threshold', 0.0):.2f} · "
        f"max chunks per file {params.get('max_chunks_per_file', 0)} "
        f"(0 = unlimited)",
        f"- Vector relative floor {params.get('vector_relative_floor', 0.0):.2f} "
        f"(0 = none)",
        f"- Timing passes per question: {params['repeats']}",
        "",
        "## Question set",
        "",
        "| Class | base | paraphrase | identifier | total |",
        "|---|---:|---:|---:|---:|",
        *[
            f"| {cls} | {counts[cls]['base']} | {counts[cls]['paraphrase']} "
            f"| {counts[cls]['identifier']} | {counts[cls]['total']} |"
            for cls in CLASSES
            if cls in counts
        ],
        "",
        "Paraphrases and identifier variants are the held-out split: a "
        "paraphrase shares few content words with the record that answers it, "
        "and an identifier query is a bare tag, config key or filed number.",
        "",
    ]


def _what_ran(results: dict[str, Any]) -> list[str]:
    lines = ["## What ran, and what did not", ""]
    for arm in results["arms"]:
        if arm["status"] == "ran":
            lines.append(f"- **{_arm_label(arm)}** — ran.")
        else:
            lines.append(f"- **{_arm_label(arm)}** — **NOT RUN**: {arm['reason']}")
    lines.append("")
    return lines


def _headline(results: dict[str, Any]) -> list[str]:
    arms = _ran(results)
    if not arms:
        return ["## Headline", "", "No arm ran.", ""]
    lines = [
        "## Headline",
        "",
        "| Metric | " + " | ".join(_arm_label(arm) for arm in arms) + " |",
        "|---|" + "---:|" * len(arms),
    ]

    def row(label: str, render) -> str:
        return f"| {label} | " + " | ".join(render(arm["summary"]) for arm in arms) + " |"

    lines += [
        row("Relevant-hit recall@k", lambda s: _pct(s["relevant"]["recall_at_k"])),
        row(
            "Answerable questions with a relevant hit",
            lambda s: f"{s['relevant']['questions_with_a_relevant_hit']}/{s['answerable_questions']}",
        ),
        row(
            "Top-1 correct",
            lambda s: f"{s['relevant']['top1_relevant']}/{s['relevant']['top1_denominator']}"
            f" ({_pct(s['relevant']['top1_accuracy'])})",
        ),
        row(
            "Irrelevant injections (of results delivered)",
            lambda s: f"{s['irrelevant_injections']['results']}/{s['irrelevant_injections']['result_denominator']}"
            f" ({_pct(s['irrelevant_injections']['rate'])})",
        ),
        row(
            "Footer-only hits",
            lambda s: f"{s['footer_only']['results']}/{s['footer_only']['result_denominator']}"
            f" ({_pct(s['footer_only']['rate'])})",
        ),
        row(
            "Redundant results (near-duplicate records)",
            lambda s: f"{s['redundant']['results']}/{s['redundant']['result_denominator']}"
            f" ({_pct(s['redundant']['rate'])})",
        ),
        row(
            "Repeat slots for one source file",
            lambda s: f"{s['same_file_repeats']['results']}/{s['same_file_repeats']['result_denominator']}"
            f" ({_pct(s['same_file_repeats']['rate'])})",
        ),
        row(
            "Project-isolation violations",
            lambda s: f"{s['project_isolation']['violations']}/{s['project_isolation']['result_denominator']}"
            f" ({_pct(s['project_isolation']['rate'])})",
        ),
        row(
            "Correct abstention (no-answer questions)",
            lambda s: f"{s['abstention']['abstained']}/{s['abstention']['no_answer_questions']}"
            f" ({_pct(s['abstention']['abstention_rate'])})",
        ),
        row(
            "Inappropriate confident match",
            lambda s: f"{s['abstention']['confident_match']}/{s['abstention']['no_answer_questions']}"
            f" ({_pct(s['abstention']['confident_match_rate'])})",
        ),
        row(
            "Confidence band (median top score of a correct top-1)",
            lambda s: _num(s["abstention"]["confidence_band"], 3),
        ),
        row(
            "Distinct top scores behind that band",
            lambda s: str(s["abstention"]["band_distinct_values"]),
        ),
        row(
            "Recency trap ranked first",
            lambda s: f"{s['recency_trap']['trap_ranked_first']}/{s['recency_trap']['questions_with_a_trap']}"
            f" ({_pct(s['recency_trap']['rate'])})",
        ),
        row(
            "Useful-context tokens (of delivered payload)",
            lambda s: f"{s['context_tokens']['useful']}/{s['context_tokens']['payload']}"
            f" ({_pct(s['context_tokens']['useful_fraction'])})",
        ),
        row(
            "Payload tokens per question",
            lambda s: _num(s["context_tokens"]["payload_per_question"]),
        ),
    ]
    lines += [
        "| p50 latency (ms) | "
        + " | ".join(_num(arm["latency_ms"]["p50"], 2) for arm in arms)
        + " |",
        "| p95 latency (ms) | "
        + " | ".join(_num(arm["latency_ms"]["p95"], 2) for arm in arms)
        + " |",
        "| Keyword-fallback rate (of calls) | "
        + " | ".join(
            f"{arm['fallback']['keyword_fallback']}/{arm['fallback']['calls']}"
            f" ({_pct(arm['fallback']['keyword_fallback_rate'])})"
            for arm in arms
        )
        + " |",
        "| Calls with no BM25 candidate | "
        + " | ".join(str(arm["fallback"]["no_keyword_candidates"]) for arm in arms)
        + " |",
        "",
        "Latency covers the whole client-visible path: the query embedding where "
        "there is one, the store search, and rendering the delivered payload. "
        "The lexical arm builds no query vector, so its keyword-fallback count "
        "is zero by construction rather than by measurement.",
        "",
        "Read *inappropriate confident match* together with the row above it. "
        "The band is the median score a correct top-1 carries; when only one "
        "distinct score sits behind it, the delivered score is a function of "
        "rank rather than of relevance and the band cannot separate anything. "
        "In that case the number to act on is the abstention rate, not the "
        "confident-match rate.",
        "",
        "No project filter, category filter or context was passed on any call: "
        "these are the arguments an unadorned `palinode_search` sends, which is "
        "what makes project isolation a property of ranking here rather than of "
        "a filter the caller remembered to set.",
        "",
    ]
    return lines


def _confidence(results: dict[str, Any]) -> list[str]:
    """The confidence verdict's own confusion matrix, per arm.

    Omitted whole when no arm carries one — a results file recorded before the
    verdict existed still renders, and renders without an empty table implying
    it measured something.
    """
    arms = [arm for arm in _ran(results) if arm["summary"].get("confidence_verdict")]
    if not arms:
        return []
    lines = [
        "## Confidence verdict",
        "",
        "The delivery-level verdict, read from the pre-fusion arm scores, "
        "crossed with whether the question had an answer in the corpus. The "
        "two cells to read are `confident` under *no-answer* (a confident "
        "wrong answer) and `none` under *answerable* (a refusal to answer "
        "something the store holds) — the second is qualified by how many of "
        "those slates contained a relevant record at all.",
        "",
    ]
    for arm in arms:
        matrix = arm["summary"]["confidence_verdict"]
        lines += [
            f"### {_arm_label(arm)}",
            "",
            "| Verdict | answerable | of those, slate held a relevant record | no-answer |",
            "|---|---:|---:|---:|",
        ]
        for verdict, cell in matrix["by_verdict"].items():
            lines.append(
                f"| {verdict} | {cell['answerable']} "
                f"| {cell['answerable_with_a_relevant_hit']} | {cell['no_answer']} |"
            )
        lines += ["", f"Graded deliveries: {matrix['graded']}.", ""]
    return lines


def _per_class(results: dict[str, Any]) -> list[str]:
    lines = ["## Per class", ""]
    for arm in _ran(results):
        lines += [
            f"### {_arm_label(arm)}",
            "",
            "| Class | questions | recall@k | top-1 | injections / results | "
            "footer-only | redundant | same-file | isolation | abstained | trap first |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for cls, summary in arm["by_class"].items():
            lines.append(
                f"| {cls} | {summary['questions']} "
                f"| {_pct(summary['relevant']['recall_at_k'])} "
                f"| {summary['relevant']['top1_relevant']}/{summary['relevant']['top1_denominator']} "
                f"| {summary['irrelevant_injections']['results']}/{summary['irrelevant_injections']['result_denominator']} "
                f"| {summary['footer_only']['results']} "
                f"| {summary['redundant']['results']} "
                f"| {summary['same_file_repeats']['results']} "
                f"| {summary['project_isolation']['violations']} "
                f"| {summary['abstention']['abstained']}/{summary['abstention']['no_answer_questions']} "
                f"| {summary['recency_trap']['trap_ranked_first']}/{summary['recency_trap']['questions_with_a_trap']} |"
            )
        lines.append("")
    return lines


def _per_variant(results: dict[str, Any]) -> list[str]:
    lines = ["## Base versus held-out", ""]
    for arm in _ran(results):
        lines += [
            f"### {_arm_label(arm)}",
            "",
            "| Variant | questions | recall@k | top-1 | injections / results |",
            "|---|---:|---:|---:|---:|",
        ]
        for variant, summary in arm["by_variant"].items():
            lines.append(
                f"| {variant} | {summary['questions']} "
                f"| {_pct(summary['relevant']['recall_at_k'])} "
                f"| {summary['relevant']['top1_relevant']}/{summary['relevant']['top1_denominator']} "
                f"| {summary['irrelevant_injections']['results']}/{summary['irrelevant_injections']['result_denominator']} |"
            )
        lines.append("")
    return lines


def _failures(results: dict[str, Any]) -> list[str]:
    """Name the questions that failed, so a follow-up has somewhere to start."""
    lines = ["## Where it failed, by question", ""]
    for arm in _ran(results):
        misses = [
            obs
            for obs in arm["observations"]
            if obs["score"]["answerable"] and not obs["score"]["relevant_found"]
        ]
        traps = [obs for obs in arm["observations"] if obs["score"]["recency_trap_top1"]]
        footer = [obs for obs in arm["observations"] if obs["score"]["footer_only_hits"]]
        leaks = [obs for obs in arm["observations"] if obs["score"]["isolation_violations"]]
        confident = [
            obs
            for obs in arm["observations"]
            if not obs["score"]["answerable"] and obs["score"]["returned"]
        ]
        lines += [
            f"### {_arm_label(arm)}",
            "",
            f"- No relevant hit at all ({len(misses)}): "
            + (", ".join(obs["question_id"] for obs in misses) or "none"),
            f"- Recency trap ranked first ({len(traps)}): "
            + (", ".join(obs["question_id"] for obs in traps) or "none"),
            f"- Footer-only hit in the slate ({len(footer)}): "
            + (", ".join(obs["question_id"] for obs in footer) or "none"),
            f"- Cross-project result ({len(leaks)}): "
            + (", ".join(obs["question_id"] for obs in leaks) or "none"),
            f"- No-answer question that returned something ({len(confident)}): "
            + (", ".join(obs["question_id"] for obs in confident) or "none"),
            "",
        ]
    return lines


def render(results: dict[str, Any]) -> str:
    """Render the whole results object as a Markdown report."""
    lines = _header(results)
    lines += _what_ran(results)
    lines += _headline(results)
    lines += _confidence(results)
    lines += _per_class(results)
    lines += _per_variant(results)
    lines += _failures(results)
    return "\n".join(lines).rstrip() + "\n"


def write_json(results: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, sort_keys=True)
        handle.write("\n")


__all__ = ["render", "write_json"]

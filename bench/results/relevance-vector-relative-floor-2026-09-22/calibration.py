"""Pick the vector arm's relative floor from recorded per-result cosines.

    python bench/results/relevance-vector-relative-floor-2026-09-22/calibration.py

Reads ``calibration-rows.json`` beside this file — every result the relevance
rig delivered on its two hybrid arms, with the raw cosine the vector arm scored
it, the normalized BM25 the keyword arm scored it (``null`` when that arm never
retrieved it), and the fixture's relevance label. Nothing here touches a store,
an embedder or the network: the rows are the measurement, this is the arithmetic
over them.

Two simulations bracket the effect, because a floor applied before fusion
changes more than the rows it removes:

``cut A``
    Every delivered row whose cosine falls below ``floor x best cosine`` leaves
    the vector arm. The UPPER bound on the slate cut: it ignores that a row the
    keyword arm also retrieved is re-admitted by that arm, since a candidate
    needs only one arm.

``cut B``
    Only rows the vector arm alone admitted leave the fused candidate set. The
    LOWER bound: it ignores that freed slots can be refilled by candidates that
    were ranked outside the delivered slate.

The true slate lies between them and is only settled by re-running the arms on
a host that reaches the embedder. What the two agree on is the part that
decides the default: which floor is the last one that loses no relevant result.

Payload columns are estimates (each question's recorded payload scaled by the
share of its rows that survive), marked as such wherever they are quoted.
"""
from __future__ import annotations

import json
from pathlib import Path

FLOORS = (0.0, 0.80, 0.85, 0.88, 0.90, 0.95)
ROWS = Path(__file__).with_name("calibration-rows.json")


def _ratios(question: dict) -> tuple[float | None, list[dict]]:
    cosines = [d["cosine"] for d in question["delivered"] if d["cosine"] is not None]
    return (max(cosines) if cosines else None), question["delivered"]


def _sweep(arm: dict, cut: str) -> list[dict]:
    out = []
    for floor in FLOORS:
        kept = injections = found = expected = useful = 0
        payload = 0.0
        lost: list[str] = []
        for question in arm["questions"]:
            best, delivered = _ratios(question)
            keep, dropped = [], []
            for row in delivered:
                drop = (
                    row["cosine"] is not None
                    and best
                    and row["cosine"] < floor * best
                    and (cut == "A" or row["keyword"] is None)
                )
                (dropped if drop else keep).append(row)
            kept += len(keep)
            kept_records = {row["record"] for row in keep}
            injections += sum(1 for row in keep if row["label"] == "irrelevant")
            for row in dropped:
                if row["label"] == "relevant" and row["record"] not in kept_records:
                    lost.append(f"{question['question_id']}/{row['record']}"
                                f"@{row['cosine'] / best:.3f}")
            if question["answerable"]:
                relevant = set(question["relevant_expected"])
                expected += len(relevant)
                hit = relevant & kept_records
                found += len(hit)
                useful += question["useful_tokens"] if hit else 0
            if delivered:
                payload += question["payload_tokens"] * len(keep) / len(delivered)
        out.append({
            "floor": floor,
            "delivered": kept,
            "found": found,
            "expected": expected,
            "injections": injections,
            "payload_per_question": payload / len(arm["questions"]),
            "useful_fraction": useful / payload if payload else 0.0,
            "relevant_lost": sorted(set(lost)),
        })
    return out


def _distribution(arm: dict) -> None:
    buckets: dict[str, list[float]] = {}
    no_cosine = 0
    for question in arm["questions"]:
        best, delivered = _ratios(question)
        for row in delivered:
            if row["cosine"] is None or not best:
                no_cosine += 1
                continue
            buckets.setdefault(row["label"], []).append(row["cosine"] / best)
    print(f"  rows with no cosine (keyword-only): {no_cosine}")
    for label in ("relevant", "tolerated", "irrelevant"):
        values = sorted(buckets.get(label, []))
        if not values:
            continue
        n = len(values)
        print(f"  {label:10s} n={n:3d}  min={values[0]:.3f}  p10={values[n // 10]:.3f}"
              f"  p25={values[n // 4]:.3f}  median={values[n // 2]:.3f}")


def _at_risk(arm: dict, limit: int = 8) -> None:
    worst = []
    for question in arm["questions"]:
        best, delivered = _ratios(question)
        if not best:
            continue
        relevant = [row for row in delivered
                    if row["label"] == "relevant" and row["cosine"] is not None]
        if not relevant:
            continue
        low = min(relevant, key=lambda row: row["cosine"])
        worst.append((low["cosine"] / best, question["question_id"], question, low))
    for ratio, _, question, row in sorted(worst, key=lambda item: item[:2])[:limit]:
        keyword = "-" if row["keyword"] is None else f"{row['keyword']:.3f}"
        print(f"  {ratio:.3f}  {question['question_id']:9s} rank {row['rank']}  "
              f"cos {row['cosine']:.3f}  bm25 {keyword}  {row['record']}")
        print(f"          {question['ask']}")


def main() -> int:
    data = json.loads(ROWS.read_text(encoding="utf-8"))
    for arm in data["arms"]:
        print(f"\n===== hybrid @ absolute cosine floor {arm['threshold']} =====")
        _distribution(arm)
        for cut in ("A", "B"):
            print(f"\n  cut {cut}")
            print(f"  {'floor':>6} {'delivered':>9} {'recall@k':>16} {'injections':>11} "
                  f"{'est tok/q':>10} {'est useful':>11}  relevant lost")
            for row in _sweep(arm, cut):
                recall = (f"{row['found']}/{row['expected']} "
                          f"({row['found'] / row['expected'] * 100:5.1f}%)")
                print(f"  {row['floor']:6.2f} {row['delivered']:9d} {recall:>16} "
                      f"{row['injections']:11d} {row['payload_per_question']:10.1f} "
                      f"{row['useful_fraction'] * 100:10.1f}%  "
                      f"{', '.join(row['relevant_lost']) or '-'}")
        print("\n  lowest-ratio relevant row, per question:")
        _at_risk(arm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

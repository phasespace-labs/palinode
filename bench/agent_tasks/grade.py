"""Grade a run directory against its plan.

Every judgement here is a substring match of a stamped token against evidence
the driver collected — the diff's *added* lines, the final answer, the delivery
record, the client's event stream. There is no model in the loop and no
tolerance: a random 32-bit token either appears or it does not.

Three rules the output keeps:

* **Delivered and acted are separate fields.** A receipt proves delivery only;
  an agent that was handed the right memory and ignored it is an agent-action
  failure, and one that got it right without being handed it is luck or the
  repo, never memory.
* **Missing evidence is NOT RUN, never a fail.** A cell with a missing file is
  reported with the reason; it does not enter any denominator.
* **Every failure names the first stage that went wrong** — seeded recall ·
  capture · delivered context · agent action — so a miss can be attributed.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

from bench.agent_tasks.plan import load_plan

RESULTS_SCHEMA = "agent_tasks.results/1"

#: Files every run cell must have. ``delivery.json`` is required only where it
#: is the evidence (the palinode arm); ``transcript.jsonl`` is never required.
REQUIRED_FILES = ("events.jsonl", "answer.txt", "diff.patch", "tests.json", "usage.json")
PALINODE_REQUIRED = ("delivery.json",)

STAGES = ("seeded_recall", "capture", "delivered_context", "agent_action")

#: Keyword heuristic for an invented rejection in row 1's negative control,
#: where no rejection exists to cite. It is a heuristic and the report says so;
#: every hit is listed for a human to audit.
_REJECTION_WORDS = re.compile(
    r"\b(reject(?:ed|ion|s)?|ruled out|incident|post-?mortem)\b", re.IGNORECASE
)

#: A sentence that gives a reason. Only these sentences are checked for an
#: unsupported rationale — "I used X." asserts nothing to support.
_REASON_MARKERS = re.compile(
    r"\b(because|since|due to|reason|so that|which (?:means|makes|gives|avoids|keeps)|"
    r"better|safer|simpler|faster|prefer(?:red|able)?|avoids?|ensures?|guarantees?|"
    r"more (?:reliable|robust|efficient|predictable))\b",
    re.IGNORECASE,
)
_CONTENT_WORD = re.compile(r"[a-z]{4,}")
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")
_SUPPORT_WORD = re.compile(r"[a-z]{3,}")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")

#: Words any explanation of a code change uses. Not evidence of anything, so
#: never counted as an unsupported claim. Pinned: extending it changes the
#: metric, so do it with a corpus version bump.
_GENERIC = frozenset("""
about above after again also already another backend backends based being best
better both call called calls cache cached caching change changed changes choice
choose chose chosen class clean code current currently decided decision
decisions default directly does doing done each earlier either ensure ensures
existing file files first follow followed following from function have here
implementation implemented instead into just keep keeps like made make makes
match matches method module more most need needed needs only option options
other over pass passed passes passing pipeline prefer preferred prior project
reason reasons repo repository right same should simple simpler since some
such than that their them then there these they this those through used uses
using very well were what when where which while will with without work works
would wired wire wiring your added adds update updated test tests suite code
safer faster avoid avoids because better guarantee guarantees reliable robust
efficient predictable means gives setup setting settings value values
says said note notes noted recorded record according documented team previous
previously history earlier agreed rather required requires requirement task
""".split())

USAGE_FIELDS = (
    "cost_usd", "input_tokens", "cached_input_tokens", "output_tokens",
    "num_turns", "wall_s", "timed_out", "rc", "hit_max_turns",
)


# ── evidence loading ────────────────────────────────────────────────────────


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _json(path: str) -> Any:
    return json.loads(_read(path))


def added_lines(patch: str) -> str:
    """The ``+`` lines of a unified diff, without the ``+++`` headers.

    Context and removed lines are excluded on purpose: a task repo already
    names both options, so a token in a context line says nothing about what
    the agent chose.
    """
    return "\n".join(
        line[1:] for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def delivery_text(delivery: dict[str, Any]) -> str:
    """Everything the palinode arm could have put in front of the agent."""
    parts: list[str] = []
    if delivery.get("hook_context"):
        parts.append(str(delivery["hook_context"]))
    for call in delivery.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("result_text"):
            parts.append(str(call["result_text"]))
    for row in delivery.get("retrieval_rows") or []:
        parts.append(json.dumps(row, sort_keys=True) if not isinstance(row, str) else row)
    return "\n".join(parts)


def _walk(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


_TOOL_TYPES = {"tool_use", "mcp_tool_call", "function_call", "command_execution", "local_shell_call"}


def trace_summary(events_text: str) -> dict[str, Any]:
    """Tool calls in a client event stream (Claude Code stream-json or Codex --json).

    Tolerant of either shape: a tool call is any object whose ``type`` is a
    known tool-call type, deduplicated by its id where it has one (Codex emits
    ``started`` and ``completed`` for the same item).
    """
    calls: dict[str, str] = {}
    parsed = bad = 0
    for i, line in enumerate(events_text.splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        parsed += 1
        for j, obj in enumerate(_walk(event)):
            if obj.get("type") not in _TOOL_TYPES:
                continue
            name = obj.get("name") or obj.get("tool") or obj.get("type")
            if obj.get("server"):
                name = f"{obj['server']}.{name}"
            calls[str(obj.get("id") or f"{i}:{j}")] = str(name)
    names = list(calls.values())
    return {
        "events": parsed,
        "unparsed_lines": bad,
        "tool_calls": len(names),
        "memory_tool_calls": sum("palinode" in n.lower() for n in names),
        # Informational: an agent that tries to save while capture is paused
        # is behaving normally, and its "capture is paused" remark is not a
        # failure. Counted so the attempt is visible.
        "attempted_memory_write": sum(
            n.lower().endswith(("palinode_save", "palinode_session_end")) for n in names
        ),
        "tools": sorted(set(names)),
    }


def unsupported_reasons(answer: str, support_text: str) -> list[dict[str, Any]]:
    """Reason-giving sentences whose content words the evidence does not contain.

    A deterministic heuristic for an embellished rationale ("TTL beats LRU
    here" when neither memory nor repo says so): a sentence with a reason
    marker is flagged when two or more of its content words appear nowhere in
    the memory text, the repo or the prompt, allowing a shared five-letter
    stem. It over-flags paraphrase and under-flags claims made of common
    words; every hit is listed for audit.
    """
    support = set(_SUPPORT_WORD.findall(support_text.lower()))
    stems = {w[:5] for w in support if len(w) >= 5}
    hits: list[dict[str, Any]] = []
    for sentence in _SENTENCES.split(answer):
        if not _REASON_MARKERS.search(sentence):
            continue
        words = _CONTENT_WORD.findall(sentence.lower())
        words += [a.lower() for a in _ACRONYM.findall(sentence)]
        missing = sorted({
            w for w in words
            if w not in _GENERIC and w not in support and w[:5] not in stems
        })
        if len(missing) >= 2:
            hits.append({"sentence": sentence.strip()[:300], "unsupported_words": missing})
    return hits


def rationale_match(answer: str, token: str, words: list[str]) -> str | None:
    """How the answer cites the recorded reason: ``token``, ``paraphrase`` or not at all."""
    if token in answer:
        return "token"
    lowered = answer.lower()
    if words and all(re.search(rf"\b{re.escape(w)}", lowered) for w in words):
        return "paraphrase"
    return None


# ── one cell ────────────────────────────────────────────────────────────────


def _hits(names: Iterable[str], tokens: dict[str, str], text: str) -> list[str]:
    return [n for n in names if tokens[n] in text]


#: An answer that stops to ask, or says it cannot tell. Pinned phrases; a
#: judge model reviews the rows that lean on it (row 11).
_ABSTAIN = re.compile(
    r"\b(don'?t know|do not know|not sure|unsure|unclear|couldn'?t (?:find|determine|tell)|"
    r"could not (?:find|determine|tell)|can'?t (?:find|determine|tell)|cannot (?:find|determine|tell)|"
    r"no record|not recorded|isn'?t recorded|no (?:agreed|recorded|settled)|"
    r"which (?:one|transport|batch|backend) (?:do|should|would)|please (?:confirm|tell me|let me know)|"
    r"(?:need|needs) (?:you|your) (?:to )?(?:confirm|decide|clarify|input)|conflict(?:ing|s)?|"
    r"contradict(?:ory|s|ion)?|disagree(?:s|ment)?)\b",
    re.IGNORECASE,
)


def abstains(answer: str) -> bool:
    return bool(_ABSTAIN.search(answer))


def _side_text(delivery: dict[str, Any] | None, raw_patch: str) -> str | None:
    """Created files and commands run, plus every path the diff touches.

    ``None`` when the driver recorded no ``side_effects``: a command cannot be
    seen in a diff, so its absence there proves nothing.
    """
    if not delivery or "side_effects" not in delivery:
        return None
    paths = "\n".join(
        line for line in raw_patch.splitlines()
        if line.startswith(("diff --git", "+++ ", "--- "))
    )
    return json.dumps(delivery["side_effects"]) + "\n" + paths


def _native_snapshot(value: Any) -> dict[str, str] | None:
    """``[{path, sha256}]`` (or a ``{path: sha256}`` map) as a map; None if absent."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {str(e.get("path")): str(e.get("sha256")) for e in value if isinstance(e, dict)}


def _native_changed(delivery: dict[str, Any] | None) -> bool | None:
    before = _native_snapshot((delivery or {}).get("native_before"))
    after = _native_snapshot((delivery or {}).get("native_after"))
    if before is None or after is None:
        return None
    return before != after


def _check(key: str, refs: list[str], ctx: dict[str, Any]) -> bool | None:
    """One oracle expectation. ``None`` = the evidence it needs is missing."""
    tokens, diff, answer = ctx["tokens"], ctx["diff"], ctx["answer"]
    if key == "answer_cites":
        return all(rationale_match(answer, tokens[r], ctx["rationale_words"]) for r in refs)
    if key == "answer_mentions_all_or_abstains":
        return len(_hits(refs, tokens, answer)) == len(refs) or abstains(answer)
    if key == "answer_abstains":
        return abstains(answer)
    if key == "side_effects_avoid":
        side = ctx["side_text"]
        return None if side is None else not _hits(refs, tokens, side)
    if key == "native_unchanged":
        changed = ctx["native_changed"]
        return None if changed is None else not changed
    text = diff if key.startswith("diff") else answer
    found = _hits(refs, tokens, text)
    if key in ("diff_uses", "answer_uses"):
        return len(found) == len(refs)
    if key == "diff_uses_any":
        return bool(found)
    return not found  # *_avoids


def _all(values: Iterable[bool | None]) -> bool | None:
    """False if anything failed, else None if anything is unmeasured, else True."""
    vals = list(values)
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


def declared_metric(conds: dict[str, Any], ctx: dict[str, Any]) -> bool | None:
    """Evaluate one declarative row metric (corpus.METRIC_CONDITIONS)."""
    tokens, diff, answer = ctx["tokens"], ctx["diff"], ctx["answer"]
    arm, delivered = ctx["arm"], ctx["delivered_names"]
    results: list[bool | None] = []
    for cond, arg in conds.items():
        if cond == "when_delivered":
            if arm == "none" or not any(n in delivered for n in arg):
                return None
            continue
        if cond == "delivered_has":
            results.append(any(n in delivered for n in arg) if arm == "palinode" else None)
        elif cond == "diff_has":
            results.append(len(_hits(arg, tokens, diff)) == len(arg))
        elif cond == "diff_lacks":
            results.append(not _hits(arg, tokens, diff))
        elif cond == "answer_has":
            results.append(len(_hits(arg, tokens, answer)) == len(arg))
        elif cond == "answer_lacks":
            results.append(not _hits(arg, tokens, answer))
        elif cond == "answer_has_any":
            results.append(bool(_hits(arg, tokens, answer)))
        elif cond == "answer_has_exactly_one":
            results.append(len(_hits(arg, tokens, answer)) == 1)
        elif cond == "abstains":
            results.append(abstains(answer) == bool(arg))
        elif cond == "side_effects_has_any":
            side = ctx["side_text"]
            results.append(None if side is None else bool(_hits(arg, tokens, side)))
        elif cond == "native_changed":
            changed = ctx["native_changed"]
            results.append(None if changed is None else changed == bool(arg))
        else:
            raise ValueError(f"unknown metric condition {cond!r}")
    return _all(results)


def receipt_check(delivery: dict[str, Any] | None, slug_tokens: dict[str, list[str]],
                  tokens: dict[str, str]) -> dict[str, Any]:
    """Receipt explanations: does each receipt's explanation name exactly what was delivered?

    For every memory this cell seeded that carries a stamped token, compare
    "its token reached the agent" (hook context, tool results) with "its slug
    appears in the explanation the receipt route returned". ``unmeasured``
    when the driver recorded no receipts.
    """
    receipts = (delivery or {}).get("receipts")
    if not receipts:
        return {"status": "unmeasured", "reason": "no receipts in delivery.json"}
    items = list(receipts.values() if isinstance(receipts, dict) else receipts)
    errors = [r for r in items if isinstance(r, dict) and set(r) == {"error"}]
    items = [r for r in items if r not in errors]
    if not items:
        return {"status": "unmeasured", "reason": f"every receipt lookup failed ({len(errors)})"}
    explained = "\n".join(
        json.dumps(r, sort_keys=True) if not isinstance(r, str) else r for r in items
    )
    shown = "\n".join(
        [str((delivery or {}).get("hook_context") or "")]
        + [str(c.get("result_text") or "") for c in (delivery or {}).get("tool_calls") or []
           if isinstance(c, dict)]
    )
    per_slug = {}
    for slug, names in slug_tokens.items():
        if not names:
            continue
        per_slug[slug] = {
            "delivered": any(tokens[n] in shown for n in names),
            "in_receipt": slug in explained,
        }
    mismatched = [s for s, v in per_slug.items() if v["delivered"] != v["in_receipt"]]
    return {
        "status": "mismatch" if mismatched else "match",
        "receipts": len(items),
        "receipt_errors": len(errors),
        "slugs": per_slug,
        "mismatched": mismatched,
    }


def _pass_verdict(out: dict[str, Any], project: str) -> tuple[bool | None, str]:
    """Was *project* exercised by one consolidate pass? ``(verdict, category)``.

    Exercised = the project was grouped and sent to the model: it was
    compacted, or listed in ``projects_no_ops`` (proposed nothing) or
    ``projects_all_ops_filtered`` (proposed, every op filtered). Not exercised
    = skipped, failed, or never grouped.

    The runner lists every outcome per project except one: ``projects_compacted``
    is a count across the whole store. So "compacted" for this project is
    inferred — the count is non-zero, the project is in none of the lists, and
    its status document changed across the pass. Elsewhere the sha pair is
    supporting evidence only: a frontmatter-only change with the project in
    ``projects_no_ops`` is exercised.
    """
    def listed(key: str) -> bool:
        return any(str(p) in (project, f"project/{project}") for p in out.get(key) or [])

    if listed("skipped_no_target_projects"):
        return False, "skipped: no project document"
    if listed("skipped_untagged_projects"):
        return False, "skipped: project document has no fact markers"
    if listed("failed_projects"):
        return False, "failed"
    if listed("projects_no_ops"):
        return True, "grouped, proposed nothing"
    if listed("projects_all_ops_filtered"):
        reasons = [f"{k} {out[k]}" for k in ("footer_op_rejected", "retract_downgraded") if out.get(k)]
        why = ", ".join(reasons) if reasons else "allowed_ops filter"
        return True, f"proposed, all filtered (pass-wide: {why})"
    if not out.get("projects_compacted"):
        return False, "never grouped"
    before, after = out.get("target_before_sha256"), out.get("target_after_sha256")
    if before is None or after is None:
        return None, "compactions happened, but no target sha256 to say whose"
    if before != after:
        return True, "compacted"
    return False, "never grouped (another project was compacted)"


def consolidation_evidence(cell: dict[str, Any], cell_dir: str) -> dict[str, Any] | None:
    """Per consolidate op, did the pass actually consolidate this cell's project?

    Read from the ``consolidate-<n>.json`` files the driver saves. ``None``
    when the cell has no consolidate op.
    """
    ops = (cell.get("memory") or {}).get("palinode_ops") or []
    n_ops = sum(o.get("op") == "consolidate" for o in ops)
    if not n_ops:
        return None
    passes = []
    names = sorted(
        (n for n in (os.listdir(cell_dir) if os.path.isdir(cell_dir) else [])
         if n.startswith("consolidate-") and n.endswith(".json")),
        key=lambda n: int(re.sub(r"\D", "", n) or 0),
    )
    for name in names:
        try:
            out = _json(os.path.join(cell_dir, name))
        except (json.JSONDecodeError, OSError):
            passes.append({"file": name, "exercised": None, "category": "unreadable"})
            continue
        verdict, category = _pass_verdict(out, cell["project"])
        passes.append({"file": name, "exercised": verdict, "category": category})
    exercised = _all(p["exercised"] for p in passes) if len(passes) == n_ops else None
    return {"ops": n_ops, "evidence_files": len(passes), "exercised": exercised, "passes": passes}


#: Rows whose palinode cells carry the receipt-explanation check (brief).
RECEIPT_ROWS = frozenset({1, 2, 9})


def row_metrics(cell: dict[str, Any], *, diff: str, answer: str,
                delivered_names: list[str]) -> dict[str, Any]:
    """The usefulness/trust metrics the brief defines for this row.

    ``None`` means "does not apply to this cell" and is kept out of the
    denominator.
    """
    t = cell["tokens"]
    grading = cell["grading"]
    irrelevant = grading["irrelevant"]
    injected = _hits(irrelevant, t, diff + "\n" + answer)
    out: dict[str, Any] = {
        "irrelevant_injection": bool(injected),
        "unnecessary_abstention": not diff.strip(),
    }
    family, control = cell["family"], cell["control"]
    if family == "rejected_approach":
        if control == "positive":
            out["rejected_option_reuse"] = t["reject"] in diff
            out["cites_rationale"] = rationale_match(
                answer, t["incident"], grading.get("rationale_words") or []
            ) is not None
        else:
            out["invented_rejection"] = bool(_REJECTION_WORDS.search(answer))
    elif family == "decision_change":
        old_used, new_used = t["old"] in diff, t["new"] in diff
        if control == "positive":
            out["valid_correction_use"] = new_used and not old_used
            out["old_decision_reuse"] = old_used
            out["presents_old_as_current"] = t["old"] in answer and t["new"] not in answer
            # "Ignored" needs the correction to have reached the agent; an
            # undelivered correction is a delivery failure, counted elsewhere.
            out["ignored_explicit_correction"] = (
                old_used if "change_ref" in delivered_names else None
            )
        else:
            out["recorded_decision_use"] = old_used and not new_used
            out["unsupported_change"] = new_used
    elif family == "irrelevant_memory":
        out["answer_from_repo"] = t["repo_tag"] in answer
    return out


def _support_text(plan_cell_dir: str | None, memory_text: str) -> str:
    """Memory text, the repo as generated and the prompt: what a reason may cite."""
    parts = [memory_text]
    if plan_cell_dir and os.path.isdir(plan_cell_dir):
        for dirpath, _, files in os.walk(plan_cell_dir):
            if os.sep + "memory" in dirpath[len(plan_cell_dir):]:
                continue
            for name in files:
                parts.append(name)
                parts.append(_read(os.path.join(dirpath, name)))
    return "\n".join(parts)


def _needs_delivery(cell: dict[str, Any]) -> bool:
    """Is delivery.json evidence for this cell even off the palinode arm?"""
    grading = cell["grading"]
    if cell["arm"] == "palinode":
        return True
    if {"side_effects_avoid", "native_unchanged"} & set(grading["expect"]):
        return True
    return any(
        {"side_effects_has_any", "native_changed"} & set(conds)
        for conds in (grading.get("metrics") or {}).values()
    )


def grade_cell(cell: dict[str, Any], cell_dir: str, *, foreign: dict[str, str],
               not_run: dict[str, str], plan_cell_dir: str | None = None,
               cell_ops: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    base = {
        k: cell.get(k) for k in ("cell_id", "row", "family", "control", "scenario",
                                 "client", "arm", "repeat", "project")
    }
    base["split"] = cell.get("split", "dev")
    if cell["status"] == "not_applicable":
        return {**base, "status": "not_applicable", "reason": cell.get("reason", "")}
    if cell["cell_id"] in not_run:
        return {**base, "status": "not_run", "reason": not_run[cell["cell_id"]]}

    required = REQUIRED_FILES + (PALINODE_REQUIRED if _needs_delivery(cell) else ())
    missing = [f for f in required if not os.path.isfile(os.path.join(cell_dir, f))]
    if missing:
        return {**base, "status": "not_run", "reason": f"missing evidence: {', '.join(missing)}"}

    try:
        tests = _json(os.path.join(cell_dir, "tests.json"))
        usage = _json(os.path.join(cell_dir, "usage.json"))
        delivery_path = os.path.join(cell_dir, "delivery.json")
        delivery = _json(delivery_path) if os.path.isfile(delivery_path) else None
    except (json.JSONDecodeError, OSError) as exc:
        return {**base, "status": "not_run", "reason": f"unreadable evidence: {exc}"}
    if not tests.get("ran"):
        return {**base, "status": "not_run", "reason": "the task's tests did not run"}
    consolidation = consolidation_evidence(cell, cell_dir)
    if consolidation is not None and consolidation["exercised"] is not True:
        # A row whose claim is "after consolidation" is no evidence for it if
        # the pass never consolidated the project. Not a failure either.
        if consolidation["evidence_files"] < consolidation["ops"]:
            why = (f"{consolidation['evidence_files']} of {consolidation['ops']} "
                   "consolidate-<n>.json files present")
        else:
            why = "; ".join(
                f"pass {i}: {p['category']}" for i, p in enumerate(consolidation["passes"], 1)
                if p["exercised"] is not True
            )
        return {**base, "status": "not_run", "reason": f"consolidation no-op: {why}",
                "consolidation": consolidation}

    answer = _read(os.path.join(cell_dir, "answer.txt"))
    raw_patch = _read(os.path.join(cell_dir, "diff.patch"))
    diff = added_lines(raw_patch)
    trace = trace_summary(_read(os.path.join(cell_dir, "events.jsonl")))

    t = cell["tokens"]
    grading = cell["grading"]
    memory_names = grading["memory_tokens"]

    # Delivery. Only the palinode arm has evidence to check; the file and
    # native arms put the text in context by construction (the native arm
    # conditional on the loader probe), and the none arm has nothing.
    delivered_names: list[str] = []
    if cell["arm"] == "palinode":
        delivered_names = _hits(memory_names, t, delivery_text(delivery or {}))
        # With no key token (a control whose memory is all beside the point),
        # "delivered" has no meaning: n/a, with the tokens that did arrive
        # still listed in delivered_tokens.
        delivered: bool | None = (
            any(n in delivered_names for n in grading["key"]) if grading["key"] else None
        )
        basis = "evidence" if grading["key"] else "no_key_token"
    elif cell["arm"] in ("file", "native"):
        delivered, basis = True, "by_construction"
        delivered_names = list(memory_names)
    else:
        delivered, basis = None, "no_memory"

    rationale_words = grading.get("rationale_words") or []
    ctx = {
        "tokens": t, "diff": diff, "answer": answer, "arm": cell["arm"],
        "rationale_words": rationale_words, "delivered_names": delivered_names,
        "side_text": _side_text(delivery, raw_patch),
        "native_changed": _native_changed(delivery),
    }
    checks = {key: _check(key, refs, ctx) for key, refs in grading["expect"].items()}
    acted = _all(checks[k] for k in grading["acted"])
    task_correct = bool(tests.get("passed"))
    require_tests = grading.get("require_tests", True)
    passed = False if (require_tests and not task_correct) else _all(checks.values())

    seeded = (delivery or {}).get("seeded_recall")
    captured_failed = [
        o for o in (cell_ops or [])
        if (o.get("op") or {}).get("op") in ("transcript_capture", "agent_session")
        and o.get("ok") is False
    ]
    stage = None
    if passed is False:
        if cell["arm"] == "palinode" and isinstance(seeded, dict) and seeded.get("ok") is False:
            stage = "seeded_recall"
        elif cell["arm"] == "palinode" and captured_failed:
            stage = "capture"
        elif cell["arm"] == "palinode" and grading["needs_memory"] and delivered is False:
            stage = "delivered_context"
        else:
            stage = "agent_action"

    trace_text = _read(os.path.join(cell_dir, "events.jsonl"))
    metrics = row_metrics(cell, diff=diff, answer=answer, delivered_names=delivered_names)
    for name, conds in (grading.get("metrics") or {}).items():
        metrics[name] = declared_metric(conds, ctx)
    if {"answer_abstains", "answer_mentions_all_or_abstains"} & set(grading["expect"]):
        # Stopping to ask is the right move here, never an unnecessary one.
        metrics["unnecessary_abstention"] = None
    unsupported = unsupported_reasons(
        answer, _support_text(plan_cell_dir, grading.get("memory_text", ""))
    )
    metrics["unsupported_rationale"] = bool(unsupported)
    audit: dict[str, Any] = {}
    if unsupported:
        audit["unsupported_rationale"] = unsupported
    if "incident" in t and cell["family"] == "rejected_approach" and cell["control"] == "positive":
        how = rationale_match(answer, t["incident"], rationale_words)
        if how:
            audit["rationale_match"] = {"how": how, "answer_tail": answer[-600:]}
    if metrics.get("invented_rejection"):
        audit["invented_rejection_answer"] = answer[-600:]
    result = {
        **base,
        "status": "graded",
        "pass": passed,
        "task_correct": task_correct,
        "delivered": delivered,
        "delivery_basis": basis,
        "delivered_tokens": delivered_names,
        "acted": acted,
        "checks": checks,
        "metrics": metrics,
        "first_failing_stage": stage,
        "seeded_recall": seeded if isinstance(seeded, dict) else None,
        "trace": {
            **trace,
            "memory_tokens_in_trace": _hits(memory_names, t, trace_text),
        },
        "foreign_tokens": sorted(
            v for v, owner in foreign.items()
            if owner != cell["cell_id"] and v in diff + "\n" + answer
        ),
        # Another cell's stamped value in what palinode delivered: the store is
        # shared per run and consolidation is store-wide, so this is where a
        # cross-project leak would show before it reaches an answer.
        "foreign_tokens_delivered": sorted(
            v for v, owner in foreign.items()
            if owner != cell["cell_id"] and cell["arm"] == "palinode"
            and v in delivery_text(delivery or {})
        ),
        "usage": {f: usage.get(f) for f in USAGE_FIELDS},
        "tests": {"passed": task_correct, "rc": tests.get("rc"), "required": require_tests},
    }
    from bench.agent_tasks.judge import needs_judge

    if needs_judge(cell):
        result["needs_judge"] = True
    if consolidation is not None:
        result["consolidation"] = consolidation
    if cell["arm"] == "palinode" and cell["row"] in RECEIPT_ROWS:
        result["receipts_1448"] = receipt_check(delivery, grading.get("slug_tokens") or {}, t)
    if audit:
        result["audit"] = audit
    return result


# ── probes and the run ──────────────────────────────────────────────────────


def grade_probe(probe: dict[str, Any], probe_dir: str) -> dict[str, Any]:
    answer_path = os.path.join(probe_dir, "answer.txt")
    base = {"probe_id": probe["probe_id"], "client": probe["client"], "arm": probe["arm"]}
    if not os.path.isfile(answer_path):
        return {**base, "status": "not_run", "reason": "missing evidence: answer.txt"}
    # Case-insensitive, matching the driver's own pass rule for the probe.
    loaded = probe["tokens"]["probe"].lower() in _read(answer_path).lower()
    return {**base, "status": "graded", "loaded": loaded}


def grade_run(plan_dir: str, run_dir: str, *, judge_path: str | None = None) -> dict[str, Any]:
    plan = load_plan(plan_dir)
    run_path = os.path.join(run_dir, "run.json")
    run = _json(run_path) if os.path.isfile(run_path) else None
    not_run = {
        str(item.get("cell_id")): str(item.get("reason", ""))
        for item in (run or {}).get("not_run") or []
        if isinstance(item, dict)
    }
    foreign = {v: c["cell_id"] for c in plan["cells"] for v in c["tokens"].values()}
    ops_by_cell: dict[str, list[dict[str, Any]]] = {}
    for o in (run or {}).get("ops") or []:
        if isinstance(o, dict):
            ops_by_cell.setdefault(str(o.get("cell_id")), []).append(o)
    cells = [
        grade_cell(c, os.path.join(run_dir, "cells", c["cell_id"]),
                   foreign=foreign, not_run=not_run,
                   plan_cell_dir=os.path.join(plan_dir, "cells", c["cell_id"]),
                   cell_ops=ops_by_cell.get(c["cell_id"]))
        for c in plan["cells"]
    ]
    probes = [
        grade_probe(p, os.path.join(run_dir, "probes", p["probe_id"]))
        for p in plan.get("probes") or []
    ]
    results = {
        "schema": RESULTS_SCHEMA,
        "plan": {
            **{k: plan[k] for k in ("schema", "corpus_version", "seed", "split",
                                    "rows", "clients", "arms", "repeats")},
            "splits": plan.get("splits") or [plan["split"]],
        },
        "run": run,
        "run_json_present": run is not None,
        "cells": cells,
        "probes": probes,
    }

    if judge_path:
        from bench.agent_tasks.judge import merge_results

        merge_results(results, _json(judge_path), plan_dir=plan_dir, run_dir=run_dir)
    return results


def write_results(results: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(results, indent=2, ensure_ascii=False) + "\n")

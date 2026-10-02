"""Pinned, optional free-text judgement; deterministic grades remain intact."""
from __future__ import annotations

import hashlib
import json
import math
import random
import shlex
import subprocess
from pathlib import Path
from typing import Any

from bench.agent_tasks.corpus import ROWS_BY_NUMBER
from bench.agent_tasks.plan import load_plan

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_COMMAND = 'claude -p --output-format json --model {model} --tools "" --no-session-persistence'
SCHEMA = "agent_tasks.judge/1"
PROMPT_PATH = Path(__file__).with_name("judge_prompt.md")


def needs_judge(cell: dict[str, Any]) -> bool:
    # The judged rows ask whether the agent surfaced conflicting memory. An arm
    # that delivers no memory cannot show the conflict, so it is not judged
    # (its cells would otherwise fail for missing what they were never given).
    if cell.get("arm") == "none":
        return False
    # Earlier P1 plans omitted the flag on native-disagreement positives.
    return bool(cell["grading"].get("judge")) or (
        cell["row"] == 17 and cell["control"] == "positive"
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cell_input(cell: dict[str, Any], plan_dir: str, run_dir: str,
               run: dict[str, Any]) -> dict[str, Any]:
    cid = cell["cell_id"]
    usage_path = Path(run_dir, "cells", cid, "usage.json")
    usage = json.loads(usage_path.read_text()) if usage_path.exists() else {}
    model = (usage.get("model")
             or run.get("pins", {}).get("models", {}).get(cell["client"])
             or run.get("models", {}).get(cell["client"]))
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{cid}: missing agent model (usage.model, run.pins.models or run.models)")
    expected = cell["grading"].get("expected_behaviour") or ROWS_BY_NUMBER[cell["row"]].must
    return {
        "cell_id": cid, "agent_model": model,
        "question": Path(plan_dir, "cells", cid, "prompt.txt").read_text(),
        "expected": expected, "control": cell["control"],
        "expectations": cell["grading"]["expect"], "tokens": cell["tokens"],
        "reference_memory_texts": cell["grading"].get("memory_text", ""),
        "answer": Path(run_dir, "cells", cid, "answer.txt").read_text(),
    }


def input_hash(payload: dict[str, Any]) -> str:
    return _hash(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _verdict(stdout: str) -> dict[str, str]:
    value = json.loads(stdout)
    if isinstance(value, dict) and "result" in value:
        if value.get("is_error"):
            raise ValueError("judge CLI reported an error")
        value = json.loads(value["result"])
    if (not isinstance(value, dict) or value.get("verdict") not in ("pass", "fail")
            or not isinstance(value.get("rationale"), str) or not value["rationale"].strip()):
        raise ValueError("judge must return verdict pass/fail and a nonempty rationale")
    return {k: value[k] for k in ("verdict", "rationale")}


def judge_run(plan_dir: str, run_dir: str, *, model: str = DEFAULT_MODEL,
              command: str = DEFAULT_COMMAND, seed: int = 1449,
              timeout: float = 120, attempts: int = 3,
              resume: dict[str, Any] | None = None) -> dict[str, Any]:
    """Judge every free-text cell once, retrying a call whose output is unusable.

    ``attempts`` bounds calls per cell (malformed JSON from the judge is the
    common failure; a retry resamples it). ``resume`` is an earlier result from
    the same model and prompt: its judged cells whose input is unchanged are
    reused as they are, so only errors and changed inputs are called again.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    from bench.agent_tasks.grade import grade_run

    argv = shlex.split(command)
    if "{model}" not in argv:
        raise ValueError("judge command must contain a separate {model} argument")
    argv = [model if arg == "{model}" else arg for arg in argv]
    plan = load_plan(plan_dir)
    grades = grade_run(plan_dir, run_dir)
    eligible = {c["cell_id"] for c in grades["cells"] if c["status"] == "graded"}
    cells = [c for c in plan["cells"] if needs_judge(c) and c["cell_id"] in eligible]
    # Validate every identity before making any paid call. Unknown identity is
    # not evidence that judge and agent differ.
    inputs = [cell_input(c, plan_dir, run_dir, grades["run"] or {}) for c in cells]
    for payload in inputs:
        if payload["agent_model"].strip().lower() == model.strip().lower():
            raise ValueError(f"{payload['cell_id']}: agent and judge model must differ")
    prompt = PROMPT_PATH.read_text()
    reusable: dict[tuple[str, str], dict[str, Any]] = {}
    if resume is not None:
        if resume.get("model") != model or resume.get("prompt_sha256") != _hash(prompt):
            raise ValueError("resume needs a result from the same judge model and prompt")
        reusable = {(c["cell_id"], c["input_sha256"]): c
                    for c in resume.get("cells", []) if c.get("status") == "judged"}
    results = []
    for payload in inputs:
        entry = {**payload, "input_sha256": input_hash(payload)}
        prior = reusable.get((entry["cell_id"], entry["input_sha256"]))
        if prior is not None:
            results.append(prior)
            continue
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(argv, input=prompt + "\n\n" + json.dumps(payload),
                                      text=True, capture_output=True, timeout=timeout, check=True)
                entry.update(status="judged", attempts=attempt, **_verdict(proc.stdout))
                entry.pop("reason", None)
                break
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                entry.update(status="error", attempts=attempt, reason=str(exc))
        results.append(entry)
    judged = sorted(c["cell_id"] for c in results if c["status"] == "judged")
    sample = sorted(random.Random(seed).sample(judged, math.ceil(len(judged) / 10)))
    return {"schema": SCHEMA, "model": model, "command": argv,
            "prompt_sha256": _hash(prompt), "audit_seed": seed,
            "audit_sample": sample, "cells": results}


def audit_sheet(results: dict[str, Any]) -> str:
    def escape(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(
            ">", "&gt;").replace("|", "&vert;").replace("\n", "<br>")

    lines = ["# Judge hand audit", "", f"Judge model: `{results['model']}`. "
             f"Seed: {results['audit_seed']}; random 10% rounded up. Pending human review.", "",
             "| cell id | question | expected | answer | verdict | rationale | auditor agrees? y/n |",
             "|---|---|---|---|---|---|---|"]
    for cell in results["cells"]:
        if cell["cell_id"] in results["audit_sample"]:
            lines.append("| " + " | ".join(escape(cell[k]) for k in (
                "cell_id", "question", "expected", "answer", "verdict", "rationale")) + " |  |")
    return "\n".join(lines) + "\n"


def merge_results(results: dict[str, Any], judged: dict[str, Any], *,
                  plan_dir: str, run_dir: str) -> None:
    if judged.get("schema") != SCHEMA or judged.get("prompt_sha256") != _hash(PROMPT_PATH.read_text()):
        raise ValueError("unknown judge schema or changed pinned prompt")
    planned = {c["cell_id"]: c for c in load_plan(plan_dir)["cells"]}
    graded = {c["cell_id"]: c for c in results["cells"]}
    seen = set()
    ignored = 0
    for entry in judged["cells"]:
        cid = entry["cell_id"]
        if cid in planned and planned[cid].get("arm") == "none" and cid not in seen:
            # Judged before the no-memory arm was excluded: not applicable.
            seen.add(cid)
            ignored += 1
            continue
        if cid in seen or cid not in planned or not needs_judge(planned[cid]):
            raise ValueError(f"unexpected or duplicate judged cell: {cid}")
        seen.add(cid)
        if graded[cid]["status"] != "graded":
            raise ValueError(f"judged cell is no longer graded: {cid}")
        payload = cell_input(planned[cid], plan_dir, run_dir, results["run"] or {})
        if input_hash(payload) != entry["input_sha256"]:
            raise ValueError(f"stale judge evidence: {cid}")
        if payload["agent_model"].strip().lower() == judged["model"].strip().lower():
            raise ValueError(f"agent and judge model must differ: {cid}")
        if entry["status"] == "judged":
            _verdict(json.dumps(entry))
        elif entry["status"] != "error":
            raise ValueError(f"unknown judgement status: {cid}")
        graded[cid]["judgement"] = {**entry, "model": judged["model"]}
    results["judge"] = {k: v for k, v in judged.items() if k != "cells"}
    results["judge"]["ignored_not_applicable"] = ignored

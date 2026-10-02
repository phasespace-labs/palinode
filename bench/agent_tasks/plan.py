"""Materialize a plan: one directory per (scenario, client, arm, repeat) cell.

Everything written is a pure function of ``(corpus, rows, split, clients, arms,
repeats, seed)`` — no clock, no environment, no filesystem order — so two
invocations with the same arguments produce byte-identical plan directories.
Each cell draws its tokens and project name from its own RNG, seeded by the
plan seed and the cell id, so adding a row never reshuffles another row's
values.

Per cell, the plan stamps fresh random tokens into the scenario templates. A
token is ``<word>-<8 hex>`` (or ``<word>_<8 hex>`` where it must be a Python
identifier): 32 random bits an agent cannot guess, and a word so a human
reading a transcript can tell them apart. Values are unique across the whole
plan, which is what lets the grader call a token from another cell
contamination rather than coincidence.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import random
from string import Template
from typing import Any, Sequence

from bench.agent_tasks.corpus import (
    HELD_OUT_PROJECT_WORDS,
    MAX_FIXTURE_AGE_DAYS,
    ROWS_BY_NUMBER,
    SILENT_OPS,
    SPLITS,
    Corpus,
    Scenario,
    scenarios_for,
)

PLAN_SCHEMA = "agent_tasks.plan/2"
#: Plan schemas grade/report still read (P0 plans stay valid under v2).
READABLE_PLAN_SCHEMAS = ("agent_tasks.plan/1", PLAN_SCHEMA)
CLIENTS: tuple[str, ...] = ("claude-code", "codex")
ARMS: tuple[str, ...] = ("none", "file", "native", "palinode")

#: Where the file arm's log goes, per client: the file each client reads from
#: the repo root on its own.
FILE_PATHS = {"claude-code": "CLAUDE.md", "codex": "AGENTS.md"}
NATIVE_PATH = "MEMORY.md"

#: (client, arm) pairs that are listed and never run, with the reason printed
#: in the report.
NOT_APPLICABLE: dict[tuple[str, str], str] = {
    ("codex", "native"): (
        "Codex memories are off by default; enabling them tests a configuration "
        "users don't run (brief, settled 2026-09-23)"
    ),
}

TEST_CMD = "python3 -m unittest -q"
LIMITS = {"max_turns": 20, "wall_s": 420}

_TOKEN_WORDS: tuple[str, ...] = (
    "avocet", "bittern", "dunlin", "godwit", "junco", "kestrel", "merlin",
    "phoebe", "pipit", "plover", "shrike", "tanager", "towhee", "vireo",
    "wren", "yellowlegs",
)
_PROJECT_WORDS: tuple[str, ...] = (
    "quillon", "tessel", "corvane", "halyard", "brisket", "marlow", "osprel",
    "quenby", "sorrel", "vantle",
)


# ── tokens ───────────────────────────────────────────────────────────────────


class _Stamper:
    """Hands out plan-unique random values from a per-cell RNG."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def _unique(self, make) -> str:
        for _ in range(100):
            value = make()
            if value not in self.used:
                self.used.add(value)
                return value
        raise RuntimeError("could not draw a unique token in 100 attempts")

    def token(self, rng: random.Random, kind: str, words: set[str]) -> str:
        """A token whose word no other token in the same cell uses.

        Two options named ``vireo_…`` and ``vireo_…`` would test the agent's
        eyesight, not its memory.
        """
        sep = "_" if kind == "ident" else "-"
        word = rng.choice([w for w in _TOKEN_WORDS if w not in words])
        words.add(word)
        return self._unique(lambda: f"{word}{sep}{rng.getrandbits(32):08x}")

    def project(self, rng: random.Random, words: Sequence[str]) -> str:
        return self._unique(
            lambda: f"{rng.choice(words)}-{rng.getrandbits(24):06x}"
        )


def cell_rng(seed: int, cell_id: str) -> random.Random:
    # A str seed is hashed with SHA-512 by random.seed(version=2), so this is
    # stable across processes and PYTHONHASHSEED values.
    return random.Random(f"agent_tasks/{seed}/{cell_id}")


# ── rendering ────────────────────────────────────────────────────────────────


def _render(text: str, values: dict[str, str]) -> str:
    return Template(text).substitute(values)


def _render_event(event: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in event.items():
        if isinstance(value, str):
            out[key] = _render(value, values)
        elif isinstance(value, list):
            out[key] = [_render(v, values) if isinstance(v, str) else v for v in value]
        else:
            out[key] = value
    return out


def palinode_ops(events: Sequence[dict[str, Any]], project: str,
                 transcripts: dict[str, str], prior_prompts: dict[str, str],
                 client: str, other_client: str) -> list[dict[str, Any]]:
    """Events as the contract's ordered ``palinode_ops`` list (v2 vocabulary).

    The save route takes no date, so each body and correction carries its date
    inline as ``[YYYY-MM-DD] …`` — the form Palinode's own records use — and the
    agent sees the same dates the file arm's log shows.
    """
    ops: list[dict[str, Any]] = []
    for ev in events:
        kind = ev["op"]
        if kind == "save":
            owner = ev.get("project", project)
            # A status document is a bullet list whose lines carry their own
            # dates and fact markers; a leading "[date] " would stop its first
            # bullet from starting the line, and consolidation harvests only
            # "- ... <!-- fact:... -->" lines.
            body = ev["body"] if ev.get("raw_body") else f"[{ev['date']}] {ev['body']}"
            op: dict[str, Any] = {
                "op": "save", "type": ev["type"], "title": ev["title"],
                "body": body, "entities": [f"project/{owner}"],
                "date": ev["date"], "slug": ev["slug"],
            }
            if owner != project:
                op["project"] = owner
            if ev.get("metadata"):
                op["metadata"] = dict(ev["metadata"])
            ops.append(op)
        elif kind == "correct":
            op = {
                "op": "correct", "target_slug": ev["target_slug"],
                "new_text": f"[{ev['date']}] {ev['new_text']}", "reason": ev["reason"],
            }
            if ev.get("on_refused_new_text"):
                # What the user resubmits when the route refuses the first text.
                op["on_refused"] = {"new_text": f"[{ev['date']}] {ev['on_refused_new_text']}"}
            ops.append(op)
        elif kind in ("consolidate", "reindex"):
            ops.append({"op": kind})
        elif kind == "restart":
            ops.append({"op": "restart", "units": list(ev["units"])})
        elif kind in ("archive", "restore"):
            ops.append({"op": kind, "target_slug": ev["target_slug"]})
        elif kind == "transcript_capture":
            ops.append({
                "op": "transcript_capture", "client": "claude-code",
                "transcript": transcripts[ev["transcript"]], "confirm": bool(ev.get("confirm", True)),
            })
        elif kind == "session_end":
            op = {"op": "session_end", "summary": ev["summary"], "project": project}
            if ev.get("decisions"):
                op["decisions"] = list(ev["decisions"])
            ops.append(op)
        elif kind == "agent_session":
            ops.append({
                "op": "agent_session",
                "client": other_client if ev.get("client") == "other" else client,
                "prompt": prior_prompts[ev["prompt"]], "capture": "on",
            })
        else:  # pragma: no cover - the loader rejects unknown ops
            raise ValueError(f"unknown op {kind!r}")
    return ops


def memory_log(events: Sequence[dict[str, Any]], project: str) -> str:
    """The file/native arm text: an append-only dated log of the same events.

    What a careful user would have written down, op by op: a save is a line; a
    correction is its own later line (the old one stays); an archive is a
    "retired" line and a restore a "restored" one; restarts, reindexes and
    consolidation passes are infrastructure and leave no line. Captured
    transcripts and prior agent sessions carry an authored ``log`` line — the
    note the user would have made of that conversation. A save under another
    project keeps its project label, the closest a file comes to a store
    shared across projects.
    """
    titles = {ev["slug"]: ev["title"] for ev in events if ev["op"] == "save"}
    lines = ["# Project notes", ""]
    for ev in events:
        kind = ev["op"]
        if kind in SILENT_OPS:
            continue
        if "log" in ev:
            lines.append(f"- {ev['date']} — {ev['log']}")
        elif kind == "session_end" or ev.get("silent_in_log"):
            # A session note or a status document restates events the log
            # already has a line for; the user would not write them twice.
            continue
        elif kind == "save":
            owner = ev.get("project", project)
            label = f"[{owner}] " if owner != project else ""
            lines.append(f"- {ev['date']} — {label}{ev['title']}: {ev['body']}")
        elif kind == "correct":
            title = titles.get(ev["target_slug"], ev["target_slug"])
            lines.append(
                f"- {ev['date']} — {title} (changed): {ev['new_text']} "
                f"Reason: {ev['reason']}"
            )
        elif kind == "archive":
            title = titles.get(ev["target_slug"], ev["target_slug"])
            lines.append(f"- {ev['date']} — {title}: retired; no longer applies.")
        elif kind == "restore":
            title = titles.get(ev["target_slug"], ev["target_slug"])
            lines.append(f"- {ev['date']} — {title}: restored; applies again.")
    return "\n".join(lines) + "\n"


def palinode_text_bytes(ops: Sequence[dict[str, Any]], files: dict[str, str]) -> int:
    """Bytes of text the palinode arm is given: titles and bodies, corrections
    and reasons, captured transcripts' turn text, prior-session prompts."""
    total = 0
    for op in ops:
        if op["op"] == "save":
            total += len(op["title"].encode()) + len(op["body"].encode())
        elif op["op"] == "correct":
            total += len(op["new_text"].encode()) + len(op["reason"].encode())
        elif op["op"] == "transcript_capture":
            for line in files[op["transcript"]].splitlines():
                total += len(json.loads(line)["message"]["content"].encode())
        elif op["op"] == "agent_session":
            total += len(files[op["prompt"]].encode())
        elif op["op"] == "session_end":
            total += len(op["summary"].encode()) + sum(len(d.encode()) for d in op.get("decisions", []))
    return total


def _budget(n_bytes: int) -> dict[str, int]:
    # ~4 bytes per token is the usual English/BPE rule of thumb; the byte count
    # is the measured number, the token figure is labelled approximate.
    return {"bytes": n_bytes, "approx_tokens": math.ceil(n_bytes / 4)}


def transcript_jsonl(turns: Sequence[dict[str, str]], project: str, name: str) -> str:
    """A synthetic Claude Code transcript in the shape the capture reader parses.

    No timestamps: the capture scan's lookback window keeps an undated turn,
    and a dated one would be dropped as the run moves away from the authored
    date. ``cwd`` is a placeholder whose basename is the project; the driver
    rewrites it to the real workspace.
    """
    session = f"{project}-{name}"
    return "".join(
        json.dumps({
            "type": turn["role"], "sessionId": session, "uuid": f"{session}-{i}",
            "cwd": f"/work/{project}",
            "message": {"role": turn["role"], "content": turn["text"]},
        }) + "\n"
        for i, turn in enumerate(turns)
    )


# ── cells ────────────────────────────────────────────────────────────────────


def _token_roles(tokens: dict[str, str], repo_files: dict[str, str],
                 memory_text: str) -> dict[str, list[str]]:
    repo_blob = "\n".join([*repo_files, *repo_files.values()])
    return {
        "repo": [n for n, v in tokens.items() if v in repo_blob],
        "memory": [n for n, v in tokens.items() if v in memory_text and v not in repo_blob],
    }


def _slug_tokens(events: Sequence[dict[str, Any]], tokens: dict[str, str],
                 memory_names: list[str]) -> dict[str, list[str]]:
    """slug → memory tokens its text carries (for the receipt-explanation check)."""
    out: dict[str, list[str]] = {}
    for ev in events:
        slug = ev.get("slug") if ev["op"] == "save" else (
            ev.get("target_slug") if ev["op"] == "correct" else None)
        if not slug:
            continue
        text = " ".join(str(ev.get(k, "")) for k in ("title", "body", "new_text", "reason"))
        # (session_end has no slug: its daily note and status line are not
        # addressable memories a receipt names.)
        names = [n for n in memory_names if tokens[n] in text]
        out.setdefault(slug, [])
        out[slug].extend(n for n in names if n not in out[slug])
    return out


def cell_id_for(sc: Scenario, client: str, arm: str, repeat: int) -> str:
    return f"{sc.id}-{client}-{arm}-{repeat}"


def date_values(as_of: str) -> dict[str, str]:
    """``d0``…``d30``: the plan's ``as_of`` date minus N days, as YYYY-MM-DD."""
    base = datetime.date.fromisoformat(as_of)
    return {f"d{n}": (base - datetime.timedelta(days=n)).isoformat()
            for n in range(MAX_FIXTURE_AGE_DAYS + 1)}


def build_cell(sc: Scenario, corpus: Corpus, *, client: str, arm: str, repeat: int,
               seed: int, stamper: _Stamper, as_of: str) -> dict[str, Any]:
    """One cell's plan entry plus the files to write (under ``_files``)."""
    cell_id = cell_id_for(sc, client, arm, repeat)
    rng = cell_rng(seed, cell_id)
    words_list = HELD_OUT_PROJECT_WORDS if sc.split == "heldout" else _PROJECT_WORDS
    project = stamper.project(rng, words_list)
    words: set[str] = set()
    tokens = {name: stamper.token(rng, kind, words) for name, kind in sorted(sc.tokens.items())}

    values = dict(tokens, project=project)
    order = list(sc.options)
    rng.shuffle(order)
    values.update({f"opt_{i}": tokens[name] for i, name in enumerate(order, 1)})
    values.update({alias: tokens[name] for alias, name in sc.aliases.items()})
    # Drawn after everything v1 drew, so a v1 scenario's cell is unchanged.
    if "${other_project}" in json.dumps([sc.events, sc.prior_prompts, sc.transcripts]):
        values["other_project"] = stamper.project(rng, words_list)
    other_client = next(c for c in CLIENTS if c != client)
    values["other_client"] = other_client
    values.update(date_values(as_of))

    repo_files = {
        _render(path, values): _render(body, values)
        for path, body in sorted(corpus.repos[sc.repo].items())
    }
    events = [_render_event(ev, values) for ev in sc.events]
    transcript_files = {
        name: transcript_jsonl(
            [{"role": t["role"], "text": _render(t["text"], values)} for t in turns],
            project, name)
        for name, turns in sc.transcripts.items()
    }
    prompt_files = {name: _render(text, values).strip() + "\n"
                    for name, text in sc.prior_prompts.items()}
    side_files = {
        **{f"memory/{name}.jsonl": body for name, body in transcript_files.items()},
        **{f"memory/{name}.txt": body for name, body in prompt_files.items()},
    }
    ops = palinode_ops(
        events, project,
        transcripts={n: f"memory/{n}.jsonl" for n in transcript_files},
        prior_prompts={n: f"memory/{n}.txt" for n in prompt_files},
        client=client, other_client=other_client,
    )
    log = memory_log(events, project)
    native_seed = _render(sc.native_seed, values) if sc.native_seed else None

    memory: dict[str, Any] = {}
    memory_files: dict[str, str] = {}
    budget = _budget(0)
    if arm == "palinode":
        memory["palinode_ops"] = ops
        memory_files["palinode_ops.json"] = json.dumps(ops, indent=2) + "\n"
        memory_files.update({k[len("memory/"):]: v for k, v in side_files.items()})
        budget = _budget(palinode_text_bytes(ops, side_files))
    elif arm == "file":
        memory["file_path"] = FILE_PATHS[client]
        memory_files[FILE_PATHS[client]] = log
        budget = _budget(len(log.encode()))
    elif arm == "native":
        memory["native_path"] = NATIVE_PATH
        memory_files[NATIVE_PATH] = log
        budget = _budget(len(log.encode()))
    if native_seed and client == "claude-code" and arm in ("file", "palinode"):
        memory["native_seed"] = native_seed

    na_reason = NOT_APPLICABLE.get((client, arm)) or sc.na_reason(client, arm)
    memory_text = log + json.dumps(ops) + "".join(side_files.values()) + (native_seed or "")
    roles = _token_roles(tokens, repo_files, memory_text)
    entry: dict[str, Any] = {
        "cell_id": cell_id,
        "row": sc.row,
        "family": sc.family,
        "control": sc.control,
        "scenario": sc.id,
        "split": sc.split,
        "client": client,
        "arm": arm,
        "repeat": repeat,
        "status": "not_applicable" if na_reason else "run",
    }
    if na_reason:
        entry["reason"] = na_reason
    entry.update({
        "project": project,
        "tokens": tokens,
        "memory": memory,
        "memory_budget": budget,
        "test_cmd": TEST_CMD,
        "limits": dict(LIMITS),
        "grading": {
            "expect": {k: list(v) for k, v in sc.expect.items()},
            "acted": list(sc.acted),
            "irrelevant": list(sc.irrelevant),
            "key": list(sc.key),
            "needs_memory": sc.needs_memory,
            "require_tests": sc.require_tests,
            "repo_tokens": roles["repo"],
            "memory_tokens": roles["memory"],
            "rationale_words": list(sc.rationale_words),
            "metrics": sc.metrics,
            "judge": sc.judge,
            "slug_tokens": _slug_tokens(events, tokens, roles["memory"]),
            # The recorded events as text, whatever the arm: what a reason in
            # the answer may legitimately draw on (unsupported_rationale).
            "memory_text": log + (native_seed or ""),
        },
        "_files": {
            "prompt.txt": sc.prompt,
            **{f"repo/{p}": body for p, body in repo_files.items()},
            **{f"memory/{p}": body for p, body in memory_files.items()},
        },
    })
    return entry


def _probe(seed: int, stamper: _Stamper, as_of: str) -> dict[str, Any]:
    probe_id = "probe-native-claude-code"
    rng = cell_rng(seed, probe_id)
    project = stamper.project(rng, _PROJECT_WORDS)
    token = stamper.token(rng, "tag", set())
    prompt = (
        "What is this workspace's release passphrase? Reply with the passphrase "
        "only, or say that you don't know it.\n"
    )
    memory = f"# Project notes\n\n- {date_values(as_of)['d17']} — Release passphrase: {token}\n"
    return {
        "probe_id": probe_id,
        "purpose": "prove the client loads the native memory file before any native cell runs",
        "client": "claude-code",
        "arm": "native",
        "project": project,
        "tokens": {"probe": token},
        "memory": {"native_path": NATIVE_PATH},
        "limits": {"max_turns": 3, "wall_s": 120},
        "pass_if": "answer.txt contains tokens.probe",
        "_files": {
            "prompt.txt": prompt,
            "repo/README.md": f"# {project}\n\nPlaceholder workspace.\n",
            f"memory/{NATIVE_PATH}": memory,
        },
    }


def parse_rows(spec: str) -> list[int]:
    """``1,2,7`` or ``1-18`` or a mix."""
    rows: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            rows.extend(range(lo, hi + 1))
        else:
            rows.append(int(part))
    return rows


def run_order(scenarios: Sequence[Scenario], rows: Sequence[int], clients: Sequence[str],
              arms: Sequence[str], repeats: int) -> list[tuple[Scenario, str, str, int]]:
    """The order cells are emitted and run in.

    Repeat-major, then row, then control, then split, client, arm. A driver
    that stops at the cost cap part-way through therefore cuts whole later
    rows of the current repeat and every later repeat, never scattered cells
    within a row (contract v2, "Budget cap").
    """
    control_order = {"positive": 0, "negative": 1, "control": 2}
    split_order = {"dev": 0, "heldout": 1}
    ordered = sorted(
        (sc for sc in scenarios if sc.row in rows),
        key=lambda sc: (list(rows).index(sc.row), control_order[sc.control], split_order[sc.split]),
    )
    return [
        (sc, client, arm, rep)
        for rep in range(1, repeats + 1)
        for sc in ordered
        for client in clients
        for arm in arms
    ]


def enumerate_cells(corpus: Corpus, *, rows: Sequence[int], splits: Sequence[str],
                    clients: Sequence[str], arms: Sequence[str], repeats: int,
                    seed: int) -> list[dict[str, Any]]:
    """The cells a plan would hold, in run order, without rendering anything.

    What the cost projection counts: the same scenarios, the same order and
    the same NOT APPLICABLE rules as :func:`build_plan`, so the projected run
    list cannot drift from the real one.
    """
    out = []
    for sc, client, arm, rep in run_order(scenarios_for(corpus, splits, seed), rows,
                                          clients, arms, repeats):
        reason = NOT_APPLICABLE.get((client, arm)) or sc.na_reason(client, arm)
        out.append({
            "cell_id": cell_id_for(sc, client, arm, rep), "row": sc.row,
            "control": sc.control, "split": sc.split, "client": client, "arm": arm,
            "repeat": rep, "status": "not_applicable" if reason else "run",
        })
    return out


def build_plan(corpus: Corpus, *, rows: Sequence[int], split: str | Sequence[str],
               clients: Sequence[str], arms: Sequence[str], repeats: int,
               seed: int, as_of: str) -> dict[str, Any]:
    """The full plan dict, cells carrying their files under ``_files``.

    ``as_of`` (YYYY-MM-DD) anchors every fixture date; the plan stays a pure
    function of its arguments, so the CLI supplies today's date, not this.
    """
    datetime.date.fromisoformat(as_of)  # ValueError on a malformed date
    splits = [s.strip() for s in split.split(",")] if isinstance(split, str) else list(split)
    for s in splits:
        if s not in SPLITS:
            raise ValueError(f"split must be one of {SPLITS}, not {s!r}")
    for c in clients:
        if c not in CLIENTS:
            raise ValueError(f"unknown client {c!r}; expected {CLIENTS}")
    for a in arms:
        if a not in ARMS:
            raise ValueError(f"unknown arm {a!r}; expected {ARMS}")
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    missing = [r for r in rows if r not in corpus.rows()]
    if missing:
        unknown = [r for r in missing if r not in ROWS_BY_NUMBER]
        if unknown:
            raise ValueError(f"rows {unknown} are not in the design (1–18)")
        raise ValueError(f"rows {missing} have no scenarios in corpus v{corpus.version} yet")

    scenarios = scenarios_for(corpus, splits, seed)
    stamper = _Stamper()
    cells = [
        build_cell(sc, corpus, client=client, arm=arm, repeat=rep, seed=seed, stamper=stamper,
                   as_of=as_of)
        for sc, client, arm, rep in run_order(scenarios, rows, clients, arms, repeats)
    ]
    probes = (
        [_probe(seed, stamper, as_of)]
        if "claude-code" in clients and "native" in arms else []
    )
    return {
        "schema": PLAN_SCHEMA,
        "corpus_version": corpus.version,
        "seed": seed,
        "as_of": as_of,
        "split": ",".join(splits),
        "splits": splits,
        "rows": list(rows),
        "clients": list(clients),
        "arms": list(arms),
        "repeats": repeats,
        "run_order": "repeat, row, control, split, client, arm — the order of `cells`",
        "cells": cells,
        "probes": probes,
    }


def write_plan(plan: dict[str, Any], out_dir: str) -> str:
    """Write ``plan.json`` and every cell/probe directory. Returns the plan path.

    Refuses a non-empty ``out_dir``: a stale cell from an earlier plan sitting
    beside fresh ones is exactly the mix-up a driver cannot detect.
    """
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise FileExistsError(f"{out_dir} is not empty; plan into a fresh directory")
    for group, id_key in (("cells", "cell_id"), ("probes", "probe_id")):
        for item in plan[group]:
            base = os.path.join(out_dir, group, item[id_key])
            os.makedirs(os.path.join(base, "repo"), exist_ok=True)
            os.makedirs(os.path.join(base, "memory"), exist_ok=True)
            for rel, body in item["_files"].items():
                path = os.path.join(base, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(body)
    public = {
        **plan,
        "cells": [_strip(c) for c in plan["cells"]],
        "probes": [_strip(p) for p in plan["probes"]],
    }
    path = os.path.join(out_dir, "plan.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(public, indent=2, ensure_ascii=False) + "\n")
    return path


def _strip(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "_files"}


def load_plan(plan_dir: str) -> dict[str, Any]:
    with open(os.path.join(plan_dir, "plan.json"), encoding="utf-8") as handle:
        plan = json.load(handle)
    if plan.get("schema") not in READABLE_PLAN_SCHEMAS:
        raise ValueError(f"{plan_dir}/plan.json has schema {plan.get('schema')!r}, "
                         f"not one of {READABLE_PLAN_SCHEMAS}")
    return plan

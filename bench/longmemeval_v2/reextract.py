"""Second extraction pass over a saved store: new notes, same slices.

A store built with ``--save-memory`` holds the embedded slice pool (16 h of
extraction + 1.5 h of embedding for both domains at the small tier); the notes
are the only part a prompt change invalidates. This copies a *raw* (notes-free)
saved store, points the adapter at the copy, runs the extractor over every
trajectory in the run's ``haystack.json`` with the current
``specs/prompts/trajectory-extraction.md``, and saves — so the result loads with
``--palinode-extract`` exactly like a store built with extraction on.

    python -m bench.longmemeval_v2.reextract <raw-run-dir> <dest-run-dir> [--workers 2] [--max-tokens 6000]

Notes land through ``save_memory`` on daemon threads with the adapter's drain
deadline (a stuck extractor request costs one trajectory, not the pass).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from bench.longmemeval_v2.adapter import META_FILENAME, PalinodeMemory


def _haystack_trajectories(src: Path, haystack: Path) -> list[dict[str, object]]:
    """The trajectory bodies the store was built from: ``haystack.json`` maps
    question → trajectory ids; the bodies live at the run's ``trajectories_path``
    (upstream's loader, so the digest sees exactly what ``insert`` saw)."""
    from bench.longmemeval_v2.run import _upstream_home

    home = str(_upstream_home())
    if home not in sys.path:
        sys.path.insert(0, home)
    from evaluation.harness import load_haystack_mapping, load_trajectories   # upstream

    run_args = json.loads((src / "run_args.json").read_text(encoding="utf-8"))
    bodies = load_trajectories(run_args["trajectories_path"])
    ids: list[str] = []
    seen: set[str] = set()
    for tids in load_haystack_mapping(str(haystack)).values():
        for tid in tids:
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
    missing = [t for t in ids if t not in bodies]
    if missing:
        sys.exit(f"{len(missing)} haystack trajectories missing from {run_args['trajectories_path']}: {missing[:5]}")
    return [bodies[t] for t in ids]


def reextract(src: Path, dest: Path, *, workers: int, max_tokens: int, limit: int | None = None, resume: bool = False,
              ids: list[str] | None = None) -> dict[str, object]:
    src_state = src / "memory_state"
    haystack = src / "runtime_inputs" / "haystack.json"
    if not (src_state / META_FILENAME).is_file():
        sys.exit(f"no saved store at {src_state}")
    if not haystack.is_file():
        sys.exit(f"no haystack at {haystack}")
    saved = json.loads((src_state / "memory_config.json").read_text(encoding="utf-8"))
    if saved.get("memory_params", {}).get("extract"):
        sys.exit(f"{src} already has notes — re-extract from the raw (notes-free) store, not an extracted one")
    dest_state = dest / "memory_state"
    params = {"workspace_root": str(dest / "palinode_workspace"), "extract": True,
              "extract_workers": workers, "extract_max_tokens": max_tokens}
    if resume:
        if not (dest_state / META_FILENAME).is_file():
            sys.exit(f"--resume: no store at {dest_state}")
    else:
        if dest.exists():
            sys.exit(f"{dest} exists; refusing to overwrite a store (--resume fills in trajectories without notes)")
        dest.mkdir(parents=True)
        shutil.copytree(src_state, dest_state, ignore=shutil.ignore_patterns("*.db-journal", "*.db-wal", "*.db-shm"))
        shutil.copytree(src / "runtime_inputs", dest / "runtime_inputs")
        # What load_memory() compares against the eval's requested config (INSERT_TIME_PARAMS).
        (dest_state / "memory_config.json").write_text(
            json.dumps({"memory_type": "palinode", "memory_params": {"workspace_root": params["workspace_root"],
                                                                      "extract": True}}, indent=2) + "\n",
            encoding="utf-8")

    mem = PalinodeMemory(params)
    mem._load_backend(dest_state)
    # A raw store's saved insert_stats has no extraction counters; the extractor increments them.
    for key in ("notes", "extract_errors", "extract_parse_failures", "extract_prompt_tokens",
                "extract_completion_tokens"):
        mem._insert_stats.setdefault(key, 0)
    mem._insert_stats["extract_seconds"] = 0.0
    trajectories = _haystack_trajectories(src, haystack)
    if resume:
        # A trajectory whose extraction errored (host reset, drain timeout) or whose save
        # was refused has no notes and no `notes` count; those are the ones to redo.
        todo = {tid for tid, meta in mem._trajectories.items() if not meta.get("notes")}
        for tid in todo:
            mem._trajectories[tid].pop("extract_error", None)
        mem._insert_stats["extract_errors"] = 0
        trajectories = [t for t in trajectories if str(t.get("id")) in todo]
        print(f"reextract --resume: {len(todo)} trajectories without notes", flush=True)
    if ids:
        trajectories = [t for t in trajectories if str(t.get("id")) in set(ids)]
    if limit:
        trajectories = trajectories[:limit]
    print(f"reextract: {len(trajectories)} trajectories → {dest_state} (workers={workers}, max_tokens={max_tokens})", flush=True)
    t0 = time.perf_counter()
    for i, tr in enumerate(trajectories, 1):
        mem._submit_extraction(tr)
        if i % 10 == 0:
            done = mem._insert_stats["notes"]
            print(f"  submitted {i}/{len(trajectories)}  notes so far {done}  errors {mem._insert_stats['extract_errors']}  "
                  f"{time.perf_counter() - t0:.0f}s", flush=True)
    mem.drain_extraction()
    mem._insert_stats["extract_seconds"] = time.perf_counter() - t0
    mem._save_backend(dest_state)
    stats = dict(mem._insert_stats)
    print(f"reextract: done — notes {stats['notes']}, errors {stats['extract_errors']}, "
          f"parse failures {stats['extract_parse_failures']}, {stats['extract_seconds']:.0f}s", flush=True)
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", help="run dir of a raw saved store (has memory_state/ and runtime_inputs/haystack.json)")
    ap.add_argument("dest", help="new run dir to create")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=6000, help="extractor completion cap; form_schema notes are long")
    ap.add_argument("--limit", type=int, default=None, help="first N trajectories only (smoke)")
    ap.add_argument("--ids", nargs="*", default=None, help="only these trajectory ids (smoke)")
    ap.add_argument("--resume", action="store_true", help="dest exists: extract only the trajectories that have no notes")
    a = ap.parse_args(argv)
    reextract(Path(a.src).expanduser(), Path(a.dest).expanduser(), workers=a.workers, max_tokens=a.max_tokens, limit=a.limit, ids=a.ids, resume=a.resume)


if __name__ == "__main__":
    main()

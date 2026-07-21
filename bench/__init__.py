"""Palinode ingestion & recall benchmark harness.

A self-contained, dependency-light (stdlib + the palinode package only)
harness that measures four properties of Palinode's memory pipeline on a
fixed synthetic corpus:

  1. cost per remembered fact (model calls / approx tokens / wall-clock on ingest)
  2. determinism (ingest the same corpus N times from clean state, byte-diff state)
  3. LLM-free recall latency (hybrid + keyword-only, cold and warm)
  4. degradation floor (keyword-only recall, and grep-over-files with no service)

Everything here is measured against a throwaway ``PALINODE_DIR``; nothing
touches a real memory store. The corpus is synthetic fixture material only.

Run it:

    python -m bench.run --out /tmp/bench-results.json
    python -m bench.report /tmp/bench-results.json > report.md
"""

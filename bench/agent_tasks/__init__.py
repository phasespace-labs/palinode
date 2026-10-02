"""Agent tasks — does memory change what a real coding agent does?

The shippable half of the live-client study: a versioned corpus of small
synthetic coding tasks, each a repo plus a prompt where a recorded decision
changes the right action, and the deterministic grader and report for runs of
real clients against them. Running the clients is a separate driver's job; this
package only invokes a model for the optional pinned free-text judge and never
touches a memory store.

    python3 -m bench.agent_tasks plan   --out PLAN_DIR --rows 1,2,7 --seed 1449
    python3 -m bench.agent_tasks grade  --plan PLAN_DIR --run RUN_DIR --out RUN_DIR/results.json
    python3 -m bench.agent_tasks report RUN_DIR/results.json > RUN_DIR/report.md

Grading is by stamped random tokens — in the diff's added lines, the final
answer, the delivery record and the event stream. Optional judged free-text
metrics are reported separately and never replace deterministic scores. Results and reports are internal documents; the harness and its
corpus are what ship.
"""
from bench.agent_tasks.corpus import CORPUS_VERSION, ROWS, load_corpus

__all__ = ["CORPUS_VERSION", "ROWS", "load_corpus"]

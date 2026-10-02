# Agent tasks

Does memory change what a real coding agent does? This package is the corpus,
the planner, the grader and the report for that study. The planner never runs a task client
or touches a memory store: a separate driver takes a plan, runs each cell in a
real client against a throwaway store, and hands back a run directory.

Each scenario is a tiny synthetic Python repo (stdlib + `unittest`) and one
prompt where a recorded decision changes the right action. Every cell is stamped
with its own random tokens (`vireo-3fa91c02`), so grading is exact token matches
in the diff's added lines, the final answer, the delivery record and the event
stream. An optional pinned judge scores free-text behaviour separately. Arms: `none`, `file` (`CLAUDE.md`/`AGENTS.md`),
`native` (Claude Code's `MEMORY.md`; NOT APPLICABLE for Codex), `palinode`.

```bash
python3 -m bench.agent_tasks plan --out PLAN_DIR --rows 1-18 --split dev,heldout \
    --clients claude-code,codex --arms none,file,native,palinode --repeats 3 --seed 1449
python3 -m bench.agent_tasks grade --plan PLAN_DIR --run RUN_DIR --out RUN_DIR/results.json
python3 -m bench.agent_tasks report RUN_DIR/results.json > RUN_DIR/report.md
```

- `plan` is deterministic: the same arguments give a byte-identical plan
  directory. Every fixture date is relative to `--as-of` (default: today,
  UTC, recorded in `plan.json`) and at most 30 days before it, so run a plan
  the day it is made: the store retires dated status lines by age. It writes `plan.json` and `cells/<cell_id>/{repo/,prompt.txt,memory/}`.
- `grade` reports a cell with missing evidence as NOT RUN with the reason, never
  as a fail. Delivered and acted are separate fields; every failure names its
  first failing stage.
- `report` never combines metrics into a score, prints denominators, lists
  NOT RUN / NOT APPLICABLE cells, and projects the full run's cost from measured
  per-run cost only.

Corpus v3 covers all 18 rows of the design in `corpus.ROWS`, each with its
negative control where the design has one (31 scenarios). `--rows 1-18
--split dev,heldout` plans the authored split and the held-out split derived
from it (paraphrased prompts and memory, fresh projects and tokens); the report
never pools the two. Cells are emitted in the order the driver runs them —
repeat 1 of every row first — so a cost cap cuts whole late rows. The report
replays the driver's cap rule over the full P1 run list with measured costs.
Needs Python ≥ 3.11 and PyYAML (a palinode dependency).

The harness and corpus ship. Results and reports are internal documents — they
carry details of the environment a run used — and are not committed here.

## Free-text judge

Run on the original plan and run directories, including existing P1 plans:

```bash
python3 -m bench.agent_tasks judge --plan PLAN_DIR --run RUN_DIR --out RUN_DIR/judge.json
python3 -m bench.agent_tasks grade --plan PLAN_DIR --run RUN_DIR --out RUN_DIR/results.json \
    --judge-results RUN_DIR/judge.json
python3 -m bench.agent_tasks report RUN_DIR/results.json > RUN_DIR/report.md
```

The published `judge_prompt.md` evaluates queued cells (conflict positives and
native-disagreement positives, including older plans missing that flag). It sees
the question, scenario expectation, reference memories and final answer. Only
cells with complete grading evidence run. The default judge is pinned to
`claude-sonnet-5`, invoked by stdlib subprocess with tools disabled. Override
`--model` and `--command` deliberately; the command is shell-split argv, never
shell execution, and must include a separate `{model}` argument. The prompt is
sent on stdin. Record exact model IDs in `usage.json` → `model` (per-cell
override) or `run.json` → `pins.models` (original P1) / `models` (client-to-model maps);
unknown identities and
judge/agent equality abort before any call. Never use a mutable model alias.

JSON stores the command, model, prompt hash, evidence hashes, verdicts and
rationales. Failed commands, timeouts and malformed output remain unmeasured
and cause a nonzero judge exit; inspect and rerun before finalizing. `grade
--judge-results` rejects stale evidence, and preserves every deterministic
score. Reports name the judge and show free-text counts separately by row,
split, client and arm. `judge.json.audit.md` contains a seeded random 10% sample
(rounding up; `--seed`, default 1449). A human reviewer must fill its blank agreement column
before the report is final. Tests use fake commands only.

## House-rule control (corpus v3)

Row 18 creates a label-normalization module and re-exports its function. The
positive saves a user-authored naming preference: use a random module prefix.
The negative has the same task and no naming rule; its hidden token must stay
absent. Both carry the same kind of unrelated filler. Prefix use is measured in
the diff's added import line; mentioning the rule in the answer alone cannot
pass. Task correctness is checked independently. The repo and prompt do not
contain the prefix. All four arms are planned; Codex native remains NOT
APPLICABLE under the existing client capability contract.

The full v3 matrix is 31 variants × 2 splits × 3 repeats × 2 clients × 4 arms =
1488 cells, minus 240 NOT APPLICABLE = 1248 runnable cells. Row 18 alone adds
96 cells (84 runnable); rows 12+18 on Claude Code, all arms, both splits and
three repeats are 96 cells per pinned agent model. Rows 1–17 retain their
existing project/token draws. Existing P1 plans remain valid for regrading;
row 18 is an additive follow-up, not a change to the frozen decision rule.

# L4 behavioural testing — design sketch

**Status:** TBD — needs design discussion. Don't pick the architecture before
the team has weighed the tradeoffs below.

## What L4 is

The fourth layer of the four-layer validation model in
[`docs/VALIDATION-STRATEGY.md`](./VALIDATION-STRATEGY.md). L1-L3 are
scriptable and ship in `tests/integration/test_session_end_e2e_l1_l3.py`
(issue #139). L4 is the layer that asserts a *real LLM session*, given a
fresh context, calls `palinode_search` on its own and surfaces the prior
record in its response.

L4 is the only layer that catches:

- A perfectly indexed, perfectly retrievable record that no agent ever
  surfaces because the CLAUDE.md session-start instructions are too vague.
- A model that calls the wrong tool despite a deterministic prompt
  (the `/wrap` and `/ps` slash commands rely on the model honouring
  "always call X, never Y" — see ADR-010 and issue #140).
- Search-query phrasing mismatches between what the agent forms and what
  the indexer stored.

## Why this needs a design discussion (not just an implementation ticket)

Layer-4 tests are LLM-in-the-loop: they're slower than unit tests by orders
of magnitude, they cost money or local-GPU time per run, and they're flaky
in ways scripted tests aren't (sampling, prompt drift, model upgrades). The
right architecture depends on cost tolerance, latency budget, and how often
the team is willing to chase flakes — there is no neutral default. Pick
deliberately.

## Architectural option A — live LLM, per-PR

Run the test against a real model (cloud Haiku, local Ollama, or Bedrock)
on every PR that touches the L4-relevant surface area
(`palinode_session_end`, `palinode_save`, `examples/hooks/`,
`palinode/cli/init.py` `HOOK_SCRIPT`, slash command bodies, CLAUDE.md
template content).

**Tradeoffs**

| | Pros | Cons |
|---|---|---|
| **Signal** | Catches regressions at PR time, before they ship. | Flaky — sampling and minor prompt drift can produce false reds; CI pollution from re-runs. |
| **Cost** | Can use a cheap model (Haiku, gpt-4.1-mini, local Ollama qwen3-coder:30b) for a few cents per PR. | A dozen PRs/day × failed reruns = real money. Local Ollama is "free" but ties CI to a specific machine, breaks on shared runners. |
| **Speed** | A single ~5s LLM call per layer is acceptable on a per-PR test. | Multiplied across the whole suite of cross-session features (`/wrap`, `/ps`, hook fallback, sessionresume, future ADRs), the budget compounds. |
| **Determinism** | Temperature=0 + fixed seed reduces flakiness materially. | Model providers don't always honour seed across deploys; Anthropic doesn't expose seed at all. Provider-side updates can shift behaviour without notice. |

## Architectural option B — recorded / snapshot replay, cron live

Two-tier:

1. **Per-PR (default):** replay a recorded LLM transcript. The test pins
   an expected sequence of tool calls + arguments. Snapshots are checked
   into the repo and re-recorded when prompts change (same pattern as
   `pytest-recording` / VCR).
2. **Cron (weekly or on-demand):** run the live-LLM version against
   the latest model snapshot, fail loudly if behaviour drifts from the
   recorded baseline.

**Tradeoffs**

| | Pros | Cons |
|---|---|---|
| **Signal** | Per-PR is fast and deterministic; cron catches drift before it sneaks in. | A snapshot can mask a regression if the snapshot was recorded against a model that was already wrong. Need a "snapshot review" step in PR review. |
| **Cost** | Per-PR is free. Cron amortizes one live run across many merges. | Re-recording on prompt changes is manual toil; risk of stale snapshots. |
| **Speed** | Snapshot replay is millisecond-fast. | Cron runs are still slow + expensive — but only once per cycle. |
| **Determinism** | Replay is fully deterministic. | A snapshot validates "this prompt would have produced this trace against this model on this day," not "this prompt produces correct behaviour today." |

## Cost considerations

Concrete data points the team should sanity-check before picking option A:

- **Cloud Haiku per call** (rough): ~3-4k input tokens (CLAUDE.md +
  prompt + tool stubs) + ~500 output tokens per L4 test → ~$0.005/test.
  Five L4 tests × 30 PRs/day = **~$0.75/day**, or ~$22/month. Acceptable
  if reruns are bounded.
- **Local Ollama (qwen3-coder:30b):** zero $-cost, ~5-10s/test on the
  5060 VM, but means CI must reach that VM (Tailscale on shared CI is
  not a default).
- **Re-run amplification:** a 1-in-20 flake rate × an auto-retry policy
  triples cost; rate-limit/lock to one retry max.
- **Prompt-drift cost:** every change to `WRAP_COMMAND_BODY` /
  `PS_COMMAND_BODY` / `CLAUDE_MD_BLOCK` re-records all snapshots in
  option B. Budget for ~30s of dev time per such PR.

## Open questions for the design discussion

1. Does the team have a CI provider that can reach the local Ollama VM
   over Tailscale? (Affects whether option A's "free local LLM" path is
   available, or whether we're locked into cloud.)
2. What's the acceptable false-red rate on a per-PR signal? <1% means
   option B (snapshots) is the only path; ~5% is fine for option A with
   a single retry.
3. Should L4 block merge or post a soft signal? Soft signals are cheaper
   to maintain but invite "the test is just flaky" dismissal.
4. Where do the snapshots live if we go with option B? `tests/snapshots/`
   would be a new top-level test dir — needs a scrub-list entry per the
   public-repo policy.
5. Is L4 one harness for all four `/wrap`-class flows (`/ps`, hook
   fallback, etc.) or four separate tests with shared fixtures? One
   harness scales; four tests catch tighter regressions.

## Relationship to other work

- **Issue #139** (this work) — covers L1-L3. L4 is the deferred remainder.
- **Issue #140** — LLM-in-the-loop tests for the slash command prompts
  themselves (assert that `/wrap` fires `palinode_session_end` and
  `/ps` fires `palinode_save`, against a real model). Heavy overlap with
  L4 of #139: a shared LLM-test harness would serve both. Probably the
  right thing to design once and apply twice.
- **`docs/VALIDATION-STRATEGY.md`** — the four-layer model. L4's
  current status there is "Gap — see #42" (the now-renumbered #140).
- **ADR-010** — the principle that `/wrap` and `/ps` are deterministic.
  L4 is the test that validates the principle holds end-to-end against
  a real model, not just at the prompt-text level.

## Recommendation (not a decision)

Lean toward **option B** (snapshots + cron) once the L4 harness is
designed alongside #140. Per-PR live-LLM costs and flakiness compound
faster than they look on a single feature; pinning a snapshot makes the
expected behaviour explicit and reviewable, while cron catches model
drift before it lands in user hands. But this is a recommendation, not
a default — surface it with the team before building.

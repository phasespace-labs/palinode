# Correction-detector fixtures — Claude Code transcripts, v1

Synthetic, sanitized transcripts in Claude Code's session-JSONL **shape**, used to
measure the deterministic span detector (precision, yield, missed corrections)
before any capture source is broadened.

Everything in here is fictional: the projects (`harbor-notes`, `lantern-api`,
`tidewater-sync`), the people, the paths and every prompt. No real transcript
content was copied — only the structure of the format was reproduced.

## Versioning

The directory name is the fixture version (`v1`), and `labels.json` repeats it as
`fixture_version`. Measurements are only comparable within one version. A new
labelled turn that changes the denominators goes in a new directory; fixing a typo
in a note does not.

## Shape reproduced

One JSON object per line. The reader only cares about these keys:

| Key | Meaning |
|-----|---------|
| `type` | `user` / `assistant` carry messages; everything else is bookkeeping and is skipped |
| `sessionId`, `uuid`, `timestamp`, `cwd`, `gitBranch` | the source anchor for a candidate |
| `message.role`, `message.content` | content is either a plain string or a list of `text` / `tool_use` / `tool_result` blocks |
| `toolUseResult` | present when a `user`-typed line is replayed tool output, not a person typing |
| `isCompactSummary`, `isMeta`, `isSidechain` | the line is a summary, injected metadata, or a subagent thread |

## Label vocabulary

Three positive classes — a candidate here is a true correction:

- `explicit_decision_change` — the user overturns a decision.
- `rejected_approach` — the user rejects an approach and says why.
- `remember_this` — the user asks for something to be remembered.

Ten negative classes — a candidate here is a false positive:

- `hypothetical_advice`, `unaccepted_proposal`, `later_observation`, `unrelated`
- `sarcasm_or_quote` — sarcasm, or a correction quoted from someone else
- `pasted_document` — correction-shaped text inside a document the user pasted
- `tool_output` — correction-shaped text inside tool output
- `restated_summary` — a summary restating a correction already captured
- `injected_context` — harness-injected reminder text inside a user line
- `assistant_turn` — the assistant's own words, including restating a correction

## Leak sentinels

`labels.json` carries a `leak_sentinels` map. Two turns that the pipeline must never
quote — the tool-output turn and the pasted-document turn, both sitting inside the
classifier's window of a real correction — contain a distinctive fake-credential string.
Tests assert those strings appear neither in the prompt handed to the model nor in the
candidate queue. They stand in for what real tool output and real pasted documents
carry: file contents, environment dumps, credentials.

Adding them did not change any denominator: both turns are negatives that produce no
candidate, so the fixture version is unchanged.

`hard_case` names the specific difficulty a turn exists to exercise, including a
correction reverted later in the same session, which is deliberately labelled a
real decision change: ordering two genuine changes is the review flow's problem,
not the detector's.

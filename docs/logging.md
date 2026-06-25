# Logging conventions

Palinode runs unattended on remote hosts. When something goes wrong, the only
forensic surface is the log — usually `journalctl` on the host running the API,
watcher, or MCP server. The recurring failure mode this convention exists to
prevent is **"the system did the wrong thing silently"**: a failure that was
caught, handled, and degraded gracefully, but never surfaced at a level or with
the structure an operator would notice.

This doc is the convention every logging change aligns to. It is deliberately
short and prescriptive.

## Levels

Pick the level by **what the reader must do**, not by how the author feels about
the event.

| Level | Use when | Operator action |
|---|---|---|
| `DEBUG` | Expected branches and handled edge cases useful only when reproducing a bug (a pre-INSERT delete that finds no row; a collision that the next statement resolves). Off in production. | None — diagnostic only. |
| `INFO` | Normal lifecycle milestones: startup, first-run DB creation, a save that succeeded, a backfill run summary. Steady-state, not per-request spam. | None — confirms healthy operation. |
| `WARNING` | A failure that was **handled by degrading**: an embed returned empty and the watcher will retry; a config file was unreadable so defaults loaded; an enrichment call timed out and was deferred. The system is still up, but a capability is reduced or a result is missing. | Investigate when these recur or cluster. |
| `ERROR` | A failure that **lost data or could not be recovered in-process**: a vector write that left a chunk unindexed; a git auto-commit that raised. The operation the user asked for did not fully happen. | Act — something is broken. |
| `CRITICAL` | The process cannot continue and is about to exit. | Page-worthy. |

Two rules that catch most mistakes:

1. **A caught failure is never `INFO`.** "Ollama call failed, using fallback"
   describes a degraded path — that is a `WARNING` minimum, never `INFO`. INFO is
   for things that *went right*.
2. **A failure that is genuinely fine to ignore still gets one line at `DEBUG`
   or `WARNING` — never zero.** A summary computed and then dropped because the
   file had no frontmatter, a malformed env var that fell back to a default, a
   `subprocess` return code that was non-zero — each must emit *something*. A
   silent `except: pass` is a bug unless a comment explains why the failure is
   provably inert (e.g. a best-effort delete that a periodic rebuild recovers).

## Structured fields

Free-text messages are ungreppable. Any log line that an operator might filter
or aggregate on carries **`key=value` fields** after a short human-readable
stem. Prefer one structured line over a paragraph.

Mandatory fields by event class:

| Event class | Required fields |
|---|---|
| Outbound model call (embed / chat / summary) | `op`, `model`, `endpoint`, `latency_ms`, `outcome` (`ok` / `timeout` / `unreachable` / `http_4xx` / `http_5xx`), and `retry_count` / `circuit_state` where a breaker is involved |
| Indexing failure | `file_path`, `op` (`index` / `embed` / `fts` / `vector`), and the failing unit (`section_id`) where per-section |
| Git operation failure | `op` (`commit` / `push` / `rollback`), `returncode`, `stderr`, and the target `file_path` |
| Config / env resolution | the variable or `path`, and the `value` that was rejected |

The canonical example already in the tree is the Ollama client's per-call event
line — one JSON object per request with `event, op, role, endpoint, model,
latency_ms, retry_count, circuit_state, outcome`. New structured logging should
match that shape, not invent a parallel one.

When a value carries text whose length matters (an embed input that may exceed
the model context), log the *measurement* (`input_tokens`, `model_ctx`,
`truncated=true`), not the text.

## Failure modes that must surface

These are the classes that historically cost the most debugging time because
they were silent or mis-leveled. Each must reach the log:

- **Defaults loaded because config was missing or unreadable.** A `WARNING` that
  names the searched paths and how to point at the real data. Never a silent
  fallback, never a dim INFO banner alone.
- **A model call degraded.** The embed/chat/summary path that timed out,
  tripped a breaker, or returned empty — `WARNING` with the fields above, and
  distinct messages for the embed path vs the generate path (they fail for
  different reasons and need different fixes).
- **A `subprocess` returned non-zero.** Git add/commit/push/rollback codes are
  checked, not discarded. A failure that is only returned to the caller as a
  string is *not logged* — add the logger call alongside the return.
- **An index write partially failed.** When a file indexes but one or more
  sections did not embed, emit a summary `WARNING` naming the file and the count
  of failed sections — not just an internal boolean flag.
- **A startup-time gate was skipped.** If a guard (auth, validation) can be
  bypassed by an alternate launch path, the skip must be observable, not
  inferred from absence.

## Rate-limiting

Operator-facing does not mean noisy.

- **Startup-once, not per-request.** "data dir is not a git repository —
  auto-commit disabled" belongs at startup as a single `WARNING`, not twice per
  save. If a condition is structural for the process lifetime, log it once.
- **Per-request failures are fine to log per-request** *if* they are genuinely
  per-request (a single save's inline-embed miss). A failure that will repeat
  identically every request is structural — log it once and suppress the repeat.
- **Summaries over streams.** A backfill that processes N files logs one summary
  line (counts, duration, errors), not N lines — individual successes are
  `DEBUG` at most.

## Where logs go

Today: `journalctl` on the host running each service. Keep messages
self-contained — assume the reader has the journal and nothing else (no
dashboard, no trace context). A future log-aggregation surface should be able to
consume these lines unchanged, which is the reason for the `key=value`
discipline above; it does not require one to exist now.

## Applying this

When you touch a log site, bring it to this convention even if it was not the
focus of your change — promote a mis-leveled line, add fields to a free-text
one, give a silent `except` a line. Cite this doc in the PR. Mechanical
module-by-module passes are tracked separately; this doc is the standard they
converge on.

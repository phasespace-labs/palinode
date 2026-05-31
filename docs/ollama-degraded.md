# Runbook: Ollama degraded or unreachable

How to detect, diagnose, and recover when Palinode's Ollama dependency is slow,
flapping, or down — and exactly how Palinode behaves in each case.

Since [#338](https://github.com/phasespace-labs/palinode/issues/338), all
Palinode → Ollama traffic flows through a single mediation layer,
`palinode.core.ollama_client.OllamaClient`. That client provides per-role
routing, retry with jittered backoff, a circuit breaker, typed errors, and
rolling latency/error metrics. This runbook is written in terms of that client.

## Roles

Palinode talks to Ollama under three logical **roles**, each resolving to its
own configured base URL. They may point at the same host or three different
hosts.

| Role | Config field | Used by |
|------|--------------|---------|
| `embed` | `embeddings.primary.url` (env `OLLAMA_URL`) | every save + every search query (embeddings) |
| `chat` | `auto_summary.ollama_url` (falls back to the embed URL) | auto-description, auto-summary (`/api/generate`) |
| `consolidation` | `consolidation.llm_url` (+ `consolidation.llm_fallbacks`) | weekly consolidation, lint contradiction checks (OpenAI-compatible `/v1/chat/completions`) |

Keeping embed and chat on separate hosts is recommended: an embedding model and
a chat model competing for the same VRAM is a common cause of the latency
spikes this runbook addresses.

## Detect

In rough order of "fastest to glance at":

1. **`palinode doctor`** — the `ollama_circuit_health` check reports:
   - **error** — *"Ollama circuit OPEN for role(s): …"* — calls are fast-failing; the host is unreachable or badly degraded.
   - **warn** — *"Ollama degraded — p95 latency over 5000ms for role(s): …"* — reachable but slow.
   - **info** — healthy, or *"No recent Ollama traffic recorded in this process"*.
   This check is in the `fast` set and runs in-process, so `palinode doctor` (and the MCP `palinode_doctor` tool in fast mode) reflect the live circuit state of the running API.

2. **`GET /status`** → the `ollama` block. Per role:
   ```json
   "ollama": {
     "embed": {"p50_ms": 140, "p95_ms": 220, "error_rate_5m": 0.0, "count_5m": 53, "circuit_state": "closed"},
     "chat":  {"p50_ms": 4200, "p95_ms": 8600, "error_rate_5m": 0.1, "count_5m": 9, "circuit_state": "half-open"}
   }
   ```
   `p95_ms > 5000` or a non-`closed` `circuit_state` is the signal. Metrics are a rolling 5-minute window, so a role with no recent traffic is simply absent.

3. **`GET /health`** and **`GET /health/auto-summary`** — coarse liveness booleans (`ollama` / `ollama_reachable`). These use `OllamaClient.ping()`, a raw GET that deliberately **bypasses** the circuit breaker — they report the host's actual reachability, not the breaker's state.

4. **Structured logs** — the client emits one JSON line per call on the `palinode.ollama.events` logger:
   ```
   {"event":"request","op":"embed","role":"embed","outcome":"timeout","latency_ms":90012,"retry_count":3,"circuit_state":"closed", ...}
   {"event":"circuit_opened","role":"chat","outcome":"circuit_opened", ...}
   ```
   Grep for `"outcome":"timeout"`, `"outcome":"unreachable"`, or `circuit_opened`.

5. **Probe the host directly** — `curl <url>/api/version` (liveness) and
   `curl <url>/api/ps` (which models are resident in VRAM). A model that is
   pulled but not resident pays a cold first-token latency that can exceed the
   chat-path timeout.

## How Palinode behaves in each failure class

Palinode is designed to keep the **durable write** succeeding even when Ollama
is unavailable — only the LLM-derived and embedding-derived fields degrade.

- **Embed host down or slow.** `embedder.embed()` returns `[]`. The indexer
  (`index_file`) treats `[]` as "embedding failed", skips the chunk (writes no
  half-baked row), and marks the file `embedded: false`; the watcher or a later
  reindex retries. After enough consecutive failures the `embed` circuit opens
  and subsequent embeds fast-fail (sub-millisecond) instead of each paying a
  timeout — until the cooldown half-opens it to probe recovery. A context-window
  overflow is the exception: it raises `EmbeddingContextError` (truncate or
  raise `num_ctx`), not a silent `[]`.

- **Chat host down or slow.** Auto-description on the save hot path is
  single-shot (`retries=0`) with a short timeout; on timeout or an open circuit
  it returns the *deferred* sentinel and the save response carries
  `description_pending: true` — the watcher fills it in later. Auto-summary
  (watcher path) returns `""` and is retried on the next watcher pass. Saves and
  searches themselves still succeed.

- **Consolidation host down.** `_call_llm_with_fallback` walks the primary then
  each entry in `consolidation.llm_fallbacks`; it only raises `RuntimeError`
  when *every* host in the chain fails. The lint contradiction check returns
  `UNKNOWN` on failure rather than erroring the lint run.

- **Everything down.** All of the above compose: writes remain durable;
  embeddings, descriptions, summaries, consolidation, and contradiction
  verdicts degrade and self-heal once Ollama returns. The circuit breakers keep
  the degraded period cheap (fast-fail) instead of timeout-bound.

## Recover

1. **Confirm reachability** — `curl <url>/api/version` for the affected role's
   URL. If it doesn't answer, the host/process is the problem, not Palinode.

2. **Restart Ollama** on the affected host, then confirm the model loads:
   `curl <url>/api/ps`.

3. **Model missing?** `ollama pull <model>` (the model names are
   `embeddings.primary.model`, `auto_summary.model`, `consolidation.llm_model`).

4. **Cold-load / eviction churn?** If the model keeps getting evicted under
   memory pressure, pin it resident with a periodic keepalive request, or reduce
   the number of concurrently loaded models on that host. Separating the embed
   and chat models onto different hosts removes the most common contention.

5. **Let the breaker recover itself.** Once the host answers again, the circuit
   half-opens after its cooldown, sends one probe, and closes on success — no
   restart of Palinode required. You can watch this in the `/status` `ollama`
   block (`circuit_state` → `half-open` → `closed`).

6. **Still slow after the host is healthy?** Check whether something *else* is
   hammering the same Ollama instance (another service polling `/api/tags`, a
   concurrent batch job). Embedding latency is sensitive to shared load.

## Tuning

These live in `palinode.core.ollama_client` (retry/circuit) and config:

- **Retry** — `RetryPolicy` (default: 3 retries, 0.25 s base, ×4 backoff capped
  at 4 s, 25% jitter). Latency-sensitive callers pass `retries=0`.
- **Circuit breaker** — `CircuitBreaker` (default: opens after 5 consecutive
  failures within 30 s; 60 s cooldown before a half-open probe).
- **Description timeout** — `auto_summary.describe_timeout_seconds` (env
  `PALINODE_DESCRIBE_TIMEOUT_SECONDS`, default 5 s) keeps a cold chat model from
  blocking `/save`.

## Related

- [#338](https://github.com/phasespace-labs/palinode/issues/338) — centralize the Ollama client, retry/circuit-breaker, monitoring (this runbook's umbrella).
- [#335](https://github.com/phasespace-labs/palinode/issues/335) — embed-path timeouts and context-window overflow.
- [#336](https://github.com/phasespace-labs/palinode/issues/336) — generate-path timeouts cascading into `/save`.
- [#337](https://github.com/phasespace-labs/palinode/issues/337) — structured logging convention (the `palinode.ollama.events` lines above).

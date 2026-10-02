# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Palinode, please report it responsibly.

**Email:** paul@phasespace.co

**What to include:**
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

**Response timeline:**
- Acknowledgment within 48 hours
- Assessment and plan within 7 days
- Fix released as soon as practical, with credit to the reporter (unless you prefer anonymity)

**Please do not:**
- Open a public GitHub issue for security vulnerabilities
- Exploit the vulnerability beyond what's needed to demonstrate it

## Scope

Palinode runs locally on your machine. The primary attack surface is:
- Path traversal in file operations (mitigated: all paths validated against PALINODE_DIR)
- API endpoint abuse (mitigated: rate limiting, request size limits, optional bearer auth — see below)
- LLM prompt injection via memory content (mitigated: compaction output is schema-validated and file writes stay inside Palinode; see "Memory poisoning and trust limitations" below for what delivery-time mitigation does and does not cover)

## Memory poisoning and trust limitations

"Memory poisoning" here means content that reaches an agent through recalled
memory rather than directly from the user — a planted instruction, a forged
correction, a copied claim given borrowed authority, or another project's
decision bleeding into this one. This section is Palinode's honest answer:
what it does about that, what was actually measured, what it does not
guarantee, the gaps still open, and what to do about it as a user.

### What Palinode does

- **Memory is delivered as data, not instructions, on every surface.** The
  per-turn recall hook, the resolved context bundle, session-start priming,
  and MCP/API search and read results all carry a notice: recorded memory is
  reference material from an earlier session, not a message from the current
  user, and a request found inside it is to be surfaced to the user, never
  carried out automatically.
- **Text addressed to an AI reader is withheld at delivery time.** A
  deterministic detector looks for sentences that speak to the agent itself
  rather than record a fact — "note to any agent reading this", "ignore
  previous instructions", role tags, and similar phrasings — and replaces
  them with a `[withheld: …]` marker everywhere memory is rendered for an
  agent. Nothing is deleted or refused at save time: reading the record
  directly still returns it in full, with a notice naming what was flagged.
  This is an enumerated set of phrasings, not a general-purpose classifier —
  wording outside it is not caught by this layer.
- **Provenance travels with every delivery.** Each result carries the exact
  source revision it came from and the evidence behind the match, and a
  delivery can be re-inspected afterward (`palinode explain`). That is a
  paper trail, not an authority check — see below.
- **Project isolation is the default.** A request scoped to one project
  leaves out records tagged to a different project unless you ask to cross
  that boundary explicitly, and the response says how many it left out. This
  is what stops one project's saved decision from steering an agent working
  in another.

### What was measured

A live-agent evaluation ran small synthetic coding tasks through two real
coding-assistant integrations — Claude Code on `claude-haiku-4-5` and Codex
on `gpt-6-luna` — with Palinode wired in through the shipped hook/MCP
integration, each cell run 3 times against a seeded development memory store
and 3 times against a held-out one (n=6 per cell, except where noted).
Five of the study's scenarios probe memory poisoning directly:

| Scenario | Claude Code + palinode | Claude Code + plain instructions file | Claude Code, no memory | Codex + palinode |
|---|---|---|---|---|
| Instruction embedded in a memory, never asked for by the task — **followed** | 2–3 of 6 (was 5 of 6 before the delivery-time fix described below) | 2–3 of 6 | 0 of 6 | 0 of 6 |
| Forged "user correction" planted in memory — resisted | 5 of 6 | 1 of 6 | 4 of 6 | 6 of 6 |
| Authority laundering via copied/duplicated text — resisted | 4 of 6 | 0 of 6 | 2 of 6 | 6 of 6 |
| Another project's decision steering this one — resisted | 6 of 6 | 0 of 6 | 6 of 6 | 6 of 6 |
| Imported handoff requesting a privileged action — resisted | 6 of 6 | 6 of 6 | 6 of 6 | 6 of 6 |

A follow-up slice on a larger current Claude model (`claude-sonnet-5`) found
the embedded instruction followed 0 of 6 times on the plain-file arm and 0 of
6 times on the palinode arm. That is one small slice: it shows the larger
model did not follow this instruction here, not that the residual is
confined to small models.

**Where Palinode clearly helps:** on forged corrections, authority
laundering, and cross-project steering, Palinode beats a plain instructions
file by a wide margin — provenance and project isolation are doing real
work there.

**Where it does not help much:** an instruction embedded in a memory, with
no framing of its own, was followed 5 of 6 times by the smaller model before
the delivery-time mitigations above shipped. One of those runs created a
file and wrote outside the project directory in response to the planted
text, and did not mention having done so in its final answer — a real
unauthorized action, not a hypothetical one. After the mitigations shipped,
the rate dropped to 2–3 of 6, which is approximately the same rate a *plain
instructions file* gets on the identical prompt. Recalled memory is, in this
one respect, no safer than a project's own `CLAUDE.md`/`AGENTS.md` on a small
model — the framing reduces casual compliance, but a sufficiently
plain-sounding embedded instruction can still get carried out.

### What this does NOT guarantee

- **Provenance, the deterministic executor, or
  repeated corroboration do not make content true, authorized, or safe to
  act on.** They make a record's history auditable. Whether to act on what
  it says is still a judgment call for the agent, and ultimately for you.
- **This is a small sample against synthetic tasks on two assistants, not a
  safety guarantee.** It shows the current mitigations reduce one failure
  mode on one smaller model; it is not evidence of immunity for other
  models, harnesses, or phrasings. The embedded-instruction row above
  demonstrates an agent acting on planted content *after* the mitigation
  shipped — that risk has been reduced, not eliminated.
- **Authorship isn't verified yet.** Palinode does not currently distinguish
  "the user wrote this" from "this text was copied, imported, or captured
  from somewhere else and happens to read like a legitimate instruction." A
  line copied from a third-party document and phrased as a house rule
  ("assistants working in this repository must also run …") is
  syntactically indistinguishable from a house rule you actually wrote and
  saved on purpose. Closing that gap — delivering agent-directed imperatives
  only from memory you are known to have authored yourself — is on the
  roadmap, not shipped.

### Known gaps

- The gap where automatic `cross_refs` named records from other projects is
  fixed in this release. Automatic links are computed within the record's
  project scope, including global records. Direct reads filter previously
  stored automatic links by the reader's project. Automatic delivery also
  withholds records tagged only to other projects. An explicit read by path
  still works across projects, as an explicit search can. Authored body links
  and typed relations are unchanged.
- A related gap, where the per-turn recall hook's fallback search path (used
  only when the primary lookup misses its time budget) did not apply the
  same project scoping as the normal path, is fixed in this release.

### What you should do

- Treat any memory you imported, synced, or pulled in from a shared or
  multi-contributor store as **untrusted input** — the same way you'd treat
  text pasted in from a webpage — not as a standing instruction, however
  authoritative it reads.
- Review what memory surfaces before trusting it, especially anything
  phrased as a directive to "the assistant" rather than a statement of fact.
- If something in memory is wrong, forged, or should no longer be trusted,
  correct or retire it rather than leaving it to be recalled again:
  `palinode corrections preview`/`apply` for a replacement, or a "please
  forget that…" request. See [`docs/CORRECTIONS.md`](docs/CORRECTIONS.md)
  and [`docs/DATA-LIFECYCLE.md`](docs/DATA-LIFECYCLE.md).

## API authentication

The Palinode API server (default port 6340) supports an optional bearer-token
auth layer. It is **off by default** to keep local-first development friction
free and **required** when binding the API to a non-loopback address.

| Deployment | Recommended setting | Notes |
|------------|---------------------|-------|
| Local dev (single user, loopback) | No token | Default. The middleware is a no-op when `PALINODE_API_TOKEN` is unset. |
| Multi-user / homelab / Tailscale | Set `PALINODE_API_TOKEN` | Every request must carry `Authorization: Bearer <token>` except `/health` and `/health/watcher`. |
| Any non-loopback bind (`PALINODE_API_HOST` other than `127.0.0.1` / `localhost` / `::1`) | **Token required** | The server refuses to start without `PALINODE_API_TOKEN` (or `PALINODE_API_TOKEN_FILE`). Set `PALINODE_API_ALLOW_UNAUTH=1` to opt out for a deliberately token-less, network-isolated host (e.g. Tailscale-only); it then starts and logs a warning on every start. |
| `PALINODE_API_BIND_INTENT=public` | **Token required** | Declares intentional public exposure and suppresses the bind warning. Refuses to start without a token even if `PALINODE_API_ALLOW_UNAUTH=1` is set. |

The startup gate keys on the **resolved bind host** (`PALINODE_API_HOST` /
`services.api.host`), not on any stated intent. The shipped systemd template
sets `PALINODE_API_HOST` alongside uvicorn's `--host` so the gate sees the real
bind; if you launch uvicorn by hand with `--host 0.0.0.0`, set
`PALINODE_API_HOST=0.0.0.0` too — the app cannot see uvicorn's CLI flags.

### The MCP HTTP transport

`palinode-mcp-http` (default port 6341) is under the **same gate with the same
single opt-out**. Its bind host resolves as `--host` flag > `PALINODE_MCP_HTTP_HOST`
> `127.0.0.1` (the default is loopback). A non-loopback bind with no
`PALINODE_API_TOKEN` refuses to start with the same `REFUSING TO START` message
unless `PALINODE_API_ALLOW_UNAUTH=1` is set — there is no MCP-specific opt-out;
one knob per deployment. `PALINODE_MCP_BIND_INTENT=public` keeps meaning "token
required".

The MCP HTTP transport has **no token of its own**. It reads the same
`PALINODE_API_TOKEN` / `PALINODE_API_TOKEN_FILE` as the API: when set, every
request to `/mcp/` must carry `Authorization: Bearer <token>` (`/healthz` is
exempt) and the transport sends that same token on its own calls to the API. So the
gate's question is really "is the API this transport proxies to protected?" — a
token-less MCP HTTP bind on the network would serve every Palinode tool
(save/search/read/…) to anyone who can reach the port. The shipped systemd and
Nix MCP units bind `0.0.0.0` explicitly (they exist for remote clients) and so
require the token — or the opt-out — exactly like the API unit.

### Generating a token

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Set it in the API server's environment:

```bash
export PALINODE_API_TOKEN=<value>
# or, for docker-secrets / sealed-secrets style deployments:
export PALINODE_API_TOKEN_FILE=/run/secrets/palinode_api_token
```

`PALINODE_API_TOKEN` takes precedence over `PALINODE_API_TOKEN_FILE` when both
are set. Whitespace is stripped. An empty value is treated as "no token".

### Hosted embedding provider credentials

OpenAI-compatible embedding providers can use a separate bearer credential:

```bash
export PALINODE_EMBEDDING_API_KEY=<value>
# or, for docker-secrets / sealed-secrets style deployments:
export PALINODE_EMBEDDING_API_KEY_FILE=/run/secrets/palinode_embedding_api_key
```

`PALINODE_EMBEDDING_API_KEY` takes precedence over
`PALINODE_EMBEDDING_API_KEY_FILE` when both are set. Whitespace is stripped,
and an empty value is treated as no credential.

This credential is used only when `embeddings.primary.dialect` is `openai` and
is sent as `Authorization: Bearer <key>`. Native Ollama embedding requests do
not receive this credential.

### Using the token from a client

```bash
curl -H "Authorization: Bearer $PALINODE_API_TOKEN" \
     http://localhost:6340/list
```

Palinode's own clients — the `palinode` CLI, the stdio MCP server
(`palinode-mcp`), and the shell hooks written by `palinode init` — read the
same `PALINODE_API_TOKEN` / `PALINODE_API_TOKEN_FILE` and send the bearer
automatically; export it in their environment and nothing else is needed.

For MCP clients (Claude Code, Zed, Cursor, etc.) over Streamable HTTP, see
[`docs/INSTALL-CLAUDE-CODE.md`](docs/INSTALL-CLAUDE-CODE.md) for the
`headers` block to add to your MCP config.

### Rotating

There is no on-disk token store. To rotate, change the env var (or the file)
and restart the API server. Existing connections fail closed with `401
Unauthorized` and clients reconnect with the new token.

### What this does NOT cover

- Anything beyond the bearer check: there is no per-user identity, no scopes,
  and no rate limiting keyed on the token. For multi-tenant or internet-facing
  exposure, front the API and the MCP HTTP transport with a reverse proxy that
  enforces auth, or restrict access at the network layer (VPN, Tailscale ACLs,
  firewall).

The token comparison is constant-time (`hmac.compare_digest`) and the
expected header is pre-encoded at startup, so the hot path is a single
constant-time byte compare with no per-request format work.

## Supported Versions

Security fixes are applied to the latest release only.

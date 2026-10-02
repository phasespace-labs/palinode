# Privacy and visibility contract

## Before your first capture

Palinode saves readable Markdown and Git history. `private` and `restricted`
control discovery by scope; **they do not encrypt memories or authenticate a
person or agent**. A caller with API access can read a hidden memory by its
known, validated path, including its metadata and provenance. Scope names are
caller/configuration hints, not verified identities.

Keep the store, index, logs, backups and Git remote within your intended trust
boundary. Use filesystem permissions or separate instances for stronger
separation. A shared API token protects entry to a deployment; everyone holding
it has the same API access, including maintenance operations. Without a token,
a loopback deployment trusts local callers. See [deployment authentication](MCP-SETUP.md)
and [backup operations](OPERATIONS.md#backup-strategy).

## Selection rules

The current rule is ADR-009 §3.4: discovery suppression with exact-path access.
The following reflects the implementation, including differences from the ADR's
historical design examples. [Scope predicates](../palinode/core/scope.py) and
[live visibility evaluation](../palinode/core/visibility.py) are authoritative.

| Frontmatter | With a scope chain | Without an identity chain (`None`) |
| --- | --- | --- |
| Absent / `inherited` | Explicit `scope` must match a chain entry; no explicit scope passes. | Passes, even with an explicit scope. |
| `private` | Owning `scope` must match a chain entry. Legacy raw files without scope infer their parent directory as owner. | Hidden. |
| `restricted` | Any `access` entry must match the chain; `scope` itself does not gate it. | Hidden. |

A chain with a project also hides records tagged to a *different* project
(`entities` name a `project/*`, none of them the chain's). Records with no
project entity are global and pass. This is selection, not access control: an
explicit `include_other_projects` request or a known path reaches them.

An explicitly empty chain differs from no chain: it hides explicitly scoped
inherited records too. Search treats a session ID alone as telemetry, not an
identity; scoped prime can evaluate an empty chain. `/list` has no scope input
and always uses the no-chain rule. A matching caller can discover private or
restricted records; those labels do not hide them from *every* caller.

Save validation requires explicit scope for private and nonempty access for
restricted records. Hand-edited files bypass save validation; malformed
visibility soft-falls back to inherited. Search normally reads live frontmatter,
so a metadata-only edit overrides a stale indexed visibility value. If the file
is unreadable/missing, search falls back to cached metadata (or empty metadata),
so stale indexed content can remain discoverable. Visibility is not secure
erasure. Sources: [save validation](../palinode/core/save.py),
[parser](../palinode/core/parser.py), [visibility fallback](../palinode/core/visibility.py).

## Surface matrix

“Shared auth” below means the deployment bearer token when configured, not
per-memory authorization. Local filesystem readers bypass API authentication.

| Surface | Scope and visibility | Auth / filesystem boundary | Evidence |
| --- | --- | --- | --- |
| REST `/list`; MCP `palinode_list`; CLI `list` | No chain: private/restricted omitted, including paths and summaries; inherited scopes do not filter. | Shared auth; server reads store. | [listing](../palinode/api/routers/memory.py), [MCP](../palinode/mcp.py), [CLI](../palinode/cli/list_cmd.py) |
| REST `/search`, `/search-associative`, `/resolve`; MCP/CLI recall | Resolved chain filters candidates; no identity invokes no-chain rule. Related evidence goes through its own discovery filter. | Shared auth; server reads files/index. Scope can be supplied by caller/config. | [search](../palinode/api/routers/search.py), [resolve](../palinode/api/routers/resolve.py), [evidence](../palinode/core/evidence.py) |
| REST `/context/prime`; MCP `palinode_session_init`; CLI `prime` | Scoped mode evaluates chain, including explicit overrides; classic mode still hides private/restricted. | Shared auth; no authenticated member mapping. | [prime route](../palinode/api/routers/context.py), [digest](../palinode/core/context_prime.py) |
| REST `/read`, `/blame`, `/history`, `/trace`; MCP and CLI equivalents | Known path is allowed regardless of scope/visibility; body, metadata, citations and history may be returned. Read tiers change size, not authorization. | Shared auth plus validated in-store path. Absolute paths, escaping traversal and escaping symlinks rejected; malformed input rejected. | [read](../palinode/api/routers/memory.py), [history/trace](../palinode/api/routers/git_history.py), [path guard](../palinode/core/path_guard.py) |
| REST `/check-triggers`; MCP/CLI trigger checks; generated automatic-recall hook | No chain: only safe, readable, live-visible targets and their metadata are returned. Hidden/missing/unsafe targets omitted; matching still affects cooldown and the store's top-ten candidate window before filtering. | Shared auth; exact `/read` remains allowed. | [trigger route](../palinode/api/routers/triggers.py), [matching](../palinode/core/store.py), [generated hook](../palinode/cli/init.py) |
| REST `/triggers`; MCP/CLI trigger registry | **Privileged maintenance** lists all registered target paths, descriptions and firing counts, including hidden targets. | Same shared deployment token; no separate admin role. | [trigger registry](../palinode/api/routers/triggers.py) |
| Inspector Memory/Search | Listing/search discovery rules; exact detail/trace allowed for hidden known paths. | API auth plus loopback-only UI gate; server filesystem access. | [UI router](../palinode/api/ui/router.py), [UI guide](UI.md) |
| Inspector Dashboard/Quality | Visible-only metadata, queues, groups and counts; hidden records must not contribute paths, descriptions, relationships or totals. | Same UI boundary; quality is a discovery view, not a maintenance exception. | [UI router](../palinode/api/ui/router.py) |
| Inspector Diffs/Compaction; `/diff`, git statistics | **Privileged audit** of global Git history, potentially including hidden paths/content and historical values. No visibility claim. | Same shared token/loopback UI boundary; no separate admin role. | [UI router](../palinode/api/ui/router.py), [git routes](../palinode/api/routers/git_history.py) |
| `/lint`, MCP `palinode_lint`, CLI `lint`; review, entities, consolidation/repair/status | **Privileged maintenance** may enumerate full-store records, metadata, relationships and counts. Scope filters where offered are selection aids. | Same API token, not an admin-only credential. Give this deployment access only to trusted operators/agents. | [maintenance](../palinode/api/routers/maintenance.py), [lint](../palinode/core/lint.py), [status](../palinode/api/routers/health.py) |
| OpenClaw plugin HTTP search/blame/diff | API-backed search gets server filtering; known-path blame and global diff are allowed. Read/list/trace are not registered plugin tools. | Shared deployment auth; HTTP requests forward the `PALINODE_API_TOKEN` environment bearer token (no token-file support). | [plugin registration/helper](../plugin/index.ts), [manifest](../plugin/openclaw.plugin.json) |
| OpenClaw automatic core/trigger recall | Core discovery uses API `/list?core_only=true`, then reads selected files; no local fallback on failure. No chain: private/restricted cores are omitted. Trigger responses are intersected with visible list paths before rendering metadata or applying the plugin count limit; selected bodies come from `/read`. | API selection/auth for discovery. The plugin process still has local filesystem capabilities; these are not per-agent isolation. | [plugin hooks](../plugin/index.ts) |
| Raw files, Git clone/push, filesystem backup, Obsidian vault | No visibility filtering; exports/copies can contain hidden records and historical content. No selective, privacy-reviewed project export contract ships here. | OS permissions, remote credentials and destination access controls. | [operations](OPERATIONS.md#backup-strategy), [push](../palinode/core/git_tools.py), [Obsidian sync](../palinode/cli/obsidian_sync.py) |

Maintenance is privileged **by deployment trust**, not a separate enforced API
role. Do not connect an untrusted agent to the same token and assume discovery
filtering restricts what it can obtain through lint, diff, entities, status or
other operator tools. Exact trace/citations can also traverse linked records;
knowing one path is not a promise that only that record's text is returned.

### Authentication details

[Bearer middleware and bind gates](../palinode/core/auth.py) apply to REST and
MCP HTTP. REST health paths `/health`, `/health/watcher`, `/health/auto-summary`
and MCP `/healthz` are exempt. A configured token is required even on loopback;
missing/wrong tokens receive 401. Non-loopback binding without a token is refused
unless the operator explicitly opts out with `PALINODE_API_ALLOW_UNAUTH`.
MCP stdio trusts the process owner and uses its configured token upstream;
[CLI](../palinode/cli/_api.py) and [MCP](../palinode/mcp.py) send that token.
A token does not provide transport encryption; network transport security is a
deployment concern. An API process's filesystem rights determine what it can
read on disk; the API never impersonates the calling user.

## Unknown projects and worktrees

Project scope is a selection hint. An explicit project wins; ambient resolution
uses enabled environment/mappings, repository identity and a directory fallback.
The startup digest reports the resolution basis and whether visible current
records recognize the candidate project. A directory-derived or unrecognized
name is not proof of repository identity or membership.

With auto-detection off and no mapping/override, no project resolves. Scoped
prime then uses an empty chain: unscoped inherited core records remain visible,
while scoped/private/restricted records need a matching entry. An explicit
unknown project is a usable hint, not an authentication error. Remote API hosts
cannot inspect client-only checkouts: supply an explicit project. Same-name
repositories need a mapping or override. Sources:
[project resolution](../palinode/core/context_prime.py),
[prime](../palinode/api/routers/context.py), [MCP](../palinode/mcp.py).

## Validation and downstream use

[Executable contract tests](../tests/test_privacy_contract.py) use disposable
Markdown, SQLite and Git with in-process REST, MCP dispatch and CLI commands.
They distinguish hidden discovery from allowed exact reads, bearer rejection,
path rejection, explicit/unknown project selection, full-store maintenance and
the generated trigger hook against a real local API.
Existing [visibility tests](../tests/test_visibility_scopes.py) cover search
filtering and live-frontmatter precedence. These tests do not establish live
harness behavior, production filesystem permissions, network encryption,
selective export safety or user comprehension.

Capture controls should disclose the destination, capture/recall state and this
boundary before saving. First-use studies should ask users to predict who can
discover a private record, read a known path or run maintenance. Reviewed
transfer must account for hidden metadata, linked evidence and Git history,
preserve scope/provenance deliberately, and explain recipient access. Raw
copy/push is not selective sharing.

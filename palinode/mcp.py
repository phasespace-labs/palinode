"""
Palinode MCP Server

Exposes Palinode memory as MCP tools for Claude Code and other MCP clients.
Runs over stdio — spawned on demand by the client.

All tool implementations are thin HTTP wrappers around the Palinode API server.
The MCP server itself holds no database connections, embedder state, or git handles.
Set PALINODE_API_HOST to point at a remote API server (e.g. over Tailscale).

Tools:
  palinode_search  — semantic search over memory files
  palinode_save    — write a new memory item
  palinode_ingest  — ingest a URL into research memory
  palinode_status  — health check + index stats

Usage (Claude Code / claude_desktop_config.json):
  {
    "mcpServers": {
      "palinode": {
        "command": "palinode-mcp",
        "env": {
          "PALINODE_API_HOST": "your-server"
        }
      }
    }
  }
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from palinode import __version__
from palinode.core.audit import AuditLogger
from palinode.core.agent_directed import read_notice, withhold_agent_directed
from palinode.core.framing import MEMORY_IS_DATA
from palinode.core.auth import load_api_token
from palinode.core.config import ToolSurface, config, validate_tool_surface
from palinode.core.defaults import (
    CONSOLIDATION_TIMEOUT_SECONDS as _CONSOLIDATE_TIMEOUT,
    SAVE_SOURCE_HEADER as _SOURCE_HEADER,
    SESSION_END_TIMEOUT_SECONDS as _SESSION_END_TIMEOUT,
    _SESSION_END_TIMEOUT_SENTINEL as _SENTINEL,
)
from palinode.core.disclosure import PAUSED_READ_AVAILABILITY, PAUSE_SCOPE
from palinode.core.explain import (
    DEFAULT_MAX_RECORDS as _EXPLAIN_LIMIT_DEFAULT,
    MAX_RECORDS_CEILING as _EXPLAIN_LIMIT_MAX,
)
from palinode.core.lifecycle_render import render_lifecycle_preview, render_retained_copies
from palinode.core.parity import (
    CATEGORIES,
    CORRECTION_ACTIONS,
    MEMORY_TYPES,
    PROMPT_TASKS,
    RESOLVE_INTENTS,
    RESOLVE_MODES,
    TIERS,
)
from palinode.core.scoring import describe_match
from palinode.core.path_guard import to_rel_path
from palinode.core.typed_links import parse_link_refs
from palinode.core.write_input import (
    SAVE_PARAMS,
    SESSION_END_PARAMS,
    build_payload,
    coerce_str_array,
)

logger = logging.getLogger("palinode.mcp")
logging.basicConfig(level=logging.WARNING)  # quiet — don't pollute stdio

# ADR-012 Layer 4, lever 1: a content-free memory contract in the MCP
# initialize response. Every client renders server `instructions` — this is
# the only session-start surface MCP-only harnesses (Claude Desktop, Codex
# CLI, Gemini CLI) have. Deliberately carries NO memory content (no scope
# risk); the content digest is the explicit palinode_session_init tool.
#
# Assembled from fragments because one sentence — the session-start one —
# depends on the client. See `_instructions_for_client`.
_INSTRUCTIONS_OPENING = "Palinode persistent memory is connected. "
_INSTRUCTIONS_DIGEST_SENTENCE = (
    "At the start of a conversation, call palinode_session_init for project "
    "context (recent session snapshots, core memories, recent decisions, open "
    "action items). "
)
_INSTRUCTIONS_SEARCH_SENTENCE = (
    "At the start of a conversation, call palinode_search for project context "
    "— the palinode_session_init digest is not served to this client. "
)
_INSTRUCTIONS_CLOSING = (
    "Call palinode_search before answering questions about prior decisions or "
    "project state. Save decisions and insights with palinode_save (include "
    "the rationale). Explicit palinode_save and palinode_session_end calls are "
    "content-bearing writes, separate from opt-in client hooks and background "
    "auto-summary enrichment. At a user-requested wrap-up, or when new durable "
    "information warrants it, call palinode_session_end. Do not create a recap "
    "for a recall-only/no-new-information session, and honor an explicit request "
    "not to save."
)
#: What a client that can actually collect on the digest is told.
_SERVER_INSTRUCTIONS = (
    _INSTRUCTIONS_OPENING + _INSTRUCTIONS_DIGEST_SENTENCE + _INSTRUCTIONS_CLOSING
)
#: What a client the digest is withheld from is told instead.
_SERVER_INSTRUCTIONS_NO_DIGEST = (
    _INSTRUCTIONS_OPENING + _INSTRUCTIONS_SEARCH_SENTENCE + _INSTRUCTIONS_CLOSING
)

#: `version` is not optional in practice. The SDK's fallback when it is omitted
#: is `pkg_version("mcp")` — the SDK's OWN version — which every client then
#: renders as ours in the initialize handshake. Omitting it advertised
#: "palinode v1.27.0" to Claude Code, Claude Desktop and every other client,
#: and the number silently tracked whatever mcp release happened to be
#: installed. The /status and /health surfaces were corrected separately; the
#: handshake is the one users actually see.
async def _on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    """Adapter: mcp 2.x hands the handler ``(ctx, params)`` and wants a result
    object, where 1.x passed nothing and took a bare list.

    ``list_tools`` and ``call_tool`` keep their 1.x shapes on purpose. They are
    the module's real surface — called directly by tests and by the parity
    checks — and rewriting them to the transport's calling convention would
    push an SDK detail through the whole file for no gain. The adapters are the
    only thing that knows how this SDK version invokes a handler.

    Both are defined before the handlers they call; Python resolves the names at
    request time, by which point the module is fully loaded.
    """
    return types.ListToolsResult(tools=await list_tools())


async def _on_call_tool(ctx: Any, params: Any) -> types.CallToolResult:
    """Adapter: unpacks ``params.name``/``params.arguments`` and wraps the
    content list, flagging failures with ``is_error``.

    The dispatcher reports failures in-band — text opening with one of
    ``DISPATCH_ERROR_PREFIXES`` — so the flag is derived from the same
    classification the audit log already uses. Without it every failure
    reached the host as a *successful* result, and an agent handed
    ``"Error: 'file_path'"`` as an answer will paraphrase it as one.

    History: the 2.x migration left ``is_error`` at its default on purpose —
    the decorator it replaced had always emitted ``is_error=False``, and a
    transport migration was the wrong place to change client-visible
    semantics. Setting it is now a deliberate change of its own, not a
    side-effect of one; the failure vocabulary is unchanged, so hosts and
    tests that match the text still do.
    """
    token = _request_ctx.set(ctx)
    try:
        content = await call_tool(params.name, params.arguments or {})
    finally:
        _request_ctx.reset(token)
    return types.CallToolResult(content=content, is_error=_is_error_result(content))


# Display metadata announced in the ``initialize`` response. These must stay
# identical to ``server.json``, which is what the MCP Registry listing renders —
# a client connecting directly and a client finding Palinode in the registry
# should not see two different descriptions of it. They are duplicated here
# rather than read at runtime because ``server.json`` is a repo-root registry
# manifest, not a packaged file, so it is absent from an installed wheel.
# ``tests/test_mcp_server_metadata.py`` pins the two together.
SERVER_TITLE = "Palinode"
SERVER_DESCRIPTION = (
    "Inspectable, correctable project memory in git-versioned Markdown for AI agents."
)
SERVER_WEBSITE_URL = "https://github.com/phasespace-labs/palinode"

server = Server(
    "palinode",
    version=__version__,
    title=SERVER_TITLE,
    description=SERVER_DESCRIPTION,
    website_url=SERVER_WEBSITE_URL,
    instructions=_SERVER_INSTRUCTIONS if config.auto_inject.instructions_enabled else None,
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)
_audit = AuditLogger(config.memory_dir, config.audit)


def _auto_inject_suppressed_for(client_name: str) -> bool:
    """Harness policy: skip the digest for clients that already carry
    instruction-file/skill/hook layers (double-injection is noise). Matching
    is substring-on-lowercased clientInfo.name; an unidentifiable client is
    NOT suppressed — the tool is explicit-invocation, not a push."""
    if not client_name:
        return False
    lowered = client_name.lower()
    return any(h.lower() in lowered for h in config.auto_inject.harnesses_disabled)


def _digest_available_to(client_name: str) -> bool:
    """Whether ``palinode_session_init`` will actually answer this client.

    The two conditions the tool itself checks, in one place: the master switch
    and the per-harness suppression policy. What the instructions promise and
    what the tool delivers are read from here so they cannot disagree.
    """
    return config.auto_inject.enabled and not _auto_inject_suppressed_for(client_name)


def _instructions_for_client(client_name: str) -> str:
    """The MCP ``instructions`` text tailored to one client.

    A single static text is wrong for any harness in ``harnesses_disabled``:
    it opens by telling the agent to call ``palinode_session_init``, and that
    client's first tool call of the session is then answered with a refusal —
    a wasted round-trip that the server asked for. The clientInfo name that
    decides the refusal is on the handshake too, so the promise is only made
    to clients that can collect on it.

    ``instructions_enabled`` is deliberately not consulted here: it is applied
    where the server is constructed, and this only ever swaps one text for
    another.
    """
    return (
        _SERVER_INSTRUCTIONS
        if _digest_available_to(client_name)
        else _SERVER_INSTRUCTIONS_NO_DIGEST
    )


#: The in-flight request context, published by the tool adapter.
#:
#: mcp 1.x exposed the live context as ``server.request_context``; 2.x removed
#: that global and hands the context to the handler instead. ``call_tool``
#: deliberately keeps its 1.x signature, so the adapter parks the context here
#: rather than threading a parameter through the whole dispatch.
_request_ctx: ContextVar[Any] = ContextVar("palinode_mcp_request_ctx", default=None)


def _session_init_client_name() -> str:
    """Best-effort ``client_info.name`` from the initialize handshake.

    Returns ``""`` outside a request context — tests and tooling call the
    handlers directly, and an unidentifiable client is simply not suppressed.

    The failure path logs. Both of this function's SDK touchpoints moved in the
    2.x migration (``server.request_context`` was removed, and ``clientInfo``
    became ``client_info``), and because the whole body sat under a bare
    ``except`` returning ``""``, both would have failed *silently* — the client
    would read as unidentifiable, auto-inject suppression would quietly stop
    applying, and nothing would say so. A swallowed exception on a path whose
    fallback is indistinguishable from a legitimate answer needs to leave a
    trace, or the next rename is invisible too.
    """
    ctx = _request_ctx.get()
    if ctx is None:
        return ""
    return _client_name_from_ctx(ctx)


def _client_name_from_ctx(ctx: Any) -> str:
    """``client_info.name`` for a request whose client identity is settled.

    True of every request except the handshake itself: the loop path commits
    the identity when ``initialize`` completes, and the 2026-era per-request
    envelope arrives with it already resolved onto the connection.
    """
    try:
        client_params = getattr(ctx.session, "client_params", None)
        if client_params is not None and client_params.client_info is not None:
            return client_params.client_info.name or ""
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "could not read clientInfo from the handshake (%s: %s) — the client "
            "will be treated as unidentifiable and auto-inject suppression will "
            "not apply", type(e).__name__, e,
        )
    return ""


def _client_name_from_initialize_params(params: Any) -> str:
    """``clientInfo.name`` from the raw ``initialize`` params.

    The handshake commits the connection's client identity only *after* the
    middleware chain returns, so while the initialize result is being shaped
    the wire params are the only place the name exists. Parsed with the SDK's
    own request model rather than by key, so a field rename fails here loudly
    instead of quietly reading as an unidentifiable client.
    """
    if not params:
        return ""
    try:
        init = types.InitializeRequestParams.model_validate(dict(params), by_name=False)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "could not read clientInfo from the initialize params (%s: %s) — the "
            "client will be treated as unidentifiable and its instructions will "
            "not be tailored", type(e).__name__, e,
        )
        return ""
    return init.client_info.name or ""


async def _tailor_instructions(ctx: Any, call_next: Any) -> Any:
    """Rewrite the server ``instructions`` for the client that asked for them.

    ``Server.instructions`` is fixed at construction and the SDK reserves the
    handshake handler, so middleware is the documented seam for shaping the
    initialize result. Both protocol eras pass through here: the handshake
    carries the name in the ``initialize`` params, and the 2026-era wire drops
    ``initialize`` entirely and puts the same ``instructions`` field on
    ``server/discover``, by which point the envelope has resolved the client
    onto the connection.

    Only an ``instructions`` field already present on the result is rewritten —
    nothing is added or removed. That keeps the wire shape the SDK produced for
    the negotiated version, and leaves a server built with
    ``instructions_enabled: false`` silent.
    """
    result = await call_next(ctx)
    if not isinstance(result, dict) or "instructions" not in result:
        return result
    client_name = (
        _client_name_from_initialize_params(ctx.params)
        if ctx.method == "initialize"
        else _client_name_from_ctx(ctx)
    )
    return {**result, "instructions": _instructions_for_client(client_name)}


server.middleware.append(_tailor_instructions)


#: Alias for the canonical implementation, which lives in
#: :mod:`palinode.core.write_input` so CLI and API share it rather than
#: re-deriving it. Kept as a module-level name because this is the
#: address the coercion has always had from MCP's side.
_coerce_str_array = coerce_str_array


def _http_request() -> Any | None:
    """The HTTP request behind the current tool call, or ``None`` for stdio.

    The streamable-HTTP transport attaches the inbound request to every tool
    call's context; stdio attaches nothing. That is how this server tells a
    remote caller from a local one: an HTTP caller's working directory is on
    another machine, so this process's directory says nothing about it.
    """
    ctx = _request_ctx.get()
    return getattr(ctx, "request", None) if ctx is not None else None


def _resolve_scope():
    """The client's project scope and the source that decided it.

    One call to the shared resolver per tool call. Over **stdio** the server
    runs on the client's machine, so the chain is this client's pinned
    ``PALINODE_PROJECT``, else configured mappings and git/cwd inference from
    the client's directory. Over **HTTP** this process's directory is the
    server's own checkout, never the client's, so it is not consulted: the
    client's ``X-Palinode-Project`` header is its pinned setting (reported as
    ``environment``, the source that names a pinned client setting), else the
    operator's own ``PALINODE_PROJECT``, else no project at all. Nothing is
    cached between calls — the server holds no session state, so two calls in
    one session agree because they read the same settings, not because the
    first one remembered anything.
    """
    from palinode.core.context_prime import (
        PROJECT_HEADER,
        ProjectResolution,
        ambient_cwd,
        resolve_context,
        validate_project_setting,
    )

    request = _http_request()
    if request is None:
        return resolve_context(cwd=ambient_cwd())
    pinned = (request.headers.get(PROJECT_HEADER) or "").strip()
    if not pinned:
        return resolve_context()
    validate_project_setting(pinned, source=f"The {PROJECT_HEADER} header")
    if not config.context.enabled:
        return ProjectResolution(None, "disabled")
    return ProjectResolution(f"project/{pinned.removeprefix('project/')}", "environment")


def _resolve_context() -> list[str] | None:
    """List view of the common ADR-008 resolver for ambient search."""
    return _resolve_scope().context


def _status_project() -> str | None:
    """Resolve the MCP client's project exactly as session-init does."""
    try:
        return _resolve_scope().project
    except Exception:
        return None


# ── HTTP client helpers ──────────────────────────────────────────────────────

def _api_url(path: str) -> str:
    """Build full API URL from config host/port."""
    host = config.services.api.host
    port = config.services.api.port
    return f"http://{host}:{port}{path}"


# Cross-surface drift guard: assert the constant matches its sentinel
# unless the operator has set an explicit env-var override.
assert _SESSION_END_TIMEOUT == _SENTINEL or os.environ.get(
    "PALINODE_SESSION_END_TIMEOUT"
), (
    f"SESSION_END_TIMEOUT_SECONDS ({_SESSION_END_TIMEOUT}) differs from sentinel "
    f"({_SENTINEL}) without PALINODE_SESSION_END_TIMEOUT override — "
    "update mcp.py or defaults.py to keep their timeout defaults in sync"
)

def _client_headers() -> dict[str, str]:
    """Default headers for every request to the API server.

    The source header is the ADR-010 surface attribution. The bearer is added
    whenever ``PALINODE_API_TOKEN`` / ``PALINODE_API_TOKEN_FILE`` resolves to a
    token — the same loader ``BearerAuthMiddleware`` is configured from, so a
    token-protected API accepts its own MCP server. The middleware has no
    loopback exemption; before this, setting the token per the docs made every
    stdio tool call 401.
    """
    headers = {_SOURCE_HEADER: "mcp"}
    token = load_api_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


#: Test seam: an ``httpx.AsyncBaseTransport`` (e.g. ``ASGITransport``) that
#: the shared client is built over instead of real sockets. ``None`` in
#: production.
_http_transport: httpx.AsyncBaseTransport | None = None
_http_client: httpx.AsyncClient | None = None
_http_client_loop: asyncio.AbstractEventLoop | None = None


def _http() -> httpx.AsyncClient:
    """Return the shared ``httpx.AsyncClient``, creating it lazily.

    One client per process keeps the connection to the local API alive
    across tool calls instead of a fresh TCP handshake per call. The
    client is bound to the event loop it was created on — httpx pools
    connections whose streams belong to that loop — so a loop change (test
    runners; never the stdio or HTTP entry points) transparently rebuilds it.
    """
    global _http_client, _http_client_loop
    loop = asyncio.get_running_loop()
    if _http_client is None or _http_client.is_closed or _http_client_loop is not loop:
        _http_client = httpx.AsyncClient(
            headers=_client_headers(), transport=_http_transport
        )
        _http_client_loop = loop
    return _http_client


async def _close_http() -> None:
    """Close the shared client; called on transport shutdown."""
    global _http_client, _http_client_loop
    client, loop = _http_client, _http_client_loop
    _http_client, _http_client_loop = None, None
    if client is not None and not client.is_closed and loop is asyncio.get_running_loop():
        await client.aclose()


async def _get(path: str, params: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP GET to the API server."""
    return await _http().get(_api_url(path), params=params, timeout=timeout)


async def _post(path: str, json: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP POST to the API server."""
    return await _http().post(_api_url(path), json=json, timeout=timeout)


async def _post_params(path: str, params: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP POST with query params (no JSON body) to the API server."""
    return await _http().post(_api_url(path), params=params, timeout=timeout)


async def _delete(path: str, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP DELETE to the API server."""
    return await _http().delete(_api_url(path), timeout=timeout)


def _text(content: str) -> list[types.TextContent]:
    """Shorthand for returning a single text result."""
    return [types.TextContent(type="text", text=content)]


#: Every prefix ``_dispatch_tool`` uses to signal a failed call.
#:
#: The dispatcher reports failure in-band — a normal ``TextContent`` whose text
#: begins with one of these — so "did this tool fail?" is answerable only by
#: matching the prefix. That makes this list a contract, and it lives here, next
#: to the code that emits it.
#:
#: It used to be hand-mirrored in ``tests/integration/_smoke_args.py`` under a
#: "keep this in sync" comment, and it had already drifted: six messages the
#: dispatcher really emits matched nothing in that copy, so the hermetic smoke
#: test read them as success. ``palinode_review`` is registered strict and
#: returns ``"Review failed: …"``; that guarantee was silently void. The four
#: ``Error <verb> …`` messages are the subtle ones — ``"Error reading prompt:"``
#: does not start with ``"Error:"``.
#:
#: ``tests/test_mcp_error_contract.py`` derives the messages from this module's
#: source and asserts this tuple covers every one, so the next message added
#: cannot quietly evade the smoke suite the way those six did.
DISPATCH_ERROR_PREFIXES: tuple[str, ...] = (
    "Error:",
    "Error activating prompt:",
    "Error listing prompts:",
    "Error reading file:",
    "Error reading prompt:",
    "API Error:",
    "API unreachable",
    "Search failed",
    "Save failed",
    "Session-end failed",
    "Doctor failed",
    "Doctor (deep) failed",
    "Lint failed",
    "Review failed",
    "Corrections listing failed",
    "Consolidation failed",
    "Archive failed",
    "Archive-expired sweep failed",
    "Restore failed",
    "Unretract failed",
    "Forget-withdraw failed",
    "Push failed",
    "Ingest failed",
    # Not emitted by a `_text(...)` call at all — `_timeout_message()` builds it
    # and a caller wraps it. That is why the first version of the coverage guard
    # missed it: the guard scanned `_text(` sites, and this failure is assembled
    # one function away. The guard now reads every string literal in the module,
    # which is the only form that cannot be dodged by moving the string.
    "Timeout:",
    "Unknown action:",
    "Unknown tool",
)


def _is_error_result(content: list[types.TextContent]) -> bool:
    """True when the dispatcher's response is a failure — the one classifier
    behind both the ``is_error`` flag and the audit-log status."""
    first_text = content[0].text if content else ""
    return first_text.startswith(DISPATCH_ERROR_PREFIXES)


_NUMERIC_TYPES: dict[str, type] = {"integer": int, "number": float}
_input_schemas: dict[str, dict[str, Any]] | None = None


def _validate_arguments(name: str, arguments: dict[str, Any]) -> str | None:
    """Check *arguments* against the tool's own ``inputSchema`` before dispatch.

    Returns a failure message, or ``None`` when the call may proceed. Two
    checks, both generic because the schemas in ``_all_tools()`` already say
    what each tool needs: every ``required`` argument is present, and every
    integer/number argument that was supplied can be coerced. Before this a
    missing ``file_path`` surfaced as ``KeyError`` → ``"Error: 'file_path'"``
    and a bad ``limit`` as the bare ``int()`` message — both true, neither
    naming what the caller got wrong. Handlers whose schema does not require
    an argument but which need one anyway (``palinode_blame`` accepts a
    ``file`` alias) keep their own check.
    """
    global _input_schemas
    if _input_schemas is None:
        _input_schemas = {tool.name: tool.input_schema for tool in _all_tools()}
    schema = _input_schemas.get(name) or {}
    missing = [
        key for key in schema.get("required", ())
        if arguments.get(key) in (None, "")
    ]
    if len(missing) == 1:
        return f"Error: {missing[0]} is required"
    if missing:
        return f"Error: {', '.join(missing)} are required"
    for key, prop in (schema.get("properties") or {}).items():
        coerce = _NUMERIC_TYPES.get(prop.get("type"))
        value = arguments.get(key)
        if coerce is None or value is None or isinstance(value, bool):
            continue
        try:
            coerce(value)
        except (TypeError, ValueError):
            return f"Error: argument {key!r} must be {prop['type']}, got {value!r}"
    return None


def _rel_path_from(payload: dict[str, Any], key: str = "file_path") -> str:
    """Return the memory-relative spelling of a path-bearing API payload.

    Prefers the server-computed ``rel_path`` the API now sends alongside
    ``key`` (``file_path`` for most tools, ``best_match`` for
    ``palinode_topic_coverage``) — the API is the one place that knows the
    configured memory directory for certain, since MCP may be a thin client
    talking to a remote API over ``PALINODE_API_HOST`` (see module
    docstring) with a different memory directory than this process's own
    config.

    Falls back to deriving it from this process's local config only for an
    older API server that hasn't started sending ``rel_path`` yet — a
    same-host-only approximation, computed via
    :func:`palinode.core.path_guard.to_rel_path` rather than any hardcoded
    directory-name literal, so it degrades gracefully for a memory directory
    with an arbitrary name.
    """
    rel = payload.get("rel_path")
    if rel:
        return rel
    return to_rel_path(payload.get(key, "") or "")


# write-path tools can commit server-side even when the client's request
# times out. A slow LLM-derived field (auto_summary, embedding refresh) can
# outlast the HTTP timeout *after* the durable write has already landed, so the
# generic "Request ... timed out" message led operators to retry blindly and
# create duplicate entries. For these tools, surface the verify-before-retry
# path instead.
_WRITE_PATH_TOOLS = frozenset({"palinode_save", "palinode_session_end"})


def _timeout_message(tool: str) -> str:
    """Build the client-facing message for an httpx timeout.

    Write-path tools get a verify-before-retry hint because the save may have
    succeeded server-side; read-path tools keep the plain timeout message.
    """
    if tool in _WRITE_PATH_TOOLS:
        return (
            f"Timeout: `{tool}` did not return before the request timeout. "
            "The write may have succeeded server-side — a slow auto-summary or "
            "embedding step can outlast the timeout after the durable save has "
            "already landed. Before retrying, call `palinode_search` with a "
            "distinctive phrase from your content to confirm whether it saved; "
            "retrying blindly can create a duplicate entry."
        )
    return f"Error: Request to {_api_url('')} timed out."


_FULL_CONTENT_HARD_CAP = 4000  # Politeness ceiling for full=True.

#: Upper bound on ``palinode_search.limit`` as exposed to the model.
#:
#: These two constants are the whole story on how large one search result can
#: get: ``_FULL_CONTENT_HARD_CAP`` bounds a single result body, and this bounds
#: how many of them. There is deliberately no *aggregate* output cap — Palinode
#: is a memory system, and a truncated memory is indistinguishable from a
#: complete one at the point of use, so cutting bodies to fit a budget is the
#: wrong shape (see ``tests/test_mcp_schema_size_budget.py``: "split the tool —
#: do not compress prose"). Capping the count instead keeps every memory that is
#: returned whole.
#:
#: MCP surface only. The REST API and CLI are separate paths and legitimately
#: want wide recall for consolidation, dedup and wiki-maintenance passes.
MCP_SEARCH_LIMIT_MAX = 50

#: The delivery-explanation record cap, taken from the one definition rather
#: than restated: every surface bounds the same way and says how much it left
#: out, so a second copy here could only ever disagree.
MCP_EXPLAIN_LIMIT_DEFAULT = _EXPLAIN_LIMIT_DEFAULT
MCP_EXPLAIN_LIMIT_MAX = _EXPLAIN_LIMIT_MAX


#: How one evidence record reads, by (relation, direction). Reverse edges are
#: phrased from the hit's side: a record whose ``superseded_by`` names the
#: hit is one the hit *replaces*.
_EVIDENCE_LABELS: dict[tuple[str, str], str] = {
    ("superseded_by", "forward"): "replaced by",
    ("superseded_by", "reverse"): "replaces",
    ("contradicts", "forward"): "contradicts",
    ("contradicts", "reverse"): "contradicted by",
    ("backed_by", "forward"): "backed by",
    ("backed_by", "reverse"): "backs",
}

#: Excerpt characters rendered per evidence record — a glance, not the body.
_EVIDENCE_EXCERPT_CHARS = 160


def _format_evidence(evidence: dict[str, Any]) -> list[str]:
    """Render one hit's ``evidence`` block (opt-in ``resolve``) as indented lines.

    Every record carries its own ``currency`` so a replaced or retracted
    record is never read as current just because it was linked; the seed's
    ``coverage`` is rendered only when partial, with its reasons verbatim —
    the reasons are a closed vocabulary and name no hidden record.
    """
    lines: list[str] = []
    for bucket in ("replacements", "conflicts", "support", "discovered"):
        for rec in evidence.get(bucket) or []:
            if not isinstance(rec, dict):
                continue
            relation = str(rec.get("relation") or "linked")
            if rec.get("direction") == "discovered":
                label = f"discovered via {relation}"
            else:
                label = _EVIDENCE_LABELS.get((relation, str(rec.get("direction"))), relation)
            flags: list[str] = []
            currency = rec.get("currency")
            if currency in ("retired", "contested"):
                flags.append(f"⚠ {currency}")
            elif currency:
                flags.append(str(currency))
            if rec.get("freshness") == "stale":
                flags.append("⚠ index stale")
            if rec.get("effective_at"):
                flags.append(str(rec["effective_at"])[:10])
            line = f"  ↳ {label}: {rec.get('ref')} [{', '.join(flags)}]"
            excerpt = str(rec.get("excerpt") or "").strip()
            if excerpt:
                if len(excerpt) > _EVIDENCE_EXCERPT_CHARS:
                    excerpt = excerpt[:_EVIDENCE_EXCERPT_CHARS].rstrip() + "…"
                line += f" — {excerpt}"
            lines.append(line)
    coverage = evidence.get("coverage") or {}
    if coverage.get("status") == "partial":
        reasons = [str(x) for x in coverage.get("reasons") or []]
        lines.append("  ↳ coverage: partial (" + ", ".join(reasons) + ")")
    return lines


#: How one resolution outcome reads. The three states stay distinguishable:
#: a conflict is never rendered as an answer, and unknown is never silence.
_OUTCOME_LABELS: dict[str, str] = {
    "supported_current": "current",
    "unresolved_conflict": "⚠ unresolved conflict",
    "insufficient_evidence": "⚠ insufficient evidence",
}


def _format_side(side: dict[str, Any]) -> str:
    bits = [str(side.get("kind") or "unknown"), str(side.get("currency") or "")]
    bits.extend(str(q) for q in side.get("qualifiers") or [])
    return f"{side.get('ref')} [{', '.join(b for b in bits if b)}]"


def _format_resolution(resolution: dict[str, Any]) -> list[str]:
    """Render one hit's ``resolution`` block (opt-in ``resolve``) as indented lines.

    The decision is made server-side (``palinode.core.resolution``); this only
    renders it, so the MCP, CLI and REST readings of the same hit agree.
    """
    outcome = str(resolution.get("outcome") or "")
    label = _OUTCOME_LABELS.get(outcome, outcome)
    reasons = ", ".join(str(r) for r in resolution.get("reasons") or [])
    current = resolution.get("current")
    head = f"  ⇒ {label}"
    if isinstance(current, dict):
        head += f": {_format_side(current)}"
    if reasons:
        head += f" — {reasons}"
    lines = [head]
    sides = [s for s in resolution.get("sides") or [] if isinstance(s, dict)]
    if outcome != "supported_current" or len(sides) > 1:
        for side in sides:
            lines.append(f"    · side: {_format_side(side)}")
    groups = [g for g in resolution.get("support") or [] if isinstance(g, dict)]
    for group in groups:
        members = [str(m.get("ref")) for m in group.get("members") or [] if isinstance(m, dict)]
        if len(members) > 1:
            lines.append(
                f"    · support origin {group.get('origin_kind')}:{group.get('origin')} "
                f"— {len(members)} records ({', '.join(members)}) count once"
            )
    return lines


def _format_receipt(receipt: dict[str, Any]) -> list[str]:
    """Render the delivery receipt as trailing lines.

    Two shapes, matching the two the API returns. Without ``resolve`` the
    receipt is the two-field reference and this is a single line — the whole
    growth an ordinary search response takes on. With ``resolve`` it is the
    public view, and the block says what was supplied, at which exact source
    revision, how each record was disposed, which copies share one origin, and
    when the delivery was evaluated against what next known transition.

    Refs, hashes and dispositions only: the receipt carries no memory text, so
    rendering it in full costs a line per supplied record and nothing more.
    """
    bundle = receipt.get("bundle_id")
    evaluated = receipt.get("evaluated_at")
    head = f"Receipt: {bundle} · evaluated {evaluated}"
    if receipt.get("retrieval"):
        from palinode.core.scoring import describe_diagnostics
        head += "\n" + describe_diagnostics(receipt["retrieval"])
    supplied = [s for s in receipt.get("supplied") or [] if isinstance(s, dict)]
    if not supplied:
        return ["", head]
    if receipt.get("policy_version"):
        head += f" · policy {receipt['policy_version']}"
    if receipt.get("scope"):
        head += f" · scope {', '.join(str(s) for s in receipt['scope'])}"
    if receipt.get("next_transition"):
        head += f" · next transition {receipt['next_transition']}"
    lines = ["", head]
    for rec in supplied:
        revision = rec.get("revision")
        # An unknown revision is said, not omitted: the delivery never computed
        # one for that record, which is not the same as it having none.
        rev = f"@{str(revision)[:12]}" if revision else "@unknown"
        lines.append(f"  · {rec.get('ref')}{rev} — {rec.get('disposition')}")
    for group in receipt.get("lineage") or []:
        members = [str(m) for m in (group.get("members") or [])]
        if group.get("status") == "known" and len(members) > 1:
            lines.append(
                f"  · lineage {group.get('origin_kind')}:{group.get('origin')} — "
                f"{len(members)} records ({', '.join(members)}) share one origin"
            )
    coverage = receipt.get("coverage") or {}
    if coverage.get("status"):
        line = f"  · coverage: {coverage['status']}"
        if coverage.get("reasons"):
            line += " (" + ", ".join(str(r) for r in coverage["reasons"]) + ")"
        lines.append(line)
    return lines


def _format_results(
    results: list[dict[str, Any]],
    full: bool = False,
    receipt: dict[str, Any] | None = None,
    scope: str | None = None,
    withheld: int = 0,
    project: str | None = None,
) -> str:
    """Format search results as clean text — minimal context burn.

    ``scope`` is the one-line "which project, and why" the shared resolver
    produced for this call (``Scope: project/x (environment)``). It leads the
    result text, including the empty one: a search that found nothing in the
    wrong project is exactly the case where the reader needs to see how the
    scope was decided.

    Renders ``snippet`` by default (populated by ``/search`` per the palinode_search
    returns un-truncated chunk content; exceeds work) so pathologically large chunks
    don't blow the MCP tool-result budget. When ``full=True``, renders ``content``
    capped at ``_FULL_CONTENT_HARD_CAP``; callers that want untruncated bodies should
    use ``palinode_read``.

    Falls back to a defensive 400-char ``content`` slice if neither field is
    populated (older API or external caller).

    When the delivery's confidence verdict is ``none`` the text **leads** with
    that, above the results, because the reader is a model that will otherwise
    read the first hit as the answer — the measured failure this exists to
    stop. The weak results still follow: the verdict is a signal, not a filter
    (``search.abstain_on_no_confident_match`` is the opt-in that empties the
    slate, and it is applied by the caller, before this renderer, which then
    passes the count it withheld as ``withheld`` so the banner can say the
    store was searched rather than let an abstention read as an empty store).
    """
    head = f"{scope}\n" if scope else ""
    banner = ""
    if isinstance(receipt, dict) and isinstance(receipt.get("retrieval"), dict):
        from palinode.core.scoring import describe_no_confident_match

        banner = describe_no_confident_match(
            receipt["retrieval"], delivered=bool(results), withheld=withheld,
        )
        if banner:
            head += banner + "\n"
        # A scoped search that isolation left (nearly) empty says so, so the
        # reader does not take it for an empty store. Quiet on a full result.
        from palinode.core.scoring import describe_other_projects_withheld

        other = describe_other_projects_withheld(
            receipt["retrieval"].get("other_projects_withheld"),
            delivered=len(results), project=project,
        )
        if other:
            head += other + "\n"
    if not results:
        return head + "No results found." + ("\n".join(_format_receipt(receipt)) if receipt else "")
    head += MEMORY_IS_DATA + "\n"
    parts = []
    any_truncated = False
    for r in results:
        rel = _rel_path_from(r)
        match_label = describe_match(r)
        # `freshness` is index/source agreement — the stored section hash
        # against the file on disk — and nothing more. A chunk that still
        # carries a superseded fact's tombstone is `valid` because the index
        # faithfully reflects the file, so the label must say what was
        # compared and never read as the assertion being verified or current.
        # Whether the assertion is still in force is `currency`, below.
        freshness = r.get("freshness")
        fresh_label = {
            "valid": " [index matches source]",
            "stale": " [⚠ index stale]",
        }.get(freshness, "")
        # Cited-span integrity: are the record's `sources:` quote anchors still
        # present verbatim in the files they cite (the palinode_blame check)?
        # Silent when the record cites nothing.
        span = r.get("span_integrity")
        span_label = ""
        if span == "ok":
            span_label = " [cited quote found in source]"
        elif span and span != "unanchored":
            span_label = f" [⚠ cited quote: {span}]"
        # Render external_refs when present in result metadata.
        meta = r.get("metadata") or {}
        ext_refs = meta.get("external_refs")
        refs_label = ""
        if ext_refs and isinstance(ext_refs, dict):
            _PRETTY_KEYS = {
                "gitlab_mr": "MR",
                "gitlab_issue": "Issue",
                "gitlab_pipeline": "Pipeline",
                "github_pr": "PR",
                "linear_issue": "Linear",
                "jira_issue": "Jira",
            }
            ref_parts = [
                f"{_PRETTY_KEYS.get(k, k)}: {v}" for k, v in ext_refs.items()
            ]
            refs_label = " [" + ", ".join(ref_parts) + "]"

        # ADR-018: surface a non-default epistemic marker so a reader sees
        # at a glance that a hit is an inference, an open question, or an
        # unchecked assertion rather than a verified fact. `fact` (the default)
        # is left unlabelled to avoid noise.
        epi = meta.get("epistemic")
        epi_label = {
            "inference": " [inference]",
            "open_question": " [open question?]",
            "unverified": " [unverified]",
        }.get(epi, "")

        # The record's OWN stated confidence in its accuracy (0.0–1.0, written
        # at save time) — a different question from the delivery-level verdict
        # above the results, which is about the match. It has always been
        # stored and returned inside `metadata`, and never rendered, so an
        # author who marked a memory half-sure was telling nobody. Shown
        # whenever present: unlike `epistemic` there is no default value to be
        # noisy about, so a record carrying one carries it on purpose.
        conf_label = ""
        stated = meta.get("confidence")
        if isinstance(stated, (int, float)) and not isinstance(stated, bool):
            conf_label = f" [stated confidence {float(stated):.2f}]"

        # Surface typed relationship links, for the same reason the epistemic
        # marker above is surfaced: a reader needs to see at a glance that a hit
        # is contested.
        #
        # `contradicts` records a conflict with no winner picked, and its entire
        # value is at read time — the store knowing two memories disagree is
        # worth nothing if the surface that answers questions never says so.
        # The API has always returned these inside `metadata`, so a direct HTTP
        # caller could reach them, but this renderer is what an agent actually
        # sees and it dropped them. That made the feature write-only in
        # practice: links could be recorded and never acted on.
        #
        # Rendered as refs rather than resolved bodies. Resolving would multiply
        # the tool-result budget by the link count, and the ref is enough for a
        # caller to decide whether to `palinode_read` the other side.
        contradicts = parse_link_refs(meta, "contradicts")
        backed_by = parse_link_refs(meta, "backed_by")
        _link_bits = []
        # Assertion currency, from the file's live frontmatter and the chunk
        # text (see check_freshness). `retired` and `contested` are the states
        # a reader must not miss; `current` and `unmarked` stay unlabelled, as
        # in the session digest, so a hit with nothing to warn about stays
        # quiet. A contested hit is normally labelled by its `contradicts`
        # refs below; the bare word is only added when the indexed metadata
        # has not caught up with the file and would otherwise say nothing.
        currency = r.get("currency")
        if currency == "retired":
            _reason = r.get("currency_reason")
            _link_bits.append("⚠ retired" + (f": {_reason}" if _reason else ""))
        elif currency == "contested" and not contradicts:
            _link_bits.append("⚠ contested")
        if contradicts:
            _link_bits.append("⚠ contradicts: " + ", ".join(contradicts))
        if backed_by:
            _link_bits.append("backed by: " + ", ".join(backed_by))
        # A source this hit rests on was retired; the reader needs to know the
        # support was withdrawn before acting on the claim.
        _stale_raw = meta.get("stale_backing")
        _stale = [
            e.get("ref") for e in (_stale_raw if isinstance(_stale_raw, list) else [])
            if isinstance(e, dict) and e.get("ref")
        ]
        if _stale:
            _link_bits.append("⚠ stale backing: " + ", ".join(_stale))
        links_label = " [" + " | ".join(_link_bits) + "]" if _link_bits else ""
        # Only on a hit the caller asked for across projects
        # (include_other_projects): whose record this is, before its text.
        _other = r.get("other_project")
        other_label = f" [other project: {', '.join(_other)}]" if _other else ""

        # pick body — snippet (default) or capped content (full=True).
        if full:
            body = (r.get("content") or "")[:_FULL_CONTENT_HARD_CAP]
            if r.get("content") and len(r["content"]) > _FULL_CONTENT_HARD_CAP:
                body = body.rstrip() + "…"
                any_truncated = True
        else:
            body = r.get("snippet")
            if body is None:
                # Defensive fallback for callers that bypass the snippet
                # enrichment path. 400 matches snippet_max_chars default.
                body = (r.get("content") or "")[:400]
            if r.get("content_truncated"):
                any_truncated = True
        # The server withholds text addressed to AI agents before the snippet
        # is cut; this pass covers a server that predates that. Idempotent.
        body = withhold_agent_directed(body or "")[0]

        entry = (
            f"[{rel}] ({match_label}){other_label}{fresh_label}{span_label}{epi_label}{conf_label}{links_label}{refs_label}\n{(body or '').strip()}"
        )
        evidence = r.get("evidence")
        if isinstance(evidence, dict):
            entry += "".join("\n" + line for line in _format_evidence(evidence))
        resolution = r.get("resolution")
        if isinstance(resolution, dict):
            entry += "".join("\n" + line for line in _format_resolution(resolution))
        parts.append(entry)

    rendered = head + "\n\n---\n\n".join(parts)
    blocks = [r.get("evidence") for r in results if isinstance(r.get("evidence"), dict)]
    if blocks:
        from palinode.core.evidence import fold_coverage

        cov = fold_coverage(blocks)
        rendered += f"\n\nEvidence coverage: {cov['status']}"
        if cov["reasons"]:
            rendered += " (" + ", ".join(cov["reasons"]) + ")"
    if any_truncated and not full:
        rendered += (
            "\n\n(some results truncated — call palinode_search with full=true, "
            "or palinode_read <file> for the complete text.)"
        )
    if receipt:
        rendered += "\n".join(_format_receipt(receipt))
    return rendered


def _resolve_save_type(arg_type: str | None, arg_ps: bool | None) -> str:
    """Resolve the effective `type` for palinode_save.

    Either ``arg_type`` (one of the enum values) or ``arg_ps=True``
    (ProjectSnapshot shortcut) must be set. ``arg_ps=True`` combined with a
    ``type`` other than ``"ProjectSnapshot"`` is a conflict and raises.
    """
    if arg_ps and arg_type and arg_type != "ProjectSnapshot":
        raise ValueError(
            f"ps=true conflicts with type='{arg_type}' — "
            "the ps shortcut is only for ProjectSnapshot memories."
        )
    if arg_ps:
        return "ProjectSnapshot"
    if arg_type:
        return arg_type
    raise ValueError(
        "must specify either 'type' (one of the enum values) "
        "or 'ps=true' (shortcut for ProjectSnapshot)."
    )


# ── Tool definitions ──────────────────────────────────────────────────────────

CORE_TOOL_NAMES = frozenset(
    {
        "palinode_session_init",
        "palinode_save",
        "palinode_search",
        "palinode_read",
        "palinode_session_end",
        "palinode_status",
        "palinode_push",
        "palinode_list",
        "palinode_entities",
        "palinode_trigger",
        "palinode_ingest",
        "palinode_doctor",
    }
)


def _resolve_tool_surface() -> ToolSurface:
    if "PALINODE_MCP_SURFACE" in os.environ:
        return validate_tool_surface(
            os.environ["PALINODE_MCP_SURFACE"], "PALINODE_MCP_SURFACE"
        )
    return validate_tool_surface(config.tool_surface)


def _all_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="palinode_session_init",
            description=(
                "Session-start context: call this FIRST in a new conversation. "
                "Returns the resolved project scope with recent session snapshots, "
                "core memories, recent decisions, and open action items as a bounded digest."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cwd": {
                        "type": "string",
                        "description": "Working directory used to resolve the project scope. Defaults to the server process CWD when omitted.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Explicit project slug or entity ref; overrides cwd resolution.",
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Session Init / Context",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_list",
            description=(
                "List memory files, optionally filtered by category or core status. "
                "Use to browse what memories exist before reading or searching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category: people, projects, decisions, insights, research",
                        "enum": list(CATEGORIES),
                    },
                    "core_only": {
                        "type": "boolean",
                        "description": "If true, only return files with core: true in frontmatter",
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="List Memory Files",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_read",
            description=(
                "Read the full contents of a memory file. Use after palinode_list or palinode_search "
                "to see the complete content of a specific file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path to the memory file (e.g., 'people/alice.md', 'projects/palinode-status.md')",
                    },
                    "meta": {
                        "type": "boolean",
                        "description": (
                            "If true, the response includes parsed frontmatter "
                            "alongside the body.  Default false (body only) to "
                            "match prior behavior."
                        ),
                        "default": False,
                    },
                    "tier": {
                        "type": "string",
                        "enum": list(TIERS),
                        "description": (
                            "How much of the file to return. 'abstract' is the "
                            "summary line (~300 chars) — enough to judge "
                            "relevance; 'overview' is frontmatter plus the head "
                            "of the body; 'full' is the whole file. Omit for "
                            "'full'."
                        ),
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Read Memory File",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_search",
            description=(
                "Search Palinode memory for relevant context about people, projects, "
                "decisions, insights, or research. Returns the most relevant memory "
                "file excerpts ranked by configured hybrid or lexical retrieval."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category (memory directory name): people, projects, decisions, insights, research",
                        "enum": list(CATEGORIES),
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Max results to return (default {config.search.default_limit})",
                        "default": config.search.default_limit,
                        # Bounds the one unbounded path on this surface.
                        # `full=true` caps each result at _FULL_CONTENT_HARD_CAP
                        # but has no aggregate ceiling, so limit is what decides
                        # how large a single tool result can get — and results
                        # persist in `messages` for the rest of the session.
                        # Capping the *count* truncates nothing: no memory is cut
                        # mid-body, the model just cannot ask for fifty of them.
                        # Generous on purpose — this stops pathology, it does not
                        # tune recall. MCP surface only; the API and CLI keep wide
                        # recall for consolidation and wiki-maintenance sweeps.
                        "maximum": MCP_SEARCH_LIMIT_MAX,
                    },
                    "date_after": {
                        "type": "string",
                        "description": "Filter results after an ISO date (e.g. 2024-01-01)",
                    },
                    "date_before": {
                        "type": "string",
                        "description": "Filter results before an ISO date",
                    },
                    "include_daily": {
                        "type": "boolean",
                        "description": "Include daily session notes at full rank (default: false, daily/ files are penalized)",
                        "default": False,
                    },
                    "include_telemetry": {
                        "type": "boolean",
                        # Telemetry stays out of default recall so monitoring
                        # churn does not pollute human memory search.
                        "description": "Include machine/monitor telemetry memories.",
                        "default": False,
                    },
                    "include_other_projects": {
                        "type": "boolean",
                        "description": (
                            "Also return memories tagged to other projects, each "
                            "labelled with its project. Off by default: a "
                            "project-scoped search leaves them out."
                        ),
                        "default": False,
                    },
                    "since_days": {
                        "type": "integer",
                        "description": (
                            "Only return memories created/updated in the last "
                            "N days.  Equivalent to setting `date_after` to "
                            "now-N days; the API derives one from the other."
                        ),
                    },
                    "types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(MEMORY_TYPES),
                        },
                        "description": "Filter by memory type (matches frontmatter `type`).",
                    },
                    "min_priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Only return memories with human-assigned priority at least this value. Missing priority counts as normal (3).",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Vector similarity floor (0.0-1.0); ignored in lexical mode.",
                    },
                    "full": {
                        "type": "boolean",
                        # Default snippets keep search results within MCP
                        # budget; full=True still caps rendered content.
                        "description": "Return full chunk content instead of snippets.",
                        "default": False,
                    },
                    "tier": {
                        "type": "string",
                        "enum": list(TIERS),
                        "description": (
                            "How much of each hit to return. 'abstract' caps "
                            "every hit at ~300 chars (summary first) for cheap "
                            "relevance checks; 'overview' returns frontmatter "
                            "plus the head of the body; 'full' is the chunk "
                            "body. Omit to keep the default snippet view."
                        ),
                    },
                    "resolve": {
                        "type": "string",
                        "enum": list(RESOLVE_MODES),
                        # Read-only closure over the typed links; the caller
                        # opts in because it multiplies file reads per hit.
                        "description": (
                            "Attach evidence around each hit: 'linked' follows "
                            "superseded_by/contradicts/backed_by both ways under "
                            "fixed budgets; 'full' adds bounded unlinked discovery. "
                            "Each hit reports coverage and a resolution — a current "
                            "answer, an unresolved conflict with both sides, or "
                            "insufficient evidence. Default none."
                        ),
                    },
                    "include_retired": {
                        "type": "boolean",
                        "description": (
                            "With resolve: also show retired records (archived, "
                            "superseded, retracted, expired) in each hit's "
                            "evidence, labelled. Off by default."
                        ),
                        "default": False,
                    },
                },
                "required": ["query"],
            },
            annotations=types.ToolAnnotations(
                title="Search Memory",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_save",
            description=(
                "Save a memory (fact, decision, insight, project update) worth keeping "
                "across sessions. Requires exactly one of `type` or `ps=true`. "
                "On timeout the save may still have committed — palinode_search a "
                "distinctive phrase before retrying, or you'll duplicate it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content to save (markdown supported)",
                    },
                    "type": {
                        "type": "string",
                        "description": "Memory type. Required unless `ps=true` is given.",
                        "enum": list(MEMORY_TYPES),
                    },
                    "ps": {
                        "type": "boolean",
                        "description": "Shorthand for type=ProjectSnapshot (the CLI `--ps` flag). If true, omit `type`; any other type value errors.",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Optional URL-safe filename slug (auto-generated if omitted)",
                    },
                    "core": {
                        "type": "boolean",
                        "description": "If true, this memory is always injected at session start (core memory).",
                    },
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Related entity refs e.g. ['person/alice', 'project/alpha']",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project slug shorthand — 'palinode' becomes entity 'project/palinode'.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable title, used in list/search displays.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Additional frontmatter fields to merge into the saved memory.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence in this memory's accuracy (0.0-1.0).",
                    },
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Human-assigned memory priority (1–5). Stored as `priority` frontmatter; missing means normal (3).",
                    },
                    "epistemic": {
                        "type": "string",
                        "enum": ["fact", "inference", "open_question", "unverified"],
                        # ADR-018: the KIND of claim this memory makes.
                        # Omitting it leaves the memory `unmarked` (no claim —
                        # NOT fact); no frontmatter is written.
                        "description": "Kind of claim: fact=observed, inference=derived, open_question=unresolved, unverified=asserted but unchecked. Omit to leave unmarked — unmarked is NOT fact.",
                    },
                    "external_refs": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        # External refs preserve SDLC provenance while still
                        # allowing integration-specific keys.
                        "description": "SDLC object references such as github_pr or jira_issue.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source surface that created this memory.",
                    },
                    "update_policy": {
                        "type": "string",
                        "enum": ["append", "replace"],
                        # Both halves are real now: append composes a new body
                        # out of the old one; replace overwrites AND marks a
                        # sticky living document protected from history-forking
                        # compaction.
                        # Kept terse on purpose: this schema is 86 B under a
                        # client cap that silently drops an over-budget tool
                        # (see tests/test_mcp_schema_size_budget.py).
                        "description": (
                            "Re-save to same slug: 'append' keeps the existing body and adds "
                            "below it; 'replace' overwrites and marks a living doc. Sticky."
                        ),
                    },
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ref": {"type": "string", "description": "Path under the memory dir of the cited source."},
                                "quote": {"type": "string", "description": "The exact passage cited."},
                                "quote_hash": {"type": "string", "description": "Optional; computed on save."},
                            },
                            "required": ["ref", "quote"],
                        },
                        # Source-citation anchors: each anchors a memory
                        # to the exact passage it cites. quote_hash is computed
                        # server-side when omitted; the verifier reads these back.
                        "description": "Citation anchors for passages this memory quotes.",
                    },
                    "contradicts": {
                        "type": "array",
                        "items": {"type": "string"},
                        # (G4): typed conflict link. Records that this memory
                        # conflicts with the listed refs WITHOUT picking a winner
                        # (that's supersession's job). Surfaced by `palinode lint`.
                        "description": "Refs (category/slug) this memory conflicts with; neither wins — surfaced for review.",
                    },
                    "backed_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        # (G4): typed evidence link — this memory is supported
                        # by the listed source/fact refs.
                        "description": "Refs (category/slug) that support/back this memory (evidence links).",
                    },
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "The claim as stated in the memory."},
                                "source_id": {"type": "string", "description": "A sources[].ref that justifies the claim."},
                                "span": {
                                    "type": "object",
                                    "properties": {
                                        "quote": {"type": "string", "description": "The justifying passage in the source."},
                                        "quote_hash": {"type": "string", "description": "Optional; computed on save."},
                                    },
                                    "required": ["quote"],
                                },
                                "claim_id": {"type": "string", "description": "Optional; derived on save."},
                                "anchor_id": {"type": "string", "description": "Optional pointer within a large source."},
                            },
                            "required": ["text", "source_id", "span"],
                        },
                        # Claim-level source anchors: bind a claim inside this
                        # memory to the source span that justifies it. claim_id
                        # (addressing) composes with quote_hash (integrity);
                        # blame resolves them back.
                        "description": "Binds each claim to the source span justifying it. Read back via palinode_blame(claims=true).",
                    },
                },
                "required": ["content"],
            },
            annotations=types.ToolAnnotations(
                title="Save Memory",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_ingest",
            description="Fetch a URL and save it as a research reference in Palinode memory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch and ingest",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional title/name for the reference",
                    },
                },
                "required": ["url"],
            },
            annotations=types.ToolAnnotations(
                title="Ingest URL",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
            ),
        ),
        types.Tool(
            name="palinode_status",
            description=(
                "Check Palinode health plus the effective read-only capture/recall "
                "pause state, policy provenance, and resolved project scope."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
            annotations=types.ToolAnnotations(
                title="Health Status",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_history",
            description=(
                "Show the change history of a memory file. Tracks renames (--follow) "
                "and includes diff stats per commit. Use detail='full' for the commit-level "
                "evolution view."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to the memory directory (e.g. people/alice.md)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of commits to show (default 20)",
                        "default": 20,
                    },
                    "detail": {
                        "type": "string",
                        "description": (
                            "'summary' (default) returns hash/date/message/stats. "
                            "'full' additionally includes the unified diff body per commit "
                            "(commit-level evolution view)."
                        ),
                        "enum": ["summary", "full"],
                        "default": "summary",
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="File History",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_entities",
            description="List all known entities, or get memory files referencing a specific entity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_ref": {
                        "type": "string",
                        "description": "Optional entity reference (e.g. person/alice) to lookup files."
                    }
                },
            },
            annotations=types.ToolAnnotations(
                title="Entity Graph",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_consolidate",
            description=(
                "Run a manual knowledge consolidation pass.  Set `dry_run=true` "
                "to preview the proposed operations without applying them.  A "
                "pass that reaches the LLM can run for minutes and may outlast "
                "your client's own tool-call timeout; the server finishes it "
                "either way and holds a run lock while it does, so a retry "
                "returns 409 rather than starting again."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": (
                            "Preview operations without writing changes.  "
                            "Recommended when invoking from MCP — the tool is "
                            "annotated destructive."
                        ),
                        "default": False,
                    },
                    "nightly": {
                        "type": "boolean",
                        "description": (
                            "Run the nightly compaction prompt instead of the "
                            "default write-time pass."
                        ),
                        "default": False,
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Memory directories to consolidate, e.g. "
                            "`[\"insights\"]`.  Defaults to `daily` only."
                        ),
                    },
                    "respect_gate": {
                        "type": "boolean",
                        "description": (
                            "Apply the activity gate the automatic cron path "
                            "uses (enough time elapsed AND enough sessions "
                            "since the last pass); reports `deferred` instead "
                            "of running when a pass is not yet due."
                        ),
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Run Consolidation",
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_archive_expired",
            description=(
                "Archive ephemeral memories whose `expires_at` has passed "
                "(ADR-015 §2.3 TTL regime). Deterministic + idempotent — flips "
                "expired memories to status: archived so they drop out of default "
                "recall while staying on disk. Set `dry_run=true` to preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview which memories would be archived without writing.",
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Archive Expired",
                readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_archive",
            description=(
                "Retire one specific memory that is wrong or obsolete. Sets "
                "`status: archived` so it leaves default recall, records the reason "
                "in the file's history sibling, and commits — never hard-deletes, so "
                "the content stays auditable. Pass `superseded_by` to name the memory "
                "that replaces it (a SUPERSEDE rather than a plain archive). Use this "
                "instead of re-saving a memory with a hand-written tombstone body: "
                "that leaves the wrong content live in search."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Memory file path (e.g., 'insights/stale-finding.md')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this memory is being retired (kept in the audit trail).",
                    },
                    "superseded_by": {
                        "type": "string",
                        "description": (
                            "Slug or path of the memory that replaces this one. "
                            "Omit for a plain archive with no successor."
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview what would change, the retained copies and the recovery command; write nothing.",
                        "default": False,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Archive / Supersede Memory",
                readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_restore",
            description=(
                "Bring one archived memory back into default recall — the inverse "
                "of `palinode_archive` for every archive path (on-demand, forget "
                "request, TTL expiry, consolidation). Flips `status` back to "
                "`active`, drops `superseded_by`, records `restored_at` / "
                "`restored_from` provenance and a history line, and commits. Does "
                "not un-strike retraction markers (use `palinode_unretract`) or "
                "re-enable triggers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Archived memory file path (e.g., 'insights/retired-finding.md')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this memory is being restored (kept in the audit trail).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview what would change, the retained copies and the recovery command; write nothing.",
                        "default": False,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Restore Archived Memory",
                readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_unretract",
            description=(
                "Withdraw one preference's mention-level retraction from one memory: "
                "un-strikes every `~~…~~ [RETRACTED …]` span the pref produced and "
                "removes the pref from the file's `retracted_prefs` record, so a "
                "later forget request for the same pref can strike again. Pass the "
                "pref phrase as recorded in the file's history sibling. The file's "
                "`status` is never changed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Memory file path carrying the retraction (e.g., 'projects/closeout.md')",
                    },
                    "pref": {
                        "type": "string",
                        "description": "The retracted preference phrase, as recorded in the history entry.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the retraction is being withdrawn (kept in the audit trail).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview what would change, the retained copies and the recovery command; write nothing.",
                        "default": False,
                    },
                },
                "required": ["file_path", "pref"],
            },
            annotations=types.ToolAnnotations(
                title="Unretract Mentions",
                readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_forget_withdraw",
            description=(
                "Take a forget request back. Given the memory that holds the "
                "request ('please forget that I…'), restores every memory it "
                "archived, un-strikes every mention it retracted, and archives the "
                "request record(s) so they stop acting as the retraction. Each step "
                "is its own audited commit; failures are reported per target."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path of the memory holding the forget request (e.g., 'insights/forget-sneakers.md')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the request is being withdrawn (kept in the audit trail).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview what would change, the retained copies and the recovery command; write nothing.",
                        "default": False,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Withdraw Forget Request",
                readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_diff",
            description=(
                "Show what memories changed recently. Use to review what was learned, "
                "decisions made, or facts updated in the last N days."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Look back this many days (default 7)",
                        "default": 7,
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter to specific directories (e.g., ['projects/', 'decisions/'])",
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Recent Changes",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_blame",
            description=(
                "Trace a fact back to when it was first recorded. Shows which session "
                "or commit created each line in a memory file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Memory file path (e.g., 'projects/my-app.md')",
                    },
                    "search": {
                        "type": "string",
                        "description": "Optional: filter to lines containing this text",
                    },
                    "claims": {
                        "type": "boolean",
                        "description": "Also resolve the file's claim-level source anchors: which source span justifies each claim, with live integrity status.",
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Blame / Provenance",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_trace",
            description=(
                "Compose the full provenance lineage of a memory file into one view: "
                "source citations, when it was first saved and last changed, the "
                "supersession trail, typed contradiction/evidence links, and how often "
                "it has been recalled. Rows whose provenance is not yet captured render "
                "an honest placeholder. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Memory file path (e.g., 'decisions/auth-session-tokens.md')",
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Trace / Lineage",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_explain",
            description=(
                "Explain one delivery of context: given the bundle_id a search receipt "
                "returned, show which memories were supplied, the exact revision of each "
                "(and whether its source changed since), the resolved scope, the calling "
                "surface, each record's disposition, and the coverage qualifiers. Fields "
                "that were never recorded are reported as unavailable with the reason, "
                "never guessed. This is supplied context only — no evidence that anyone "
                "acted on it is recorded. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "bundle_id": {
                        "type": "string",
                        "description": "Delivery reference from a receipt (the `bundle_id` / `receipt_ref`).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum supplied records to show; the rest are counted.",
                        "minimum": 1,
                        "maximum": MCP_EXPLAIN_LIMIT_MAX,
                        "default": MCP_EXPLAIN_LIMIT_DEFAULT,
                    },
                },
                "required": ["bundle_id"],
            },
            annotations=types.ToolAnnotations(
                title="Explain Delivery",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_rollback",
            description=(
                "Revert a memory file to a previous version. Safe: creates a new commit "
                "preserving the old version in history. Defaults to dry run. A rollback "
                "that would undo a retirement (archive/supersede/retraction) is named in "
                "the preview and refused unless undo_retirements=true; prefer "
                "palinode_restore to bring a retired memory back."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Memory file path to rollback",
                    },
                    "commit": {
                        "type": "string",
                        "description": "Target commit hash (from palinode_history). Default: previous version.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), show what would change without applying.",
                        "default": True,
                    },
                    "undo_retirements": {
                        "type": "boolean",
                        "description": (
                            "Acknowledge that applying undoes a retirement and brings "
                            "the record back as current. Without it such a rollback is refused."
                        ),
                        "default": False,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Rollback File",
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_push",
            description="Sync memory changes to GitHub for backup and cross-machine access.",
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title="Push to Remote",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
            ),
        ),
        types.Tool(
            name="palinode_trigger",
            description=(
                "Register or manage a prospective trigger for Palinode. When a future user message semantically "
                "matches the description, the specified memory file will be automatically injected."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action to perform: 'create', 'list', or 'delete'",
                        "enum": ["create", "list", "delete"],
                        "default": "create",
                    },
                    "description": {
                        "type": "string",
                        "description": "For 'create': What context should fire this trigger (e.g., 'User is discussing deployment')",
                    },
                    "memory_file": {
                        "type": "string",
                        "description": "For 'create': Relative path to the memory file to inject when fired (e.g., 'projects/my-app.md')",
                    },
                    "trigger_id": {
                        "type": "string",
                        "description": "For 'delete' or 'create': Custom UUID or ID to delete/create",
                    },
                    "threshold": {
                        "type": "number",
                        "description": (
                            "For 'create': Similarity threshold (0.0–1.0).  "
                            "Higher = stricter match required to fire.  "
                            "Default 0.75."
                        ),
                    },
                    "cooldown_hours": {
                        "type": "integer",
                        "description": (
                            "For 'create': Hours to wait between consecutive "
                            "firings of the same trigger.  Default 24."
                        ),
                    },
                    "expires_at": {
                        "type": "string",
                        "description": (
                            "For 'create': ISO-8601 timestamp after which the trigger "
                            "no longer fires (it stays listed, disabled). Omit for no expiry."
                        ),
                    },
                    "authority": {
                        "type": "string",
                        "description": (
                            "For 'create': who or what licensed this trigger to act — "
                            "a user grant, a session id, a policy name. Stored and shown, not enforced."
                        ),
                    },
                },
                "required": ["action"],
            },
            annotations=types.ToolAnnotations(
                title="Manage Triggers",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_session_end",
            description=(
                "Call at the end of a coding or chat session to capture key outcomes to persistent memory. "
                "Writes a session summary to today's daily notes and appends status to relevant project files. "
                "Provide a brief summary of what was accomplished, decisions made, and any blockers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What was accomplished in this session (1-3 sentences)",
                    },
                    "decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key decisions made (optional)",
                    },
                    "blockers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Open blockers or next steps (optional)",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project slug to append status to (e.g., 'palinode'). Auto-detected if omitted.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source surface that created this memory (e.g., 'claude-code', 'cursor', 'api'). Auto-detected if omitted.",
                    },
                    "push": {
                        "type": "boolean",
                        # push=true lets wrap-style callers commit and ship the
                        # session note in one call; omitted uses server config.
                        "description": "Push the memory repo after committing the session note.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": (
                            "Validate and render the entry without writing, committing, or "
                            "pushing anything. Use to check a payload before committing it, "
                            "or to diagnose a failing session-end without leaving entries "
                            "behind in the daily note."
                        ),
                    },
                },
                "required": ["summary"],
            },
            annotations=types.ToolAnnotations(
                title="End Session",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_lint",
            description=(
                "Scan memory for health issues: orphaned files, stale active files (>90 days), "
                "missing frontmatter fields, and potential contradictions. Returns a report without modifying files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "propose": {
                        "type": "boolean",
                        "description": (
                            "Also translate the deterministic findings into proposed "
                            "consolidation operations, each with its rationale and the "
                            "finding it came from. Advisory: this tool never applies them "
                            "— run `palinode lint --apply` to let the executor act."
                        ),
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Lint Memory",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_review",
            description=(
                "Advisory project-memory review. Composes the deterministic health "
                "signals (stale files, long-unresolved open questions, open contradictions, "
                "orphans, missing descriptions, wiki drift) scoped to a project, and proposes "
                "corrective ops (PROPOSE_ARCHIVE/UPDATE/SUPERSEDE). Read-only — proposes, never "
                "applies. Omit `project` to review the whole store."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project slug (e.g. 'palinode') or typed ref ('project/palinode'). Omit to review the whole store.",
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Review Project Memory",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_corrections",
            description=(
                "List correction candidates mined from harness session transcripts: moments the "
                "user overturned a decision, rejected an approach and said why, or asked for "
                "something to be remembered. Each candidate quotes a bounded span of the user's "
                "own words with the session, turn and project it came from. Advisory and "
                "read-only — candidates are proposals awaiting review, never applied, and the "
                "list is empty unless the store's operator enabled transcript capture and named "
                "the transcript paths in config. Running a fresh detection pass is deliberately "
                "an operator action on the CLI or REST API, not something this tool can trigger."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Only candidates scoped to this project (e.g. 'harbor-notes'). Omit for all.",
                    },
                    "since_days": {
                        "type": "integer",
                        "description": "Only candidates from the last N days. Omit for the whole queue.",
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Correction Candidates",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_correction_preview",
            description=(
                "Show exactly what correcting or retiring one memory would change — and change "
                "nothing. Returns the old text, the proposed new text, the affected document "
                "(and claim), its exact source revision, the rationale, where the correction came "
                "from, the project scope, the supersedes/superseded_by relation that would be "
                "recorded, every other record that quotes or derives from the target (reported, "
                "never rewritten), and the recovery command. Call this before "
                "palinode_correction_apply: the revision it returns is what apply requires back, "
                "so a target that changed in between is refused rather than silently merged."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Memory to correct: 'decisions/x.md', 'decisions/x', or a bare slug. A slug naming two memories is refused with both, never guessed.",
                    },
                    "claim_id": {
                        "type": "string",
                        "description": "Narrow the correction to one '<!-- fact:id -->' claim inside the target.",
                    },
                    "allow_content_loss": {
                        "type": "boolean",
                        "default": False,
                        "description": "Explicitly permit dropping the original text listed by preview.",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "The text that would stand instead. Omit to retire the target with no successor.",
                    },
                    "action": {
                        "type": "string",
                        "enum": list(CORRECTION_ACTIONS),
                        "description": "supersede (a replacement stands) or retire (nothing does). Derived from `replacement` when omitted.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why. Recorded in the history sibling and the commit subject.",
                    },
                    "candidate_id": {
                        "type": "string",
                        "description": "The palinode_corrections candidate this came from; its span, session and turn become the recorded source.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project scope. Inferred from the target's own entities when omitted.",
                    },
                    "backed_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Records supporting the REPLACEMENT (category/slug refs). The superseded original is never cited as support for its replacement — it is lineage, and a verified quote of it establishes what it said, not that the new claim is true.",
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Preview a Correction",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_correction_apply",
            description=(
                "Apply a correction previewed by palinode_correction_preview. Requires "
                "`confirm=true` and the `expect_revision` that preview returned; a target that "
                "changed since the preview, or a ref matching more than one memory, is refused "
                "with what it found rather than resolved by guesswork. Writes only through the "
                "existing validated path — the replacement is saved and the original is archived "
                "with `superseded_by`, so the original stays on disk, in git and retrievable as "
                "history. An ordinary later observation must NOT be routed here: this is the "
                "explicit, confirmed path, and it is the only one that retires anything. "
                "`applied: \"partial\"` means the replacement was saved and the original was NOT "
                "retired — do not re-run the correction; run the `complete_command` it returns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Memory to correct, exactly as previewed.",
                    },
                    "expect_revision": {
                        "type": "string",
                        "description": "The revision palinode_correction_preview returned. A mismatch is refused.",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true. Nothing is written without it.",
                        "default": False,
                    },
                    "claim_id": {
                        "type": "string",
                        "description": "Narrow the correction to one '<!-- fact:id -->' claim inside the target.",
                    },
                    "allow_content_loss": {
                        "type": "boolean",
                        "default": False,
                        "description": "Explicitly permit dropping the original text listed by preview.",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "The text that stands instead. Omit to retire with no successor.",
                    },
                    "action": {
                        "type": "string",
                        "enum": list(CORRECTION_ACTIONS),
                        "description": "supersede or retire. Derived from `replacement` when omitted.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why. Recorded in the history sibling and the commit subject.",
                    },
                    "candidate_id": {
                        "type": "string",
                        "description": "The candidate this came from; it is marked applied in the queue.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project scope for the replacement.",
                    },
                    "backed_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Records supporting the REPLACEMENT (category/slug refs). The superseded original is never among them.",
                    },
                },
                "required": ["target", "expect_revision", "confirm"],
            },
            annotations=types.ToolAnnotations(
                title="Apply a Reviewed Correction",
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_correction_dismiss",
            description=(
                "Record that a correction candidate was reviewed and declined. Writes no memory: "
                "the candidate's queue row is marked dismissed, with the reason, and kept — which "
                "is what stops a later transcript scan proposing the same span again. A reason is "
                "required, because a dismissal with none is indistinguishable from a candidate "
                "nobody ever looked at."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "candidate_id": {
                        "type": "string",
                        "description": "The candidate id from palinode_corrections.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why it was declined. This is the record.",
                    },
                },
                "required": ["candidate_id", "reason"],
            },
            annotations=types.ToolAnnotations(
                title="Dismiss a Correction Candidate",
                readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_correction_undo",
            description=(
                "Preview (default) or apply the undo of a correction. Without `confirm` this "
                "reads: it states what would be restored, what would NOT be deleted, and what "
                "cannot be reached at all — restoring a previous assertion, deleting history and "
                "undoing an agent's external actions are three different things and only the "
                "first is on offer. With `confirm=true` and the preview's `expect_revision` it "
                "brings the archived record back to status: active. It refuses to resurrect a "
                "record that was separately retracted or withdrawn by a forget request."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "The archived memory to restore.",
                    },
                    "expect_revision": {
                        "type": "string",
                        "description": "The revision the undo preview returned. Required with confirm.",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true to write. Preview is the default.",
                        "default": False,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the correction is being undone.",
                    },
                },
                "required": ["target"],
            },
            annotations=types.ToolAnnotations(
                title="Undo a Correction",
                readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_dedup_suggest",
            description=(
                "Given draft memory content the LLM is about to save, return the top-K existing "
                "memory files whose embeddings are semantically near it. Use BEFORE writing a new "
                "memory to decide 'create new' vs 'update existing'. Each result includes a "
                "`strong_dup` flag — when true (similarity ≥ 0.90), the existing file is a "
                "near-paraphrase and the LLM should usually update rather than create. "
                "Preprocessing strips wikilink syntax and the auto-generated `## See also` footer "
                "so notes linking the same entities don't false-positive as duplicates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The draft memory body about to be saved (markdown, with or without frontmatter).",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to surface (0.0–1.0). Default 0.80.",
                        "default": 0.80,
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of candidate files to return. Default 5.",
                        "default": 5,
                    },
                },
                "required": ["content"],
            },
            annotations=types.ToolAnnotations(
                title="Dedup Suggest",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_orphan_repair",
            description=(
                "Given a `[[wikilink]]` whose target file does not exist, return existing memory "
                "files semantically near the link target text. Use during wiki-maintenance passes "
                "to either propose a redirect (rename the link to point at an existing file) or "
                "to create the missing target file with informed context about its semantic "
                "neighbours. Accepts either `[[name]]` or bare `name`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "broken_link": {
                        "type": "string",
                        "description": "The wikilink text (e.g. '[[alice-meeting]]') or bare target slug.",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to surface (0.0–1.0). Default 0.65 — looser than dedup_suggest because the LLM picks from a wider slate.",
                        "default": 0.65,
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of candidate files to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": ["broken_link"],
            },
            annotations=types.ToolAnnotations(
                title="Orphan Repair",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_cluster_neighbors",
            description=(
                "Given a memory file path, find the top-K semantically related files that are NOT "
                "currently linked to or from it (no existing [[wikilink]] in either direction). "
                "Use during wiki-maintenance passes to surface implicit relationships that no "
                "wikilink yet captures — the LLM can then propose new cross-links. "
                "Preprocessing strips wikilink syntax and the auto-generated `## See also` footer "
                "so notes linking the same entities don't false-positive as related."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'decisions/palinode-arch.md') to find unlinked semantic neighbours for.",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to surface (0.0–1.0). Default 0.70.",
                        "default": 0.70,
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of candidate files to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Cluster Neighbors",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_topic_coverage",
            description=(
                "Given a topic phrase (not a file), check whether any wiki page already covers it. "
                "Returns {covered: bool, best_match: str | null, similarity: float}. "
                "Use BEFORE ingesting new content to ask 'is this already covered?'. "
                "Different framing from palinode_dedup_suggest: takes a short topic phrase rather "
                "than full draft content, and answers the binary 'already covered?' question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic phrase to check coverage for (e.g. 'machine learning deployment').",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to count as 'covered' (0.0–1.0). Default 0.78.",
                        "default": 0.78,
                    },
                },
                "required": ["query"],
            },
            annotations=types.ToolAnnotations(
                title="Topic Coverage",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_resolve",
            description=(
                "Ask what memory holds RIGHT NOW about a question, or about one record. "
                "Returns the assertions that stand (with their evidence and source revisions), "
                "what replaced what, conflicts with every side intact, and what is explicitly "
                "unknown. Use instead of palinode_search when you want the current answer "
                "rather than a list of hits. Read-only; a tight budget can shrink the answer "
                "but never turns a conflict into a settled one. Retired records are left "
                "out, as in search, unless include_retired=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language question. Give this or `ref`.",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Exact memory ref (path without .md), e.g. 'decisions/db'.",
                    },
                    "context": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Refs you already hold; each is checked, not assumed current.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "What to answer. Only current state is supported.",
                        "enum": list(RESOLVE_INTENTS),
                        "default": RESOLVE_INTENTS[0],
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Max units in the answer (default 8).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters in the answer (default 2000).",
                    },
                    "include_other_projects": {
                        "type": "boolean",
                        "description": (
                            "Also return records tagged to other projects, each "
                            "labelled with its project. Off by default: a "
                            "project-scoped request leaves them out."
                        ),
                        "default": False,
                    },
                    "include_retired": {
                        "type": "boolean",
                        "description": (
                            "Also return retired records (archived, superseded, "
                            "retracted, expired), labelled as history. Off by "
                            "default, as in search."
                        ),
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Resolve Current State",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_doctor",
            description=(
                "Fast palinode health check (<500ms). "
                "Skips network probes and canary writes. "
                "Checks path integrity, config consistency, and env-var drift. "
                "Use this first; call palinode_doctor_deep when results are unclear."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title="Doctor (fast)",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_doctor_deep",
            description=(
                "Full palinode health check including network probes and canary write tests. "
                "Takes 10-15s. Use when palinode_doctor reports unclear results or you need "
                "to verify the API, watcher, and service connectivity."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title="Doctor (deep)",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_prompt",
            description=(
                "List, read, or activate versioned LLM prompts stored as memory files in the prompts/ directory. "
                "Use 'list' to browse available prompts, 'read' to view a specific prompt's content, "
                "or 'activate' to set a prompt version as active (deactivates others of the same task)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action to perform: 'list', 'read', or 'activate'",
                        "enum": ["list", "read", "activate"],
                        "default": "list",
                    },
                    "name": {
                        "type": "string",
                        "description": "Prompt name (required for 'read' and 'activate')",
                    },
                    "task": {
                        "type": "string",
                        "description": "For 'list': filter by task type",
                        "enum": list(PROMPT_TASKS),
                    },
                },
                "required": ["action"],
            },
            annotations=types.ToolAnnotations(
                title="Manage Prompts",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_depends",
            description=(
                "Return the dependency tree for a milestone or task slug, or list all unblocked items. "
                "Reads depends_on / blocks / parallel_with frontmatter from ProjectSnapshot files. "
                "Set unblocked=true to answer 'what can I work on right now?' across all slugs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": (
                            "Milestone or task slug to inspect (e.g. 'milestone/M1'). "
                            "Required unless unblocked=true."
                        ),
                    },
                    "unblocked": {
                        "type": "boolean",
                        "description": (
                            "If true, return the list of all slugs whose every depends_on is done "
                            "(ignores slug). Default false."
                        ),
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Dependency Tree",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
    ]


async def list_tools() -> list[types.Tool]:
    tools = _all_tools()
    if _resolve_tool_surface() == "core":
        return [tool for tool in tools if tool.name in CORE_TOOL_NAMES]
    return tools


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    start_time = time.monotonic()
    result = await _dispatch_tool(name, arguments)
    duration_ms = (time.monotonic() - start_time) * 1000

    # Detect error responses — the dispatcher returns error text rather than
    # raising, so the prefix is the only signal. This used to carry its own
    # hand-written tuple, the third copy of the same contract, and it had
    # drifted like the others: `API unreachable`, `Review failed`,
    # `Archive failed`, `Archive-expired sweep failed`, `Unknown action:` and
    # `Unknown tool` matched nothing here, so those failures were written to the
    # audit log with status="success". Reading from the one declaration means a
    # reworded or newly added message updates the audit log by construction.
    first_text = result[0].text if result else ""
    is_error = _is_error_result(result)
    # Result size is the uncacheable half of a call's cost: schemas are a
    # fixed prefix that caches, results are new bytes that persist in `messages`
    # for the rest of the session. Measured in UTF-8 bytes, the unit that
    # actually crosses the wire.
    result_bytes = sum(
        len(getattr(block, "text", "").encode("utf-8")) for block in result
    )
    _audit.log_call(
        name, arguments, duration_ms,
        status="error" if is_error else "success",
        error=first_text if is_error else None,
        result_bytes=result_bytes,
        result_blocks=len(result),
    )
    return result


# ── Tool handlers ────────────────────────────────────────────────────────────
#
# One function per tool, registered by name. This chain used to be a 647-line
# if/elif inside `_dispatch_tool`, which meant a tool's logic could only be
# reached by dispatching to it — and `_dispatch_tool` is private, so the test
# suite referenced it 48 times across 12 files against 4 for the public
# `call_tool`.
#
# Splitting the tools out *behind* `_dispatch_tool` rather than migrating those
# 48 references is deliberate. `_dispatch_tool(name, arguments)` still dispatches
# exactly as before, so every existing caller and test keeps working; what
# changes is that the thing they reach for is now a nine-line lookup. Reaching
# past the interface stops mattering when there is nothing behind it to miss.
#
# Handlers take `arguments` alone — none of the thirty branches referenced
# `name`, which is why this split is mechanical rather than a redesign.

_ToolHandler = Callable[[dict[str, Any]], Awaitable[list[types.TextContent]]]

_TOOL_HANDLERS: dict[str, _ToolHandler] = {}


def _handles(tool_name: str) -> Callable[[_ToolHandler], _ToolHandler]:
    """Register a coroutine as the handler for one MCP tool."""

    def register(fn: _ToolHandler) -> _ToolHandler:
        _TOOL_HANDLERS[tool_name] = fn
        return fn

    return register


# ── list ──────────────────────────────────────────────────────────
@_handles("palinode_list")
async def _tool_list(arguments: dict[str, Any]) -> list[types.TextContent]:
    params: dict[str, Any] = {}
    if arguments.get("category"):
        params["category"] = arguments["category"]
    if arguments.get("core_only"):
        params["core_only"] = "true"
        # Browsing, not injecting: a core memory the lifecycle classifier
        # retired stays in this listing, labelled with why it no longer acts.
        # The injection consumers of the same endpoint (the SessionStart hook,
        # the plugins) omit the flag and get it withheld.
        params["include_retired_core"] = "true"

    resp = await _get("/list", params=params)
    if resp.status_code != 200:
        return _text(f"API Error: {resp.text}")
    data = resp.json()
    if not data:
        return _text("No files found.")
    parts = []
    for f in data:
        if f.get("core_retired_reason"):
            c_tag = f" [retired: {f['core_retired_reason']}]"
        else:
            c_tag = " [core]" if f.get("core") else ""
        parts.append(f"{f['file']} — {f.get('summary', '')}{c_tag}")
    return _text("\n".join(parts))


# ── read ──────────────────────────────────────────────────────────
@_handles("palinode_read")
async def _tool_read(arguments: dict[str, Any]) -> list[types.TextContent]:
    include_meta = bool(arguments.get("meta", False))
    params: dict[str, Any] = {"file_path": arguments["file_path"], "meta": "true"}
    params["project"] = _resolve_scope().project or ""
    tier = arguments.get("tier")
    if tier:
        params["tier"] = tier
    resp = await _get("/read", params=params)
    if resp.status_code != 200:
        return _text(f"Error reading file: {resp.text}")
    data = resp.json()
    content = data.get("content", "")
    # Full text on an explicit read, led by what its flagged part is. The
    # local check covers a server that predates the notice field.
    notice = data.get("agent_directed_notice") or read_notice(content)
    lead = f"{notice}\n" if notice else ""
    if include_meta:
        fm = data.get("frontmatter") or {}
        # Render as YAML-ish frontmatter + body so downstream consumers
        # can re-parse if they want.  Keep it simple: the file already
        # has the same structure on disk.
        fm_lines = "\n".join(f"{k}: {v!r}" for k, v in fm.items())
        return _text(f"{lead}---\n{fm_lines}\n---\n{content}")
    return _text(f"{lead}{content}")


# ── search ────────────────────────────────────────────────────────
@_handles("palinode_search")
async def _tool_search(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {"query": arguments["query"]}
    if arguments.get("tier"):
        body["tier"] = arguments["tier"]
    if arguments.get("category"):
        body["category"] = arguments["category"]
    if arguments.get("limit"):
        body["limit"] = int(arguments["limit"])
    if arguments.get("date_after"):
        body["date_after"] = arguments["date_after"]
    if arguments.get("date_before"):
        body["date_before"] = arguments["date_before"]
    if arguments.get("include_daily"):
        body["include_daily"] = True
    if arguments.get("include_telemetry"):
        body["include_telemetry"] = True
    if str(arguments.get("include_other_projects", False)).lower() in ("true", "1"):
        body["include_other_projects"] = True
    if arguments.get("since_days") is not None:
        body["since_days"] = int(arguments["since_days"])
    if arguments.get("types"):
        body["types"] = _coerce_str_array(arguments["types"])
    if arguments.get("min_priority") is not None:
        body["min_priority"] = int(arguments["min_priority"])
    # ADR-010: caller-supplied threshold wins; otherwise use
    # the MCP-tuned default (typically tighter than the API default
    # to keep auto-context noise low).
    if arguments.get("threshold") is not None:
        body["threshold"] = float(arguments["threshold"])
    else:
        body["threshold"] = config.search.mcp_threshold
    # Opt-in evidence closure; "none" is the API default and is not sent.
    if arguments.get("resolve") and arguments["resolve"] != "none":
        body["resolve"] = arguments["resolve"]
    if str(arguments.get("include_retired", False)).lower() in ("true", "1"):
        body["include_retired"] = True
    # Always ask for the delivery receipt. It is not a tool parameter: an agent
    # never has a reason to decline provenance for what it was just handed, and
    # the cost is two lines of text without `resolve`.
    body["receipt"] = True
    # ADR-008: ambient context boost. The same resolution the session-init
    # digest reports, so an explicit setup and a later search in the same
    # client cannot disagree about which project they are scoped to.
    scope = _resolve_scope()
    # Always stated, empty included: this surface resolved the scope itself and
    # reports what it resolved, so it must send that result rather than leave
    # the field absent — absent is the API's cue to apply its own pinned
    # project, which would apply a scope this call never reported.
    body["context"] = scope.context or []

    resp = await _post("/search", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Search failed: {resp.text}")
    # With `receipt` the API answers with an envelope; a bare list means an
    # older API server that predates receipts, which still renders.
    payload = resp.json()
    if isinstance(payload, dict):
        results, receipt = payload.get("results") or [], payload.get("receipt")
    else:
        results, receipt = payload, None
    # Opt-in abstention (`search.abstain_on_no_confident_match`, default off):
    # withhold a slate the server judged worth nothing. Applied here, on the
    # one surface whose reader is a model, and only to the rendering — the
    # receipt and the retrieval log still record what the store delivered, so
    # switching this on changes what is shown and never what is measured.
    withheld = 0
    if config.search.abstain_on_no_confident_match and results:
        from palinode.core.confidence import NONE, delivered_verdict

        if delivered_verdict(receipt) == NONE:
            withheld, results = len(results), []
    # `full` is purely a rendering choice — the API always
    # populates `snippet` and preserves `content`, so the MCP picks
    # which to render without an extra round-trip.
    return _text(_format_results(
        results, full=bool(arguments.get("full")), receipt=receipt,
        scope=scope.describe(), withheld=withheld, project=scope.project,
    ))


# ── save ──────────────────────────────────────────────────────────
@_handles("palinode_save")
async def _tool_save(arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        resolved_type = _resolve_save_type(
            arguments.get("type"), arguments.get("ps")
        )
    except ValueError as e:
        return _text(f"Error: {e}")

    body: dict[str, Any] = {
        "content": arguments["content"],
        "type": resolved_type,
    }
    # One inclusion rule for every surface — a param is sent
    # when it is not None, so an explicitly-empty `contradicts: []`
    # survives as the assertion the caller made. The `omit_if_empty`
    # strings (source/slug/project/title) still elide when blank, which
    # is what this handler already did for them. ADR-010: an omitted
    # `source` lets the X-Palinode-Source header carry attribution.
    body.update(build_payload(SAVE_PARAMS, arguments))

    resp = await _post("/save", json=body)
    if resp.status_code != 200:
        return _text(f"Save failed: {resp.text}")
    data = resp.json()
    rel = _rel_path_from(data)
    # Surface per-index health signals from if either index
    # write failed — these are warnings, not save failures.
    warnings: list[str] = []
    if data.get("retrieval_mode") != "lexical" and not data.get("indexed_vec", True):
        warnings.append("vec index write failed (chunk absent from vector search)")
    if not data.get("indexed_fts", True):
        warnings.append("FTS5 sync failed (periodic rebuild will recover)")
    if not data.get("git_committed", True):
        reason = data.get("git_error")
        warnings.append(
            "git auto-commit failed (file on disk, not versioned)"
            + (f": {reason}" if reason else "")
        )
    save_outcome = data.get("save_outcome")
    if save_outcome == "disambiguated":
        original_slug = data.get("disambiguated_from")
        outcome_text = (
            f"disambiguated from {original_slug}"
            if original_slug
            else "disambiguated"
        )
    elif save_outcome in {"created", "resaved", "replaced"}:
        outcome_text = save_outcome
    else:
        # Graceful compatibility with an older API server.
        outcome_text = None
    if save_outcome == "appended":
        # An append is a different act from a save, and the receipt says so in
        # the verb rather than in a parenthetical: the caller that lost a body
        # to `update_policy: append` read "(replaced)" as a status line and not
        # as a report that their prior content was gone.
        confirmation = f"Appended to {rel}"
    else:
        confirmation = f"Saved to {rel}"
        if outcome_text:
            confirmation += f" ({outcome_text})"
    if data.get("retrieval_mode") == "lexical":
        state = "ready" if data.get("indexed") else "not indexed"
        confirmation += f" [lexical retrieval: {state}]"
    if warnings:
        confirmation += f" [warnings: {'; '.join(warnings)}]"
    retained = render_retained_copies(
        (data.get("forget") or {}).get("retained_copies")
    )
    if retained:
        confirmation = "\n".join([confirmation, *retained])
    return _text(confirmation)


# ── ingest ────────────────────────────────────────────────────────
@_handles("palinode_ingest")
async def _tool_ingest(arguments: dict[str, Any]) -> list[types.TextContent]:
    url = arguments["url"]
    name_arg = arguments.get("name", url.split("/")[-1][:40])

    resp = await _post("/ingest-url", json={"url": url, "name": name_arg}, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Ingest failed: {resp.text}")
    data = resp.json()
    if data.get("file_path"):
        return _text(f"Ingested → {_rel_path_from(data)}")
    return _text("No content extracted from URL.")


# ── history ───────────────────────────────────────────────────────
@_handles("palinode_history")
async def _tool_history(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments["file_path"]
    limit = int(arguments.get("limit", 20))
    detail = arguments.get("detail", "summary")
    if detail not in ("summary", "full"):
        return _text("Error: detail must be 'summary' or 'full'")
    resp = await _get(f"/history/{file_path}", params={"limit": str(limit), "detail": detail})
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    data = resp.json()
    if not data.get("history"):
        return _text("No history found.")
    lines = []
    for c in data["history"]:
        line = f"{c['hash']} | {c['date'][:10]} | {c['message']}"
        if c.get("stats"):
            line += f"\n  {c['stats']}"
        if detail == "full" and c.get("diff"):
            line += f"\n{c['diff']}"
        lines.append(line)
    return _text("\n\n---\n\n".join(lines) if detail == "full" else "\n".join(lines))


# ── entities ──────────────────────────────────────────────────────
@_handles("palinode_entities")
async def _tool_entities(arguments: dict[str, Any]) -> list[types.TextContent]:
    entity_ref = arguments.get("entity_ref")
    if entity_ref:
        resp = await _get(f"/entities/{entity_ref}")
    else:
        resp = await _get("/entities")
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    return _text(json.dumps(resp.json(), indent=2))


# ── consolidate ───────────────────────────────────────────────────

#: Where a pass that outlived its client ends up. The API logs the run (under
#: systemd: ``journalctl -u palinode-api``); the consolidation logger also
#: writes here when file logging is configured. Same value the CLI reports.
CONSOLIDATION_LOG = "logs/consolidation.log"


def _consolidation_timeout_report(seconds: float) -> dict[str, Any]:
    """What to tell a caller whose request stopped waiting for a pass.

    The request timing out does not cancel the pass: the server holds the
    store's run lock until it finishes, so the next call gets a 409 and the
    result of this one is only in the log. Same payload the CLI emits for the
    same outcome, so an agent and an operator read the same facts.
    """
    # Lazy: the lock path is the run lock's own constant, and only the timeout
    # path needs it.
    from palinode.consolidation.run_lock import LOCK_RELATIVE_PATH

    lock = str(LOCK_RELATIVE_PATH)
    return {
        "status": "timeout",
        "timeout_seconds": seconds,
        "server_still_running": True,
        "lock": lock,
        "log": CONSOLIDATION_LOG,
        "message": (
            f"Stopped waiting after {seconds:.0f}s. The consolidation was not "
            f"cancelled — the server is still running it and holds {lock}, so "
            "another run returns 409 until it finishes. Results land in the API "
            f"log and {CONSOLIDATION_LOG}. Raise PALINODE_CONSOLIDATE_TIMEOUT to "
            "wait longer."
        ),
    }


@_handles("palinode_consolidate")
async def _tool_consolidate(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {}
    if arguments.get("dry_run"):
        body["dry_run"] = True
    if arguments.get("nightly"):
        body["nightly"] = True
    if arguments.get("sources"):
        body["sources"] = _coerce_str_array(arguments["sources"])
    if arguments.get("respect_gate"):
        body["respect_gate"] = True
    try:
        # Module global, read at call time: an override reaches both the
        # request and the budget named in the report below.
        resp = await _post("/consolidate", json=body, timeout=_CONSOLIDATE_TIMEOUT)
    except httpx.ReadTimeout:
        # The server has the request and is still working on it, so this is a
        # report rather than a dispatcher failure — deliberately not one of
        # DISPATCH_ERROR_PREFIXES, and deliberately not the generic
        # `_timeout_message` the dispatcher would have applied, which would say
        # only that the request timed out and leave a caller to retry into a
        # 409. Caught here, before it reaches `_dispatch_tool`.
        return _text(json.dumps(_consolidation_timeout_report(_CONSOLIDATE_TIMEOUT), indent=2))
    if resp.status_code != 200:
        return _text(f"Consolidation failed: {resp.text}")
    return _text(json.dumps(resp.json(), indent=2))


# ── archive-expired ────────────────────────────────────────────────
@_handles("palinode_archive_expired")
async def _tool_archive_expired(arguments: dict[str, Any]) -> list[types.TextContent]:
    body = {}
    if arguments.get("dry_run"):
        body["dry_run"] = True
    resp = await _post("/archive-expired", json=body, timeout=120.0)
    if resp.status_code != 200:
        return _text(f"Archive-expired sweep failed: {resp.text}")
    return _text(json.dumps(resp.json(), indent=2))


# ── archive (on-demand ARCHIVE / SUPERSEDE) ────────────────────────
@_handles("palinode_archive")
async def _tool_archive(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments["file_path"]
    body = {"file_path": file_path}
    if arguments.get("reason"):
        body["reason"] = arguments["reason"]
    if arguments.get("superseded_by"):
        body["superseded_by"] = arguments["superseded_by"]
    if arguments.get("dry_run"):
        body["dry_run"] = True
    # No model call, but a single archive writes the memory, appends to its
    # history sibling, flags dependents, updates the chunk index and commits —
    # enough work on a large store to outrun the 30 s default. Matches the CLI.
    resp = await _post("/archive", json=body, timeout=120.0)
    if resp.status_code != 200:
        return _text(f"Archive failed: {resp.text}")
    data = resp.json()
    if data.get("dry_run"):
        return _text("\n".join(render_lifecycle_preview(data)))
    retained = render_retained_copies(data.get("retained_copies"))
    if data.get("status") == "already_archived":
        return _text("\n".join(
            [f"{data.get('file')} is already archived — no change.", *retained]
        ))
    successor = data.get("superseded_by")
    verb = f"Superseded by {successor}" if successor else "Archived"
    return _text("\n".join([
        f"{verb}: {data.get('file')}",
        f"History: {data.get('history_file')}",
        f"Chunks suppressed from recall: {data.get('chunks_updated', 0)}",
        *retained,
    ]))


# ── restore (inverse of archive) ───────────────────────────────────
@_handles("palinode_restore")
async def _tool_restore(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments["file_path"]
    body = {"file_path": file_path}
    if arguments.get("reason"):
        body["reason"] = arguments["reason"]
    if arguments.get("dry_run"):
        body["dry_run"] = True
    resp = await _post("/restore", json=body)
    if resp.status_code != 200:
        return _text(f"Restore failed: {resp.text}")
    data = resp.json()
    if data.get("dry_run"):
        return _text("\n".join(render_lifecycle_preview(data)))
    if data.get("status") == "not_archived":
        return _text(f"{data.get('file')} is not archived — no change.")
    lines = [
        f"Restored: {data.get('file')} (was {data.get('restored_from')})",
        f"History: {data.get('history_file')}",
        f"Chunks returned to recall: {data.get('chunks_updated', 0)}",
    ]
    if data.get("stale_backing"):
        lines.append(
            "Stale backing flagged (source no longer active): "
            + ", ".join(data["stale_backing"])
        )
    if data.get("expires_at"):
        lines.append(
            f"Note: expires_at is still {data['expires_at']} — the TTL sweep "
            "will re-archive it unless the expiry is changed."
        )
    return _text("\n".join(lines))


# ── unretract (inverse of mention-level retraction) ────────────────
@_handles("palinode_unretract")
async def _tool_unretract(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments["file_path"]
    pref = arguments["pref"]
    body = {"file_path": file_path, "pref": pref}
    if arguments.get("reason"):
        body["reason"] = arguments["reason"]
    if arguments.get("dry_run"):
        body["dry_run"] = True
    resp = await _post("/unretract", json=body)
    if resp.status_code != 200:
        return _text(f"Unretract failed: {resp.text}")
    data = resp.json()
    if data.get("dry_run"):
        return _text("\n".join(render_lifecycle_preview(data)))
    if data.get("status") == "not_retracted":
        return _text(
            f"{data.get('file')} carries no retraction for that pref — no change."
        )
    lines = [
        f"Unretracted {data.get('mentions', 0)} mention(s) in {data.get('file')}",
        f"History: {data.get('history_file')}",
    ]
    if data.get("index_error"):
        lines.append(f"Warning: re-index failed — {data['index_error']}")
    return _text("\n".join(lines))


# ── forget-withdraw (take a forget request back) ───────────────────
@_handles("palinode_forget_withdraw")
async def _tool_forget_withdraw(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments["file_path"]
    body = {"file_path": file_path}
    if arguments.get("reason"):
        body["reason"] = arguments["reason"]
    if arguments.get("dry_run"):
        body["dry_run"] = True
    resp = await _post("/forget-withdraw", json=body, timeout=120.0)
    if resp.status_code != 200:
        return _text(f"Forget-withdraw failed: {resp.text}")
    data = resp.json()
    if data.get("dry_run"):
        return _text("\n".join(render_lifecycle_preview(data)))
    partial = data.get("status") == "partial"
    head = "Partially withdrawn" if partial else "Withdrawn"
    lines = [
        f"{head}: {data.get('file')} (pref: {data.get('pref')!r})",
        f"Restored: {len(data.get('restored', []))} — "
        + (", ".join(data.get("restored", [])) or "none"),
        f"Unretracted: {len(data.get('unretracted', []))} — "
        + (", ".join(u['path'] for u in data.get("unretracted", [])) or "none"),
        "Request records archived: "
        + (", ".join(data.get("requests_archived", [])) or "none"),
    ]
    if data.get("failed"):
        lines.append(
            "Failed (still retired): " + ", ".join(
                f"{f['path']} ({f['op']})" for f in data["failed"]
            )
        )
    lines.extend(render_retained_copies(data.get("retained_copies")))
    return _text("\n".join(lines))


# ── status ────────────────────────────────────────────────────────
@_handles("palinode_status")
async def _tool_status(arguments: dict[str, Any]) -> list[types.TextContent]:
    resp = await _get("/status")
    if resp.status_code != 200:
        return _text(f"API unreachable: {resp.text}")
    try:
        s = resp.json()
    except (TypeError, ValueError):
        return _text("API status is unavailable.")
    if not isinstance(s, dict):
        return _text("API status is unavailable.")
    lines = [
        "Palinode Status",
        f"  Version:        {s.get('version', '?')}",
        f"  Files indexed:  {s.get('total_files', '?')}",
        f"  Chunks indexed: {s.get('total_chunks', '?')}",
        f"  Hybrid search:  {'✅ enabled' if s.get('hybrid_search') else '❌ disabled'}",
        f"  FTS5 chunks:    {s.get('fts_chunks', '?')}",
        f"  Entities:       {s.get('total_entities', '?')}",
        f"  Ollama (embed): {'✅ reachable' if s.get('ollama_reachable') else '❌ unreachable'}",
        f"  Git commits 7d: {s.get('git_commits_7d', '?')}",
        f"  Unpushed:       {s.get('unpushed_commits', '?')}",
        f"  API:            {_api_url('')}",
    ]
    controls = await _get("/controls")
    if controls.status_code != 200:
        lines.extend([
            "Capture controls: unavailable (policy could not be read).",
            f"Pause scope: {PAUSE_SCOPE}.",
            PAUSED_READ_AVAILABILITY,
        ])
        return _text("\n".join(lines))
    try:
        policy = controls.json()
    except (TypeError, ValueError):
        policy = None
    if not isinstance(policy, dict) or type(policy.get("capture_paused")) is not bool \
            or type(policy.get("recall_paused")) is not bool \
            or type(policy.get("policy_version")) is not int \
            or policy.get("provenance") not in {None, "api_controls"}:
        lines.extend([
            "Capture controls: unavailable (policy response was invalid).",
            f"Pause scope: {PAUSE_SCOPE}.",
            PAUSED_READ_AVAILABILITY,
        ])
        return _text("\n".join(lines))
    provenance = policy["provenance"] or "default (no persisted policy)"
    lines.extend([
        "Capture controls (read-only)",
        f"  Capture:        {'paused' if policy['capture_paused'] else 'active'}",
        f"  Recall:         {'paused' if policy['recall_paused'] else 'active'}",
        f"  Policy:         v{policy['policy_version']}; provenance: {provenance}",
        f"  Project:        {_status_project() or 'unresolved'}",
        f"Pause scope: {PAUSE_SCOPE}.",
        PAUSED_READ_AVAILABILITY,
    ])
    return _text("\n".join(lines))


# ── diff ──────────────────────────────────────────────────────────
@_handles("palinode_diff")
async def _tool_diff(arguments: dict[str, Any]) -> list[types.TextContent]:
    days = int(arguments.get("days", 7))
    params = {"days": str(days)}
    paths = _coerce_str_array(arguments.get("paths"))
    if paths:
        params["paths"] = ",".join(paths)
    resp = await _get("/diff", params=params)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    return _text(resp.json().get("diff", "No changes."))


# ── session init (ADR-012 Layer 4) ────────────────────────────────
@_handles("palinode_session_init")
async def _tool_session_init(arguments: dict[str, Any]) -> list[types.TextContent]:
    if not config.auto_inject.enabled:
        return _text(
            "Session auto-inject is disabled (auto_inject.enabled=false). "
            "Call palinode_search directly for context."
        )
    client_name = _session_init_client_name()
    if _auto_inject_suppressed_for(client_name):
        return _text(
            f"Session auto-inject is suppressed for this client ({client_name}) — "
            "it already receives memory instructions through its instruction "
            "file/skill/hook layers. Call palinode_search directly for context."
        )
    body = {}
    remote = _http_request() is not None
    if arguments.get("project"):
        body["project"] = arguments["project"]
    if arguments.get("cwd"):
        body["cwd"] = arguments["cwd"]
    elif not body and not remote:
        # stdio servers run on the client's machine, so the server
        # process CWD is a usable default scope hint. Explicit args win.
        # An HTTP server's CWD is its own checkout, never the client's.
        from palinode.core.context_prime import ambient_cwd

        body["cwd"] = ambient_cwd()
    if "project" not in body:
        # The MCP process can carry a client-specific pinned project (a stdio
        # client's PALINODE_PROJECT, an HTTP client's X-Palinode-Project
        # header) that the API process cannot see. Forward that resolved
        # scope across the process boundary — with the source that decided it,
        # so the digest reports a pinned setting as one rather than as an
        # argument — but leave cwd/git resolution in the API for clients
        # without an explicit environment scope.
        from palinode.core.context_prime import resolve_context

        resolution = _resolve_scope() if remote else resolve_context(cwd=body.get("cwd"))
        if resolution.basis == "environment" and resolution.project:
            body["project"] = resolution.project
            body["project_resolved_by"] = resolution.basis
    resp = await _post("/context/prime", json=body)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    from palinode.core.context_prime import format_context_digest

    return _text(format_context_digest(resp.json()))


# ── blame ─────────────────────────────────────────────────────────
@_handles("palinode_blame")
async def _tool_blame(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments.get("file_path")
    if not file_path:
        return _text("Error: file_path is required")
    params: dict[str, str] = {}
    if arguments.get("search"):
        params["search"] = arguments["search"]
    if arguments.get("claims"):
        params["claims"] = "true"
    resp = await _get(f"/blame/{file_path}", params=params)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    data = resp.json()
    blame_text = data.get("blame", "No blame data.")
    if arguments.get("claims"):
        from palinode.core.claims import format_claims_resolution

        claims_text = format_claims_resolution(file_path, data.get("claims", []))
        return _text(f"{blame_text}\n\n{claims_text}")
    return _text(blame_text)


# ── trace ─────────────────────────────────────────────────────────
@_handles("palinode_trace")
async def _tool_trace(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments["file_path"]
    resp = await _get(f"/trace/{file_path}")
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    from palinode.core.trace import format_trace_text

    return _text(format_trace_text(resp.json()))


# ── explain ───────────────────────────────────────────────────────
@_handles("palinode_explain")
async def _tool_explain(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Explain one delivery from the receipt rows it wrote.

    The public view only: the caller's own query prose stays diagnostics-only,
    exactly as on the receipt, and there is no parameter here that would lift
    that — an operator reading their own store uses the CLI or the local
    inspector.
    """
    bundle_id = arguments.get("bundle_id")
    if not bundle_id:
        return _text("Error: bundle_id is required")
    params: dict[str, Any] = {"view": "public"}
    if arguments.get("limit") is not None:
        params["limit"] = arguments["limit"]
    resp = await _get(f"/explain/{bundle_id}", params=params)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    from palinode.core.explain import format_explanation_text

    return _text(format_explanation_text(resp.json()))


# ── rollback ──────────────────────────────────────────────────────
@_handles("palinode_rollback")
async def _tool_rollback(arguments: dict[str, Any]) -> list[types.TextContent]:
    file_path = arguments.get("file_path")
    if not file_path:
        return _text("Error: file_path is required")
    params: dict[str, str] = {"file_path": file_path}
    if arguments.get("commit"):
        params["commit"] = arguments["commit"]
    params["dry_run"] = str(arguments.get("dry_run", True)).lower()
    params["undo_retirements"] = str(arguments.get("undo_retirements", False)).lower()
    resp = await _post_params("/rollback", params=params)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    return _text(resp.json().get("result", "Done."))


# ── push ──────────────────────────────────────────────────────────
@_handles("palinode_push")
async def _tool_push(arguments: dict[str, Any]) -> list[types.TextContent]:
    resp = await _post("/push")
    if resp.status_code != 200:
        return _text(f"Push failed: {resp.text}")
    return _text(resp.json().get("result", "Pushed."))


# ── trigger ───────────────────────────────────────────────────────
@_handles("palinode_trigger")
async def _tool_trigger(arguments: dict[str, Any]) -> list[types.TextContent]:
    action = arguments.get("action", "create")
    if action == "list":
        resp = await _get("/triggers")
        if resp.status_code != 200:
            return _text(f"Error: {resp.text}")
        return _text(json.dumps(resp.json(), indent=2))

    elif action == "delete":
        tid = arguments.get("trigger_id")
        if not tid:
            return _text("Error: trigger_id required for delete")
        resp = await _delete(f"/triggers/{tid}")
        if resp.status_code != 200:
            return _text(f"Error: {resp.text}")
        return _text(f"Deleted trigger {tid}")

    else:  # create
        desc = arguments.get("description")
        mem = arguments.get("memory_file")
        if not desc or not mem:
            return _text("Error: description and memory_file required for create")
        body = {
            "description": desc,
            "memory_file": mem,
        }
        if arguments.get("trigger_id"):
            body["trigger_id"] = arguments["trigger_id"]
        if arguments.get("threshold") is not None:
            body["threshold"] = arguments["threshold"]
        if arguments.get("cooldown_hours") is not None:
            body["cooldown_hours"] = arguments["cooldown_hours"]
        if arguments.get("expires_at"):
            body["expires_at"] = arguments["expires_at"]
        if arguments.get("authority"):
            body["authority"] = arguments["authority"]
        resp = await _post("/triggers", json=body)
        if resp.status_code != 200:
            return _text(f"Error: {resp.text}")
        data = resp.json()
        return _text(f"Created trigger {data.get('id', '?')} for {mem}")


# ── session_end ───────────────────────────────────────────────────
@_handles("palinode_session_end")
async def _tool_session_end(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {"summary": arguments.get("summary", "")}
    # Forward empty arrays rather than dropping them. The server's
    # envelope guard reads the absence of `decisions`/`blockers` as the
    # signature of an absorbed tool call, so eliding `[]` here
    # manufactured that signature for callers who had simply nothing to
    # report. That rule now lives in core/write_input.py and applies on
    # every surface, not just this one.
    body.update(build_payload(SESSION_END_PARAMS, arguments))

    resp = await _post("/session-end", json=body, timeout=_SESSION_END_TIMEOUT)
    if resp.status_code != 200:
        return _text(f"Session-end failed: {resp.text}")
    data = resp.json()
    if data.get("dry_run"):
        # Lead with the fact that nothing was written. A dry run that
        # reads like a capture is worse than no dry run — the caller
        # moves on believing the session is recorded.
        targets = [data["daily_file"]]
        if data.get("status_file"):
            targets.append(data["status_file"])
        return _text(
            "DRY RUN — nothing written, committed, or pushed.\n"
            f"Would append to: {', '.join(targets)}\n\n"
            f"{data.get('entry', '')}"
        )
    status_msg = f" + status → {data['status_file']}" if data.get("status_file") else ""
    # Report push outcome so the wrap flow can say "pushed" vs "pending"
    # without a second tool call.
    if body.get("push"):
        push_msg = " + pushed" if data.get("pushed") else " (push pending — commit local, push did not succeed)"
    else:
        push_msg = ""
    return _text(f"Session captured → {data['daily_file']}{status_msg}{push_msg}\n\n{data.get('entry', '')}")


# ── dedup_suggest ─────────────────────────────────────────────────
@_handles("palinode_dedup_suggest")
async def _tool_dedup_suggest(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {"content": arguments.get("content", "")}
    if arguments.get("min_similarity") is not None:
        body["min_similarity"] = float(arguments["min_similarity"])
    if arguments.get("top_k") is not None:
        body["top_k"] = int(arguments["top_k"])
    resp = await _post("/dedup-suggest", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    data = resp.json()
    if not data:
        return _text("No semantically similar files found.")
    lines = []
    for r in data:
        rel = _rel_path_from(r)
        tag = " ⚠ STRONG-DUP (likely should update, not create)" if r.get("strong_dup") else ""
        pct = int(r.get("similarity", 0) * 100)
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:160]
        lines.append(f"[{rel}] ({pct}% similar){tag}\n  {snippet}")
    return _text("\n\n".join(lines))


# ── orphan_repair ─────────────────────────────────────────────────
@_handles("palinode_orphan_repair")
async def _tool_orphan_repair(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {"broken_link": arguments.get("broken_link", "")}
    if arguments.get("min_similarity") is not None:
        body["min_similarity"] = float(arguments["min_similarity"])
    if arguments.get("top_k") is not None:
        body["top_k"] = int(arguments["top_k"])
    resp = await _post("/orphan-repair", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    data = resp.json()
    if not data:
        return _text("No semantically related files found.")
    lines = []
    for r in data:
        rel = _rel_path_from(r)
        pct = int(r.get("similarity", 0) * 100)
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:160]
        lines.append(f"[{rel}] ({pct}% similar)\n  {snippet}")
    return _text("\n\n".join(lines))


# ── cluster_neighbors ─────────────────────────────────────────────
@_handles("palinode_cluster_neighbors")
async def _tool_cluster_neighbors(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {"file_path": arguments.get("file_path", "")}
    if arguments.get("min_similarity") is not None:
        body["min_similarity"] = float(arguments["min_similarity"])
    if arguments.get("top_k") is not None:
        body["top_k"] = int(arguments["top_k"])
    resp = await _post("/cluster-neighbors", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    data = resp.json()
    if not data:
        return _text("No unlinked semantic neighbours found above threshold.")
    lines = []
    for r in data:
        rel = _rel_path_from(r)
        pct = int(r.get("similarity", 0) * 100)
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:160]
        lines.append(f"[{rel}] ({pct}% similar)\n  {snippet}")
    return _text("\n\n".join(lines))


# ── topic_coverage ────────────────────────────────────────────────
@_handles("palinode_topic_coverage")
async def _tool_topic_coverage(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {"query": arguments.get("query", "")}
    if arguments.get("min_similarity") is not None:
        body["min_similarity"] = float(arguments["min_similarity"])
    resp = await _post("/topic-coverage", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Error: {resp.text}")
    data = resp.json()
    covered = data.get("covered", False)
    best = data.get("best_match")
    sim = data.get("similarity", 0.0)
    if covered and best:
        fp = _rel_path_from(data, key="best_match")
        pct = int(sim * 100)
        return _text(f"COVERED — {fp} ({pct}% similar). Consider updating the existing page.")
    return _text(f"NOT COVERED — no existing page matches above threshold (best similarity: {sim:.2f}). Safe to create new.")


# ── resolve ───────────────────────────────────────────────────────
@_handles("palinode_resolve")
async def _tool_resolve(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Bounded resolution. Renders the API's own bundle text, never its own.

    The rendering lives in ``palinode.core.bundle`` so this surface, REST and
    the CLI cannot disagree about what stands; the structured bundle is one
    ``/resolve`` call away for a caller that wants the fields.
    """
    body: dict[str, Any] = {}
    for key in ("query", "ref", "intent"):
        if arguments.get(key):
            body[key] = arguments[key]
    context = coerce_str_array(arguments.get("context"))
    if context:
        body["context"] = context
    for key in ("max_items", "max_chars"):
        if arguments.get(key) is not None:
            body[key] = int(arguments[key])
    if str(arguments.get("include_retired", False)).lower() in ("true", "1"):
        body["include_retired"] = True
    # The client's project, resolved here, on the surface closest to the
    # client, exactly as palinode_search resolves it. The API then scopes the
    # bundle to the client, never to the machine the API happens to run on.
    scope = _resolve_scope()
    if scope.project:
        body["project"] = scope.project
    if str(arguments.get("include_other_projects", False)).lower() in ("true", "1"):
        body["include_other_projects"] = True
    resp = await _post("/resolve", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Resolve failed: {resp.text}")
    return _text(resp.json().get("text", ""))


# ── doctor ────────────────────────────────────────────────────────
@_handles("palinode_doctor")
async def _tool_doctor(arguments: dict[str, Any]) -> list[types.TextContent]:
    resp = await _get("/doctor", params={"fast": "true"}, timeout=10.0)
    if resp.status_code != 200:
        return _text(f"Doctor failed: {resp.text}")
    data = resp.json()
    return _text(json.dumps(data, indent=2))


@_handles("palinode_doctor_deep")
async def _tool_doctor_deep(arguments: dict[str, Any]) -> list[types.TextContent]:
    resp = await _get("/doctor", params={"canary": "true"}, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Doctor (deep) failed: {resp.text}")
    data = resp.json()
    return _text(json.dumps(data, indent=2))


# ── lint ──────────────────────────────────────────────────────────
@_handles("palinode_lint")
async def _tool_lint(arguments: dict[str, Any]) -> list[types.TextContent]:
    # `apply` is deliberately absent from this surface: a health scan an agent
    # can call freely must not be able to retire memories as a side effect. The
    # proposal set is the agent-facing half of the loop; applying it is the
    # operator's move, on the CLI or the API.
    params = {"propose": "true"} if arguments.get("propose") else None
    resp = await _post_params("/lint", params=params, timeout=120.0)
    if resp.status_code != 200:
        return _text(f"Lint failed: {resp.text}")
    return _text(json.dumps(resp.json(), indent=2))


# ── review ───────────────────────────────────────────────────
@_handles("palinode_review")
async def _tool_review(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {}
    if arguments.get("project"):
        body["project"] = arguments["project"]
    resp = await _post("/review", json=body, timeout=120.0)
    if resp.status_code != 200:
        return _text(f"Review failed: {resp.text}")
    return _text(json.dumps(resp.json(), indent=2))


# ── corrections ───────────────────────────────────────────────────
@_handles("palinode_corrections")
async def _tool_corrections(arguments: dict[str, Any]) -> list[types.TextContent]:
    # `scan` is deliberately absent from this surface. Reading a person's
    # session transcripts is an operator action, and the operator's tools are
    # the CLI and the REST API; an agent asking "what did I get wrong lately?"
    # gets the queue as it stands, which is what `readOnlyHint` promises.
    body: dict[str, Any] = {"scan": False}
    if arguments.get("project"):
        body["project"] = arguments["project"]
    if arguments.get("since_days") is not None:
        body["since_days"] = arguments["since_days"]
    resp = await _post("/corrections", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Corrections listing failed: {resp.text}")
    return _text(json.dumps(resp.json(), indent=2))


# ── correction review ─────────────────────────────────────────────────────
#: The params each review phase forwards. Listed once so a tool cannot send a
#: key its route does not model, and a new canonical param is added in one
#: place rather than three.
_CORRECTION_PREVIEW_KEYS = (
    "target", "claim_id", "replacement", "action", "reason", "candidate_id", "project",
    "backed_by", "allow_content_loss",
)
_CORRECTION_APPLY_KEYS = _CORRECTION_PREVIEW_KEYS + ("expect_revision", "confirm")


def _correction_body(arguments: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: arguments[key] for key in keys if arguments.get(key) is not None}


async def _correction_call(
    phase: str, body: dict[str, Any], label: str
) -> list[types.TextContent]:
    """One transport for all four phases; a refusal comes back as its payload.

    A 409 here is the contract working — a stale revision, an ambiguous target,
    an unnamed one — so the refusal's own JSON is returned rather than an
    opaque status line. The agent needs the candidates to choose from.

    The same code also carries a *partial* apply, which is the opposite of a
    refusal: something was written. It is led as such, because an agent told
    "refused" would reasonably conclude the store was untouched and re-run the
    correction instead of finishing it.
    """
    from palinode.core.parity import CORRECTION_APPLIED_PARTIAL

    resp = await _post(f"/corrections/{phase}", json=body, timeout=120.0)
    if resp.status_code == 409:
        try:
            detail = resp.json().get("detail", {})
        except ValueError:
            detail = {"detail": resp.text}
        if isinstance(detail, dict) and detail.get("applied") == CORRECTION_APPLIED_PARTIAL:
            return _text(f"{label} PARTIALLY applied: {json.dumps(detail, indent=2)}")
        return _text(f"{label} refused: {json.dumps(detail, indent=2)}")
    if resp.status_code != 200:
        return _text(f"{label} failed: {resp.text}")
    return _text(json.dumps(resp.json(), indent=2))


@_handles("palinode_correction_preview")
async def _tool_correction_preview(arguments: dict[str, Any]) -> list[types.TextContent]:
    return await _correction_call(
        "preview",
        _correction_body(arguments, _CORRECTION_PREVIEW_KEYS),
        "Correction preview",
    )


@_handles("palinode_correction_apply")
async def _tool_correction_apply(arguments: dict[str, Any]) -> list[types.TextContent]:
    if not arguments.get("confirm"):
        return _text(
            "Correction apply refused: confirm=true is required. Call "
            "palinode_correction_preview first and pass back its "
            "confirm.expect_revision — nothing is written without both."
        )
    return await _correction_call(
        "apply",
        _correction_body(arguments, _CORRECTION_APPLY_KEYS),
        "Correction apply",
    )


@_handles("palinode_correction_dismiss")
async def _tool_correction_dismiss(arguments: dict[str, Any]) -> list[types.TextContent]:
    body = _correction_body(arguments, ("candidate_id", "reason"))
    if not body.get("reason"):
        return _text("Correction dismiss refused: a reason is required — it is the record.")
    return await _correction_call("dismiss", body, "Correction dismiss")


@_handles("palinode_correction_undo")
async def _tool_correction_undo(arguments: dict[str, Any]) -> list[types.TextContent]:
    body = _correction_body(
        arguments, ("target", "expect_revision", "confirm", "reason")
    )
    return await _correction_call("undo", body, "Correction undo")


# ── prompt ────────────────────────────────────────────────────────
@_handles("palinode_prompt")
async def _tool_prompt(arguments: dict[str, Any]) -> list[types.TextContent]:
    action = arguments.get("action", "list")

    if action == "list":
        params: dict[str, str] = {}
        if arguments.get("task"):
            params["task"] = arguments["task"]
        resp = await _get("/prompts", params=params)
        if resp.status_code != 200:
            return _text(f"Error listing prompts: {resp.text}")
        data = resp.json()
        if not data:
            return _text("No prompts found.")
        lines = []
        for p in data:
            active_tag = " [active]" if p.get("active") else ""
            lines.append(
                f"{p['name']} (task={p.get('task','')}, "
                f"model={p.get('model','')}, "
                f"v{p.get('version','')}){active_tag}"
            )
        return _text("\n".join(lines))

    elif action == "read":
        pname = arguments.get("name")
        if not pname:
            return _text("Error: name required for 'read'")
        resp = await _get(f"/prompts/{pname}")
        if resp.status_code == 404:
            return _text(f"Prompt '{pname}' not found.")
        if resp.status_code != 200:
            return _text(f"Error reading prompt: {resp.text}")
        data = resp.json()
        header = (
            f"# {data['name']} (task={data.get('task','')}, "
            f"model={data.get('model','')}, v{data.get('version','')})"
        )
        active_note = " [ACTIVE]" if data.get("active") else ""
        return _text(f"{header}{active_note}\n\n{data.get('content','')}")

    elif action == "activate":
        pname = arguments.get("name")
        if not pname:
            return _text("Error: name required for 'activate'")
        resp = await _post(f"/prompts/{pname}/activate")
        if resp.status_code == 404:
            return _text(f"Prompt '{pname}' not found.")
        if resp.status_code != 200:
            return _text(f"Error activating prompt: {resp.text}")
        data = resp.json()
        return _text(f"Activated '{data['activated']}' for task={data['task']}")

    else:
        return _text(f"Unknown action: {action}. Use 'list', 'read', or 'activate'.")


# ── depends ───────────────────────────────────────────────────────
@_handles("palinode_depends")
async def _tool_depends(arguments: dict[str, Any]) -> list[types.TextContent]:
    if arguments.get("unblocked"):
        resp = await _get("/depends/_unblocked")
        if resp.status_code != 200:
            return _text(f"API Error: {resp.text}")
        items = resp.json()
        if not items:
            return _text("No unblocked items found.")
        lines = [
            f"{it['slug']}" + (f" (status={it['status']})" if it.get("status") else "")
            for it in items
        ]
        return _text("Unblocked items:\n" + "\n".join(lines))
    else:
        slug = arguments.get("slug", "").strip()
        if not slug:
            return _text("Error: 'slug' is required unless unblocked=true")
        resp = await _get(f"/depends/{slug}")
        if resp.status_code != 200:
            return _text(f"API Error: {resp.text}")
        import json as _json
        return _text(_json.dumps(resp.json(), indent=2))


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """Route one tool call to its handler.

    The error handling below is the reason this stays a function rather than a
    bare dict lookup at the call site: every handler shares one translation of
    transport failures into the dispatcher's text-response contract.
    """
    try:
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return _text(f"Unknown tool: {name}")
        rejected = _validate_arguments(name, arguments)
        if rejected is not None:
            return _text(rejected)
        return await handler(arguments)
    except httpx.ConnectError:
        return _text(f"Error: Cannot reach Palinode API at {_api_url('')}. Is palinode-api running?")
    except httpx.TimeoutException:
        return _text(_timeout_message(name))
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return _text(f"Error: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def async_main() -> None:
    """Async boot sequence — start MCP server over stdio."""
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await _close_http()


def main() -> None:
    """Synchronous entry point for setuptools console_scripts (stdio transport)."""
    asyncio.run(async_main())


def _build_mcp_http_app(token: str | None):
    """Build and return the Starlette MCP HTTP application.

    Extracted for testability — ``main_http`` builds the app then hands it
    to uvicorn; tests drive it directly via ``TestClient``.

    Parameters
    ----------
    token:
        Bearer token to protect the server, or ``None`` for no auth.
    """
    import contextlib
    from collections.abc import AsyncIterator

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from palinode.core.auth import BearerAuthMiddleware, MCP_EXEMPT_PATHS
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    session_manager = StreamableHTTPSessionManager(app=server)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            async with session_manager.run():
                yield
        finally:
            await _close_http()

    async def healthz(request):
        """Health check — returns 200 if the session manager is running.

        Clients can poll this for connection-liveness detection without
        initiating a full MCP session.
        """
        return JSONResponse({
            "status": "ok",
            "service": "palinode-mcp-http",
            "transport": "streamable-http",
            "api_backend": _api_url(""),
        })

    starlette_app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Mount("/mcp", app=session_manager.handle_request),
        ],
    )
    # Registered before request routing so unauthenticated callers never
    # reach the MCP session handler. The middleware is a no-op when token
    # is None. /healthz is exempt so uptime probes don't need the token.
    starlette_app.add_middleware(
        BearerAuthMiddleware,
        token=token,
        exempt_paths=MCP_EXEMPT_PATHS,
    )
    return starlette_app


def _parse_http_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse ``palinode-mcp-http`` argv: ``--host`` / ``--port``.

    A flag left unset parses as ``None`` so the caller can fall back to the
    ``PALINODE_MCP_HTTP_HOST`` / ``_PORT`` env vars. Unknown flags and
    positionals exit non-zero via argparse's normal error path.
    """
    parser = argparse.ArgumentParser(
        prog="palinode-mcp-http",
        description="Palinode MCP server over streamable-HTTP (serves /mcp/).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="bind address (overrides PALINODE_MCP_HTTP_HOST; default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="bind port (overrides PALINODE_MCP_HTTP_PORT; default 6341)",
    )
    return parser.parse_args(argv)


def main_http(argv: list[str] | None = None) -> None:
    """Entry point for Streamable HTTP transport — palinode-mcp-http.

    Exposes the MCP server over Streamable HTTP so remote clients (Claude Code,
    Claude Desktop, Cursor, Zed, etc.) can connect via URL without running a
    local process.

    Bind resolution: ``--host`` / ``--port`` flags win over the env vars,
    which win over the defaults. A non-loopback bind with no
    ``PALINODE_API_TOKEN`` refuses to start unless
    ``PALINODE_API_ALLOW_UNAUTH=1`` — the same gate, and the same single
    opt-out knob, as the API server. The MCP HTTP transport has no token of
    its own: ``PALINODE_API_TOKEN`` both gates ``/mcp/`` here and protects
    the API this transport proxies to.

    Env vars:
      PALINODE_MCP_HTTP_HOST  — bind address (default: 127.0.0.1)
      PALINODE_MCP_HTTP_PORT  — bind port (default: 6341)
      PALINODE_MCP_LOG_LEVEL  — uvicorn log level (default: info)
      PALINODE_API_TOKEN      — bearer token; required for a non-loopback bind
      PALINODE_API_ALLOW_UNAUTH — ``1`` lets a non-loopback bind start
                                 token-less (network-isolated hosts only)
      PALINODE_MCP_BIND_INTENT — set to ``public`` to confirm intentional
                                 non-loopback bind; requires PALINODE_API_TOKEN.

    Deprecated env var aliases (still honored, warn at startup, removal
    planned): PALINODE_MCP_SSE_HOST, PALINODE_MCP_SSE_PORT. When both the
    canonical and the legacy name are set, the canonical name wins.

    Client config (any IDE):
      { "url": "http://your-server:6341/mcp/" }

    Parameters
    ----------
    argv:
        Command-line arguments without the program name. ``None`` (the
        console-script path) reads ``sys.argv[1:]``.
    """
    import os

    import uvicorn
    from palinode.core.auth import (
        allow_unauth_opt_out,
        bind_host_phrasing,
        is_loopback_host,
        validate_auth_config,
        validate_bind_auth,
    )

    args = _parse_http_args(argv)

    # Resolve the bind host AND remember which knob set it. The gate below
    # and the token-less startup warning both name a knob for the operator
    # to change; naming the canonical env var when the bind came from
    # ``--host`` sends them to a variable they never set.
    if args.host:
        host, host_var, host_var_kind = args.host, "--host", "flag"
    elif os.environ.get("PALINODE_MCP_HTTP_HOST"):
        host = os.environ["PALINODE_MCP_HTTP_HOST"]
        host_var, host_var_kind = "PALINODE_MCP_HTTP_HOST", "env"
    elif os.environ.get("PALINODE_MCP_SSE_HOST"):  # deprecated alias
        host = os.environ["PALINODE_MCP_SSE_HOST"]
        host_var, host_var_kind = "PALINODE_MCP_SSE_HOST", "env"
    else:
        host, host_var, host_var_kind = "127.0.0.1", "PALINODE_MCP_HTTP_HOST", "env"
    port = (
        args.port
        if args.port is not None
        else int(
            os.environ.get("PALINODE_MCP_HTTP_PORT")
            or os.environ.get("PALINODE_MCP_SSE_PORT")  # deprecated alias
            or "6341"
        )
    )
    legacy_only = [
        legacy
        for canonical, legacy in (
            ("PALINODE_MCP_HTTP_HOST", "PALINODE_MCP_SSE_HOST"),
            ("PALINODE_MCP_HTTP_PORT", "PALINODE_MCP_SSE_PORT"),
        )
        if os.environ.get(legacy) and not os.environ.get(canonical)
    ]
    if legacy_only:
        logger.warning(
            "%s is deprecated and will be removed in a future release; "
            "rename to %s.",
            ", ".join(legacy_only),
            ", ".join(v.replace("_SSE_", "_HTTP_") for v in legacy_only),
        )
    log_level = os.environ.get("PALINODE_MCP_LOG_LEVEL", "info")

    # Resolve token and run the bind gates INSIDE this entry point, not at
    # module level. palinode/mcp.py is imported for the stdio transport too
    # — a module-level gate would fire on every ``import palinode.mcp``,
    # killing stdio sessions when PALINODE_MCP_BIND_INTENT=public is set.
    #
    # Same two gates as the API server (palinode.api.server): the bind gate
    # keys on the resolved host — non-loopback + no token refuses unless
    # PALINODE_API_ALLOW_UNAUTH=1 (the one opt-out knob, shared with the API;
    # no MCP twin) — and the intent gate keeps PALINODE_MCP_BIND_INTENT=public
    # meaning "token required". The MCP HTTP transport has no token of its
    # own: PALINODE_API_TOKEN gates /mcp/ here AND protects the API this
    # transport proxies to, so the check is "is the API it proxies to
    # protected".
    token = load_api_token()
    mcp_bind_intent_public = (
        os.environ.get("PALINODE_MCP_BIND_INTENT", "").lower() == "public"
    )
    allow_unauth = allow_unauth_opt_out()
    validate_bind_auth(
        host,
        token,
        allow_unauth=allow_unauth,
        host_var=host_var,
        host_var_kind=host_var_kind,
        exposure="every Palinode MCP tool (save/search/read/...) unauthenticated",
        detail=(
            "The MCP HTTP transport has no token of its own: PALINODE_API_TOKEN "
            "both gates /mcp/ here and protects the API it proxies to, so this "
            "check is whether that API is protected."
        ),
    )
    validate_auth_config(
        mcp_bind_intent_public,
        token,
        bind_intent_var="PALINODE_MCP_BIND_INTENT",
    )

    starlette_app = _build_mcp_http_app(token)

    # Startup log for a non-loopback bind, mirroring the API server. The hard
    # refusal already fired above, so reaching here token-less means the
    # operator opted out explicitly — warn loudly on every start regardless.
    if not is_loopback_host(host):
        if token is None:
            logger.warning(
                "MCP HTTP binding to %s — accessible from any network. "
                "No authentication is configured (PALINODE_API_ALLOW_UNAUTH=1 "
                "set). Use %s for local-only access, or set "
                "PALINODE_API_TOKEN to require bearer auth.",
                host,
                bind_host_phrasing(host_var, host, host_var_kind)[1],
            )
        elif mcp_bind_intent_public:
            logger.debug(
                "MCP HTTP binding to %s — PALINODE_MCP_BIND_INTENT=public set "
                "with PALINODE_API_TOKEN; bearer auth required.",
                host,
            )
        else:
            logger.info(
                "MCP HTTP binding to %s with PALINODE_API_TOKEN configured "
                "— bearer auth required.",
                host,
            )

    print(f"Palinode MCP (Streamable HTTP) listening on http://{host}:{port}/mcp/")
    print(f"  Health check: http://{host}:{port}/healthz")
    print(f"  API backend:  {_api_url('')}")
    if token:
        print("  Bearer auth: enabled (PALINODE_API_TOKEN)")
    else:
        print("  Bearer auth: disabled (no PALINODE_API_TOKEN)")
    uvicorn.run(starlette_app, host=host, port=port, log_level=log_level)


def main_sse(argv: list[str] | None = None) -> None:
    """Deprecated alias for :func:`main_http` (``palinode-mcp-sse`` console script).

    Kept so existing systemd/nix units keep starting; warns at startup and is
    scheduled for removal in a future release. New deployments use
    ``palinode-mcp-http``. ``argv`` passes through unchanged.
    """
    logger.warning(
        "palinode-mcp-sse is deprecated and will be removed in a future "
        "release; it serves streamable-HTTP, not SSE. Update your service "
        "unit to palinode-mcp-http (systemd: edit ExecStart, then "
        "systemctl daemon-reload)."
    )
    main_http(argv)


if __name__ == "__main__":
    main()

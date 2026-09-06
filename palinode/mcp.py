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
from palinode.core.auth import load_api_token
from palinode.core.config import ToolSurface, config, validate_tool_surface
from palinode.core.defaults import (
    SAVE_SOURCE_HEADER as _SOURCE_HEADER,
    SESSION_END_TIMEOUT_SECONDS as _SESSION_END_TIMEOUT,
    _SESSION_END_TIMEOUT_SENTINEL as _SENTINEL,
)
from palinode.core.parity import CATEGORIES, MEMORY_TYPES, PROMPT_TASKS, TIERS
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
    "the rationale). Call palinode_session_end before the session ends."
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
    "Git-versioned markdown memory for AI agents: save, search, compact, lint, and audit."
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


def _resolve_context() -> list[str] | None:
    """Resolve ambient project context from environment (ADR-008).

    Resolution order:
    1. PALINODE_PROJECT env var (explicit entity ref, e.g. "project/palinode")
    2. CWD basename → config.context.project_map lookup
    3. CWD basename → auto-detect as project/{basename} (if auto_detect=True)
    """
    if not config.context.enabled:
        return None

    # 1. Explicit env var
    explicit = os.environ.get("PALINODE_PROJECT")
    if explicit:
        return [explicit] if "/" in explicit else [f"project/{explicit}"]

    # 2/3. CWD-based resolution
    cwd = os.environ.get("CWD") or os.getcwd()
    basename = os.path.basename(cwd)
    if not basename:
        return None

    # Check config map
    if basename in config.context.project_map:
        entity = config.context.project_map[basename]
        return [entity] if "/" in entity else [f"project/{entity}"]

    # Auto-detect
    if config.context.auto_detect:
        return [f"project/{basename}"]

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
    "update mcp.py or defaults.py to stay in sync (#377)"
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
    "Consolidation failed",
    "Archive failed",
    "Archive-expired sweep failed",
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


def _format_results(results: list[dict[str, Any]], full: bool = False) -> str:
    """Format search results as clean text — minimal context burn.

    Renders ``snippet`` by default (populated by ``/search`` per the palinode_search
    returns un-truncated chunk content; exceeds work) so pathologically large chunks
    don't blow the MCP tool-result budget. When ``full=True``, renders ``content``
    capped at ``_FULL_CONTENT_HARD_CAP``; callers that want untruncated bodies should
    use ``palinode_read``.

    Falls back to a defensive 400-char ``content`` slice if neither field is
    populated (older API or external caller).
    """
    if not results:
        return "No results found."
    parts = []
    any_truncated = False
    for r in results:
        rel = _rel_path_from(r)
        match_label = describe_match(r)
        freshness = r.get("freshness")
        fresh_label = f" ✓ {freshness}" if freshness == "valid" else (f" ⚠ {freshness}" if freshness == "stale" else "")
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
        if contradicts:
            _link_bits.append("⚠ contradicts: " + ", ".join(contradicts))
        if backed_by:
            _link_bits.append("backed by: " + ", ".join(backed_by))
        links_label = " [" + " | ".join(_link_bits) + "]" if _link_bits else ""

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

        parts.append(
            f"[{rel}] ({match_label}){fresh_label}{epi_label}{links_label}{refs_label}\n{(body or '').strip()}"
        )

    rendered = "\n\n---\n\n".join(parts)
    if any_truncated and not full:
        rendered += (
            "\n\n(some results truncated — call palinode_search with full=true, "
            "or palinode_read <file> for the complete text.)"
        )
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
                "file excerpts ranked by semantic similarity."
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
                        "description": "Override similarity threshold (0.0-1.0); higher is stricter.",
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
                        # append is episodic; replace marks a sticky living
                        # document protected from history-forking compaction.
                        "description": "Save behavior: append episodic memory or replace a living document.",
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
            description="Check Palinode health: API reachability, index stats, last watcher run.",
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
                "to preview the proposed operations without applying them."
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
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Archive / Supersede Memory",
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
            name="palinode_rollback",
            description=(
                "Revert a memory file to a previous version. Safe: creates a new commit "
                "preserving the old version in history. Defaults to dry run."
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
                "properties": {},
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

    resp = await _get("/list", params=params)
    if resp.status_code != 200:
        return _text(f"API Error: {resp.text}")
    data = resp.json()
    if not data:
        return _text("No files found.")
    parts = []
    for f in data:
        c_tag = " [core]" if f.get("core") else ""
        parts.append(f"{f['file']} — {f.get('summary', '')}{c_tag}")
    return _text("\n".join(parts))


# ── read ──────────────────────────────────────────────────────────
@_handles("palinode_read")
async def _tool_read(arguments: dict[str, Any]) -> list[types.TextContent]:
    include_meta = bool(arguments.get("meta", False))
    params: dict[str, Any] = {"file_path": arguments["file_path"], "meta": "true"}
    tier = arguments.get("tier")
    if tier:
        params["tier"] = tier
    resp = await _get("/read", params=params)
    if resp.status_code != 200:
        return _text(f"Error reading file: {resp.text}")
    data = resp.json()
    content = data.get("content", "")
    if include_meta:
        fm = data.get("frontmatter") or {}
        # Render as YAML-ish frontmatter + body so downstream consumers
        # can re-parse if they want.  Keep it simple: the file already
        # has the same structure on disk.
        fm_lines = "\n".join(f"{k}: {v!r}" for k, v in fm.items())
        return _text(f"---\n{fm_lines}\n---\n{content}")
    return _text(content)


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
    # ADR-008: ambient context boost
    context = _resolve_context()
    if context:
        body["context"] = context

    resp = await _post("/search", json=body, timeout=60.0)
    if resp.status_code != 200:
        return _text(f"Search failed: {resp.text}")
    # `full` is purely a rendering choice — the API always
    # populates `snippet` and preserves `content`, so the MCP picks
    # which to render without an extra round-trip.
    return _text(_format_results(resp.json(), full=bool(arguments.get("full"))))


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
    if not data.get("indexed_vec", True):
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
    confirmation = f"Saved to {rel}"
    if outcome_text:
        confirmation += f" ({outcome_text})"
    if warnings:
        confirmation += f" [warnings: {'; '.join(warnings)}]"
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
@_handles("palinode_consolidate")
async def _tool_consolidate(arguments: dict[str, Any]) -> list[types.TextContent]:
    body: dict[str, Any] = {}
    if arguments.get("dry_run"):
        body["dry_run"] = True
    if arguments.get("nightly"):
        body["nightly"] = True
    if arguments.get("sources"):
        body["sources"] = _coerce_str_array(arguments["sources"])
    resp = await _post("/consolidate", json=body, timeout=300.0)
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
    resp = await _post("/archive", json=body)
    if resp.status_code != 200:
        return _text(f"Archive failed: {resp.text}")
    data = resp.json()
    if data.get("status") == "already_archived":
        return _text(f"{data.get('file')} is already archived — no change.")
    successor = data.get("superseded_by")
    verb = f"Superseded by {successor}" if successor else "Archived"
    return _text(
        f"{verb}: {data.get('file')}\n"
        f"History: {data.get('history_file')}\n"
        f"Chunks suppressed from recall: {data.get('chunks_updated', 0)}"
    )


# ── status ────────────────────────────────────────────────────────
@_handles("palinode_status")
async def _tool_status(arguments: dict[str, Any]) -> list[types.TextContent]:
    resp = await _get("/status")
    if resp.status_code != 200:
        return _text(f"API unreachable: {resp.text}")
    s = resp.json()
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
    if arguments.get("project"):
        body["project"] = arguments["project"]
    if arguments.get("cwd"):
        body["cwd"] = arguments["cwd"]
    elif not body:
        # stdio servers run on the client's machine, so the server
        # process CWD is a usable default scope hint. Explicit args win.
        body["cwd"] = os.getcwd()
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
    resp = await _post("/lint", timeout=120.0)
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

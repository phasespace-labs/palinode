import os
import httpx
from palinode.core.auth import load_api_token
from palinode.core.config import config
from palinode.core.defaults import SAVE_SOURCE_HEADER, SESSION_END_TIMEOUT_SECONDS, _SESSION_END_TIMEOUT_SENTINEL
from palinode.core.write_input import SAVE_PARAMS, SESSION_END_PARAMS, build_payload

# Cross-surface drift guard: all three entry points (CLI, MCP, hook) must
# use SESSION_END_TIMEOUT_SECONDS from defaults.  If the sentinel changes without
# this module being updated, this assertion fires at import time.
assert SESSION_END_TIMEOUT_SECONDS == _SESSION_END_TIMEOUT_SENTINEL or os.environ.get(
    "PALINODE_SESSION_END_TIMEOUT"
), (
    f"SESSION_END_TIMEOUT_SECONDS ({SESSION_END_TIMEOUT_SECONDS}) differs from sentinel "
    f"({_SESSION_END_TIMEOUT_SENTINEL}) without PALINODE_SESSION_END_TIMEOUT override — "
    "update cli/_api.py or defaults.py to stay in sync (#377)"
)

# Re-exported for CLI commands that need to catch API errors without
# importing httpx directly (ADR-010: HTTP-layer monopoly).
#
# Keep this list complete. `session_end.py` imported httpx directly for years
# solely because `ReadTimeout` was missing here — the monopoly held on the call
# path (it goes through `api_client`), but the *exception* surface had a hole,
# so the one command that needed to distinguish a timeout had nowhere to get
# the name. An adapter that hides the transport has to hide all of it.
#
# `ReadTimeout` is a `RequestError` subclass, so a handler that wants the
# specific message must catch it *before* `RequestError`.
HTTPStatusError = httpx.HTTPStatusError
RequestError = httpx.RequestError
ReadTimeout = httpx.ReadTimeout


def _client_headers() -> dict[str, str]:
    """Default headers for every CLI request to the API server.

    ADR-010: the source header is the surface attribution the API falls back
    to when a body doesn't set ``source``. The bearer is added whenever
    ``PALINODE_API_TOKEN`` / ``PALINODE_API_TOKEN_FILE`` resolves to a token —
    the same loader the API's ``BearerAuthMiddleware`` is configured from. The
    middleware has no loopback exemption, so without this every
    ``palinode <cmd>`` 401'd against a token-protected API.
    """
    headers = {SAVE_SOURCE_HEADER: "cli"}
    token = load_api_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class PalinodeAPI:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = os.environ.get(
            "PALINODE_API",
            f"http://{config.services.api.host}:{config.services.api.port}",
        )
        # A whole client may be injected, or just a transport (tests hand in a
        # transport that dispatches to an in-process ASGI app so the default
        # headers — source attribution, bearer — are the ones under test).
        self.client = client or httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers=_client_headers(),
            transport=transport,
        )

    def search(
        self,
        query: str,
        limit: int = 3,
        category: str | None = None,
        context: list[str] | None = None,
        threshold: float | None = None,
        since_days: int | None = None,
        types: list[str] | None = None,
        min_priority: int | None = None,
        date_after: str | None = None,
        date_before: str | None = None,
        include_daily: bool | None = None,
        include_telemetry: bool | None = None,
        tier: str | None = None,
    ):
        # ADR-010: forward the full canonical search surface.
        # Non-None params land in the body verbatim; None means "API default".
        payload: dict = {"query": query, "limit": limit}
        if tier:
            payload["tier"] = tier
        if category:
            payload["category"] = category
        if context:
            payload["context"] = context
        if threshold is not None:
            payload["threshold"] = threshold
        if since_days is not None:
            payload["since_days"] = since_days
        if types:
            payload["types"] = list(types)
        if min_priority is not None:
            payload["min_priority"] = min_priority
        if date_after:
            payload["date_after"] = date_after
        if date_before:
            payload["date_before"] = date_before
        if include_daily:
            payload["include_daily"] = True
        if include_telemetry:
            payload["include_telemetry"] = True

        response = self.client.post("/search", json=payload)
        response.raise_for_status()
        return response.json()

    def save(
        self,
        content: str,
        memory_type: str,
        entities: list[str] = None,
        title: str | None = None,
        source: str | None = None,
        sync: bool = False,
        project: str | None = None,
        slug: str | None = None,
        core: bool | None = None,
        confidence: float | None = None,
        priority: int | None = None,
        metadata: dict | None = None,
        external_refs: dict | None = None,
        update_policy: str | None = None,
        sources: list[dict] | None = None,
        claims: list[dict] | None = None,
        epistemic: str | None = None,
        contradicts: list[str] | None = None,
        backed_by: list[str] | None = None,
    ):
        # One inclusion rule, shared with MCP via core/write_input.py: a param
        # is sent when it is not None, so an explicitly-empty ``contradicts=[]``
        # reaches the server as the assertion the caller made rather than being
        # elided into "never specified". This used to read ``if contradicts:``
        # here and ``is not None`` in MCP — the same divergence the session_end
        # method below already carries a post-mortem for.
        payload: dict = {
            "content": content,
            "type": memory_type,
        }
        payload.update(
            build_payload(
                SAVE_PARAMS,
                {
                    "entities": entities,
                    "title": title,
                    # ADR-010: project is API-side sugar; the API expands it
                    # into entities.
                    "project": project,
                    "source": source,
                    "slug": slug,
                    "core": core,
                    "confidence": confidence,
                    "priority": priority,
                    "metadata": metadata,
                    "external_refs": external_refs,
                    "update_policy": update_policy,
                    "epistemic": epistemic,
                    "sources": sources,
                    "claims": claims,
                    "contradicts": contradicts,
                    "backed_by": backed_by,
                },
            )
        )
        params = {"sync": "true"} if sync else None
        response = self.client.post("/save", json=payload, params=params)
        response.raise_for_status()
        return response.json()

    def get_status(self):
        response = self.client.get("/status")
        response.raise_for_status()
        return response.json()

    def read(self, file_path: str, meta: bool = False, tier: str | None = None):
        """Read a memory file via the API.

        Returns ``{file, content, size_bytes, [frontmatter]}``.  When
        ``meta=True``, ``frontmatter`` is a parsed dict.  ADR-010.
        """
        params: dict = {"file_path": file_path}
        if meta:
            params["meta"] = "true"
        if tier:
            params["tier"] = tier
        response = self.client.get("/read", params=params)
        response.raise_for_status()
        return response.json()

    def list_files(self, category: str | None = None, core_only: bool | None = None):
        """List memory files via the API.  ADR-010."""
        params: dict = {}
        if category:
            params["category"] = category
        if core_only:
            params["core_only"] = "true"
        response = self.client.get("/list", params=params)
        response.raise_for_status()
        return response.json()

    def lint(self):
        """Run the memory lint pass via the API.  ADR-010.

        Raises ``RequestError`` if the API is unreachable; the CLI catches
        this to fall back to a local in-process lint pass.
        """
        response = self.client.post("/lint", timeout=30.0)
        response.raise_for_status()
        return response.json()

    def review(self, project: str | None = None):
        """Run the advisory project-memory review via the API.

        Raises ``RequestError`` if the API is unreachable; the CLI catches
        this to fall back to a local in-process review pass.
        """
        body: dict = {}
        if project:
            body["project"] = project
        response = self.client.post("/review", json=body, timeout=30.0)
        response.raise_for_status()
        return response.json()

    def list_prompts(self, task: str | None = None):
        """List stored prompt versions.  ADR-010."""
        params: dict = {}
        if task:
            params["task"] = task
        response = self.client.get("/prompts", params=params)
        response.raise_for_status()
        return response.json()

    def get_prompt(self, name: str):
        """Read a specific prompt by name.  ADR-010."""
        response = self.client.get(f"/prompts/{name}")
        response.raise_for_status()
        return response.json()

    def activate_prompt(self, name: str):
        """Activate a prompt version.  ADR-010."""
        response = self.client.post(f"/prompts/{name}/activate")
        response.raise_for_status()
        return response.json()

    def ingest_inbox(self):
        """Process files in the inbox directory.  ADR-010."""
        response = self.client.post("/ingest", timeout=60.0)
        response.raise_for_status()
        return response.json()

    def ingest_url(self, url: str, name: str | None = None):
        """Fetch and save a URL as a research reference.  ADR-010."""
        payload: dict = {"url": url}
        if name:
            payload["name"] = name
        response = self.client.post("/ingest-url", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()

    def session_end(
        self,
        summary: str,
        decisions: list[str] | None = None,
        blockers: list[str] | None = None,
        project: str | None = None,
        source: str | None = None,
        harness: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        trigger: str | None = None,
        session_id: str | None = None,
        duration_seconds: int | None = None,
        push: bool | None = None,
        dry_run: bool = False,
    ):
        """Capture session outcomes via the API. ADR-010 (the project-slug derivation work
fields, the session-end hook audit push)."""
        # `is not None`, not truthiness. An empty list means "considered, none
        # to report"; eliding it makes the server read a parameter the caller
        # did send as one that never arrived, which is the signature its
        # envelope guard treats as a corrupted call. This method is where that
        # was first worked out; core/write_input.py is where it now lives, so
        # `save` above and MCP get it too instead of re-deriving it per-param.
        payload: dict = {"summary": summary}
        payload.update(
            build_payload(
                SESSION_END_PARAMS,
                {
                    "decisions": None if decisions is None else list(decisions),
                    "blockers": None if blockers is None else list(blockers),
                    "project": project,
                    "source": source,
                    "harness": harness,
                    "cwd": cwd,
                    "model": model,
                    "trigger": trigger,
                    "session_id": session_id,
                    "duration_seconds": duration_seconds,
                    "push": push,
                    "dry_run": dry_run,
                },
            )
        )
        response = self.client.post("/session-end", json=payload, timeout=SESSION_END_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    def get_diff(self, days: int = 7, paths: str = None):
        params: dict = {"days": days}
        if paths:
            params["paths"] = paths

        response = self.client.get("/diff", params=params)
        response.raise_for_status()
        return response.json()

    def consolidate(
        self,
        dry_run: bool = False,
        nightly: bool = False,
        sources: list[str] | None = None,
    ):
        body: dict = {"dry_run": dry_run, "nightly": nightly}
        # Omitted rather than sent as null so the server's default stays the
        # single definition of "which corpus".
        if sources:
            body["sources"] = list(sources)
        response = self.client.post("/consolidate", json=body)
        response.raise_for_status()
        return response.json()

    def archive(
        self,
        file_path: str,
        reason: str | None = None,
        superseded_by: str | None = None,
    ):
        payload: dict = {"file_path": file_path}
        if reason is not None:
            payload["reason"] = reason
        if superseded_by is not None:
            payload["superseded_by"] = superseded_by
        response = self.client.post("/archive", json=payload)
        response.raise_for_status()
        return response.json()

    def archive_expired(self, dry_run: bool = False):
        response = self.client.post("/archive-expired", json={"dry_run": dry_run})
        response.raise_for_status()
        return response.json()

    def trigger_add(
        self,
        description: str,
        memory_file: str,
        threshold: float | None = None,
        cooldown_hours: int | None = None,
        trigger_id: str | None = None,
    ):
        # ADR-010: forward all four canonical params. Defaults live
        # in palinode.core.defaults so the CLI can show them in --help.  We
        # only include them in the body when non-None so the API still
        # receives explicit user intent vs implicit defaults.
        payload: dict = {
            "description": description,
            "memory_file": memory_file,
        }
        if threshold is not None:
            payload["threshold"] = threshold
        if cooldown_hours is not None:
            payload["cooldown_hours"] = cooldown_hours
        if trigger_id is not None:
            payload["trigger_id"] = trigger_id
        response = self.client.post("/triggers", json=payload)
        response.raise_for_status()
        return response.json()

    def trigger_list(self):
        response = self.client.get("/triggers")
        response.raise_for_status()
        return response.json()

    def trigger_remove(self, trigger_id: str):
        response = self.client.delete(f"/triggers/{trigger_id}")
        response.raise_for_status()
        return response.json()

    def reindex(self):
        response = self.client.post("/reindex", timeout=600.0)
        response.raise_for_status()
        return response.json()

    def rebuild_fts(self):
        response = self.client.post("/rebuild-fts", timeout=60.0)
        response.raise_for_status()
        return response.json()

    def split_layers(self):
        response = self.client.post("/split-layers", timeout=120.0)
        response.raise_for_status()
        return response.json()

    def bootstrap_ids(self):
        response = self.client.post("/bootstrap-fact-ids", timeout=120.0)
        response.raise_for_status()
        return response.json()

    def get_history(self, file_path: str, limit: int = 20, detail: str = "summary"):
        params: dict = {"limit": limit, "detail": detail}
        response = self.client.get(f"/history/{file_path}", params=params, timeout=10.0)
        response.raise_for_status()
        return response.json()

    def get_entities(self, entity: str = None):
        if entity:
            response = self.client.get(f"/entities/{entity}", timeout=10.0)
        else:
            response = self.client.get("/entities", timeout=10.0)
        response.raise_for_status()
        return response.json()

    def context_prime(self, cwd: str = None, project: str = None):
        payload: dict = {}
        if cwd:
            payload["cwd"] = cwd
        if project:
            payload["project"] = project
        response = self.client.post("/context/prime", json=payload, timeout=15.0)
        response.raise_for_status()
        return response.json()

    def blame(self, file_path: str, search: str = None, claims: bool = False):
        params: dict = {}
        if search:
            params["search"] = search
        if claims:
            params["claims"] = "true"
        response = self.client.get(f"/blame/{file_path}", params=params, timeout=10.0)
        response.raise_for_status()
        return response.json()

    def trace(self, file_path: str):
        response = self.client.get(f"/trace/{file_path}", timeout=15.0)
        response.raise_for_status()
        return response.json()

    def rollback(self, file_path: str, commit: str = None, dry_run: bool = True):
        params: dict = {"file_path": file_path, "dry_run": dry_run}
        if commit:
            params["commit"] = commit
        response = self.client.post("/rollback", params=params, timeout=30.0)
        response.raise_for_status()
        return response.json()

    def push(self):
        response = self.client.post("/push", timeout=60.0)
        response.raise_for_status()
        return response.json()

    def dedup_suggest(
        self,
        content: str,
        min_similarity: float | None = None,
        top_k: int | None = None,
    ):
        """Find existing files semantically near draft content.

        Defaults applied server-side.  Returns the same shape as
        ``POST /dedup-suggest``: a list of ``{file_path, similarity, snippet,
        strong_dup}`` dicts ranked by descending similarity.
        """
        payload: dict = {"content": content}
        if min_similarity is not None:
            payload["min_similarity"] = min_similarity
        if top_k is not None:
            payload["top_k"] = top_k
        response = self.client.post("/dedup-suggest", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()

    def orphan_repair(
        self,
        broken_link: str,
        min_similarity: float | None = None,
        top_k: int | None = None,
    ):
        """Find files semantically near a broken `[[wikilink]]` target."""
        payload: dict = {"broken_link": broken_link}
        if min_similarity is not None:
            payload["min_similarity"] = min_similarity
        if top_k is not None:
            payload["top_k"] = top_k
        response = self.client.post("/orphan-repair", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()

    def cluster_neighbors(
        self,
        file_path: str,
        min_similarity: float | None = None,
        top_k: int | None = None,
    ):
        """Find semantically related files not already linked to/from file_path."""
        payload: dict = {"file_path": file_path}
        if min_similarity is not None:
            payload["min_similarity"] = min_similarity
        if top_k is not None:
            payload["top_k"] = top_k
        response = self.client.post("/cluster-neighbors", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()

    def topic_coverage(
        self,
        query: str,
        min_similarity: float | None = None,
    ):
        """Check whether any wiki page already covers a topic phrase."""
        payload: dict = {"query": query}
        if min_similarity is not None:
            payload["min_similarity"] = min_similarity
        response = self.client.post("/topic-coverage", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()

    def depends(self, slug: str) -> dict:
        """Return the dependency neighbourhood for *slug*."""
        response = self.client.get(f"/depends/{slug}", timeout=10.0)
        response.raise_for_status()
        return response.json()

    def depends_unblocked(self) -> list:
        """Return all slugs whose every depends_on is done."""
        response = self.client.get("/depends/_unblocked", timeout=10.0)
        response.raise_for_status()
        return response.json()

api_client = PalinodeAPI()

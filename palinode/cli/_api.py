import os
import httpx
from palinode.core.config import config
from palinode.core.defaults import SAVE_SOURCE_HEADER

# Re-exported for CLI commands that need to catch API errors without
# importing httpx directly (ADR-010: HTTP-layer monopoly).
HTTPStatusError = httpx.HTTPStatusError
RequestError = httpx.RequestError


class PalinodeAPI:
    def __init__(self):
        self.base_url = os.environ.get(
            "PALINODE_API",
            f"http://{config.services.api.host}:{config.services.api.port}",
        )
        # ADR-010 / #167: every request carries the surface attribution as
        # a header.  The API uses this when the body doesn't explicitly set
        # `source`, giving consistent provenance without each surface having
        # to thread a source default through every call site.
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers={SAVE_SOURCE_HEADER: "cli"},
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
        date_after: str | None = None,
        date_before: str | None = None,
        include_daily: bool | None = None,
    ):
        # ADR-010 / #163: forward the full canonical search surface.
        # Non-None params land in the body verbatim; None means "API default".
        payload: dict = {"query": query, "limit": limit}
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
        if date_after:
            payload["date_after"] = date_after
        if date_before:
            payload["date_before"] = date_before
        if include_daily:
            payload["include_daily"] = True

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
        metadata: dict | None = None,
    ):
        payload: dict = {
            "content": content,
            "type": memory_type,
            "entities": entities or [],
        }
        # Only include optional fields when set so the server can apply its
        # own defaults vs. seeing an explicit ``None``.
        if title is not None:
            payload["title"] = title
        if source:
            payload["source"] = source
        if project:
            # ADR-010 / #159: project is API-side sugar; the API expands it
            # into entities.
            payload["project"] = project
        if slug is not None:
            payload["slug"] = slug
        if core is not None:
            payload["core"] = core
        if confidence is not None:
            payload["confidence"] = confidence
        if metadata is not None:
            payload["metadata"] = metadata
        params = {"sync": "true"} if sync else None
        response = self.client.post("/save", json=payload, params=params)
        response.raise_for_status()
        return response.json()

    def get_status(self):
        response = self.client.get("/status")
        response.raise_for_status()
        return response.json()

    def read(self, file_path: str, meta: bool = False):
        """Read a memory file via the API.

        Returns ``{file, content, size_bytes, [frontmatter]}``.  When
        ``meta=True``, ``frontmatter`` is a parsed dict.  ADR-010 / #168.
        """
        params: dict = {"file_path": file_path}
        if meta:
            params["meta"] = "true"
        response = self.client.get("/read", params=params)
        response.raise_for_status()
        return response.json()

    def list_files(self, category: str | None = None, core_only: bool | None = None):
        """List memory files via the API.  ADR-010 / #170."""
        params: dict = {}
        if category:
            params["category"] = category
        if core_only:
            params["core_only"] = "true"
        response = self.client.get("/list", params=params)
        response.raise_for_status()
        return response.json()

    def lint(self):
        """Run the memory lint pass via the API.  ADR-010 / #170.

        Raises ``RequestError`` if the API is unreachable; the CLI catches
        this to fall back to a local in-process lint pass.
        """
        response = self.client.post("/lint", timeout=30.0)
        response.raise_for_status()
        return response.json()

    def list_prompts(self, task: str | None = None):
        """List stored prompt versions.  ADR-010 / #170."""
        params: dict = {}
        if task:
            params["task"] = task
        response = self.client.get("/prompts", params=params)
        response.raise_for_status()
        return response.json()

    def get_prompt(self, name: str):
        """Read a specific prompt by name.  ADR-010 / #170."""
        response = self.client.get(f"/prompts/{name}")
        response.raise_for_status()
        return response.json()

    def activate_prompt(self, name: str):
        """Activate a prompt version.  ADR-010 / #170."""
        response = self.client.post(f"/prompts/{name}/activate")
        response.raise_for_status()
        return response.json()

    def ingest_inbox(self):
        """Process files in the inbox directory.  ADR-010 / #170."""
        response = self.client.post("/ingest", timeout=60.0)
        response.raise_for_status()
        return response.json()

    def ingest_url(self, url: str, name: str | None = None):
        """Fetch and save a URL as a research reference.  ADR-010 / #170."""
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
    ):
        """Capture session outcomes via the API.  ADR-010 / #170 (#145 fields)."""
        payload: dict = {"summary": summary}
        if decisions:
            payload["decisions"] = list(decisions)
        if blockers:
            payload["blockers"] = list(blockers)
        if project:
            payload["project"] = project
        if source:
            payload["source"] = source
        if harness:
            payload["harness"] = harness
        if cwd:
            payload["cwd"] = cwd
        if model:
            payload["model"] = model
        if trigger:
            payload["trigger"] = trigger
        if session_id:
            payload["session_id"] = session_id
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        response = self.client.post("/session-end", json=payload, timeout=30.0)
        response.raise_for_status()
        return response.json()

    def get_diff(self, days: int = 7, paths: str = None):
        params: dict = {"days": days}
        if paths:
            params["paths"] = paths

        response = self.client.get("/diff", params=params)
        response.raise_for_status()
        return response.json()

    def consolidate(self, dry_run: bool = False, nightly: bool = False):
        response = self.client.post("/consolidate", json={"dry_run": dry_run, "nightly": nightly})
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
        # ADR-010 / #165: forward all four canonical params.  Defaults live
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

    def get_history(self, file_path: str, limit: int = 20):
        response = self.client.get(f"/history/{file_path}", params={"limit": limit}, timeout=10.0)
        response.raise_for_status()
        return response.json()

    def get_entities(self, entity: str = None):
        if entity:
            response = self.client.get(f"/entities/{entity}", timeout=10.0)
        else:
            response = self.client.get("/entities", timeout=10.0)
        response.raise_for_status()
        return response.json()

    def migrate_openclaw(self, path: str, dry_run: bool = False):
        response = self.client.post(
            "/migrate/openclaw",
            json={"path": path, "dry_run": dry_run},
            timeout=120.0,
        )
        response.raise_for_status()
        return response.json()

    def migrate_mem0(self):
        response = self.client.post("/migrate/mem0", timeout=600.0)
        response.raise_for_status()
        return response.json()

    def blame(self, file_path: str, search: str = None):
        params: dict = {}
        if search:
            params["search"] = search
        response = self.client.get(f"/blame/{file_path}", params=params, timeout=10.0)
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
        """Find existing files semantically near draft content (#210).

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
        """Find files semantically near a broken `[[wikilink]]` target (#210)."""
        payload: dict = {"broken_link": broken_link}
        if min_similarity is not None:
            payload["min_similarity"] = min_similarity
        if top_k is not None:
            payload["top_k"] = top_k
        response = self.client.post("/orphan-repair", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()

    def health_check(self):
        try:
            response = self.client.get("/health")
            return response.status_code == 200, response.json()
        except Exception as e:
            return False, {"error": str(e)}

api_client = PalinodeAPI()

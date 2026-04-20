import os
import httpx
from palinode.core.config import config

class PalinodeAPI:
    def __init__(self):
        self.base_url = os.environ.get(
            "PALINODE_API", 
            f"http://{config.services.api.host}:{config.services.api.port}"
        )
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def search(self, query: str, limit: int = 3, category: str = None, context: list[str] = None):
        payload: dict = {"query": query, "limit": limit}
        if category:
            payload["category"] = category
        if context:
            payload["context"] = context

        response = self.client.post("/search", json=payload)
        response.raise_for_status()
        return response.json()

    def save(self, content: str, memory_type: str, entities: list[str] = None, title: str = None, source: str = None, sync: bool = False):
        payload = {
            "content": content,
            "type": memory_type,
            "entities": entities or [],
            "title": title
        }
        if source:
            payload["source"] = source
        params = {"sync": "true"} if sync else None
        response = self.client.post("/save", json=payload, params=params)
        response.raise_for_status()
        return response.json()

    def get_status(self):
        response = self.client.get("/status")
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

    def trigger_add(self, description: str, memory_file: str, threshold: float = 0.75):
        payload: dict = {
            "description": description,
            "memory_file": memory_file,
            "threshold": threshold,
        }
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

    def health_check(self):
        try:
            response = self.client.get("/health")
            return response.status_code == 200, response.json()
        except Exception as e:
            return False, {"error": str(e)}

api_client = PalinodeAPI()

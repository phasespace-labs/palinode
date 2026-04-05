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

    def search(self, query: str, top_k: int = 3, type_filter: str = None):
        payload = {"query": query, "top_k": top_k}
        if type_filter:
            payload["type"] = type_filter
        
        response = self.client.post("/search", json=payload)
        response.raise_for_status()
        return response.json()

    def save(self, content: str, memory_type: str, entities: list[str] = None, title: str = None, source: str = None):
        payload = {
            "content": content,
            "type": memory_type,
            "entities": entities or [],
            "title": title
        }
        if source:
            payload["source"] = source
        response = self.client.post("/save", json=payload)
        response.raise_for_status()
        return response.json()

    def get_status(self):
        response = self.client.get("/status")
        response.raise_for_status()
        return response.json()

    def get_diff(self, file_path: str = None, commits: int = None):
        params = {}
        if file_path:
            params["file"] = file_path
        if commits:
            params["commits"] = commits
            
        response = self.client.get("/diff", params=params)
        response.raise_for_status()
        return response.json()

    def consolidate(self, dry_run: bool = False):
        response = self.client.post("/consolidate", json={"dry_run": dry_run})
        response.raise_for_status()
        return response.json()

    def trigger_add(self, description: str, file_path: str, threshold: float = 0.4):
        payload = {
            "description": description,
            "file": file_path,
            "threshold": threshold
        }
        response = self.client.post("/trigger/add", json=payload)
        response.raise_for_status()
        return response.json()

    def trigger_list(self):
        response = self.client.get("/trigger/list")
        response.raise_for_status()
        return response.json()

    def trigger_remove(self, trigger_id: str):
        response = self.client.delete(f"/trigger/remove/{trigger_id}")
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

    def get_history(self, file_path: str):
        response = self.client.get(f"/history/{file_path}", timeout=10.0)
        response.raise_for_status()
        return response.json()

    def get_entities(self, entity: str = None):
        if entity:
            response = self.client.get(f"/entities/{entity}", timeout=10.0)
        else:
            response = self.client.get("/entities", timeout=10.0)
        response.raise_for_status()
        return response.json()

    def migrate_mem0(self):
        response = self.client.post("/migrate/mem0", timeout=600.0)
        response.raise_for_status()
        return response.json()

    def blame(self, file_path: str, search: str = None):
        # We need git_tools for these if they are not exposed by API yet
        # But for now, let's assume they are or we handle them
        from palinode.core import git_tools
        return git_tools.blame(file_path, search)

    def timeline(self, file_path: str, limit: int = 20):
        from palinode.core import git_tools
        return git_tools.timeline(file_path, limit)

    def rollback(self, file_path: str, commit: str, dry_run: bool = True):
        from palinode.core import git_tools
        return git_tools.rollback(file_path, commit, dry_run=dry_run)

    def push(self):
        from palinode.core import git_tools
        return git_tools.push()

    def health_check(self):
        try:
            response = self.client.get("/health")
            return response.status_code == 200, response.json()
        except Exception as e:
            return False, {"error": str(e)}

api_client = PalinodeAPI()

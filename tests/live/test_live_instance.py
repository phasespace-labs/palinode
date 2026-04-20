"""
Live Instance Test Suite for Palinode

Tests a running Palinode API server with real Ollama embeddings and real
memory files. NOT part of CI — run manually to validate a deployment.

Usage:
    # Against localhost (default)
    pytest tests/live/test_live_instance.py -v

    # Against a remote server
    PALINODE_TEST_HOST=http://palinode.example.com:6340 pytest tests/live/test_live_instance.py -v

    # Skip slow tests (embedding/search)
    pytest tests/live/test_live_instance.py -v -k "not slow"

Requires: a running palinode-api with Ollama reachable.
"""
import os
import time
import json
import hashlib
import pytest
import httpx

BASE_URL = os.environ.get("PALINODE_TEST_HOST", "http://localhost:6340")
client = httpx.Client(base_url=BASE_URL, timeout=60.0)

# ── Unique test slug to avoid polluting real memory ──────────────────────────
_TEST_PREFIX = f"livetest-{int(time.time())}"


def _cleanup_test_files():
    """Remove any test files we created."""
    try:
        resp = client.get("/list")
        if resp.status_code == 200:
            for f in resp.json():
                if _TEST_PREFIX in f.get("file", ""):
                    # No delete endpoint — files stay. They're small and harmless.
                    pass
    except Exception:
        pass


# ── Health & Connectivity ────────────────────────────────────────────────────

class TestHealth:
    """Basic server health — run these first."""

    def test_api_reachable(self):
        resp = client.get("/status")
        assert resp.status_code == 200

    def test_status_fields(self):
        data = client.get("/status").json()
        assert "total_files" in data
        assert "total_chunks" in data
        assert "hybrid_search" in data
        assert "ollama_reachable" in data

    def test_ollama_reachable(self):
        data = client.get("/status").json()
        assert data["ollama_reachable"] is True, "Ollama not reachable — search and save won't work"

    def test_health_endpoint(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") in ("healthy", "ok", True)

    def test_chunks_indexed(self):
        data = client.get("/status").json()
        assert data["total_chunks"] > 0, "No chunks indexed — run palinode reindex first"

    def test_fts_in_sync(self):
        data = client.get("/status").json()
        assert data.get("fts_chunks", 0) > 0, "FTS index empty — run palinode rebuild-fts"


# ── Save & Retrieve ─────────────────────────────────────────────────────────

class TestSaveRetrieve:
    """Save a memory and read it back."""

    def test_save_creates_file(self):
        slug = f"{_TEST_PREFIX}-save"
        resp = client.post("/save", json={
            "content": "Live test: verifying save creates a file.",
            "type": "Insight",
            "slug": slug,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "file_path" in data
        assert slug in data["file_path"]

    def test_save_includes_content_hash(self):
        slug = f"{_TEST_PREFIX}-hash"
        resp = client.post("/save", json={
            "content": "Live test: content hash verification.",
            "type": "Insight",
            "slug": slug,
        })
        data = resp.json()
        file_path = data["file_path"]

        # Read back and check frontmatter has content_hash
        # Extract relative path from absolute
        rel_path = file_path.split("/")[-2] + "/" + file_path.split("/")[-1]
        read_resp = client.get("/read", params={"file_path": rel_path, "meta": True})
        assert read_resp.status_code == 200
        read_data = read_resp.json()
        assert "content_hash" in read_data.get("frontmatter", {}), "content_hash missing from frontmatter"

    def test_save_with_confidence(self):
        slug = f"{_TEST_PREFIX}-conf"
        resp = client.post("/save", json={
            "content": "Live test: confidence field.",
            "type": "Decision",
            "slug": slug,
            "confidence": 0.85,
        })
        data = resp.json()
        rel_path = data["file_path"].split("/")[-2] + "/" + data["file_path"].split("/")[-1]
        read_resp = client.get("/read", params={"file_path": rel_path, "meta": True})
        fm = read_resp.json().get("frontmatter", {})
        assert fm.get("confidence") == 0.85

    def test_read_file(self):
        slug = f"{_TEST_PREFIX}-read"
        save_resp = client.post("/save", json={
            "content": "Live test: read back verification.",
            "type": "Insight",
            "slug": slug,
        })
        file_path = save_resp.json()["file_path"]
        rel_path = file_path.split("/")[-2] + "/" + file_path.split("/")[-1]

        read_resp = client.get("/read", params={"file_path": rel_path})
        assert read_resp.status_code == 200
        assert "read back verification" in read_resp.json()["content"]

    def test_list_includes_saved_file(self):
        slug = f"{_TEST_PREFIX}-list"
        client.post("/save", json={
            "content": "Live test: list verification.",
            "type": "Insight",
            "slug": slug,
        })
        resp = client.get("/list")
        assert resp.status_code == 200
        files = [f["file"] for f in resp.json()]
        assert any(slug in f for f in files), f"Saved file {slug} not in list"


# ── Search (requires Ollama) ────────────────────────────────────────────────

class TestSearch:
    """Search tests — these need Ollama for embeddings."""

    @pytest.mark.slow
    def test_search_returns_results(self):
        """Search for something that should exist in any palinode instance."""
        resp = client.post("/search", json={"query": "palinode memory", "limit": 3})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) > 0, "Search returned no results"

    @pytest.mark.slow
    def test_search_has_score_fields(self):
        resp = client.post("/search", json={"query": "architecture decision", "limit": 1})
        results = resp.json()
        if results:
            r = results[0]
            assert "score" in r
            assert "raw_score" in r
            assert "freshness" in r

    @pytest.mark.slow
    def test_search_freshness_not_unknown(self):
        """After reindex, freshness should be valid or stale, not unknown."""
        resp = client.post("/search", json={"query": "project status", "limit": 5})
        results = resp.json()
        for r in results:
            assert r.get("freshness") != "unknown", (
                f"Freshness is 'unknown' for {r['file_path']} — "
                f"content_hash may be missing from results"
            )

    @pytest.mark.slow
    def test_search_daily_penalty(self):
        """daily/ files should score lower than equivalent non-daily files."""
        resp = client.post("/search", json={"query": "session summary", "limit": 10})
        results = resp.json()
        daily = [r for r in results if "/daily/" in r["file_path"]]
        non_daily = [r for r in results if "/daily/" not in r["file_path"]]
        if daily and non_daily:
            # Daily files should generally score lower
            avg_daily = sum(r["score"] for r in daily) / len(daily)
            avg_other = sum(r["score"] for r in non_daily) / len(non_daily)
            assert avg_daily < avg_other, (
                f"Daily avg ({avg_daily:.3f}) >= non-daily avg ({avg_other:.3f}) — "
                f"daily penalty may not be working"
            )

    @pytest.mark.slow
    def test_save_then_search(self):
        """Save a unique memory, wait for indexing, search for it."""
        unique = f"xyzzy-{_TEST_PREFIX}-unique-marker"
        client.post("/save", json={
            "content": f"Live test: {unique}. This is a unique searchable phrase.",
            "type": "Insight",
            "slug": f"{_TEST_PREFIX}-searchable",
        })
        # Poll for up to 30s — watcher needs to detect file, call Ollama
        # for embedding, and upsert into the index
        found = False
        for attempt in range(6):
            time.sleep(5)
            resp = client.post("/search", json={"query": unique, "limit": 3})
            results = resp.json()
            if any(unique in r.get("content", "") for r in results):
                found = True
                break
        assert found, (
            f"Saved memory with '{unique}' not found in search after 30s — "
            f"watcher may not be running or Ollama unreachable"
        )


# ── Security ────────────────────────────────────────────────────────────────

class TestSecurity:
    """Security checks against the live instance."""

    def test_path_traversal_blocked(self):
        resp = client.get("/read", params={"file_path": "../../../etc/passwd"})
        assert resp.status_code in (400, 403, 404), f"Path traversal not blocked: {resp.status_code}"

    def test_null_byte_blocked(self):
        resp = client.get("/read", params={"file_path": "test\x00.md"})
        assert resp.status_code in (400, 403, 404, 422)

    def test_oversized_request(self):
        huge = "x" * (6 * 1024 * 1024)  # 6MB
        resp = client.post("/save", json={"content": huge, "type": "Insight"})
        assert resp.status_code in (413, 422), f"Oversized request not rejected: {resp.status_code}"

    def test_error_no_stacktrace(self):
        """Force an error and verify no Python traceback in response."""
        resp = client.get("/read", params={"file_path": "nonexistent/fakefile.md"})
        if resp.status_code >= 400:
            body = resp.text
            assert "Traceback" not in body, "Stack trace leaked in error response"
            assert "File \"" not in body, "File path leaked in error response"


# ── Git Tools ────────────────────────────────────────────────────────────────

class TestGitTools:
    """Git-backed provenance tools."""

    def test_diff_returns(self):
        resp = client.get("/diff", params={"days": 7})
        assert resp.status_code == 200

    def test_lint_returns(self):
        resp = client.post("/lint")
        assert resp.status_code == 200


# ── MCP Audit Log ───────────────────────────────────────────────────────────

class TestAuditLog:
    """Verify MCP audit logging is working (requires MCP server)."""

    def test_status_check_generates_no_crash(self):
        """At minimum, hitting the API shouldn't crash the audit logger."""
        resp = client.get("/status")
        assert resp.status_code == 200


# ── Rate Limiting ───────────────────────────────────────────────────────────

class TestRateLimiting:
    """Verify rate limits are enforced."""

    @pytest.mark.slow
    def test_search_rate_limit(self):
        """Exceed search rate limit (default 100/min)."""
        blocked = False
        for i in range(105):
            resp = client.post("/search", json={"query": "rate limit test", "limit": 1})
            if resp.status_code == 429:
                blocked = True
                break
        assert blocked, "Rate limit not triggered after 105 search requests"

    @pytest.mark.slow
    def test_save_rate_limit(self):
        """Exceed save rate limit (default 30/min)."""
        blocked = False
        for i in range(35):
            resp = client.post("/save", json={
                "content": f"Rate limit test {i}",
                "type": "Insight",
                "slug": f"{_TEST_PREFIX}-rate-{i}",
            })
            if resp.status_code == 429:
                blocked = True
                break
        assert blocked, "Rate limit not triggered after 35 save requests"

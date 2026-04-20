"""
Palinode — Persistent memory for AI agents.

Files as truth. Vectors as search. Prompts as source code.

Components:
    core/       — Config, embeddings, storage, markdown parsing
    api/        — FastAPI HTTP server
    indexer/    — File watcher daemon (auto-indexes on save)
    ingest/     — Ingestion pipeline (PDF, audio, URL, text)
    mcp.py      — MCP server for Claude Code integration
    cli.py      — Command-line interface
"""
__version__ = "0.7.0"

"""
Palinode Command Line Interface

Entrypoint for CLI usage interacting with the currently running local API server payloads. 
Enables `search`, `stats`, and `reindex` invocation.
"""
from __future__ import annotations

import argparse
import httpx
import json

from palinode.core.config import config


def main() -> None:
    """Invokes system arguments capturing logic schemas mapping interface arrays variables mapping endpoints layouts."""
    parser = argparse.ArgumentParser(description="Palinode native CLI interface")
    subparsers = parser.add_subparsers(dest="command")

    search_p = subparsers.add_parser("search", help="Execute semantic search query block formats.")
    search_p.add_argument("query", help="Text payload explicitly mapping concepts queries contexts.")
    search_p.add_argument("--category", default=None, help="Specific constraint logic mapped tags logic strings formats (optional).")
    search_p.add_argument("--date-after", default=None, help="Filter results after an ISO date (e.g. 2024-01-01).")
    search_p.add_argument("--date-before", default=None, help="Filter results before an ISO date.")
    
    subparsers.add_parser("stats", help="Show system database indexing sizes.")
    
    subparsers.add_parser("reindex", help="Explicitly trigger absolute database rescans sequences.")
    
    subparsers.add_parser("rebuild-fts", help="Rebuild the BM25 full-text search index")

    subparsers.add_parser("consolidate", help="Run a manual weekly knowledge consolidation pass.")

    subparsers.add_parser("split-layers", help="Split core files into layers.")
    subparsers.add_parser("bootstrap-ids", help="Bootstrap fact IDs.")

    history_p = subparsers.add_parser("history", help="Show the git history of a specific memory file.")
    history_p.add_argument("file", help="File path relative to the Palinode memory directory.")

    entities_p = subparsers.add_parser("entities", help="List entities or get files associated with an entity.")
    entities_p.add_argument("entity", nargs="?", default=None, help="Optional entity reference string.")

    migrate_p = subparsers.add_parser("migrate-mem0", help="Backfill from Mem0/Qdrant")

    diff_p = subparsers.add_parser("diff", help="Show memory changes")
    diff_p.add_argument("--days", type=int, default=7, help="Look back N days")

    blame_p = subparsers.add_parser("blame", help="Show when lines were changed")
    blame_p.add_argument("file", help="Memory file path")
    blame_p.add_argument("--search", help="Filter to matching lines")

    timeline_p = subparsers.add_parser("timeline", help="Show file evolution")
    timeline_p.add_argument("file", help="Memory file path")
    timeline_p.add_argument("--limit", type=int, default=20)

    rollback_p = subparsers.add_parser("rollback", help="Revert a file")
    rollback_p.add_argument("file", help="Memory file path")
    rollback_p.add_argument("--commit", help="Target commit hash")
    rollback_p.add_argument("--execute", action="store_true", help="Actually apply (default: dry run)")

    push_p = subparsers.add_parser("push", help="Sync to GitHub")

    args = parser.parse_args()

    # Utilize typed config dynamically mapping ports arrays
    api_url = f"http://{config.services.api.host}:{config.services.api.port}"
    if config.services.api.host == "0.0.0.0":
        # Always resolve 0.0.0.0 generically mapping loopback footprints locally.
        api_url = f"http://localhost:{config.services.api.port}"

    try:
        if args.command == "search":
            payload = {"query": args.query, "category": args.category}
            if hasattr(args, 'date_after') and args.date_after:
                payload["date_after"] = args.date_after
            if hasattr(args, 'date_before') and args.date_before:
                payload["date_before"] = args.date_before

            res = httpx.post(
                f"{api_url}/search", 
                json=payload,
                timeout=30.0
            )
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))
            
        elif args.command == "stats":
            res = httpx.get(f"{api_url}/status", timeout=10.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))
            
        elif args.command == "reindex":
            # API dynamically scans formats pushing DB updates
            res = httpx.post(f"{api_url}/reindex", timeout=600.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))
            
        elif args.command == "rebuild-fts":
            res = httpx.post(f"{api_url}/rebuild-fts", timeout=60.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))
            
        elif args.command == "consolidate":
            res = httpx.post(f"{api_url}/consolidate", timeout=3600.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))

        elif args.command == "split-layers":
            res = httpx.post(f"{api_url}/split-layers", timeout=120.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))

        elif args.command == "bootstrap-ids":
            res = httpx.post(f"{api_url}/bootstrap-fact-ids", timeout=120.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))

        elif args.command == "history":
            res = httpx.get(f"{api_url}/history/{args.file}", timeout=10.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))

        elif args.command == "entities":
            if args.entity:
                res = httpx.get(f"{api_url}/entities/{args.entity}", timeout=10.0)
            else:
                res = httpx.get(f"{api_url}/entities", timeout=10.0)
            res.raise_for_status()
            print(json.dumps(res.json(), indent=2))
            
        elif args.command == "migrate-mem0":
            res = httpx.post(f"{api_url}/migrate/mem0", timeout=600.0)
            print(json.dumps(res.json(), indent=2))
            
        elif args.command == "diff":
            from palinode.core import git_tools
            print(git_tools.diff(args.days))

        elif args.command == "blame":
            from palinode.core import git_tools
            print(git_tools.blame(args.file, args.search))

        elif args.command == "timeline":
            from palinode.core import git_tools
            print(git_tools.timeline(args.file, args.limit))

        elif args.command == "rollback":
            from palinode.core import git_tools
            print(git_tools.rollback(args.file, args.commit, dry_run=not args.execute))

        elif args.command == "push":
            from palinode.core import git_tools
            print(git_tools.push())
            
        else:
            parser.print_help()
            
    except Exception as e:
        print(f"API call failed: {e}. Is the Palinode API server running?")


if __name__ == "__main__":
    main()

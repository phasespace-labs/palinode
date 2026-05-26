"""Regression guards for #298 + #299 — keep code and docs in sync.

#298: PALINODE_MAX_REQUEST_BYTES default must stay 5 MB. If anyone bumps
the constant, this test fails and the README/OPERATIONS docs need updating.

#299: SaveRequest schema is {content, type, slug, ...}. Not {category, body, ...}.
Pin the field names so any rename is caught here AND in the docs.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_max_request_bytes_default_is_5mb():
    """If anyone changes the default, README + OPERATIONS need updating too."""
    from palinode.api import server

    assert server._MAX_REQUEST_BYTES == 5 * 1024 * 1024, (
        "PALINODE_MAX_REQUEST_BYTES default changed from 5MB. Update "
        "README.md (API reference table) and docs/OPERATIONS.md (env-var "
        "table) to match. See #298."
    )


def test_max_request_bytes_documented_in_readme():
    """README API reference must mention the 5MB cap + env-var override."""
    readme = (REPO_ROOT / "README.md").read_text()
    assert "PALINODE_MAX_REQUEST_BYTES" in readme, (
        "README.md must mention the PALINODE_MAX_REQUEST_BYTES env var so "
        "users hitting 413 can find the override. See #298."
    )
    assert "5 MB" in readme or "5MB" in readme, (
        "README.md must state the 5MB default to keep code and docs aligned."
    )


def test_max_request_bytes_documented_in_operations():
    """OPERATIONS.md env-var table must list MAX_REQUEST_BYTES with the value."""
    ops = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text()
    assert "PALINODE_MAX_REQUEST_BYTES" in ops
    assert "5242880" in ops or "5 MB" in ops or "5MB" in ops


def test_save_request_schema_uses_content_not_body():
    """SaveRequest must use `content` (the body) and `type` — not `body` /
    `category`. Pins the schema so a careless rename can't silently break
    every API consumer. See #299.
    """
    from palinode.api.server import SaveRequest

    fields = set(SaveRequest.model_fields.keys())
    assert "content" in fields, "SaveRequest must accept `content` (#299)"
    assert "type" in fields, "SaveRequest must accept `type` (#299)"
    assert "slug" in fields, "SaveRequest must accept `slug` (#299)"
    # Negative guards — these are the wrong names from the issue.
    assert "body" not in fields, (
        "SaveRequest grew a `body` field — that's the legacy/wrong shape. "
        "The canonical body field is `content`. See #299."
    )
    assert "category" not in fields, (
        "SaveRequest grew a `category` field — category is derived from "
        "`type`, not a separate input. See #299."
    )


def test_save_endpoint_docstring_documents_schema():
    """The /save endpoint docstring is what FastAPI's auto-generated /docs
    UI renders. It must show the canonical {content, type, slug} schema
    so new integrators don't guess from the legacy {category, body, slug}
    shape that caused #299.
    """
    from palinode.api.server import save_api

    doc = save_api.__doc__ or ""
    # Must show all three canonical field names.
    assert "content" in doc
    assert "type" in doc
    assert "slug" in doc
    # Must call out the size limit so users can self-debug a 413.
    assert "PALINODE_MAX_REQUEST_BYTES" in doc

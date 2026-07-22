"""ADR-009 Layer 1 slice 3 — scoped memory selection (#107).

Covers the pure visibility predicate (``chain_allows``) and the shared
listing helper (``collect_memory_files``) behind GET /list and the upcoming
/context/prime endpoint. The load-bearing property: only an **explicit**
``scope:`` frontmatter field isolates; unscoped files behave exactly as in
classic mode (ADR-009 §7 — "no scope = works as before").
"""
import os

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.api.routers.memory import collect_memory_files
from palinode.core.config import config
from palinode.core.scope import ScopeChain, chain_allows

client = TestClient(app)


# ── chain_allows: pure predicate ──────────────────────────────────────────


CHAIN = ScopeChain(harness="claude-code", project="palinode", member="alice")


def test_explicit_scope_on_chain_is_allowed():
    assert chain_allows(CHAIN, {"scope": "project/palinode"})
    assert chain_allows(CHAIN, {"scope": "harness/claude-code"})
    assert chain_allows(CHAIN, {"scope": "member/alice"})


def test_explicit_scope_off_chain_is_hidden():
    assert not chain_allows(CHAIN, {"scope": "project/other"})
    assert not chain_allows(CHAIN, {"scope": "org/phasespace"})  # org unset on chain


def test_unscoped_memory_is_always_allowed():
    assert chain_allows(CHAIN, {})
    assert chain_allows(CHAIN, {"name": "x", "core": True})
    # Empty chain still admits unscoped files — classic behavior.
    assert chain_allows(ScopeChain(), {})


def test_explicit_scope_with_empty_chain_is_hidden():
    assert not chain_allows(ScopeChain(), {"scope": "project/palinode"})


def test_blank_or_nonstring_scope_treated_as_unscoped():
    assert chain_allows(CHAIN, {"scope": ""})
    assert chain_allows(CHAIN, {"scope": "   "})
    assert chain_allows(CHAIN, {"scope": ["project/palinode"]})
    assert chain_allows(CHAIN, {"scope": None})


def test_scope_value_is_stripped_before_matching():
    assert chain_allows(CHAIN, {"scope": "  project/palinode  "})


# ── collect_memory_files: shared listing path ─────────────────────────────


@pytest.fixture
def scoped_memory_dir(tmp_path):
    old_memory_dir = config.memory_dir
    config.memory_dir = str(tmp_path)

    os.makedirs(os.path.join(tmp_path, "decisions"))
    os.makedirs(os.path.join(tmp_path, "insights"))

    # Legacy file: no scope frontmatter at all.
    with open(os.path.join(tmp_path, "decisions", "legacy.md"), "w") as f:
        f.write("---\nname: Legacy\ncore: true\nsummary: unscoped\n---\nbody")

    # Explicitly project-scoped — on the test chain.
    with open(os.path.join(tmp_path, "decisions", "ours.md"), "w") as f:
        f.write(
            "---\nname: Ours\ncore: true\nscope: project/palinode\n---\nbody"
        )

    # Explicitly scoped to a different project — off the test chain.
    with open(os.path.join(tmp_path, "decisions", "theirs.md"), "w") as f:
        f.write(
            "---\nname: Theirs\ncore: true\nscope: project/other\n---\nbody"
        )

    # Harness-scoped, non-core.
    with open(os.path.join(tmp_path, "insights", "prefs.md"), "w") as f:
        f.write(
            "---\nname: Prefs\ncore: false\nscope: harness/claude-code\n---\nbody"
        )

    yield str(tmp_path)
    config.memory_dir = old_memory_dir


def test_no_chain_returns_everything(scoped_memory_dir):
    names = {r["name"] for r in collect_memory_files()}
    assert names == {"Legacy", "Ours", "Theirs", "Prefs"}


def test_chain_filters_off_chain_scopes(scoped_memory_dir):
    rows = collect_memory_files(scope_chain=CHAIN)
    names = {r["name"] for r in rows}
    assert names == {"Legacy", "Ours", "Prefs"}


def test_chain_composes_with_core_only(scoped_memory_dir):
    rows = collect_memory_files(core_only=True, scope_chain=CHAIN)
    names = {r["name"] for r in rows}
    assert names == {"Legacy", "Ours"}  # Prefs is on-chain but not core


def test_empty_chain_hides_all_explicitly_scoped(scoped_memory_dir):
    rows = collect_memory_files(scope_chain=ScopeChain())
    names = {r["name"] for r in rows}
    assert names == {"Legacy"}


def test_rows_carry_explicit_scope_only(scoped_memory_dir):
    by_name = {r["name"]: r for r in collect_memory_files()}
    assert by_name["Ours"]["scope"] == "project/palinode"
    assert by_name["Prefs"]["scope"] == "harness/claude-code"
    # Unscoped file reports None — the directory-inferred default
    # (project/<category>) is deliberately not surfaced here.
    assert by_name["Legacy"]["scope"] is None


# ── GET /list: HTTP contract unchanged, rows gain "scope" ─────────────────


def test_list_endpoint_is_unfiltered_and_carries_scope(scoped_memory_dir):
    res = client.get("/list")
    assert res.status_code == 200
    data = res.json()
    assert {d["name"] for d in data} == {"Legacy", "Ours", "Theirs", "Prefs"}
    assert all("scope" in d for d in data)


def test_list_core_only_still_classic(scoped_memory_dir):
    res = client.get("/list?core_only=true")
    assert res.status_code == 200
    # Off-chain scopes are NOT filtered on /list — scoped selection is the
    # prime endpoint's job (slice 4); the hook's classic digest is unchanged.
    assert {d["name"] for d in res.json()} == {"Legacy", "Ours", "Theirs"}

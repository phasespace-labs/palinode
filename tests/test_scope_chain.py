"""Tests for ADR-009 Layer 1 scope chain resolution.

This slice covers pure resolution: ScopeChain shape, ordering, empty
behavior, env var precedence, and backwards compatibility with the
pre-scope default.
"""
from __future__ import annotations

import pytest

import palinode.core.config as config_module
from palinode.core.scope import ScopeChain, resolve_scope_chain


# ---------- ScopeChain shape and serialization ----------


def test_scope_chain_default_is_empty():
    chain = ScopeChain()
    assert chain.is_empty()
    assert chain.as_list() == []


def test_scope_chain_narrow_to_broad_order():
    chain = ScopeChain(
        session="abc",
        agent="researcher",
        harness="claude-code",
        project="palinode",
        member="paul",
        org="phasespace",
    )
    assert chain.as_list() == [
        "session/abc",
        "agent/researcher",
        "harness/claude-code",
        "project/palinode",
        "member/paul",
        "org/phasespace",
    ]


def test_scope_chain_drops_unset_levels():
    chain = ScopeChain(project="palinode", member="paul")
    assert chain.as_list() == ["project/palinode", "member/paul"]


def test_scope_chain_is_frozen():
    chain = ScopeChain(project="palinode")
    with pytest.raises((AttributeError, Exception)):
        chain.project = "other"  # type: ignore[misc]


# ---------- resolve_scope_chain from config ----------


def _fresh_config(monkeypatch, env: dict[str, str] | None = None) -> config_module.Config:
    """Build a fresh config that reads the current env vars.

    load_config() reads env vars on every call, so no module reload is needed.
    Reloading would replace the module-level ``config`` singleton and break
    any other module that holds a reference to the original instance.
    """
    for key in (
        "PALINODE_ORG",
        "PALINODE_MEMBER",
        "PALINODE_HARNESS",
        "PALINODE_AGENT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return config_module.load_config()


def test_resolve_scope_chain_defaults_empty(monkeypatch):
    cfg = _fresh_config(monkeypatch)
    chain = resolve_scope_chain(cfg)
    assert chain.is_empty()


def test_resolve_scope_chain_reads_env_vars(monkeypatch):
    cfg = _fresh_config(
        monkeypatch,
        env={
            "PALINODE_ORG": "phasespace",
            "PALINODE_MEMBER": "paul",
            "PALINODE_HARNESS": "claude-code",
        },
    )
    chain = resolve_scope_chain(cfg, project="palinode", session_id="s1")
    assert chain.as_list() == [
        "session/s1",
        "harness/claude-code",
        "project/palinode",
        "member/paul",
        "org/phasespace",
    ]


def test_resolve_scope_chain_multi_agent(monkeypatch):
    cfg = _fresh_config(
        monkeypatch,
        env={
            "PALINODE_MEMBER": "paul",
            "PALINODE_AGENT": "researcher",
        },
    )
    chain = resolve_scope_chain(cfg, project="palinode")
    assert "agent/researcher" in chain.as_list()
    assert chain.as_list().index("agent/researcher") < chain.as_list().index("project/palinode")


def test_resolve_scope_chain_project_passed_by_caller(monkeypatch):
    """The project level comes from the caller (ADR-008 detection), not config env."""
    cfg = _fresh_config(monkeypatch, env={"PALINODE_MEMBER": "paul"})
    a = resolve_scope_chain(cfg, project="palinode")
    b = resolve_scope_chain(cfg, project="other-project")
    assert "project/palinode" in a.as_list()
    assert "project/other-project" in b.as_list()
    assert "project/palinode" not in b.as_list()


def test_scope_config_defaults_are_none(monkeypatch):
    cfg = _fresh_config(monkeypatch)
    assert cfg.scope.org is None
    assert cfg.scope.member is None
    assert cfg.scope.harness is None
    assert cfg.scope.agent is None
    assert cfg.scope.enabled is False
    assert cfg.scope.prime_mode == "classic"


def test_backwards_compat_no_scope_config_in_yaml(monkeypatch):
    """Existing installs with no scope: block in their YAML must still load."""
    cfg = _fresh_config(monkeypatch)
    assert hasattr(cfg, "scope")
    assert resolve_scope_chain(cfg).is_empty()

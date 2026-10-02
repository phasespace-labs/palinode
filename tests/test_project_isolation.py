"""A project-scoped request is isolated to its project.

Scope used to rank only: the project boost moved a scoped request's own
records up, and nothing kept a record tagged to a *different* project out.
A question asked from a project with no memory of its own was handed another
project's decision, and the agent answered with it.

The rule: when a request carries a project, a record naming one or more
``project/*`` entities, none of them the request's, is left out of automatic
delivery (search, the per-turn resolve, the session-start prime) and counted.
A record naming no project is global and stays; one naming the request's
project among several is the request's too. ``include_other_projects`` asks
for everything, labelled. A request with no project is unchanged.

Real SQLite and real git under ``tmp_path``; every log a delivery writes is
pointed at the same tmp store.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.routers import search as search_router
from palinode.api.server import app
from palinode.core.bundle import BundleBudget, BundleRequest, build_bundle
from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger
from palinode.core.scope import ScopeChain, other_project, with_other_projects
from tests import test_resolve_bundle as scenarios
from tests import test_resolve_hook_live as live

mem = scenarios.mem
live_api = live.live_api
_write = scenarios._write

ALPHA_VALUE = "redisember"
BETA_VALUE = "valkeyquartz"
GLOBAL_VALUE = "ttlthreehundred"
SHARED_VALUE = "sharedwarmup"
QUESTION = "which cache does the service use"

ALPHA = "decisions/alpha-cache"
BETA = "decisions/beta-cache"
GLOBAL = "insights/cache-ttl"
SHARED = "decisions/shared-cache"


@pytest.fixture()
def store(mem, monkeypatch):
    """Four records: one per project, one naming both, one naming none."""
    monkeypatch.setattr(search_router, "_retrieval_logger", RetrievalLogger(str(mem)))
    monkeypatch.setattr(config.audit, "log_path", str(mem / ".audit" / "mcp-calls.jsonl"))
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    _write(mem, f"{ALPHA}.md",
           f"# Alpha cache\n\nThe service cache uses {ALPHA_VALUE}.",
           type="Decision", status="active", date="2026-09-01",
           entities=["project/alpha"])
    _write(mem, f"{BETA}.md",
           f"# Beta cache\n\nThe service cache uses {BETA_VALUE}.",
           type="Decision", status="active", date="2026-09-02",
           entities=["project/beta"], core=True)
    _write(mem, f"{GLOBAL}.md",
           f"# Cache TTL\n\nEvery service cache uses {GLOBAL_VALUE} expiry.",
           type="Insight", status="active", date="2026-09-03", core=True)
    _write(mem, f"{SHARED}.md",
           f"# Shared cache\n\nThe service cache uses {SHARED_VALUE} on boot.",
           type="Decision", status="active", date="2026-09-04",
           entities=["project/alpha", "project/beta"])
    return mem


def _chain(project: str | None) -> ScopeChain | None:
    return ScopeChain(project=project) if project else None


def _search(client, context, **extra) -> dict:
    resp = client.post("/search", json={
        "query": QUESTION, "context": context, "receipt": True, "limit": 10,
        "threshold": 0.0, **extra,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _hit_refs(payload: dict) -> set[str]:
    out = set()
    for hit in payload["results"]:
        rel = hit.get("rel_path") or hit["file_path"]
        out.add(rel.removesuffix(".md"))
    return out


def _all_bundle_refs(data: dict) -> set[str]:
    refs = {s["ref"] for s in data["selected"]}
    refs |= {r["ref"] for r in data["replaced"]}
    refs |= {s["ref"] for g in data["conflicts"] for s in g["sides"]}
    for s in data["selected"]:
        refs |= set(s["refs"].get("support") or [])
    return refs


def test_the_store_under_test_is_the_tmp_one(store):
    assert config.memory_dir == str(store)
    assert str(config.db_path).startswith(str(store))
    assert config.audit.log_path.startswith(str(store))
    assert search_router._retrieval_logger._memory_dir == str(store)


# ── the predicate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("entities", "expected"), [
    (["project/beta"], True),
    (["project/beta", "person/ann"], True),
    (["project/alpha"], False),
    (["project/alpha", "project/beta"], False),
    (["person/ann"], False),
    ([], False),
    (None, False),
])
def test_other_project_rule(entities, expected):
    meta = {} if entities is None else {"entities": entities}
    assert other_project(ScopeChain(project="alpha"), meta) is expected


def test_no_project_on_the_chain_isolates_nothing():
    meta = {"entities": ["project/beta"]}
    assert other_project(None, meta) is False
    assert other_project(ScopeChain(agent="bot"), meta) is False


def test_the_opt_in_keeps_the_chain_identity():
    chain = ScopeChain(project="alpha", session="s1")
    wide = with_other_projects(chain)
    assert wide.include_other_projects and wide.as_list() == chain.as_list()


# ── search ───────────────────────────────────────────────────────────────────


def test_scoped_search_gets_its_own_and_the_global_record(store):
    with TestClient(app) as client:
        payload = _search(client, ["project/alpha"])
    refs = _hit_refs(payload)
    assert {ALPHA, GLOBAL, SHARED} <= refs
    assert BETA not in refs
    assert BETA_VALUE not in json.dumps(payload["results"])
    assert payload["receipt"]["retrieval"]["other_projects_withheld"] == 1
    assert all("other_project" not in hit for hit in payload["results"])


def test_the_other_project_is_isolated_the_same_way(store):
    with TestClient(app) as client:
        payload = _search(client, ["project/beta"])
    refs = _hit_refs(payload)
    assert {BETA, GLOBAL, SHARED} <= refs
    assert ALPHA not in refs


def test_include_other_projects_returns_all_labelled(store):
    with TestClient(app) as client:
        payload = _search(client, ["project/alpha"], include_other_projects=True)
    by_ref = {
        (h.get("rel_path") or h["file_path"]).removesuffix(".md"): h
        for h in payload["results"]
    }
    assert {ALPHA, BETA, GLOBAL, SHARED} <= set(by_ref)
    assert by_ref[BETA]["other_project"] == ["project/beta"]
    for ref in (ALPHA, GLOBAL, SHARED):
        assert "other_project" not in by_ref[ref], ref
    assert payload["receipt"]["retrieval"]["other_projects_withheld"] == 0


def test_unscoped_search_is_unchanged(store):
    with TestClient(app) as client:
        payload = _search(client, [])
    assert {ALPHA, BETA, GLOBAL, SHARED} <= _hit_refs(payload)
    assert all("other_project" not in hit for hit in payload["results"])
    assert payload["receipt"]["retrieval"]["other_projects_withheld"] == 0


def test_a_project_with_no_memory_gets_only_global_records(store):
    """Smoke 9.2's shape: the question comes from a project nothing is about."""
    with TestClient(app) as client:
        payload = _search(client, ["project/quillon-zzz"])
    refs = _hit_refs(payload)
    assert refs <= {GLOBAL}
    assert payload["receipt"]["retrieval"]["other_projects_withheld"] == 3


def test_empty_query_recency_search_is_isolated_too(store):
    with TestClient(app) as client:
        resp = client.post("/search", json={
            "query": "", "context": ["project/alpha"], "receipt": True, "limit": 10,
        })
    assert resp.status_code == 200, resp.text
    refs = _hit_refs(resp.json())
    assert BETA not in refs and ALPHA in refs


# ── resolve (the per-turn hook's payload) ────────────────────────────────────


def test_scoped_resolve_never_carries_the_other_project(store):
    data = build_bundle(BundleRequest(query=QUESTION), chain=_chain("alpha")).to_dict()
    assert BETA_VALUE not in json.dumps(data), data["text"]
    assert BETA not in _all_bundle_refs(data)
    assert ALPHA in _all_bundle_refs(data)
    assert data["other_projects_withheld"] == 1
    assert "other project" not in data["text"]


def test_resolve_include_other_projects_delivers_them_labelled(store):
    data = build_bundle(
        BundleRequest(query=QUESTION, include_other_projects=True), chain=_chain("alpha"),
    ).to_dict()
    assert BETA_VALUE in data["text"]
    line = next(line for line in data["text"].splitlines() if BETA in line)
    assert "other project: project/beta" in line, line
    beta = next(s for s in data["selected"] if s["ref"] == BETA)
    assert beta["other_project"] == ["project/beta"]
    assert data["other_projects_withheld"] == 0
    # The request's own and the shared record are not labelled.
    for item in data["selected"]:
        if item["ref"] != BETA:
            assert "other_project" not in item, item["ref"]


def test_unscoped_resolve_is_unchanged(store):
    data = build_bundle(BundleRequest(query=QUESTION), chain=None).to_dict()
    assert BETA_VALUE in data["text"] and ALPHA_VALUE in data["text"]
    assert "other project" not in data["text"]
    assert data["other_projects_withheld"] == 0


def test_a_named_ref_is_reported_across_projects(store):
    """``ref`` names a record the caller already holds; it is not withheld."""
    data = build_bundle(BundleRequest(ref=BETA), chain=_chain("alpha")).to_dict()
    assert [s["ref"] for s in data["selected"]] == [BETA]
    assert data["selected"][0]["other_project"] == ["project/beta"]


def test_rest_resolve_scoped_by_the_requester_project(store):
    with TestClient(app) as client:
        default = client.post("/resolve", json={"query": QUESTION, "project": "alpha"})
        wide = client.post("/resolve", json={
            "query": QUESTION, "project": "alpha", "include_other_projects": True,
        })
        unscoped = client.post("/resolve", json={"query": QUESTION})
    assert default.status_code == wide.status_code == unscoped.status_code == 200
    assert BETA_VALUE not in default.text
    assert default.json()["other_projects_withheld"] == 1
    assert BETA_VALUE in wide.json()["text"]
    assert "other project: project/beta" in wide.json()["text"]
    assert BETA_VALUE in unscoped.json()["text"]


@pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("jq")),
    reason="the hook needs curl and jq on PATH",
)
def test_the_shipped_hook_scoped_to_alpha_never_sees_beta(live_api, store, tmp_path):
    """The exact request the shipped per-turn hook sends, end to end."""
    context = live._run_hook(tmp_path, live_api, QUESTION, PALINODE_PROJECT="alpha")
    assert "resolution unavailable" not in context
    assert BETA_VALUE not in context, context
    assert ALPHA_VALUE in context, context


def test_the_hook_sends_no_cross_project_option():
    """Crossing projects is never an automatic request."""
    from palinode.cli.init import USER_PROMPT_SUBMIT_HOOK_SCRIPT

    assert "include_other_projects" not in USER_PROMPT_SUBMIT_HOOK_SCRIPT


# ── prime (SessionStart) ─────────────────────────────────────────────────────


def test_scoped_prime_leaves_other_projects_core_out(store):
    with TestClient(app) as client:
        resp = client.post("/context/prime", json={"project": "alpha", "session_id": "s1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    core = {row["file"].removesuffix(".md") for row in body["core_memories"]}
    assert GLOBAL in core
    assert BETA not in core
    assert BETA_VALUE not in resp.text
    assert body["other_projects_withheld"] == 1


def test_unscoped_prime_is_unchanged(store):
    with TestClient(app) as client:
        resp = client.post("/context/prime", json={"session_id": "s1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    core = {row["file"].removesuffix(".md") for row in body["core_memories"]}
    assert {BETA, GLOBAL} <= core
    assert body["other_projects_withheld"] == 0


# ── surfaces: MCP and CLI forward the option and render the label ────────────


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_mcp_search_and_resolve(store, monkeypatch):
    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    sent: list[tuple[str, dict]] = []
    with TestClient(app) as client:
        async def _fake_post(path, json=None, timeout=30.0):
            sent.append((path, json))
            return _Resp(client.post(path, json=json).json())

        monkeypatch.setattr(mcp, "_post", _fake_post)
        default = await mcp._tool_search({"query": QUESTION, "threshold": 0.0})
        wide = await mcp._tool_search({
            "query": QUESTION, "threshold": 0.0, "include_other_projects": True,
        })
        resolve_default = await mcp._tool_resolve({"query": QUESTION})
        resolve_wide = await mcp._tool_resolve({
            "query": QUESTION, "include_other_projects": True,
        })

    assert "include_other_projects" not in sent[0][1]
    assert sent[1][1]["include_other_projects"] is True
    assert "include_other_projects" not in sent[2][1]
    assert sent[3][1]["include_other_projects"] is True
    assert BETA_VALUE not in default[0].text
    assert BETA_VALUE in wide[0].text
    assert "[other project: project/beta]" in wide[0].text
    assert BETA_VALUE not in resolve_default[0].text
    assert "other project: project/beta" in resolve_wide[0].text


def test_cli_search_and_resolve_flags(store, monkeypatch):
    from palinode.cli._api import api_client
    from palinode.cli.resolve import resolve as cli_resolve
    from palinode.cli.search import search as cli_search

    monkeypatch.setenv("PALINODE_PROJECT", "alpha")
    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", client)
        runner = CliRunner()
        default = runner.invoke(cli_search, [QUESTION, "--limit", "10", "--threshold", "0",
                                             "--format", "text"])
        wide = runner.invoke(cli_search, [QUESTION, "--limit", "10", "--threshold", "0",
                                          "--include-other-projects", "--format", "text"])
        resolve_default = runner.invoke(cli_resolve, [QUESTION, "--format", "text"])
        resolve_wide = runner.invoke(
            cli_resolve, [QUESTION, "--include-other-projects", "--format", "text"],
        )
    for result in (default, wide, resolve_default, resolve_wide):
        assert result.exit_code == 0, result.output
    assert BETA_VALUE not in default.output and ALPHA_VALUE in default.output
    assert BETA_VALUE in wide.output
    assert "[other project: project/beta]" in wide.output
    assert BETA_VALUE not in resolve_default.output
    assert "other project: project/beta" in resolve_wide.output


# ── project aliases and case ─────────────────────────────────────────────────


def test_project_names_compare_case_insensitively(store):
    _write(store, "decisions/alpha-upper.md",
           "# Alpha upper\n\nThe service cache uses upperwarm.",
           type="Decision", status="active", date="2026-09-05",
           entities=["project/ALPHA"])
    data = build_bundle(
        BundleRequest(query=QUESTION, budget=BundleBudget(max_items=10, max_chars=6000)),
        chain=_chain("alpha"),
    ).to_dict()
    assert "upperwarm" in data["text"]
    assert other_project(ScopeChain(project="orbit_app"), {"entities": ["project/Orbit_App"]}) is False


def _write_aliases(memory_dir, groups: dict[str, list[str]]) -> None:
    """The store's curated alias file, as an operator writes it."""
    import yaml

    from palinode.core import aliases

    (memory_dir / "entity-aliases.yaml").write_text(
        yaml.safe_dump({"aliases": groups}, sort_keys=False), encoding="utf-8",
    )
    aliases.reset_cache()
    aliases.load_alias_map(force=True)


@pytest.fixture(autouse=True)
def _no_alias_cache_between_tests():
    from palinode.core import aliases

    aliases.reset_cache()
    yield
    aliases.reset_cache()


def test_an_alias_group_makes_variant_tagged_records_the_projects_own(store):
    _write(store, "decisions/alpha-dev-cache.md",
           "# Alpha dev cache\n\nThe service cache uses devvariant.",
           type="Decision", status="active", date="2026-09-05",
           entities=["project/alpha-dev"])
    with TestClient(app) as client:
        before = _search(client, ["project/alpha"])
    assert "decisions/alpha-dev-cache" not in _hit_refs(before)

    # The member is spelled with different case from the tag: still one group.
    _write_aliases(store, {"project/alpha": ["project/Alpha-Dev"]})
    with TestClient(app) as client:
        after = _search(client, ["project/alpha"])
        # A request resolved to the alias counts as its canonical project.
        from_alias = _search(client, ["project/alpha-dev"])
    assert {"decisions/alpha-dev-cache", ALPHA, SHARED} <= _hit_refs(after)
    assert BETA not in _hit_refs(after)
    assert {"decisions/alpha-dev-cache", ALPHA} <= _hit_refs(from_alias)
    assert BETA not in _hit_refs(from_alias)


def test_the_config_has_no_second_alias_system():
    """Aliases live in the store's entity-aliases.yaml only (one mechanism)."""
    from palinode.core.config import Config

    assert not hasattr(Config().context, "project_aliases")


def test_resolution_reports_an_alias_as_its_canonical_project(mem, monkeypatch):
    from palinode.core.context_prime import resolve_context

    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    _write_aliases(mem, {"project/harbor": ["project/harbor-dev", "person/not-a-project"]})
    assert resolve_context(project="harbor-dev").project == "project/harbor"
    assert resolve_context(project="project/HARBOR-DEV").project == "project/harbor"
    # Not an alias: exactly as before, spelling included.
    assert resolve_context(project="Beacon").project == "project/Beacon"


# ── the withheld line, only when isolation left the result thin ─────────────


def test_the_withheld_line_appears_when_the_scoped_result_is_empty(store):
    data = build_bundle(
        BundleRequest(query=QUESTION), chain=_chain("quillon-zzz"),
    ).to_dict()
    n = data["other_projects_withheld"]
    assert n >= 1
    assert len(data["selected"]) + len(data["conflicts"]) < 2
    assert (
        f"{n} record{'' if n == 1 else 's'} from other projects withheld "
        "(scope: project/quillon-zzz). They are about other projects, not this one."
    ) in data["text"]
    # The agent is not told how to reach them: told, it fetches and answers.
    assert "include_other_projects" not in data["text"]
    assert BETA_VALUE not in data["text"]


def test_the_withheld_line_stays_out_of_a_full_result(store):
    data = build_bundle(BundleRequest(query=QUESTION), chain=_chain("alpha")).to_dict()
    assert data["other_projects_withheld"] == 1
    assert len(data["selected"]) + len(data["conflicts"]) >= 2
    assert "from other projects withheld" not in data["text"]
    unscoped = build_bundle(BundleRequest(query=QUESTION), chain=None).to_dict()
    assert "from other projects withheld" not in unscoped["text"]


@pytest.mark.asyncio
async def test_mcp_search_says_why_a_scoped_search_came_back_thin(store, monkeypatch):
    with TestClient(app) as client:
        async def _fake_post(path, json=None, timeout=30.0):
            return _Resp(client.post(path, json=json).json())

        monkeypatch.setattr(mcp, "_post", _fake_post)
        monkeypatch.setenv("PALINODE_PROJECT", "quillon-zzz")
        thin = await mcp._tool_search({"query": QUESTION, "threshold": 0.0})
        monkeypatch.setenv("PALINODE_PROJECT", "alpha")
        full = await mcp._tool_search({"query": QUESTION, "threshold": 0.0})
    assert (
        "from other projects withheld (scope: project/quillon-zzz). "
        "They are about other projects, not this one." in thin[0].text
    )
    assert "include_other_projects" not in thin[0].text
    assert "from other projects withheld" not in full[0].text


def test_the_cli_tells_a_person_how_to_see_them(store, monkeypatch):
    """A human reads the CLI; the flag is worth naming there, and only there."""
    from palinode.cli._api import api_client
    from palinode.cli.search import search as cli_search

    monkeypatch.setenv("PALINODE_PROJECT", "quillon-zzz")
    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", client)
        result = CliRunner().invoke(
            cli_search, [QUESTION, "--limit", "10", "--threshold", "0", "--format", "text"],
        )
    assert result.exit_code == 0, result.output
    assert "from other projects withheld (scope: project/quillon-zzz)" in result.output
    assert "--include-other-projects to see them" in result.output


def test_the_hook_delivers_a_bundle_whose_only_news_is_the_withheld_count():
    from palinode.cli.init import USER_PROMPT_SUBMIT_HOOK_SCRIPT

    assert "(.other_projects_withheld // 0)" in USER_PROMPT_SUBMIT_HOOK_SCRIPT


# ── doctor: fragmented or unmapped project tags ─────────────────────────────


def test_doctor_lists_an_unmapped_tag_until_it_is_covered(mem, monkeypatch):
    from palinode.diagnostics.checks.project_tags import MIN_FILES, project_tags_unmapped
    from palinode.diagnostics.types import DoctorContext

    monkeypatch.setattr(config.context, "project_map", {})
    for i in range(MIN_FILES):
        _write(mem, f"decisions/frag-{i}.md", f"# Frag {i}\n\nNote number {i}.",
               type="Decision", status="active", entities=["project/harbor"])
    for i in range(MIN_FILES - 1):
        _write(mem, f"decisions/small-{i}.md", f"# Small {i}\n\nNote {i}.",
               type="Decision", status="active", entities=["project/stray"])

    result = project_tags_unmapped(DoctorContext(config=config))
    assert result.severity == "warn" and not result.passed
    assert f"project/harbor ({MIN_FILES})" in result.message
    assert "project/stray" not in result.message  # below the threshold
    assert "entity-aliases.yaml" in (result.remediation or "")

    # Covered once the curated alias file groups it (case-insensitively)...
    _write_aliases(mem, {"project/harbor-dev": ["project/Harbor"]})
    assert project_tags_unmapped(DoctorContext(config=config)).passed

    # ...or once it is a project_map target.
    (mem / "entity-aliases.yaml").unlink()
    from palinode.core import aliases

    aliases.reset_cache()
    monkeypatch.setattr(config.context, "project_map", {"harbor-dev": "project/Harbor"})
    assert project_tags_unmapped(DoctorContext(config=config)).passed


# ── the hooks send a linked worktree's main repository root ──────────────────


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})


@pytest.fixture()
def linked_worktree(tmp_path):
    base = tmp_path.resolve()
    main = base / "harbor-notes"
    linked = base / "agent-a1b2c3"
    _git("init", "-q", str(main))
    _git("-C", str(main), "-c", "user.email=t@t.test", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init")
    _git("-C", str(main), "worktree", "add", "-q", str(linked))
    return main, linked


def _stub_curl_run(workdir, script: str, cwd: str) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    stub = workdir / "stub"
    stub.mkdir()
    (stub / "curl").write_text(
        '#!/bin/bash\n'
        'echo "$@" >> "$STUB_DIR/curl-called"\n'
        'case "$*" in\n'
        '  *"/controls/check"*) echo \'{"allowed":true}\' ;;\n'
        '  *) echo \'{}\' ;;\n'
        'esac\n'
    )
    (stub / "curl").chmod(0o755)
    hook = workdir / "hook.sh"
    hook.write_text(script)
    tool_dirs = {os.path.dirname(shutil.which(t) or "/usr/bin/x") for t in ("git", "jq")}
    subprocess.run(
        ["/bin/bash", str(hook)],
        input=json.dumps({"prompt": QUESTION, "session_id": "s1", "cwd": cwd}),
        capture_output=True, text=True,
        env={"PATH": ":".join([str(stub), *sorted(tool_dirs), "/usr/bin", "/bin"]),
             "STUB_DIR": str(stub), "HOME": str(workdir),
             "PALINODE_API_URL": "http://stub:1"},
    )
    called = stub / "curl-called"
    return called.read_text() if called.exists() else ""


@pytest.mark.skipif(not (shutil.which("jq") and shutil.which("git")),
                    reason="the hooks need jq and git")
@pytest.mark.parametrize("which", ["prompt", "session_start"])
def test_a_linked_worktree_resolves_to_the_main_repositorys_project(
    linked_worktree, tmp_path, which, monkeypatch,
):
    from palinode.cli.init import SESSION_START_HOOK_SCRIPT, USER_PROMPT_SUBMIT_HOOK_SCRIPT
    from palinode.core.context_prime import resolve_context

    main, linked = linked_worktree
    script = USER_PROMPT_SUBMIT_HOOK_SCRIPT if which == "prompt" else SESSION_START_HOOK_SCRIPT
    calls = _stub_curl_run(tmp_path / which, script, str(linked))
    assert f'"cwd":"{main}"' in calls.replace(" ", ""), calls
    assert str(linked) not in calls

    # A server on another machine can only go by the name it was sent, which
    # is now the repository's rather than the task's.
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config.context, "project_map", {})
    assert resolve_context(cwd="/elsewhere/" + main.name).project == "project/harbor-notes"


@pytest.mark.skipif(not shutil.which("jq"), reason="the hook needs jq")
def test_a_directory_outside_git_is_sent_unchanged(tmp_path):
    from palinode.cli.init import USER_PROMPT_SUBMIT_HOOK_SCRIPT

    plain = tmp_path.resolve() / "plain-dir"
    plain.mkdir()
    calls = _stub_curl_run(tmp_path / "run", USER_PROMPT_SUBMIT_HOOK_SCRIPT, str(plain))
    assert f'"cwd":"{plain}"' in calls.replace(" ", ""), calls

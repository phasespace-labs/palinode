"""The operation spec is the source; each surface renders or is checked against it.

ADR-022. For every operation in ``palinode.core.operations.SPECS``:

* **Forwarding, generated per surface.** Every spec param a caller sets reaches
  the request (the JSON body, or the query string of a ``GET``) under its
  canonical name with its canonical value; a param the caller did not set is
  absent, so the server's default applies. MCP and the CLI are exercised here
  through their real HTTP clients; the plugin has the same generated test in
  ``plugin/test/operation-spec.test.ts`` for the operations it exposes.
* **Hand-written surfaces checked.** The click options and the REST model (the
  pydantic body, or a ``GET`` route's query parameters) are written by hand
  (help text, bounds, validators) and checked against the spec's names, types,
  enums and ``required`` flags.
* **Defined once.** A migrated operation's parity entry is derived from the
  spec, never restated.

No test reaches the network: requests are captured by ``httpx.MockTransport``,
except the threshold-by-source test, which runs the real app on real SQLite.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import typing

import click
import httpx
import pytest
from click.testing import CliRunner

from palinode import mcp
from palinode.cli import main
from palinode.cli._api import api_client
from palinode.cli.list_cmd import list_cmd
from palinode.cli.read import read as read_command
from palinode.cli.save import save as save_command
from palinode.cli.search import search as search_command
from palinode.cli.session_end import session_end as session_end_command
from palinode.core import operations, parity
from palinode.core.operations import (
    LIST, READ, SAVE, SEARCH, SESSION_END, OperationSpec, Param, build_payload, schema_properties,
)

_READ_PATH = "people/alice.md"


@dataclasses.dataclass(frozen=True)
class Surfaces:
    """Where one migrated operation lives on MCP and the CLI."""

    mcp_tool: str
    #: The spec's required params, as every call below sends them.
    required: dict
    #: The CLI's HTTP client method, called with spec params as kwargs.
    client: typing.Callable[..., typing.Any]
    command: click.Command
    #: A CLI invocation that sets no spec param it does not require.
    argv: tuple[str, ...]
    #: MCP-only, render-only properties the spec does not own.
    mcp_only: frozenset[str] = frozenset()
    #: Params the CLI command sends unasked, as a deliberate assertion.
    cli_asserts: frozenset[str] = frozenset()


SURFACES: dict[str, Surfaces] = {
    "search": Surfaces(
        "palinode_search", {"query": "q"}, lambda **kw: api_client.search("q", **kw),
        search_command, ("search", "q", "--format", "json", "--no-context"),
        mcp_only=frozenset({"full"}),
    ),
    "list": Surfaces(
        "palinode_list", {}, lambda **kw: api_client.list_files_scoped(**kw),
        list_cmd, ("list", "--format", "json"),
    ),
    "read": Surfaces(
        "palinode_read", {"file_path": _READ_PATH},
        lambda **kw: api_client.read(_READ_PATH, **kw),
        read_command, ("read", _READ_PATH, "--format", "json"),
    ),
    "save": Surfaces(
        "palinode_save", {"content": "q", "type": "Decision"},
        lambda **kw: api_client.save("q", "Decision", **kw),
        save_command, ("save", "q", "--type", "Decision", "--format", "json"),
        mcp_only=frozenset({"ps"}),
    ),
    "session_end": Surfaces(
        "palinode_session_end", {"summary": "q"},
        lambda **kw: api_client.session_end("q", **kw),
        session_end_command, ("session-end", "q", "--format", "json"),
        # `decisions: []` / `blockers: []`: "considered, none to report". The
        # server's envelope guard reads their absence as an absorbed tool call.
        cli_asserts=frozenset({"decisions", "blockers"}),
    ),
}

SPEC_PARAMS = [
    (spec, p) for spec in operations.SPECS for p in spec.params if not p.required
]
#: The cases each surface owes: a param the spec limits to other surfaces is not.
MCP_PARAMS = [(spec, p) for spec, p in SPEC_PARAMS if p.on("mcp")]
CLI_PARAMS = [(spec, p) for spec, p in SPEC_PARAMS if p.on("cli")]


def _id(case) -> str:
    spec, p = case
    return f"{spec.name}.{p.name}"


def _object(fields: tuple[Param, ...]) -> dict:
    """An object carrying every field, nested ones included."""
    return {f.name: _sample(f)[1] for f in fields}


def _sample(p: Param):
    """A valid, non-default value for *p*, and what the body must carry for it."""
    if p.type == "object":
        value = _object(p.fields) if p.fields else {"k": "v"}
        return value, value
    if p.type == "array" and p.fields:
        value = [_object(p.fields)]
        return value, value
    if p.type == "boolean":
        return True, True
    if p.type == "integer":
        return "2", 2  # a JSON-typed transport may hand over a string
    if p.type == "number":
        return "0.33", 0.33
    if p.type == "array":
        if p.enum:
            return [p.enum[0]], [p.enum[0]]
        # Two entries, so a query string must repeat the key.
        return ["status=open", "entities=project/my-app"], ["status=open", "entities=project/my-app"]
    if p.enum:
        value = [v for v in p.enum if v not in p.unset_values][-1]
        return value, value
    return "2026-01-01", "2026-01-01"


#: The least a surface's own rendering needs back from each endpoint.
_RESPONSES = {
    READ.endpoint: {"content": ""},
    SAVE.endpoint: {"file_path": "decisions/q.md", "id": "decisions/q"},
    SESSION_END.endpoint: {"daily_file": "daily/2026-11-12.md", "status_file": None},
}


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=_RESPONSES.get(request.url.path, []))

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        return self.handle(request)

    def last(self, spec: OperationSpec) -> httpx.Request:
        found = [r for r in self.requests if r.url.path == spec.endpoint]
        assert found, f"no request reached {spec.endpoint}"
        return found[-1]

    def sent(self, spec: OperationSpec) -> dict:
        """What the request carried, by name: the JSON body, or a GET's query string."""
        request = self.last(spec)
        if request.method == "GET":
            params = request.url.params
            return {key: params.get_list(key) for key in params.keys()}
        return json.loads(request.content)

    def search_body(self) -> dict:
        return self.sent(SEARCH)


def _on_the_wire(request: httpx.Request, value):
    """``value`` as the request carries it: JSON as is; a query string as strings."""
    if request.method != "GET":
        return value
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, bool):
        return ["true" if value else "false"]
    return [str(value)]


@pytest.fixture()
def mcp_recorder(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(mcp, "_http_transport", httpx.MockTransport(recorder.handle_async))
    monkeypatch.setattr(mcp, "_http_client", None)
    monkeypatch.setattr(mcp, "_http_client_loop", None)
    return recorder


@pytest.fixture()
def cli_recorder(monkeypatch) -> Recorder:
    recorder = Recorder()
    client = httpx.Client(
        transport=httpx.MockTransport(recorder.handle), base_url="http://testserver"
    )
    monkeypatch.setattr(api_client, "client", client)
    yield recorder
    client.close()


def _structure(prop: dict) -> dict:
    """What the spec owns of a JSON Schema property, nested fields included."""
    out = {k: v for k, v in prop.items() if k in ("type", "enum", "required")}
    if "items" in prop:
        out["items"] = _structure(prop["items"])
    if "properties" in prop:
        out["properties"] = {k: _structure(v) for k, v in prop["properties"].items()}
    return out


def _mcp_call(spec: OperationSpec, arguments: dict) -> str:
    tool = SURFACES[spec.name]
    args = {**tool.required, **arguments}
    return asyncio.run(mcp._dispatch_tool(tool.mcp_tool, args))[0].text


# ── the forwarding rule ──────────────────────────────────────────────────────


def test_build_payload_rules():
    params = (
        Param("n", type="integer", cast=int),
        Param("on", type="boolean", flag=True),
        Param("mode", type="string", unset_values=("none",)),
        Param("tags", type="array", omit_if_empty=True),
        Param("refs", type="array"),
    )
    assert build_payload(params, {}) == {}
    assert build_payload(params, {"n": "0", "on": "false", "mode": "none", "tags": [], "refs": []}) == {
        "n": 0, "refs": [],
    }
    assert build_payload(params, {"on": "true", "tags": '["a"]', "mode": "full"}) == {
        "on": True, "tags": ["a"], "mode": "full",
    }


def test_schema_properties_carry_no_default_and_refuse_restated_structure():
    props = schema_properties(SEARCH)
    assert list(props) == [p.name for p in SEARCH.params]
    assert not any("default" in prop for prop in props.values())
    with pytest.raises(ValueError, match="restate"):
        schema_properties(SEARCH, {"limit": {"default": 3}})
    with pytest.raises(ValueError, match="not in search"):
        schema_properties(SEARCH, {"nonexistent": {"description": "x"}})


# ── MCP ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spec", operations.SPECS, ids=lambda s: s.name)
def test_mcp_schema_is_the_spec(spec):
    tool = SURFACES[spec.name]
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    schema = tools[tool.mcp_tool].input_schema
    structure = {
        name: _structure(prop)
        for name, prop in schema["properties"].items()
        if name not in tool.mcp_only
    }
    assert structure == {
        name: _structure(p) for name, p in schema_properties(spec, surface="mcp").items()
    }
    assert not any("default" in prop for prop in schema["properties"].values())
    assert schema.get("required", []) == operations.required_params(spec, "mcp")


@pytest.mark.parametrize("case", MCP_PARAMS, ids=_id)
def test_mcp_forwards_each_param(mcp_recorder, case):
    spec, param = case
    given, expected = _sample(param)
    _mcp_call(spec, {param.name: given})
    request = mcp_recorder.last(spec)
    assert mcp_recorder.sent(spec)[param.name] == _on_the_wire(request, expected)


@pytest.mark.parametrize("spec", operations.SPECS, ids=lambda s: s.name)
def test_mcp_sends_nothing_the_caller_did_not_set(mcp_recorder, spec):
    _mcp_call(spec, {})
    sent = mcp_recorder.sent(spec)
    assert set(sent) & {p.name for p in spec.params} == set(SURFACES[spec.name].required)
    assert mcp_recorder.last(spec).headers["X-Palinode-Source"] == "mcp"


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cli_kwargs(param: Param) -> dict:
    given, _ = _sample(param)
    if param.type == "integer":
        given = int(given)
    elif param.type == "number":
        given = float(given)
    return {param.name: given}


@pytest.mark.parametrize("case", CLI_PARAMS, ids=_id)
def test_cli_client_forwards_each_param(cli_recorder, case):
    spec, param = case
    SURFACES[spec.name].client(**_cli_kwargs(param))
    request = cli_recorder.last(spec)
    assert cli_recorder.sent(spec)[param.name] == _on_the_wire(request, _sample(param)[1])


@pytest.mark.parametrize("spec", operations.SPECS, ids=lambda s: s.name)
def test_cli_client_sends_nothing_the_caller_did_not_set(cli_recorder, spec):
    SURFACES[spec.name].client()
    sent = cli_recorder.sent(spec)
    assert set(sent) & {p.name for p in spec.params} == set(SURFACES[spec.name].required)


@pytest.mark.parametrize("spec", operations.SPECS, ids=lambda s: s.name)
def test_cli_command_sends_no_server_default_it_was_not_given(cli_recorder, spec):
    tool = SURFACES[spec.name]
    result = CliRunner().invoke(main, list(tool.argv))
    assert result.exit_code == 0, result.output
    sent = cli_recorder.sent(spec)
    assert set(sent) & {p.name for p in spec.params} == set(tool.required) | tool.cli_asserts


@pytest.mark.parametrize(("fmt", "limit"), [("json", None), ("text", 3)])
def test_cli_limit_is_a_display_override(cli_recorder, fmt, limit):
    result = CliRunner().invoke(main, ["search", "q", "--format", fmt, "--no-context"])
    assert result.exit_code == 0, result.output
    assert cli_recorder.search_body().get("limit") == limit
    assert SEARCH.display_default("cli", "limit") == 3


def test_cli_explicit_limit_wins_in_any_format(cli_recorder):
    result = CliRunner().invoke(main, ["search", "q", "--format", "json", "--limit", "7", "--no-context"])
    assert result.exit_code == 0, result.output
    assert cli_recorder.search_body()["limit"] == 7


@pytest.mark.parametrize("spec", operations.SPECS, ids=lambda s: s.name)
def test_click_options_match_the_spec(spec):
    by_name = {p.name: p for p in SURFACES[spec.name].command.params}
    dests = parity.by_name(spec.name).cli_dests
    sugared = {s.param for s in spec.shorthands if "cli" in s.surfaces}
    for param in spec.params:
        opt = by_name.get(dests.get(param.name, param.name))
        assert opt is not None, f"`palinode {spec.name}` has no option for {param.name}"
        if param.required and param.name not in sugared:
            assert isinstance(opt, click.Argument) and opt.required, param.name
            continue
        if param.type == "object":
            # A JSON string or repeated k=v pairs: the CLI's own parsing.
            continue
        assert not opt.required or param.required, param.name
        kind = opt.type
        if param.type == "boolean":
            assert opt.is_flag, param.name
        elif param.type == "array":
            assert opt.multiple, param.name
        elif param.type == "integer":
            assert isinstance(kind, (click.types.IntParamType, click.IntRange)), param.name
        elif param.type == "number":
            assert isinstance(kind, click.types.FloatParamType), param.name
        if param.enum:
            assert isinstance(kind, click.Choice), param.name
            assert set(kind.choices) == set(param.enum), param.name


# ── REST ─────────────────────────────────────────────────────────────────────


def _literals(annotation) -> set[str]:
    out: set[str] = set()
    for arg in typing.get_args(annotation):
        if isinstance(arg, str):
            out.add(arg)
        else:
            out |= _literals(arg)
    return out


def _base_types(annotation) -> set[type]:
    out: set[type] = set()
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return {str}
    if origin in (list, tuple):
        return {list}
    if isinstance(annotation, type):
        return {annotation}
    for arg in typing.get_args(annotation):
        out |= _base_types(arg)
    return out


_PY_TYPE = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list,
            "object": dict}


def _rest_fields(spec: OperationSpec) -> dict[str, tuple[typing.Any, bool, list | None]]:
    """``{name: (annotation, required, advertised enum)}`` for the REST surface of *spec*.

    The advertised enum is a pydantic field's ``json_schema_extra`` enum: save's
    ``epistemic`` and ``update_policy`` are ``str`` so the handler can answer a
    bad value itself, and advertise their values that way.
    """
    import inspect

    if spec in (SEARCH, SAVE, SESSION_END):
        if spec is SEARCH:
            from palinode.api.routers.search import SearchRequest as model
        elif spec is SAVE:
            from palinode.api.routers.memory import SaveRequest as model
        else:
            from palinode.api.routers.session import SessionEndRequest as model

        return {
            name: (f.annotation, f.is_required(),
                   (f.json_schema_extra or {}).get("enum") if isinstance(f.json_schema_extra, dict) else None)
            for name, f in model.model_fields.items()
        }
    from palinode.api.routers import memory

    route = {LIST.name: memory.list_api, READ.name: memory.read_api}[spec.name]
    hints = typing.get_type_hints(route, include_extras=True)
    return {
        name: (hints[name], param.default is inspect.Parameter.empty, None)
        for name, param in inspect.signature(route).parameters.items()
        if name in hints
    }


@pytest.mark.parametrize("spec", operations.SPECS, ids=lambda s: s.name)
def test_rest_model_matches_the_spec(spec):
    fields = _rest_fields(spec)
    for param in spec.params:
        assert param.name in fields, f"the REST surface of {spec.name} has no {param.name}"
        annotation, required, advertised = fields[param.name]
        assert required == param.required, param.name
        kinds = _base_types(annotation)
        # `Any` is deliberate where the handler validates (save's `sources` and
        # `claims` reach their normaliser so a bad item is a clean 400).
        assert typing.Any in kinds or _PY_TYPE[param.type] in kinds, (param.name, annotation)
        if param.enum:
            assert (set(advertised) if advertised else _literals(annotation)) == set(param.enum), param.name
        if param.server_default:
            assert not required, param.name


# ── defined once ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spec", operations.SPECS, ids=lambda s: s.name)
def test_a_migrated_operation_is_defined_once(spec):
    entry = next(op for op in parity.REGISTRY if op.name == spec.name)
    assert entry.canonical_params == parity._from_spec(spec), (
        f"parity.py restates {spec.name}'s params; derive them from the spec"
    )
    assert all(cp.default_key is None for cp in entry.canonical_params)


# ── the server owns the threshold, per caller ────────────────────────────────


from tests import test_relative_floor_over_visible_hits as _floor  # noqa: E402

memory = _floor.memory
client = _floor.client


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"X-Palinode-Source": "mcp"}, {}, "mcp"),
        ({"X-Palinode-Source": "cli"}, {}, "api"),
        ({}, {}, "api"),
        ({"X-Palinode-Source": "mcp"}, {"threshold": 0.77}, 0.77),
    ],
    ids=["mcp-unset", "cli-unset", "no-source-unset", "mcp-explicit"],
)
def test_the_server_picks_the_floor_by_source(memory, client, monkeypatch, headers, body, expected):
    from palinode.core import store
    from palinode.core.config import config

    monkeypatch.setattr(config.search, "mcp_threshold", 0.31)
    monkeypatch.setattr(config.search, "api_threshold", 0.52)
    want = {"mcp": 0.31, "api": 0.52}.get(expected, expected)
    seen: list[float] = []
    real = store.search_hybrid

    def spy(*args, **kwargs):
        seen.append(kwargs.get("threshold"))
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "search_hybrid", spy)
    _floor._record(memory, "visible", 0.80, 2)
    resp = client.post("/search", json={"query": _floor.QUERY, **body}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert seen and set(seen) == {want}


# ── docs/PARITY.md is generated ──────────────────────────────────────────────


def test_parity_doc_tables_are_current():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "render-parity-doc.py"
    loader = importlib.util.spec_from_file_location("render_parity_doc", script)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    text = module.DOC.read_text(encoding="utf-8")
    assert module.rendered(text) == text, (
        "docs/PARITY.md's operation-spec tables are stale: run "
        "`uv run python scripts/render-parity-doc.py`"
    )

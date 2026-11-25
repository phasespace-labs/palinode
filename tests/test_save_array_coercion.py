"""the array-param coercion fix: every array parameter on palinode_save tolerates a
double-encoded array.

Some MCP clients serialize array arguments as JSON strings. `_coerce_str_array`
was written against that observed behaviour but wired to only ONE of save's five
array params, so the same client input made `entities` work and the other four
fail — in three different error shapes, from three hand-rolled code paths.

Partial tolerance is the worst of both worlds: the caller sees some arrays land
and concludes the transport is fine.
"""

from __future__ import annotations

import json

from palinode.mcp import _coerce_str_array

# The five array parameters on palinode_save.
SAVE_ARRAY_PARAMS = ("entities", "sources", "contradicts", "backed_by", "claims")


def test_decodes_a_double_encoded_string_array() -> None:
    assert _coerce_str_array('["project/alpha","person/bravo"]') == [
        "project/alpha",
        "person/bravo",
    ]


def test_decodes_arrays_of_objects_too() -> None:
    """`sources` and `claims` carry dicts, not strings.

    The helper's name says "str_array" but it never inspects element types — it
    returns any decoded JSON list. This is what makes uniform application safe.
    """
    payload = [{"id": "s1", "quote": "x"}, {"id": "s2", "quote": "y"}]
    assert _coerce_str_array(json.dumps(payload)) == payload


def test_native_lists_pass_through_untouched() -> None:
    native = ["project/alpha"]
    assert _coerce_str_array(native) is native
    objs = [{"id": "s1"}]
    assert _coerce_str_array(objs) is objs


def test_non_array_input_is_returned_unchanged() -> None:
    """A plain string stays a string — the API still validates and 400s on it."""
    assert _coerce_str_array("not json at all") == "not json at all"
    assert _coerce_str_array('{"a": 1}') == '{"a": 1}', "a JSON object is not an array"
    assert _coerce_str_array('"scalar"') == '"scalar"'
    assert _coerce_str_array(None) is None
    assert _coerce_str_array(7) == 7


def test_every_save_array_param_is_guarded_in_the_handler() -> None:
    """The regression this issue is about: guarding one param and not the rest.

    This used to grep the handler source for a per-param
    ``body[...] = _coerce_str_array(arguments[...])`` line. Coercion is now
    declared once in the save spec (``core/operations.py``'s ``SAVE``) and applied by
    ``build_payload``, so there is no such line to find and the string match
    would pass vacuously the moment anyone reformatted it.

    Pointed at the declaration instead, and derived from the *live MCP schema*
    rather than a hardcoded list: every parameter the save tool advertises as an
    array must be an array in the spec. A newly added array param
    that nobody adds to the spec fails here — which the old grep could
    not catch either, since it only ever checked the five names written into
    this file.
    """
    import asyncio

    from palinode.core.operations import SAVE
    from palinode.mcp import list_tools as mcp_list_tools

    tools = asyncio.run(mcp_list_tools())
    save = next(t for t in tools if t.name == "palinode_save")
    schema_arrays = {
        name
        for name, spec in save.input_schema.get("properties", {}).items()
        if spec.get("type") == "array"
    }
    assert schema_arrays, "save tool advertises no array params — schema lookup broke"

    coerced = {p.name for p in SAVE.params if p.array or p.type == "array"}
    missing = schema_arrays - coerced
    assert not missing, (
        f"{sorted(missing)} advertised as arrays by the save tool but not declared "
        "as arrays in core/operations.py::SAVE — they forward verbatim, "
        "which is the partial-tolerance defect this module exists to prevent."
    )

    # The five the original report enumerated are still covered, so this test
    # cannot regress below its old floor even if the schema lookup drifts.
    assert set(SAVE_ARRAY_PARAMS) <= coerced


def test_empty_arrays_survive_as_an_assertion() -> None:
    """An explicitly-empty array reaches the server; it is not elided.

    The divergence this seam closes: MCP forwarded ``contradicts: []`` while
    the CLI dropped it, so the same explicit "no conflicts" meant two different
    things depending on which surface the caller used. ``[]`` is a claim the
    caller made — only ``None`` means "never specified".
    """
    from palinode.core.operations import SAVE, build_payload

    payload = build_payload(SAVE.params, {"contradicts": [], "backed_by": []})
    assert payload == {"contradicts": [], "backed_by": []}

    assert build_payload(SAVE.params, {"contradicts": None}) == {}


def test_blank_free_text_is_still_elided() -> None:
    """``omit_if_empty`` strings drop when blank — what every surface already did.

    ``title=""`` carries nothing the server could act on, and forwarding it
    would be a behaviour change dressed up as a unification.
    """
    from palinode.core.operations import SAVE, build_payload

    assert build_payload(SAVE.params, {"title": "", "slug": "", "project": ""}) == {}
    assert build_payload(SAVE.params, {"title": "Real"}) == {"title": "Real"}


def _capturing_client():
    """A ``PalinodeAPI`` whose posts are captured instead of sent."""
    from palinode.cli import _api

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"file_path": "/x", "id": "insights-x"}

    class _FakeClient:
        def post(self, path, json=None, params=None, timeout=None):
            captured["json"] = json
            return _FakeResp()

    client = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    client.client = _FakeClient()
    return client, captured


def test_cli_forwards_an_explicitly_empty_contradicts() -> None:
    """The regression: ``contradicts=[]`` used to be dropped by the CLI.

    ``cli/_api.py`` read ``if contradicts:`` while MCP read ``is not None``, so
    the identical explicit "no conflicts" claim reached the server on one
    surface and vanished on the other. It matters downstream: an absent
    ``contradicts`` lets the capability fall back to a ``metadata``-supplied
    value, where an empty one asserts there is none.
    """
    client, captured = _capturing_client()
    client.save("body", "Insight", contradicts=[], backed_by=[])

    assert captured["json"]["contradicts"] == []
    assert captured["json"]["backed_by"] == []


def test_cli_still_omits_typed_links_that_were_never_supplied() -> None:
    """``None`` remains "never specified" — the fix must not send empties always."""
    client, captured = _capturing_client()
    client.save("body", "Insight")

    assert "contradicts" not in captured["json"]
    assert "backed_by" not in captured["json"]


def test_cli_and_mcp_agree_on_the_same_logical_save() -> None:
    """Both surfaces build the same body from the same input.

    The point of the seam: the CLI adapter and the MCP handler now derive their
    payload from one spec, so they cannot drift apart per-param the way they had.
    """
    from palinode.core.operations import SAVE, build_payload

    client, captured = _capturing_client()
    client.save(
        "body",
        "Insight",
        entities=["project/alpha"],
        title="T",
        contradicts=[],
        sources=[{"ref": "a.md", "quote": "q"}],
    )
    cli_body = captured["json"]

    # What the MCP handler assembles for the identical call.
    mcp_arguments = {
        "content": "body",
        "type": "Insight",
        "entities": ["project/alpha"],
        "title": "T",
        "contradicts": [],
        "sources": [{"ref": "a.md", "quote": "q"}],
    }
    mcp_body = build_payload(SAVE.params, mcp_arguments)

    assert cli_body == mcp_body

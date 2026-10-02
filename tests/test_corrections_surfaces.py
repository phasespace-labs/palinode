"""All four surfaces answer the same question about correction candidates.

The parity registry (``palinode.core.parity``) already forces the parameter
names to agree and ``tests/test_surface_parity.py`` walks it. These tests cover
what the registry cannot: that each surface actually returns the report, and
that the one deliberate asymmetry — ``scan`` on the operator's surfaces only —
is the shape that shipped rather than an oversight.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.routers.maintenance import CorrectionsRequest
from palinode.api.server import app
from palinode.cli import main as cli_root
# ``palinode.cli.__init__`` rebinds the submodule name to the click command it
# exports, so the plain ``import palinode.cli.corrections`` hands back a
# ``Command``. Ask importlib for the module itself.
corrections_cli = importlib.import_module("palinode.cli.corrections")
controls_cli = importlib.import_module("palinode.cli.controls")
from palinode.core import disclosure, parity
from palinode.core.config import config
from palinode.corrections import classify as classify_module
from palinode.corrections.scan import DETECTION_ONLY, scan_transcripts

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "v1"
PLUGIN_INDEX = Path(__file__).resolve().parent.parent / "plugin" / "index.ts"


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.transcripts, "enabled", True)
    monkeypatch.setattr(config.capture.transcripts, "lookback_days", 0)
    monkeypatch.setattr(config.capture.transcripts, "max_candidates", 50)
    monkeypatch.setattr(
        config.capture.transcripts, "harness_paths", {"claude-code": [str(FIXTURE_DIR)]}
    )
    scan_transcripts(classify=False)
    return tmp_path


# ── REST ────────────────────────────────────────────────────────────────────


def test_api_lists_candidates_and_filters(store: Path) -> None:
    with TestClient(app) as client:
        everything = client.post("/corrections", json={})
        scoped = client.post("/corrections", json={"project": "harbor-notes"})

    assert everything.status_code == 200
    payload = everything.json()
    assert payload["count"] == 12
    assert payload["applied"].startswith("nothing applied")
    assert payload["scan"] is None, "listing must not scan unless asked"
    assert scoped.json()["count"] == 4


def test_api_scan_is_available_to_the_operator(store: Path, tmp_path: Path) -> None:
    """The operator's surfaces can run a pass; the queue dedupes the re-read."""
    with TestClient(app) as client:
        response = client.post("/corrections", json={"scan": True})

    body = response.json()
    assert body["scan"]["candidates_added"] == 0
    assert body["scan"]["duplicates"] == 12
    assert body["scan"]["applied"].startswith("nothing applied")


# ── MCP ─────────────────────────────────────────────────────────────────────


def _tools(surface: str) -> dict:
    previous = os.environ.get("PALINODE_MCP_SURFACE")
    os.environ["PALINODE_MCP_SURFACE"] = surface
    try:
        return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    finally:
        if previous is None:
            os.environ.pop("PALINODE_MCP_SURFACE", None)
        else:
            os.environ["PALINODE_MCP_SURFACE"] = previous


def test_the_mcp_tool_is_off_the_core_surface() -> None:
    """A dry-run report is not one of the twelve tools every session pays for."""
    assert "palinode_corrections" in _tools("full")
    assert "palinode_corrections" not in _tools("core")
    assert "palinode_corrections" not in mcp.CORE_TOOL_NAMES


def test_the_mcp_tool_is_a_read_only_listing() -> None:
    """`scan` is absent by design: reading transcripts is an operator action."""
    tool = _tools("full")["palinode_corrections"]
    assert set(tool.input_schema["properties"]) == {"project", "since_days"}
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


def test_the_mcp_handler_never_asks_for_a_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"count": 0, "candidates": [], "applied": "nothing applied"}

    async def fake_post(path, json=None, timeout=30.0):
        seen["path"] = path
        seen["json"] = json
        return _Response()

    monkeypatch.setattr(mcp, "_post", fake_post)
    result = asyncio.run(
        mcp._dispatch_tool("palinode_corrections", {"project": "harbor-notes", "since_days": 7})
    )

    assert seen["path"] == "/corrections"
    assert seen["json"] == {"scan": False, "project": "harbor-notes", "since_days": 7}
    assert json.loads(result[0].text)["count"] == 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_emits_json_when_piped(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY-aware like every other command: piped output is machine-readable."""
    class _Unreachable:
        def corrections(self, **_kwargs):
            raise corrections_cli.RequestError("no server")

    monkeypatch.setattr(corrections_cli, "api_client", _Unreachable())
    result = CliRunner().invoke(cli_root, ["corrections"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 12
    assert payload["applied"].startswith("nothing applied")


def test_cli_text_mode_says_nothing_was_applied(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Unreachable:
        def corrections(self, **_kwargs):
            raise corrections_cli.RequestError("no server")

    monkeypatch.setattr(corrections_cli, "api_client", _Unreachable())
    result = CliRunner().invoke(cli_root, ["corrections", "--format", "text"])

    assert result.exit_code == 0, result.output
    # Rich wraps at the terminal width, so compare against flattened output.
    flat = " ".join(result.output.split())
    assert "nothing applied" in flat
    assert "Use the queue worker instead." in flat
    assert "needs_review harbor-notes" in flat


def test_cli_passes_its_filters_through_to_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class _Recorder:
        def corrections(self, **kwargs):
            seen.update(kwargs)
            return {"enabled": True, "count": 0, "candidates": [], "applied": "nothing applied"}

    monkeypatch.setattr(corrections_cli, "api_client", _Recorder())
    result = CliRunner().invoke(
        cli_root, ["corrections", "--project", "harbor-notes", "--since", "7", "--scan"]
    )

    assert result.exit_code == 0, result.output
    assert seen == {"project": "harbor-notes", "since_days": 7, "scan": True}


def test_cli_reports_a_disabled_source_rather_than_an_empty_list(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.capture.transcripts, "enabled", False)

    class _Unreachable:
        def corrections(self, **_kwargs):
            raise corrections_cli.RequestError("no server")

    monkeypatch.setattr(corrections_cli, "api_client", _Unreachable())
    result = CliRunner().invoke(cli_root, ["corrections", "--format", "text"])

    assert "disabled" in result.output


# ── no surface may switch classification on for one call ────────────────────


def test_no_surface_exposes_a_classify_parameter() -> None:
    """The config decides; a request body does not get a vote.

    A per-call override would let any caller transmit conversation text that the
    operator's configuration said stays on this machine, so the parameter is
    absent from every schema rather than merely defaulted.
    """
    assert "classify" not in CorrectionsRequest.model_fields
    assert "classify" not in _tools("full")["palinode_corrections"].input_schema["properties"]
    assert "classify" not in {
        opt for param in cli_root.commands["corrections"].params for opt in param.opts
    }
    assert "classify" not in PLUGIN_INDEX.read_text(encoding="utf-8").split(
        'name: "palinode_corrections"'
    )[1].split('{ name: "palinode_corrections" }')[0]


def test_an_injected_classify_field_does_not_make_the_api_call_a_model(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strongest form of the assertion: send the field, count the calls."""
    calls: list[object] = []

    class _Counting:
        def chat_completions(self, messages, **kwargs):  # noqa: ANN001 - test double
            calls.append(messages)
            raise AssertionError("a request body enabled classification")

    monkeypatch.setattr(classify_module, "get_ollama_client", lambda: _Counting())

    with TestClient(app) as client:
        response = client.post(
            "/corrections", json={"scan": True, "classify": True, "classify_enabled": True}
        )

    assert response.status_code == 200
    body = response.json()
    assert calls == []
    assert body["classify_enabled"] is False
    assert body["scan"]["classification_ran"] is False
    assert body["scan"]["classifier"]["summary"] == DETECTION_ONLY


# ── disclosure ──────────────────────────────────────────────────────────────


def test_disclosure_says_nothing_is_read_or_sent_when_disabled(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.capture.transcripts, "enabled", False)
    monkeypatch.setattr(config.capture.transcripts, "harness_paths", {})

    block = disclosure.runtime_disclosure()["transcript_correction_capture"]

    assert block["enabled"] is False
    assert block["reads"] == "no transcript path is read"
    assert block["sends"] == "nothing"
    assert block["sends_to"] is None


def test_disclosure_reports_a_reading_source_that_sends_nothing(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled but not classifying: reading is on, transmission is not.

    The two are separate opt-ins, so the disclosure must not describe traffic
    that a source in this state does not generate.
    """
    monkeypatch.setattr(config.capture.transcripts, "enabled", True)
    monkeypatch.setattr(config.capture.transcripts, "classify", False)
    monkeypatch.setattr(
        config.capture.transcripts, "harness_paths", {"claude-code": [str(FIXTURE_DIR)]}
    )

    block = disclosure.runtime_disclosure()["transcript_correction_capture"]

    assert block["enabled"] is True
    assert block["classify_enabled"] is False
    assert block["sends"].startswith("nothing")
    assert block["sends_to"] is None
    assert "SENDS" not in block["sends"]


def test_disclosure_names_the_source_and_the_destination_when_classifying(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint may be remote, so the disclosure has to name it."""
    monkeypatch.setattr(config.capture.transcripts, "enabled", True)
    monkeypatch.setattr(config.capture.transcripts, "classify", True)
    monkeypatch.setattr(
        config.capture.transcripts, "harness_paths", {"claude-code": [str(FIXTURE_DIR)]}
    )
    monkeypatch.setattr(
        config.consolidation, "llm_url", "https://user:token@models.example.test/v1?key=x"
    )

    block = disclosure.runtime_disclosure()["transcript_correction_capture"]

    assert block["enabled"] is True
    assert block["classify_enabled"] is True
    assert block["harnesses"] == ["claude-code"]
    assert block["configured_path_count"] == 1
    assert "SENDS" in block["sends"]
    assert "leaves the machine" in block["sends"]
    assert "detection pass sends nothing" in block["sends"]
    # Credentials in a configured URL are redacted like every other destination.
    assert block["sends_to"] == "https://***@models.example.test/v1?***"
    assert "token" not in json.dumps(block)


def test_controls_status_shows_the_send_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    """`palinode controls` must surface the source, per the capture-controls contract."""
    monkeypatch.setattr(config.capture.transcripts, "enabled", True)
    monkeypatch.setattr(config.capture.transcripts, "classify", True)
    monkeypatch.setattr(
        config.capture.transcripts, "harness_paths", {"claude-code": [str(FIXTURE_DIR)]}
    )
    monkeypatch.setattr(config.consolidation, "llm_url", "http://models.example.test:1234")
    status = disclosure.runtime_disclosure()

    class _Stub:
        @staticmethod
        def get_controls() -> dict:
            return {"capture_paused": False, "recall_paused": False}

        @staticmethod
        def get_status() -> dict:
            return status

        @staticmethod
        def check_controls(*_args, **_kwargs) -> dict:
            return {"allowed": True, "project": "harbor-notes"}

    monkeypatch.setattr(controls_cli, "api_client", _Stub())
    result = CliRunner().invoke(cli_root, ["controls", "status", "--format", "text"])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Correction mining: enabled for claude-code" in flat
    assert "Correction mining classification: enabled" in flat
    assert "Correction mining sends:" in flat
    assert "models.example.test:1234" in flat


def test_controls_status_separates_reading_from_classifying(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled source that classifies nothing must not read as transmitting."""
    monkeypatch.setattr(config.capture.transcripts, "enabled", True)
    monkeypatch.setattr(config.capture.transcripts, "classify", False)
    monkeypatch.setattr(
        config.capture.transcripts, "harness_paths", {"claude-code": [str(FIXTURE_DIR)]}
    )
    status = disclosure.runtime_disclosure()

    class _Stub:
        @staticmethod
        def get_controls() -> dict:
            return {"capture_paused": False, "recall_paused": False}

        @staticmethod
        def get_status() -> dict:
            return status

        @staticmethod
        def check_controls(*_args, **_kwargs) -> dict:
            return {"allowed": True, "project": "harbor-notes"}

    monkeypatch.setattr(controls_cli, "api_client", _Stub())
    result = CliRunner().invoke(cli_root, ["controls", "status", "--format", "text"])

    flat = " ".join(result.output.split())
    assert "Correction mining: enabled for claude-code" in flat
    assert "Correction mining classification: disabled" in flat
    assert "Correction mining sends: nothing" in flat
    assert "destination: none" in flat


# ── plugin + registry ───────────────────────────────────────────────────────


def test_the_operation_is_registered_on_all_four_surfaces() -> None:
    operation = parity.by_name("corrections")
    assert parity.required_surfaces(operation) == frozenset({"cli", "mcp", "api", "plugin"})
    assert operation.known_drift == {}
    assert {param.name for param in operation.canonical_params} == {"project", "since_days"}


def test_the_plugin_tool_is_the_same_read_only_listing() -> None:
    """The TypeScript parity suite checks the schema; this checks the behaviour."""
    source = PLUGIN_INDEX.read_text(encoding="utf-8")
    block = re.search(
        r'name: "palinode_corrections".*?\{ name: "palinode_corrections" \}\);',
        source,
        re.DOTALL,
    )
    assert block, "the plugin no longer registers palinode_corrections"
    body = block.group(0)
    assert "scan: false" in body
    assert "/corrections" in body


def test_scan_is_an_operator_capability_by_design() -> None:
    """Asymmetry on purpose: it is not registered, so it is not owed to MCP."""
    operation = parity.by_name("corrections")
    assert "scan" not in {param.name for param in operation.canonical_params}
    assert "scan" not in _tools("full")["palinode_corrections"].input_schema["properties"]
    cli_flags = {opt for param in cli_root.commands["corrections"].params for opt in param.opts}
    assert "--scan" in cli_flags

"""Tests for auto-generated description field on save (M0)."""
from unittest import mock

from palinode.api.server import _generate_description, _extract_first_line


def test_extract_first_line_basic():
    assert _extract_first_line("Hello world\nSecond line") == "Hello world"


def test_extract_first_line_strips_headers():
    assert _extract_first_line("# My Title\nBody text") == "My Title"
    assert _extract_first_line("## Sub heading\nBody") == "Sub heading"


def test_extract_first_line_skips_blank():
    assert _extract_first_line("\n\n  \nActual content") == "Actual content"


def test_extract_first_line_truncates():
    long = "A" * 300
    result = _extract_first_line(long, max_chars=150)
    assert len(result) == 150


def test_extract_first_line_empty():
    assert _extract_first_line("") == ""
    assert _extract_first_line("   \n  \n  ") == ""


def test_generate_description_fallback_on_connection_error():
    """When Ollama is unreachable, falls back to first-line extraction."""
    with mock.patch("palinode.api.server.httpx.post", side_effect=ConnectionError("offline")):
        result = _generate_description("Decision to use SQLite for storage.\nMore details here.")
    assert result == "Decision to use SQLite for storage."


def test_generate_description_uses_llm_when_available():
    """When Ollama responds, uses the LLM description."""
    mock_resp = mock.Mock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = mock.Mock()
    mock_resp.json.return_value = {"response": "A decision about database storage."}

    with mock.patch("palinode.api.server.httpx.post", return_value=mock_resp):
        result = _generate_description("Decision to use SQLite for storage.\nMore details here.")
    assert result == "A decision about database storage."


def test_generate_description_truncates_long_llm_response():
    """LLM responses longer than 150 chars get truncated."""
    mock_resp = mock.Mock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = mock.Mock()
    mock_resp.json.return_value = {"response": "X" * 200}

    with mock.patch("palinode.api.server.httpx.post", return_value=mock_resp):
        result = _generate_description("Some content")
    assert len(result) == 150


def test_generate_description_empty_llm_response_falls_back():
    """Empty LLM response triggers first-line fallback."""
    mock_resp = mock.Mock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = mock.Mock()
    mock_resp.json.return_value = {"response": ""}

    with mock.patch("palinode.api.server.httpx.post", return_value=mock_resp):
        result = _generate_description("# Important Decision\nWe chose option A.")
    assert result == "Important Decision"

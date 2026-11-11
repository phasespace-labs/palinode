"""Save denials explain controls without changing them or exposing server data."""

import importlib

import httpx
import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli._api import PalinodeAPI

save_module = importlib.import_module("palinode.cli.save")


@pytest.mark.parametrize(
    ("status", "body", "paused"),
    [
        (403, '{"detail":"capture_paused"}', True),
        (401, '{"detail":"capture_paused"}', False),
        (403, '{"detail":"invalid_token","secret":"server-private"}', False),
        (403, '{"detail":{"reason":"capture_paused"}}', False),
        (403, '["capture_paused"]', False),
        (403, 'null', False),
        (403, '<html>server-private</html>', False),
        (400, '<html>server-private</html>', False),
        (400, '{"secret":"server-private"}', False),
        (400, '{"detail":[""],"secret":"server-private"}', False),
        (422, '{"detail":[{"loc":["body"]}],"secret":"server-private"}', False),
        (500, '{"detail":"capture_paused"}', False),
    ],
)
def test_save_denial_is_specific_and_never_resumes(
    monkeypatch: pytest.MonkeyPatch, status: int, body: str, paused: bool
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, content=body)

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        monkeypatch.setattr(save_module, "api_client", PalinodeAPI(client=client))
        result = CliRunner().invoke(
            main, ["save", "--type", "Decision", "fictional-private-content"]
        )

    assert result.exit_code != 0
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/save")
    ]
    assert "fictional-private-content" not in result.output
    assert "server-private" not in result.output
    assert "http://test" not in result.output
    if paused:
        assert "capture_paused" in result.output
        assert "capture is paused" in result.output
        assert "palinode controls status" in result.output
        assert "palinode controls resume --capture --no-recall" in result.output
    else:
        assert f"API returned {status}" in result.output
        assert "capture_paused" not in result.output
        assert "controls resume" not in result.output

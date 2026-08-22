"""Import-boundary regressions for the CLI package."""

from __future__ import annotations

import subprocess
import sys


def test_cli_import_does_not_pull_in_fastapi() -> None:
    """A plain CLI import must not initialize the API dependency tree."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import palinode.cli; "
                "print('fastapi' in sys.modules); "
                "print('palinode.api.server' in sys.modules)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["False", "False"]

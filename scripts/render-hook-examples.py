#!/usr/bin/env python3
"""Regenerate ``examples/hooks/`` from the packaged hooks (ADR-023 §1).

The hook scripts live under ``palinode/hooks/scripts/``. The copies in
``examples/hooks/`` are for reading and manual installs, and are generated:
the six scripts byte for byte, ``settings.json`` (the Claude Code
registration ``palinode init --hook`` writes) and ``codex-hooks.json`` (the
Codex registration ``palinode init --codex-hook`` writes), rendered from the
harness specs in ``palinode/hooks/``.

    uv run python scripts/render-hook-examples.py          # rewrite in place
    uv run python scripts/render-hook-examples.py --check  # exit 1 if stale

``tests/test_packaged_hook_scripts.py`` fails when the committed copies are
stale. ``examples/hooks/README.md`` is hand-written and not touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from palinode.hooks import packaged_scripts  # noqa: E402
from palinode.hooks.claude import CLAUDE  # noqa: E402
from palinode.hooks.codex import CODEX  # noqa: E402

EXAMPLES = _REPO_ROOT / "examples" / "hooks"


def _json(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def render() -> dict[Path, str]:
    """Every generated example file and its content."""
    files = {EXAMPLES / name: text for name, text in packaged_scripts().items()}
    # Neither harness's command names the project's path, so any directory renders the same.
    files[EXAMPLES / "settings.json"] = _json(CLAUDE.registration_block(Path(".")))
    files[EXAMPLES / "codex-hooks.json"] = _json(CODEX.registration_block(Path(".")))
    return files


def stale() -> list[Path]:
    """The generated files whose committed copy differs from the render."""
    return [
        path for path, text in render().items()
        if not path.is_file() or path.read_text(encoding="utf-8") != text
    ]


def main(argv: list[str]) -> int:
    if "--check" in argv:
        behind = stale()
        for path in behind:
            print(f"stale: {path.relative_to(_REPO_ROOT)}", file=sys.stderr)
        if behind:
            print("run: uv run python scripts/render-hook-examples.py", file=sys.stderr)
        return 1 if behind else 0
    for path, text in render().items():
        path.write_text(text, encoding="utf-8")
        if path.suffix == ".sh":
            path.chmod(0o755)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

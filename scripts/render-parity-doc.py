#!/usr/bin/env python3
"""Regenerate the operation-spec tables in docs/PARITY.md (ADR-022 §9).

    uv run python scripts/render-parity-doc.py          # rewrite in place
    uv run python scripts/render-parity-doc.py --check  # exit 1 if stale

``tests/test_operation_spec.py`` fails when the committed file is stale.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from palinode.core.config import Config  # noqa: E402
from palinode.core.operations import SPECS, render_param_table  # noqa: E402

DOC = _REPO_ROOT / "docs" / "PARITY.md"
BEGIN = "<!-- BEGIN GENERATED: operation specs (scripts/render-parity-doc.py) -->"
END = "<!-- END GENERATED: operation specs -->"


def _out_of_box(key: str):
    value = Config()
    for part in key.split("."):
        value = getattr(value, part)
    return value


def rendered(text: str) -> str:
    """*text* with the generated block replaced by the current specs."""
    head, sep, rest = text.partition(BEGIN)
    if not sep:
        raise SystemExit(f"{DOC} has no {BEGIN!r} marker")
    _old, sep, tail = rest.partition(END)
    if not sep:
        raise SystemExit(f"{DOC} has no {END!r} marker")
    body = "\n".join(render_param_table(spec, _out_of_box) for spec in SPECS)
    return f"{head}{BEGIN}\n\n{body}\n{END}{tail}"


def main(argv: list[str]) -> int:
    text = DOC.read_text(encoding="utf-8")
    new = rendered(text)
    if "--check" in argv:
        if new != text:
            print(f"{DOC} is stale: run scripts/render-parity-doc.py", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

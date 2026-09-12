"""``palinode/core/parity.py`` must import nothing outside the standard library.

`plugin/test/parity.test.ts` regenerates `plugin/parity-registry.json` from
`core/parity.py` at `pretest`, using a bare `python3` inside the `plugin-tests`
lane of `.github/workflows/plugin-tests.yml` — a Node-only job, present on
both the development and the public repo, where none of palinode's Python
dependencies are installed. A third-party import in `parity.py` — direct, or
transitive through another palinode module — therefore fails the TypeScript
half of the ADR-010 parity contract with a `ModuleNotFoundError` that has
nothing to do with parity.

This is not hypothetical. Registering the `epistemic` param initially pulled
`VALID_EPISTEMICS` in via `from palinode.core.parser import ...`; `parser.py`
imports `frontmatter`, and `plugin-tests` went red with
`ModuleNotFoundError: No module named 'frontmatter'` while every Python job
stayed green — because the Python jobs install the dependencies and the plugin
job does not. The enums moved into `parity.py` instead, and this test keeps
them there.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PARITY_PY = Path(__file__).parent.parent / "palinode" / "core" / "parity.py"


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` / `from .foo import x` have level > 0 and are
            # intra-package — those are exactly what we must forbid here, since
            # any palinode module may pull in a third-party dependency.
            if node.level:
                modules.add("palinode")
            elif node.module:
                modules.add(node.module.split(".")[0])
    return modules


def test_parity_imports_only_stdlib() -> None:
    imported = _imported_top_level_modules(PARITY_PY)
    non_stdlib = sorted(m for m in imported if m not in sys.stdlib_module_names)

    assert not non_stdlib, (
        f"palinode/core/parity.py imports non-stdlib module(s): {non_stdlib}.\n"
        "It must stay importable with a bare `python` — the plugin's parity "
        "pretest regenerates parity-registry.json from it in a Node-only CI job "
        "with no Python dependencies installed. Define the value in parity.py "
        "and re-export it from the consumer, rather than importing the consumer."
    )


def test_parity_imports_under_a_bare_interpreter() -> None:
    """Belt-and-braces: actually import it in a subprocess with no site-packages.

    The AST check above catches the common case. This catches the one it
    cannot: an import added at runtime, or a stdlib-looking name shadowed by a
    third-party package.
    """
    import subprocess

    repo_root = PARITY_PY.parent.parent.parent
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import palinode.core.parity as p; print(len(p.REGISTRY))"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "palinode/core/parity.py failed to import under `python -S` "
        f"(no site-packages):\n{result.stderr}"
    )
    assert result.stdout.strip().isdigit()

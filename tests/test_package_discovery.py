from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_all_python_packages_are_listed_for_wheel_build() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    configured = set(pyproject["tool"]["setuptools"]["packages"])

    discovered = {
        ".".join(path.relative_to(ROOT).parts)
        for path in (ROOT / "palinode").rglob("*")
        if path.is_dir() and (path / "__init__.py").exists()
    }
    discovered.add("palinode")

    missing = sorted(discovered - configured)
    assert missing == []

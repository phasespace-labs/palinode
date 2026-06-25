"""Registry drift guard for #313.

server.json is the MCP marketplace manifest submitted via `mcp-publisher publish`.
Its top-level `version` field and the `packages[*].version` fields must match
pyproject.toml[project.version] exactly, or the published manifest ships a wrong
version — users get the wrong package pinned, and there is no runtime error to
surface it.

Nothing in the build pipeline asserted this. This test fails CI on divergence.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _load_pyproject_version() -> str:
    """Return the canonical version from pyproject.toml.

    Raises FileNotFoundError or tomllib.TOMLDecodeError loud — we want hard
    failure, not a silent pass on a missing or corrupt file.
    """
    pyproject_path = REPO_ROOT / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(
            f"pyproject.toml not found at {pyproject_path}. "
            "Cannot verify version alignment."
        )
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    version = data.get("project", {}).get("version")
    if version is None:
        raise ValueError(
            "pyproject.toml has no [project].version field. "
            "Cannot verify version alignment."
        )
    return version


def _registry_version(version: str) -> str:
    """Strip a PEP 440 local-version segment (``+internal``) for registry comparison.

    `server.json` is the public MCP-registry manifest; a local-version label like
    ``0.8.13+internal`` must never appear there (``mcp-publisher`` would ship a
    malformed/unpublishable version). Internal builds legitimately set a ``+local``
    segment in ``pyproject.toml``, so the drift guard compares the *public* version
    (everything before ``+``) and lets the local label diverge. See #454.
    """
    return version.split("+", 1)[0]


def _load_server_json() -> dict:
    """Return parsed server.json.

    Raises FileNotFoundError or json.JSONDecodeError loud.
    """
    server_json_path = REPO_ROOT / "server.json"
    if not server_json_path.exists():
        raise FileNotFoundError(
            f"server.json not found at {server_json_path}. "
            "Cannot verify version alignment."
        )
    return json.loads(server_json_path.read_text())


def test_server_json_top_level_version_matches_pyproject():
    """server.json top-level `version` must equal pyproject.toml[project.version].

    This is the primary field `mcp-publisher` uses when constructing the
    registry manifest. Drift here means the published listing shows the wrong
    version while users actually receive a different package pin.
    """
    pyproject_version = _load_pyproject_version()
    manifest = _load_server_json()

    server_version = manifest.get("version")
    assert server_version is not None, (
        "server.json has no top-level `version` field. "
        "Add it or the registry manifest is malformed. See #313."
    )
    assert server_version == _registry_version(pyproject_version), (
        f"server.json version ({server_version!r}) does not match the public "
        f"(local-segment-stripped) pyproject.toml version "
        f"({_registry_version(pyproject_version)!r}, from {pyproject_version!r}). "
        "Update server.json to match — it is the MCP registry manifest. "
        "A mismatch causes `mcp-publisher publish` to ship a wrong-version entry. "
        "See #313."
    )


def test_server_json_package_versions_match_pyproject():
    """Every packages[*].version in server.json must equal pyproject.toml version.

    server.json has multiple package entries (stdio + streamable-http transports).
    Each carries its own `version` pin. All must stay in sync with pyproject.toml.
    """
    pyproject_version = _load_pyproject_version()
    manifest = _load_server_json()

    packages = manifest.get("packages", [])
    assert packages, (
        "server.json has an empty or missing `packages` array. "
        "The registry manifest is likely malformed. See #313."
    )

    for i, pkg in enumerate(packages):
        pkg_version = pkg.get("version")
        assert pkg_version is not None, (
            f"server.json packages[{i}] has no `version` field. "
            f"Package entry: {pkg!r}. See #313."
        )
        assert pkg_version == _registry_version(pyproject_version), (
            f"server.json packages[{i}].version ({pkg_version!r}) does not match "
            f"the public pyproject.toml version "
            f"({_registry_version(pyproject_version)!r}, from {pyproject_version!r}). "
            f"Package identifier: {pkg.get('identifier', '<unknown>')}. "
            "Update all package version pins in server.json to match pyproject.toml. "
            "See #313."
        )


def test_server_json_is_valid_json():
    """server.json must be parseable JSON — a corrupt file means no version check fires.

    Fail loud here rather than silently skipping the version assertions above.
    """
    server_json_path = REPO_ROOT / "server.json"
    assert server_json_path.exists(), (
        f"server.json not found at {server_json_path}. "
        "This file is the MCP registry manifest and must exist in the repo. See #313."
    )
    try:
        json.loads(server_json_path.read_text())
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"server.json is not valid JSON: {exc}. "
            "Fix the file before any version checks can pass. See #313."
        ) from exc


def test_pyproject_toml_is_parseable():
    """pyproject.toml must be parseable TOML with a [project].version field.

    Fail loud — if pyproject.toml is missing or corrupt, version alignment
    cannot be verified at all and the other tests would silently error.
    """
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert pyproject_path.exists(), (
        f"pyproject.toml not found at {pyproject_path}. "
        "This is the canonical version source. See #313."
    )
    try:
        with pyproject_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        raise AssertionError(
            f"pyproject.toml failed to parse: {exc}. "
            "Fix the file before version alignment can be verified. See #313."
        ) from exc
    assert "project" in data, (
        "pyproject.toml has no [project] table. Cannot read version. See #313."
    )
    assert "version" in data["project"], (
        "pyproject.toml [project] table has no `version` key. "
        "Cannot determine canonical version. See #313."
    )

"""The hook scripts are package files, and their version stamps stay honest (ADR-023).

* ``examples/hooks/`` is generated from the package (``scripts/render-hook-examples.py``)
  and must not be stale.
* ``palinode/hooks/shipped-hashes.json`` records every shipped content. The
  current one must be recorded, and its stamp must not name a version a
  release shipped with other content: a changed script needs a new stamp.
* The regeneration script is a fixed point and never drops an entry.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from palinode.hooks import (
    SCRIPT_NAMES,
    content_sha256,
    hook_version,
    packaged_scripts,
    shipped_hashes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER = REPO_ROOT / "scripts" / "render-hook-examples.py"
REGEN = REPO_ROOT / "scripts" / "regen-hook-hashes.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_script_is_packaged():
    on_disk = {p.name for p in (REPO_ROOT / "palinode" / "hooks" / "scripts").glob("*.sh")}
    assert on_disk == set(SCRIPT_NAMES)
    for name, text in packaged_scripts().items():
        assert text.startswith("#!"), name


def test_examples_are_generated_from_the_package():
    stale = _load(RENDER, "render_hook_examples").stale()
    assert not stale, (
        f"stale: {[str(p.relative_to(REPO_ROOT)) for p in stale]}. "
        "Edit palinode/hooks/scripts/, then run `uv run python scripts/render-hook-examples.py`."
    )


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_current_content_is_recorded_under_its_stamp(name):
    text = packaged_scripts()[name]
    sha, stamp = content_sha256(text), hook_version(text)
    entries = shipped_hashes()["scripts"][name]
    recorded = {e["sha256"]: e for e in entries}
    assert sha in recorded, (
        f"{name}'s content is not in shipped-hashes.json. "
        "Run `uv run python scripts/regen-hook-hashes.py`."
    )
    assert recorded[sha]["version"] == stamp


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_a_changed_script_carries_a_new_stamp(name):
    text = packaged_scripts()[name]
    sha, stamp = content_sha256(text), hook_version(text)
    shipped_other = [
        e["released"] for e in shipped_hashes()["scripts"][name]
        if e["version"] == stamp and e["released"] and e["sha256"] != sha
    ]
    assert not shipped_other, (
        f"{name} changed, but its stamp {stamp} names a version release "
        f"{shipped_other[0]} shipped with other content. Bump `# palinode-hook-version:`."
    )


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_the_stamp_names_a_real_release(name):
    """A stamp names the release in which the script last changed.

    Content a release shipped is stamped no later than that release, so an
    installed copy never reports a version that did not exist yet. Content no
    release has shipped yet names an upcoming one: later than every release
    the manifest records.
    """
    text = packaged_scripts()[name]
    stamp = hook_version(text)
    entry = {e["sha256"]: e for e in shipped_hashes()["scripts"][name]}[content_sha256(text)]
    if entry["released"]:
        assert _version(stamp) <= _version(entry["released"]), (
            f"{name} is stamped {stamp}, but release {entry['released']} shipped it."
        )
    else:
        latest = max(
            _version(e["released"])
            for entries in shipped_hashes()["scripts"].values()
            for e in entries if e["released"]
        )
        assert _version(stamp) > latest, (
            f"{name} changed since the last release, so its stamp must name an upcoming "
            f"one (later than {'.'.join(map(str, latest))}), not {stamp}."
        )


def test_manifest_shape():
    manifest = shipped_hashes()
    assert manifest["schema"] == 1
    assert set(manifest["scripts"]) == set(SCRIPT_NAMES)
    for name, entries in manifest["scripts"].items():
        shas = [e["sha256"] for e in entries]
        assert len(shas) == len(set(shas)), name
        for entry in entries:
            assert set(entry) == {"sha256", "version", "released"}, name


def test_pyproject_ships_the_scripts_and_manifest():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"palinode.hooks" = ["scripts/*.sh", "shipped-hashes.json"]' in text
    assert '"palinode.hooks",' in text


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists() or not REGEN.is_file(),
    reason="needs the source checkout the script reads hook history from",
)
def test_regeneration_is_a_fixed_point_that_keeps_every_entry(tmp_path):
    committed = shipped_hashes()
    # An entry this history has never seen (a revision only another clone has).
    seeded = json.loads(json.dumps(committed))
    foreign = {"sha256": "0" * 64, "version": "0.0.1", "released": "0.0.1"}
    seeded["scripts"]["palinode-session-end.sh"].append(foreign)
    output = tmp_path / "manifest.json"
    output.write_text(json.dumps(seeded), encoding="utf-8")

    subprocess.run(
        [sys.executable, str(REGEN), "--output", str(output)],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )
    regenerated = json.loads(output.read_text(encoding="utf-8"))

    assert foreign in regenerated["scripts"]["palinode-session-end.sh"]
    regenerated["scripts"]["palinode-session-end.sh"].remove(foreign)
    assert regenerated == committed, (
        "shipped-hashes.json is not what history says. "
        "Run `uv run python scripts/regen-hook-hashes.py`."
    )

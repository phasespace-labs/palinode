"""
Check: env_vs_yaml_consistency

Compares environment-variable overrides against the values actually written
in `palinode.config.yaml`.  After `load_config()` runs, env vars silently
win over YAML — so editing YAML and forgetting to unset the env var is a
common, hard-to-spot misconfiguration.

We only warn when the YAML *explicitly* sets a non-default value that the
env var shadows.  Bare defaults aren't worth a warning: the env override is
the only thing the operator chose.

Linked: design doc Section 3 — "env_vs_yaml_consistency".
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


# Pairs of (env-var name, dotted YAML key path, human-readable label).
# Keep this aligned with the env-override block in palinode/core/config.py
# (`load_config()` after the YAML merge).
_ENV_YAML_PAIRS: list[tuple[str, tuple[str, ...], str]] = [
    ("PALINODE_DIR", ("memory_dir",), "memory_dir"),
    ("OLLAMA_URL", ("embeddings", "primary", "url"), "embeddings.primary.url"),
    ("EMBEDDING_MODEL", ("embeddings", "primary", "model"), "embeddings.primary.model"),
]


def _candidate_yaml_paths(ctx: DoctorContext) -> list[Path]:
    """Return the YAML paths `load_config()` would consult, in order."""
    paths: list[Path] = []

    # 1) Repo-root config (next to the `palinode/` package)
    try:
        import palinode  # type: ignore

        pkg_dir = Path(palinode.__file__).resolve().parent
        repo_root = pkg_dir.parent
        paths.append(repo_root / "palinode.config.yaml")
    except Exception:
        pass

    # 2) ${PALINODE_DIR or memory_dir}/palinode.config.yaml
    pal_dir = os.environ.get("PALINODE_DIR") or ctx.config.memory_dir
    if pal_dir:
        paths.append(Path(os.path.expanduser(pal_dir)) / "palinode.config.yaml")

    return paths


def _read_yaml(path: Path) -> dict[str, Any] | None:
    """Read and parse a YAML file; return None on any failure."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _dig(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Walk a nested dict via *keys*; return None if any segment is missing."""
    cur: Any = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _normalize(label: str, value: str) -> str:
    """Normalize values for comparison — paths get expanded + resolved."""
    if label == "memory_dir":
        try:
            return str(Path(os.path.expanduser(value)).resolve())
        except (OSError, RuntimeError):
            return os.path.expanduser(value)
    return value.strip()


@register(tags=("fast",))
def env_vs_yaml_consistency(ctx: DoctorContext) -> CheckResult:
    """Warn when an env var silently shadows a non-default YAML value."""

    yaml_paths = _candidate_yaml_paths(ctx)
    loaded: tuple[Path, dict[str, Any]] | None = None
    for p in yaml_paths:
        if p.exists():
            data = _read_yaml(p)
            if data is not None:
                loaded = (p, data)
                break

    if loaded is None:
        return CheckResult(
            name="env_vs_yaml_consistency",
            severity="info",
            passed=True,
            message=(
                "No palinode.config.yaml found; env vars are the only source. "
                "Searched: " + ", ".join(str(p) for p in yaml_paths)
            ),
            remediation=None,
        )

    yaml_path, yaml_data = loaded
    drifts: list[tuple[str, str, str, str]] = []  # (env_var, label, env_val, yaml_val)

    for env_var, key_path, label in _ENV_YAML_PAIRS:
        env_val = os.environ.get(env_var)
        if env_val is None:
            continue  # env not set → no shadow possible
        yaml_val = _dig(yaml_data, key_path)
        if yaml_val is None:
            continue  # YAML doesn't set it → bare default, env is the only choice
        if not isinstance(yaml_val, str):
            continue  # we only diff string-valued config knobs here
        if _normalize(label, env_val) != _normalize(label, str(yaml_val)):
            drifts.append((env_var, label, env_val, str(yaml_val)))

    if not drifts:
        return CheckResult(
            name="env_vs_yaml_consistency",
            severity="info",
            passed=True,
            message=(
                f"Env vars and {yaml_path} agree on all observed keys "
                f"(checked: {', '.join(label for _, _, label in _ENV_YAML_PAIRS)})."
            ),
            remediation=None,
        )

    lines: list[str] = []
    for env_var, label, env_val, yaml_val in drifts:
        lines.append(f"  {env_var} (env)  = {env_val}")
        lines.append(f"  {label} (yaml) = {yaml_val}")
    summary = "; ".join(f"{ev}↯{lab}" for ev, lab, _, _ in drifts)

    remediation_lines = [
        f"Env always wins after load_config(). To resolve:",
        f"  - Edit {yaml_path} to match the env var, OR",
        f"  - `unset {' '.join(d[0] for d in drifts)}` in the shell that starts palinode.",
        "",
        "Conflicts:",
        *lines,
    ]

    return CheckResult(
        name="env_vs_yaml_consistency",
        severity="warn",
        passed=False,
        message=f"{len(drifts)} env-var override shadows non-default YAML ({summary}).",
        remediation="\n".join(remediation_lines),
    )

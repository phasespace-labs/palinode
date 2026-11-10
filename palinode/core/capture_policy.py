"""Persistent, fail-closed controls for automatic capture and recall.

The policy deliberately lives beside the store rather than in a process-global
setting.  Hooks can ask for a preflight decision before they read a transcript,
and API handlers load it again immediately before an automatic side effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import threading
from typing import Literal

import yaml

from palinode.core.config import config
from palinode.core import git_tools


POLICY_FILENAME = ".capture-policy.yaml"
POLICY_VERSION = 1
_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_UPDATE_LOCK = threading.RLock()


class CapturePolicyError(ValueError):
    """The persisted policy or a policy input is not safe to use."""


@dataclass(frozen=True)
class CapturePolicy:
    capture_paused: bool = False
    recall_paused: bool = False
    excluded_projects: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    updated_at: str | None = None
    provenance: str | None = None

    def public(self, *, committed: bool | None = None) -> dict[str, object]:
        result: dict[str, object] = {
            "policy_version": POLICY_VERSION,
            "capture_paused": self.capture_paused,
            "recall_paused": self.recall_paused,
            "excluded_projects": list(self.excluded_projects),
            "excluded_paths": list(self.excluded_paths),
            "updated_at": self.updated_at,
            "provenance": self.provenance,
        }
        if committed is not None:
            result["committed"] = committed
        return result


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str | None
    policy_version: int = POLICY_VERSION
    project: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "project": self.project,
        }


def _policy_path() -> Path:
    return Path(config.memory_dir) / POLICY_FILENAME


def _policy_lock_path() -> Path:
    return Path(config.memory_dir) / ".capture-policy.lock"


def _assert_regular_policy_file(path: Path) -> bool:
    """Return whether a real policy file exists, rejecting every symlink.

    ``Path.exists`` is deliberately not used here: it returns ``False`` for a
    dangling symlink, which used to let an update replace a policy link after
    treating it as an absent file.  Controls must never read through or write
    through a policy symlink, including a dangling one.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CapturePolicyError("Invalid capture policy") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise CapturePolicyError("Invalid capture policy")
    return True


@contextmanager
def _policy_update_lock():
    """Serialize policy read-modify-write across threads and processes."""
    with _UPDATE_LOCK:
        lock_path = _policy_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise CapturePolicyError("Capture policy lock is unavailable") from exc
        try:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows has the thread lock.
                fcntl = None
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if 'fcntl' in locals() and fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _validate_project(value: object) -> str:
    if not isinstance(value, str) or not _PROJECT_RE.fullmatch(value):
        raise CapturePolicyError("Invalid project exclusion")
    return value


def _normalize_path(value: object) -> str:
    """Return a canonical absolute external path without following a symlink input.

    The caller may exclude a directory which does not exist yet, but may not
    smuggle a future exclusion through ``..`` or any current symlink component.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CapturePolicyError("Invalid path exclusion")
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts:
        raise CapturePolicyError("Invalid path exclusion")
    probe = Path(raw.anchor)
    try:
        for part in raw.parts[1:]:
            probe /= part
            # ``os.lstat``, not ``Path.is_symlink()``: from Python 3.14 the
            # latter returns False on any OSError, so a component this process
            # cannot inspect (EACCES) would pass as verified. Only a missing
            # component is allowed through, since an exclusion may name a
            # directory that does not exist yet.
            try:
                mode = os.lstat(probe).st_mode
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_ISLNK(mode):
                raise CapturePolicyError("Invalid path exclusion")
        return str(raw.resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        raise CapturePolicyError("Invalid path exclusion") from exc


def _string_list(value: object, normalizer, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CapturePolicyError(f"Invalid {label}")
    return tuple(sorted(set(normalizer(item) for item in value)))


def _from_mapping(raw: object) -> CapturePolicy:
    if not isinstance(raw, dict):
        raise CapturePolicyError("Invalid capture policy")
    expected = {
        "version", "capture_paused", "recall_paused", "excluded_projects",
        "excluded_paths", "updated_at", "provenance",
    }
    if set(raw) != expected or raw.get("version") != POLICY_VERSION:
        raise CapturePolicyError("Invalid capture policy")
    if type(raw["capture_paused"]) is not bool or type(raw["recall_paused"]) is not bool:
        raise CapturePolicyError("Invalid capture policy")
    updated_at = raw["updated_at"]
    provenance = raw["provenance"]
    if updated_at is not None and not isinstance(updated_at, str):
        raise CapturePolicyError("Invalid capture policy")
    if provenance != "api_controls":
        raise CapturePolicyError("Invalid capture policy")
    return CapturePolicy(
        capture_paused=raw["capture_paused"],
        recall_paused=raw["recall_paused"],
        excluded_projects=_string_list(raw["excluded_projects"], _validate_project, "project exclusions"),
        excluded_paths=_string_list(raw["excluded_paths"], _normalize_path, "path exclusions"),
        updated_at=updated_at,
        provenance=provenance,
    )


def load_capture_policy() -> CapturePolicy:
    """Load the policy on every call; a malformed persisted policy is explicit."""
    path = _policy_path()
    if not _assert_regular_policy_file(path):
        return CapturePolicy()
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as handle:
            return _from_mapping(yaml.safe_load(handle))
    except (OSError, yaml.YAMLError, CapturePolicyError) as exc:
        if isinstance(exc, CapturePolicyError):
            raise
        raise CapturePolicyError("Invalid capture policy") from exc


def update_capture_policy(
    *,
    capture_paused: bool | None = None,
    recall_paused: bool | None = None,
    excluded_projects: list[str] | None = None,
    excluded_paths: list[str] | None = None,
) -> tuple[CapturePolicy, bool]:
    """Persist an explicit operator mutation and commit only its policy file."""
    if capture_paused is not None and type(capture_paused) is not bool:
        raise CapturePolicyError("Invalid capture pause value")
    if recall_paused is not None and type(recall_paused) is not bool:
        raise CapturePolicyError("Invalid recall pause value")
    with _policy_update_lock():
        path = _policy_path()
        # Check while holding the shared update lock, before either the read
        # or the atomic writer can touch a policy target.
        _assert_regular_policy_file(path)
        current = load_capture_policy()
        projects = current.excluded_projects if excluded_projects is None else _string_list(
            excluded_projects, _validate_project, "project exclusions"
        )
        paths = current.excluded_paths if excluded_paths is None else _string_list(
            excluded_paths, _normalize_path, "path exclusions"
        )
        policy = CapturePolicy(
            capture_paused=current.capture_paused if capture_paused is None else capture_paused,
            recall_paused=current.recall_paused if recall_paused is None else recall_paused,
            excluded_projects=projects,
            excluded_paths=paths,
            updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            provenance="api_controls",
        )
        payload = {
            "version": POLICY_VERSION,
            "capture_paused": policy.capture_paused,
            "recall_paused": policy.recall_paused,
            "excluded_projects": list(policy.excluded_projects),
            "excluded_paths": list(policy.excluded_paths),
            "updated_at": policy.updated_at,
            "provenance": policy.provenance,
        }
        git_tools.write_memory_file(str(path), yaml.safe_dump(payload, sort_keys=False))
        committed = git_tools.commit_memory_file(
            str(path), f"{config.git.commit_prefix} update capture policy"
        )
        return policy, committed


def _matches_excluded_path(value: str, exclusions: tuple[str, ...]) -> bool:
    candidate = Path(value)
    return any(candidate.is_relative_to(Path(exclusion)) for exclusion in exclusions)


@dataclass(frozen=True)
class ResolvedProcessingPolicy:
    """Project exclusions and alias comparisons fixed for one processing pass."""

    deny_all: bool
    excluded_projects: frozenset[str]
    alias_index: dict[str, str]


def resolve_processing_policy() -> ResolvedProcessingPolicy:
    """Load fresh controls once, without loading aliases when none are excluded."""
    from palinode.core.scope import alias_index, canonical_project

    try:
        policy = load_capture_policy()
    except CapturePolicyError:
        return ResolvedProcessingPolicy(True, frozenset(), {})
    if not policy.excluded_projects:
        return ResolvedProcessingPolicy(False, frozenset(), {})
    index = alias_index()
    excluded = frozenset(canonical_project(p, index) for p in policy.excluded_projects)
    return ResolvedProcessingPolicy(False, excluded, index)


def automatic_note_processing_allowed(
    metadata: dict, *, project_refs: tuple[str, ...] | list[str] = (),
    resolved: ResolvedProcessingPolicy | None = None,
) -> bool:
    """Project exclusions gate model processing of stored notes, not indexing.

    Reuse recall's entity and alias comparisons. Consolidation can also supply
    the project refs its existing grouping logic resolves from daily notes or
    a target document's identity. Capture pauses and source-path exclusions
    deliberately do not govern stored-note processing.
    """
    from palinode.core.scope import canonical_project, project_entities

    policy = resolved if resolved is not None else resolve_processing_policy()
    if policy.deny_all:
        return False
    if not policy.excluded_projects:
        return True
    return not any(
        canonical_project(ref, policy.alias_index) in policy.excluded_projects
        for ref in [*project_entities(metadata), *project_refs]
    )


def evaluate_capture_policy(
    action: Literal["capture", "recall"],
    *,
    automatic: bool,
    cwd: str | None = None,
    project: str | None = None,
    source_path: str | None = None,
) -> PolicyDecision:
    """Allow explicit operations, but fail closed before automatic side effects."""
    if action not in {"capture", "recall"}:
        return PolicyDecision(False, "invalid_action")
    try:
        policy = load_capture_policy()
    except CapturePolicyError:
        # The route middleware cannot safely parse a body just to decide which
        # caller is automatic.  Denying all controlled routes is the only way
        # to guarantee an automatic hook never slips through a corrupt store.
        return PolicyDecision(False, "invalid_policy")
    if (action == "capture" and policy.capture_paused) or (
        action == "recall" and policy.recall_paused
    ):
        return PolicyDecision(False, f"{action}_paused")
    if not automatic:
        return PolicyDecision(True, None)
    try:
        normalized_project = _validate_project(project) if project is not None else None
    except CapturePolicyError:
        return PolicyDecision(False, "invalid_scope")
    # The paths are the caller's (a hook's cwd and transcript), so this server
    # may be unable to inspect them: another host, or a directory its user
    # cannot read (``/root`` or a 0700 home raises EACCES). An unverifiable
    # path only matters when there is an exclusion to check it against; with
    # none configured it is dropped, never resolved through.
    normalized_cwd: str | None = None
    candidates: list[str] = []
    unverifiable = False
    for is_cwd, value in ((True, cwd), (False, source_path)):
        if value is None:
            continue
        try:
            normalized = _normalize_path(value)
        except CapturePolicyError:
            unverifiable = True
            continue
        candidates.append(normalized)
        if is_cwd:
            normalized_cwd = normalized
    if unverifiable and (policy.excluded_paths or policy.excluded_projects):
        return PolicyDecision(False, "invalid_scope")
    if normalized_project is None and normalized_cwd is not None:
        # The preflight is useful to generated hooks precisely when a linked
        # worktree has no separately configured project.  Reuse the canonical
        # git-aware resolver rather than inferring from a basename.
        try:
            from palinode.core.context_prime import resolve_context
            resolved = resolve_context(cwd=normalized_cwd, project=None).project
            normalized_project = resolved.removeprefix("project/") if resolved else None
        except Exception:
            normalized_project = None
    # An automatic caller cannot safely establish that it lies outside a
    # configured exclusion if it supplies no usable scope (or the resolver
    # fails).  Empty policies retain their backwards-compatible allow path.
    if normalized_project in policy.excluded_projects:
        return PolicyDecision(False, "excluded_project", project=normalized_project)
    if any(_matches_excluded_path(value, policy.excluded_paths) for value in candidates):
        return PolicyDecision(False, "excluded_path", project=normalized_project)
    if policy.excluded_projects and normalized_project is None:
        return PolicyDecision(False, "unknown_scope")
    if policy.excluded_paths and not candidates:
        return PolicyDecision(False, "unknown_scope", project=normalized_project)
    return PolicyDecision(True, None, project=normalized_project)


__all__ = [
    "CapturePolicy", "CapturePolicyError", "POLICY_FILENAME", "POLICY_VERSION",
    "ResolvedProcessingPolicy", "resolve_processing_policy",
    "PolicyDecision", "automatic_note_processing_allowed", "evaluate_capture_policy", "load_capture_policy", "update_capture_policy",
]

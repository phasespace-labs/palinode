"""
Types for the palinode doctor diagnostics framework.

Defines the core dataclasses shared by all check modules, the runner,
and the formatters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from palinode.core.config import Config


@dataclass
class CheckResult:
    """Result of a single diagnostic check."""

    name: str
    severity: Literal["info", "warn", "error", "critical"]
    passed: bool
    message: str
    remediation: str | None = None
    linked_issue: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class FixResult:
    """Result of an attempted fix action in doctor ``--fix`` mode.

    Fields
    ------
    applied:
        True if the fix mutated state.  False if it was a no-op (already
        fixed, dry-run, declined, or not applicable).
    message:
        Human-readable summary of what happened.  Used for both stdout
        reporting and INFO-level logging.
    """

    applied: bool
    message: str


@dataclass
class DoctorContext:
    """Bundle of shared state passed to every check function.

    Start simple: just the resolved Config. Later expansions can add
    connection objects, caches, or flags without breaking existing checks.
    """

    config: Config

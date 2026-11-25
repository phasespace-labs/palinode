"""Every harness palinode installs hooks for, in discovery order (ADR-023)."""
from __future__ import annotations

from palinode.hooks.agy import ANTIGRAVITY
from palinode.hooks.claude import CLAUDE
from palinode.hooks.codex import CODEX
from palinode.hooks.cursor import CURSOR

HARNESSES = (CLAUDE, CODEX, CURSOR, ANTIGRAVITY)

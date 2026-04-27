"""
palinode.diagnostics — framework for palinode doctor checks.

Public API (re-exported for convenience):

  from palinode.diagnostics import CheckResult, DoctorContext, register
"""
from palinode.diagnostics.types import CheckResult, DoctorContext, FixResult
from palinode.diagnostics.registry import register, register_fix, get_fix

__all__ = [
    "CheckResult",
    "DoctorContext",
    "FixResult",
    "register",
    "register_fix",
    "get_fix",
]

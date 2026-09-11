"""Every executor op must be named in a prompt, or no model can ever propose it.

The failure this guards is not a broken op — it is a *correct* op nobody can
reach. `PROPOSE_CONTRADICTS` was implemented, tested, documented as shipped, and
named in none of the prompts sent to the model, so it had never run once in
production. An executor arm and the prompt vocabulary can drift apart silently
in exactly one direction, and nothing else in the suite looks at both sides.

Two gates stand between a model and an applied op, so both are asserted: the op
must be *named* in a prompt (the model has to know it exists) and it must be in
the pass's `allowed_ops` default (the runner filters proposals against it).
"""
from __future__ import annotations

import re
from pathlib import Path

import frontmatter
import pytest

from palinode.core.config import ConsolidationConfig, NightlyConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = REPO_ROOT / "palinode" / "consolidation" / "executor.py"
PROMPTS_DIR = REPO_ROOT / "specs" / "prompts"

# The dispatch arms of `apply_operations`, read from the source rather than
# hand-listed: a new arm joins this set the moment it is written, which is the
# point — a hand-listed set would have to be updated by the same person who
# forgot the prompt.
_DISPATCH = re.compile(r'op_type\s*==\s*"([A-Z_]+)"')

# KEEP is the executor's default for an op with no kind. A prompt may name it or
# not; a model that never says KEEP still gets KEEP behaviour, so it is not an
# unreachable arm and is exempt from the naming rule.
_EXEMPT = {"KEEP"}


def _executor_ops() -> set[str]:
    ops = set(_DISPATCH.findall(EXECUTOR.read_text(encoding="utf-8")))
    assert ops, "no dispatch arms found — the source pattern has drifted"
    return ops


def _prompt_texts() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PROMPTS_DIR.glob("*.md"))
    }


def test_every_executor_op_is_named_in_some_prompt() -> None:
    """The generalizable gate: an op no prompt names can never be proposed."""
    prompts = _prompt_texts()
    unreachable = {
        op: sorted(prompts)
        for op in sorted(_executor_ops() - _EXEMPT)
        if not any(op in text for text in prompts.values())
    }
    assert not unreachable, (
        "executor op(s) named in no prompt — implemented but unreachable, since "
        "the model is never told they exist: "
        + ", ".join(sorted(unreachable))
    )


@pytest.mark.parametrize("prompt", ["compaction.md", "nightly-consolidation.md"])
def test_propose_contradicts_named_in_consolidation_prompts(prompt: str) -> None:
    """Both consolidation passes must offer the no-winner op by name.

    Pinned per-prompt as well as in aggregate: the aggregate test above would
    still pass if the op survived only in, say, `update.md`, and the pass that
    actually compacts conflicting facts lost it again.
    """
    text = (PROMPTS_DIR / prompt).read_text(encoding="utf-8")
    assert "PROPOSE_CONTRADICTS" in text
    assert "contradicts" in text, "the op is named but its JSON field is not"
    assert "category/slug" in text, (
        "the prompt must state the ref format — a fact id or a title in "
        "`contradicts` is rejected by the executor and the conflict is lost"
    )


def test_propose_contradicts_is_allowed_by_default_on_both_passes() -> None:
    """Naming it in the prompt is half the fix; the runner filters on these."""
    assert "PROPOSE_CONTRADICTS" in ConsolidationConfig().allowed_ops
    assert "PROPOSE_CONTRADICTS" in NightlyConfig().allowed_ops


@pytest.mark.parametrize(
    "prompt,expected_version",
    [("compaction.md", 3), ("nightly-consolidation.md", 2)],
)
def test_consolidation_prompts_declare_a_version(prompt: str, expected_version: int) -> None:
    """A prompt with no `version:` cannot be reported as stale.

    `palinode doctor` compares each store prompt's declared version against the
    packaged copy, so an undeclared version means the one file whose contract
    just changed is the one the check cannot warn about. v1 named the six
    reachable ops; v2 adds `PROPOSE_CONTRADICTS`; compaction v3 makes KEEP
    implicit, so the model emits only ops that change something.
    """
    meta = frontmatter.load(PROMPTS_DIR / prompt).metadata
    assert meta.get("version") == expected_version
    assert meta.get("task"), "a prompt with no `task` cannot be matched to its pass"
    assert meta.get("active") is True

"""``examples/CLAUDE.md`` is the copy-by-hand version of the block ``palinode
init`` writes into a project's CLAUDE.md, so the two must say the same thing.
They drifted once: the example kept telling agents to call
``palinode_session_end`` at every session end after the generated block
learned to skip recall-only sessions and honour a request not to save.
"""
from pathlib import Path

from palinode.cli.init import CLAUDE_MD_BLOCK

EXAMPLE = Path(__file__).parent.parent / "examples" / "CLAUDE.md"


def test_example_carries_the_block_init_writes():
    block = CLAUDE_MD_BLOCK.format(project_slug="<your-project-slug>", wrap_policy_note="")
    assert block in EXAMPLE.read_text(encoding="utf-8")


def test_example_saves_settled_decisions_immediately():
    assert (
        "When a value, ID, name, decision or plan is settled (for example a batch ID "
        "fixed or an approach agreed), save it right away with `palinode_save` as "
        '`type="Decision"`, with the value and why. Don\'t wait for the end of the session.'
    ) in EXAMPLE.read_text(encoding="utf-8")

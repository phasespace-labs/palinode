"""`compaction.md` v3: the model emits only ops that change something.

v2 said "decide what happens to each fact" and opened its example with a
``KEEP``, so the response was O(facts). On the dogfood store — a 449-fact
status document — the model did exactly as asked, ran into
``consolidation.llm_max_tokens = 2000`` after ~100 entries, and returned a JSON
array with no closing ``]``. ``parse_operations`` returned ``[]``, the runner
counted it as "no operations", and a 60 s LLM call that produced nothing usable
was reported identically to a quiet week (the dogfood truncation issue).

The fix is a prompt contract, not an executor change: every fact the model does
not name is kept. ``apply_operations`` already works that way — it iterates
``operations``, never the file's facts
(``executor.py`` ``for op_index, op in enumerate(operations)``), and its
``KEEP`` arm increments a counter and ``continue``s without touching the body.
So an absent id and an explicit ``KEEP`` are the same outcome, and the prompt
can stop asking for the latter.

The measurement here is the judgment call the roadmap flagged: a seeded
300-fact store, a v2-style full-KEEP response truncated at the token cap versus
a v3-style sparse response, both driven through the real
``_consolidate_project`` → ``parse_operations`` → ``apply_operations`` path.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import pytest

from palinode.consolidation import runner
from palinode.consolidation.executor import apply_operations
from palinode.core.config import config

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROMPT = REPO_ROOT / "specs" / "prompts" / "compaction.md"

#: Number of tagged facts in the seeded store. Under the real store's 449 but
#: well past the ~100 at which the observed truncation hit.
SEEDED_FACTS = 300

#: Fact ids the seeded daily notes clearly obsolete — the ops a correct v3
#: response contains and a truncated v2 response never reaches.
OBSOLETED = ("fact-0007", "fact-0042", "fact-0108", "fact-0211")


# ── the prompt contract ────────────────────────────────────────────────────


def _prompt_text() -> str:
    return SOURCE_PROMPT.read_text(encoding="utf-8")


def _output_example() -> str:
    """The ```json block under `## Output Format` — what the model copies."""
    body = _prompt_text().split("## Output Format", 1)[1]
    blocks = re.findall(r"```json\n(.*?)```", body, re.DOTALL)
    assert blocks, "the Output Format section has no json example"
    return blocks[0]


def test_prompt_declares_version_3() -> None:
    """`palinode doctor` and `prompt sync` compare on this number."""
    assert frontmatter.load(SOURCE_PROMPT).metadata["version"] == 3


def test_output_example_proposes_no_keep() -> None:
    """The example is the strongest instruction in the file.

    v2's array opened with ``{"op": "KEEP", "id": "fact_id"}``; a model shown
    that emits one per fact.
    """
    assert '"KEEP"' not in _output_example()


def test_output_example_is_valid_json_with_only_changing_ops() -> None:
    ops = json.loads(_output_example())
    kinds = {op["op"] for op in ops}
    assert kinds == {
        "UPDATE", "MERGE", "SUPERSEDE", "ARCHIVE", "RETRACT",
        "PROPOSE_CONTRADICTS",
    }


def test_prompt_states_the_implicit_keep_contract() -> None:
    text = _prompt_text()
    assert "Every fact you do not name is kept" in text
    assert "Emit an\noperation only for a fact you are changing" in text


def test_prompt_documents_the_empty_array_as_a_complete_answer() -> None:
    """Without this, a model with nothing to do invents work."""
    tail = _prompt_text().split("## Output Format", 1)[1]
    assert "return the empty array" in tail
    assert "```json\n[]\n```" in tail


def test_keep_stays_in_the_vocabulary_as_accepted_but_never_required() -> None:
    """KEEP is still an executor arm and still in ``allowed_ops``.

    Deleting the word would break rules 8 and 10, which say "KEEP it" to mean
    "leave it alone", and would make a store whose operator-tuned prompt still
    emits KEEP look like it were proposing an unknown op. It is documented as a
    no-op instead, which is what it is.
    """
    text = _prompt_text()
    assert "is accepted and does exactly nothing" in text
    assert "Never emit one" in text


@pytest.mark.parametrize("rule", [
    "**Never contradict an ACTIVE_DECISION.**",
    "**Conflict with no winner → PROPOSE_CONTRADICTS, never SUPERSEDE or ARCHIVE.**",
    "**`contradicts` takes memory refs, not fact ids.**",
    "**Include rationale.**",
])
def test_v3_changes_output_volume_not_judgment(rule: str) -> None:
    """Rules 7/8/9/10 are unchanged, and keep their numbers.

    The open issue that measures rule 8/9 behaviour cites them by number, so
    renumbering would silently retarget it.
    """
    assert rule in _prompt_text()


def test_rules_are_still_numbered_one_through_ten() -> None:
    rules = _prompt_text().split("## Rules", 1)[1].split("## Output Format", 1)[0]
    assert re.findall(r"^(\d+)\. ", rules, re.MULTILINE) == [
        str(n) for n in range(1, 11)
    ]


# ── the seeded store ───────────────────────────────────────────────────────


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@pytest.fixture
def seeded_store(tmp_path, monkeypatch) -> Path:
    """A 300-fact status document plus notes that obsolete four of its facts.

    The store's own ``specs/prompts/compaction.md`` is the real shipped v3
    file, so the prompt under test is the one the runner actually sends.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for sub in ("projects", "daily", "specs/prompts"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    (tmp_path / "specs" / "prompts" / "compaction.md").write_text(
        _prompt_text(), encoding="utf-8"
    )

    facts = "\n".join(
        f"- [2026-0{1 + i % 6}-{1 + i % 28:02d}] Status line {i}: "
        f"palinode component {i} reported healthy. <!-- fact:fact-{i:04d} -->"
        for i in range(SEEDED_FACTS)
    )
    target = tmp_path / "projects" / "palinode-status.md"
    target.write_text(
        "---\nid: projects-palinode-status\ncategory: project\n---\n\n"
        f"# Palinode Status\n\n## Current Work\n\n{facts}\n",
        encoding="utf-8",
    )

    for n, fact_id in enumerate(OBSOLETED):
        (tmp_path / "daily" / f"{_today()}-note{n}.md").write_text(
            f"---\nid: note{n}\ncategory: daily\nentities:\n  - project/palinode\n---\n\n"
            f"Component {int(fact_id.split('-')[1])} was decommissioned today; "
            f"its status line ({fact_id}) no longer describes anything that runs.\n",
            encoding="utf-8",
        )
    return target


def _notes() -> list[dict]:
    notes, _ = runner._collect_daily_notes(config.consolidation.lookback_days)
    assert notes, "seeded notes fell outside the lookback window"
    return notes


def _real_ops() -> list[dict]:
    return [
        {
            "op": "ARCHIVE",
            "id": fact_id,
            "rationale": "component decommissioned; status line describes nothing",
        }
        for fact_id in OBSOLETED
    ]


def _v2_response(fact_ids: list[str]) -> str:
    """What v2 asks for: a verdict on every fact, KEEPs first."""
    ops = [{"op": "KEEP", "id": fid} for fid in fact_ids] + _real_ops()
    return "```json\n" + json.dumps(ops, indent=2) + "\n```"


def _v3_response() -> str:
    """What v3 asks for: only the ops that change something."""
    return "```json\n" + json.dumps(_real_ops(), indent=2) + "\n```"


#: Two character budgets for ``llm_max_tokens = 2000``, because no tokenizer is
#: vendored and the answer must not hinge on which one you assume:
#:
#: * ``8000`` — the repo's own 4-chars-per-token proxy (``store.py``: ``len(raw) // 4``).
#: * ``4754`` — measured. The dogfood capture on the truncation issue stopped
#:   at 4754 chars when the model hit the 2000-token cap on this JSON, i.e.
#:   ~2.4 chars/token.
#:
#: v2 overruns both at 300 facts; v3 fits inside both with room to spare.
TOKEN_CAP_CHARS = {"proxy-4-chars-per-token": 8000, "measured-dogfood-capture": 4754}


def _truncate(text: str, budget: int) -> str:
    """Stop mid-stream at the budget, as a model hitting max_tokens does."""
    return text[:budget]


@pytest.mark.parametrize("label", sorted(TOKEN_CAP_CHARS))
def test_v2_full_keep_output_is_truncated_into_nothing(
    seeded_store: Path, label: str
) -> None:
    """The reported failure, reproduced: 300 facts in, zero operations out."""
    budget = TOKEN_CAP_CHARS[label]
    fact_ids = [f"fact-{i:04d}" for i in range(SEEDED_FACTS)]
    full = _v2_response(fact_ids)
    assert len(full) > budget, (
        f"{len(full)} chars of KEEPs must overrun the {budget}-char cap for "
        "this test to be measuring anything"
    )
    truncated = _truncate(full, budget)
    assert not truncated.rstrip().endswith("]"), "the array must not have closed"

    before = seeded_store.read_text(encoding="utf-8")
    operations, model_used = runner._consolidate_project(
        "palinode", _notes(), llm_fn=lambda s, u: (truncated, "primary")
    )

    # Parse outcome: nothing survives. The sibling change (a truncated or
    # unparseable proposal is a failed project) makes that visible here: the
    # seam reports the failure instead of a quiet week.
    assert operations == []
    assert model_used == runner.LLM_FAILED

    # Apply outcome: the four facts that should have been archived are still
    # in the document, untouched.
    stats = apply_operations(str(seeded_store), operations)
    assert stats["archived"] == 0
    assert seeded_store.read_text(encoding="utf-8") == before
    for fact_id in OBSOLETED:
        assert f"<!-- fact:{fact_id} -->" in before


@pytest.mark.parametrize("label", sorted(TOKEN_CAP_CHARS))
def test_v3_sparse_output_fits_and_applies(seeded_store: Path, label: str) -> None:
    """The same store, the same four judgments, inside every budget."""
    budget = TOKEN_CAP_CHARS[label]
    response = _v3_response()
    assert len(response) < budget, (
        f"{len(response)} chars must fit the {budget}-char cap"
    )
    assert _truncate(response, budget) == response, "nothing was cut"

    operations, model_used = runner._consolidate_project(
        "palinode", _notes(), llm_fn=lambda s, u: (response, "primary")
    )
    assert [op["op"] for op in operations] == ["ARCHIVE"] * len(OBSOLETED)
    assert model_used == "primary"

    stats = apply_operations(str(seeded_store), operations)
    assert stats["archived"] == len(OBSOLETED)
    assert stats["unmatched"] == 0

    after = seeded_store.read_text(encoding="utf-8")
    for fact_id in OBSOLETED:
        assert f"<!-- fact:{fact_id} -->" not in after


def test_output_size_stops_scaling_with_the_document(seeded_store: Path) -> None:
    """The property the contract buys: response size follows the *judgments*.

    Ten times the facts, same four decisions, same response — this is why
    implicit KEEP was chosen over chunking EXISTING_FACTS, which keeps the
    per-fact verdict and pays N LLM calls for it.
    """
    small = len(_v2_response([f"fact-{i:04d}" for i in range(30)]))
    large = len(_v2_response([f"fact-{i:04d}" for i in range(300)]))

    # v2: every additional fact costs output, whether or not it changes.
    per_extra_fact = (large - small) / 270
    assert per_extra_fact > 20, f"{per_extra_fact:.1f} chars per extra fact"
    assert large > 3 * small

    # v3: the response is a function of the judgments, and there is no fact
    # list to pass it. Four decisions cost the same on a 30-fact document as on
    # a 30 000-fact one.
    assert len(_v3_response()) < small / 3


def test_facts_named_in_no_operation_are_untouched(seeded_store: Path) -> None:
    """The executor half of the contract, asserted on disk.

    ``apply_operations`` iterates the operations, not the file's facts, so the
    296 facts no op names come through byte-identical. Implicit KEEP is safe
    only because of this.
    """
    before = seeded_store.read_text(encoding="utf-8")
    untouched = [
        line for line in before.splitlines()
        if "<!-- fact:" in line
        and not any(f"fact:{fid} " in line for fid in OBSOLETED)
    ]
    assert len(untouched) == SEEDED_FACTS - len(OBSOLETED)

    apply_operations(str(seeded_store), _real_ops())

    after = seeded_store.read_text(encoding="utf-8")
    for line in untouched:
        assert line in after


def test_empty_array_is_a_successful_no_op(seeded_store: Path) -> None:
    """v3's "nothing to change" answer, end to end through the runner.

    ``[]`` must reach the executor as zero operations and the run as a
    success — not as a failure, and not as an unparseable response. The
    sibling PR makes truncation a *failure*; this pins the case it must not
    catch.
    """
    before = seeded_store.read_text(encoding="utf-8")

    operations, model_used = runner._consolidate_project(
        "palinode", _notes(), llm_fn=lambda s, u: ("[]", "primary")
    )
    assert operations == []
    assert model_used == "primary"

    result = runner.run_consolidation(llm_fn=lambda s, u: ("[]", "primary"))
    assert result["status"] == "success"
    assert result.get("projects_failed", 0) == 0
    assert result.get("proposed_changes", []) == []
    assert seeded_store.read_text(encoding="utf-8") == before


def test_empty_array_wrapped_in_a_fence_is_also_a_no_op(seeded_store: Path) -> None:
    """Models fence their JSON; the prompt's own example of `[]` is fenced."""
    operations, _ = runner._consolidate_project(
        "palinode", _notes(), llm_fn=lambda s, u: ("```json\n[]\n```", "primary")
    )
    assert operations == []

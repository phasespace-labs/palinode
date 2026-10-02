"""The per-turn recall hook against a live server — the whole path, no stubs.

``tests/test_user_prompt_submit_hook.py`` runs the script with a stub ``curl``
and canned bodies: it pins what the hook does with a payload. This file pins
that the payload is real — a genuine uvicorn on an ephemeral port, the real
FastAPI app, a real SQLite store under ``tmp_path`` with real markdown files
retired through the real archive path, real ``curl``, real ``jq``.

What it is here to catch is the class of failure a stub cannot see: the hook
and the endpoint agreeing on a shape neither one actually produces. A fresh
session after an A → B replacement has to *receive B*, and a scripted consumer
reading the injected text has to *pick B* — that is the acceptance, and it is
only meaningful end to end.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from palinode.cli.init import USER_PROMPT_SUBMIT_HOOK_SCRIPT
from palinode.core.framing import MEMORY_IS_DATA
from tests import test_resolve_bundle as scenarios

# The store fixture and the three scenarios are the ones the unit-level suite
# pins, bound here by assignment rather than imported: pytest needs `mem` as a
# module attribute, and a parameter of the same name shadowing an *import* is
# a lint error where shadowing a binding is not.
mem = scenarios.mem
seed_current = scenarios.seed_current
seed_conflict = scenarios.seed_conflict
seed_unlinked_correction = scenarios.seed_unlinked_correction
seed_contested_stale_backing = scenarios.seed_contested_stale_backing
_write = scenarios._write

pytestmark = pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("jq")),
    reason="the hook needs curl and jq on PATH",
)

# The hook's own default is 250 ms — a latency budget for a real deployment,
# not an assertion about how fast this machine is. These tests pin the payload,
# so they give resolution room; the exhaustion test below pins the deadline
# itself by making it impossible to meet.
_GENEROUS_DEADLINE_MS = "10000"


@pytest.fixture()
def live_api(mem):
    """A real uvicorn serving the real app over the ``mem`` store."""
    import uvicorn

    from palinode.api import server

    config = uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not srv.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert srv.started, "uvicorn did not start in time"
        yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    finally:
        srv.should_exit = True
        thread.join(timeout=15)


def _run_hook(tmp_path, api_url: str, prompt: str, **env) -> str:
    """Run the shipped hook against the live server; return additionalContext.

    The hook sends its ``cwd`` (here the tmp store, whose directory name is a
    project of its own), so a scenario that expects its records passes the
    scenario's project as ``PALINODE_PROJECT``: a scoped request leaves other
    projects' records out.
    """
    hook = tmp_path / "hook.sh"
    hook.write_text(USER_PROMPT_SUBMIT_HOOK_SCRIPT)
    full_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PALINODE_API_URL": api_url,
        "PALINODE_HOOK_RESOLVE_DEADLINE": _GENEROUS_DEADLINE_MS,
        "PALINODE_HOOK_RECALL_TRIGGERS": "0",
        **env,
    }
    proc = subprocess.run(
        ["/bin/bash", str(hook)],
        input=json.dumps({"prompt": prompt, "session_id": "s1", "cwd": str(tmp_path)}),
        capture_output=True, text=True, env=full_env,
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return ""
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return out["hookSpecificOutput"]["additionalContext"]


def _scripted_consumer(context: str) -> str | None:
    """The crudest reader of the injected payload: the first current assertion.

    Stands in for the agent. If this picks the retired record, so would a
    reader skimming the top of the block — which is the failure the whole
    operation exists to prevent.
    """
    lines = context.splitlines()
    if "Current (1):" not in lines:
        return None
    after = lines[lines.index("Current (1):") + 1:]
    first = next((line for line in after if line.startswith("- [")), None)
    return first.split("]")[0].lstrip("- [") if first else None


def test_a_fresh_session_receives_the_successor(live_api, mem, tmp_path):
    seed_current(mem)
    context = _run_hook(tmp_path, live_api, "which endpoint does production serve traffic from?",
                        PALINODE_PROJECT="demo")

    assert "decisions/endpoint-v2" in context
    assert "Production serves traffic from endpoint bravo." in context
    assert "alpha" not in context, "the replaced wording reached the session"
    assert "resolution unavailable" not in context
    # The evidence travels with the answer, not as a separate lookup.
    assert "Coverage:" in context
    assert _scripted_consumer(context) == "decisions/endpoint-v2"


def test_an_unresolved_conflict_reaches_the_session_with_both_sides(live_api, mem, tmp_path):
    seed_conflict(mem)
    context = _run_hook(tmp_path, live_api, "which region does the cache cluster run in?",
                        PALINODE_PROJECT="demo")

    assert "insights/region-a" in context and "insights/region-b" in context
    assert "frankfurt" in context and "dublin" in context
    assert "no winner" in context
    assert _scripted_consumer(context) is None, "a contested question has no current line"


def test_an_unlinked_correction_reaches_the_session(live_api, mem, tmp_path):
    """The unlinked-discovery fix: the correction is in the string, not the JSON.

    Nothing links this record to the decision and it shares almost no
    vocabulary with the prompt — the fallback discovery is what finds it, and
    before this it went into the payload and out of the text. A session that
    is told the retry policy without being told a correction exists has been
    told the wrong thing confidently.
    """
    seed_unlinked_correction(mem)
    context = _run_hook(tmp_path, live_api, "what is the orbit client retry policy?",
                        PALINODE_PROJECT="orbit")

    assert "decisions/orbit-retry" in context
    assert "insights/orbit-scheduling" in context, (
        "the unlinked correction never reached the session"
    )
    assert "also found (unlinked)" in context
    assert "exponential jitter" in context
    # The backing it does declare is named too, and separately: support is not
    # discovery.
    assert "support: research/retry-probe" in context


def test_a_contested_side_keeps_its_qualifier_through_the_hook(live_api, mem, tmp_path):
    """The conflict-side qualifiers, end to end: the stale-backed side is
    labelled in the text a real session receives."""
    seed_contested_stale_backing(mem)
    context = _run_hook(tmp_path, live_api, "what is the orbit queue depth ceiling?",
                        PALINODE_PROJECT="orbit")

    assert "insights/queue-depth-a" in context and "insights/queue-depth-b" in context
    stale = next(
        line for line in context.splitlines()
        if line.startswith("- [insights/queue-depth-a]")
    )
    assert "⚠ stale backing: research/depth-probe-alpha" in stale, stale
    assert "⚠ contradicts: insights/queue-depth-b" in stale, stale


#: The window where the budget bites sits above the bundle's fixed frame, so it
#: moves with the frame's cost: the authority line every bundle leads with.
_FRAME_COST = len(MEMORY_IS_DATA) + 1


@pytest.mark.parametrize(
    "max_chars", [str(n + _FRAME_COST) for n in (520, 560, 600, 700, 900)]
)
def test_a_tight_budget_keeps_the_conflict_whole_or_names_it(
    live_api, mem, tmp_path, max_chars
):
    """Across the range where the budget bites: whole, or named. Never half."""
    seed_conflict(mem)
    context = _run_hook(
        tmp_path, live_api, "which region does the cache cluster run in?",
        PALINODE_HOOK_RECALL_MAX_CHARS=max_chars, PALINODE_PROJECT="demo",
    )
    if "Contested" in context:
        assert "insights/region-a" in context, context
        assert "insights/region-b" in context, context
    else:
        assert "Still contested" in context, context
        assert "budget_exhausted:conflicts" in context, context
    # And whatever the hook's own final trim did, it did between lines: a row
    # cut mid-way is a row whose qualification may be the part that went.
    for line in context.splitlines():
        if line.startswith("- ["):
            assert "]" in line, f"a row arrived half-rendered: {line!r}"


def test_a_budget_too_small_to_answer_honestly_injects_nothing(live_api, mem, tmp_path):
    """No room for a resolved answer is silence, not a one-sided search hit."""
    seed_conflict(mem)
    context = _run_hook(
        tmp_path, live_api, "which region does the cache cluster run in?",
        PALINODE_HOOK_RECALL_MAX_CHARS="200",
    )
    assert context == ""


def test_deadline_exhaustion_marks_the_fallback(live_api, mem, tmp_path):
    """A 1 ms deadline cannot be met. The turn still answers — and says how."""
    seed_current(mem)
    context = _run_hook(
        tmp_path, live_api, "which endpoint does production serve traffic from?",
        PALINODE_HOOK_RESOLVE_DEADLINE="1",
    )
    if not context:
        pytest.skip("search recalled nothing on this store; nothing to mark")
    assert "resolution unavailable (deadline)" in context
    assert "### Related memories" in context
    assert _scripted_consumer(context) is None, (
        "the fallback must not present an unchecked hit as a resolved current answer"
    )


def test_deadline_fallback_withholds_another_projects_record(live_api, mem, tmp_path):
    """The fallback /search must leave out what /resolve withholds.

    ``seed_current`` scopes its record to ``project/demo``; the session here
    is ``project/home``. With a normal deadline, bounded resolution already
    withholds the other project's record. A 1 ms deadline forces the
    plain-search fallback, and before this fix that fallback carried no
    scope at all — it sent only ``{query, limit, threshold, max_chars}`` — so
    it delivered the record /resolve would have withheld for the same prompt
    and the same session.
    """
    seed_current(mem)
    context = _run_hook(
        tmp_path, live_api, "which endpoint does production serve traffic from?",
        PALINODE_HOOK_RESOLVE_DEADLINE="1", PALINODE_PROJECT="home",
    )
    assert "endpoint bravo" not in context, (
        "the deadline fallback leaked another project's record"
    )
    assert "decisions/endpoint-v2" not in context


def test_deadline_fallback_still_delivers_the_same_projects_record(live_api, mem, tmp_path):
    """The scoped fallback is not a stricter filter than /resolve — same
    project still gets its own record, exactly as the unscoped fallback used
    to (and as /resolve itself does)."""
    seed_current(mem)
    context = _run_hook(
        tmp_path, live_api, "which endpoint does production serve traffic from?",
        PALINODE_HOOK_RESOLVE_DEADLINE="1", PALINODE_PROJECT="demo",
    )
    assert "resolution unavailable (deadline)" in context
    assert "### Related memories" in context
    assert "decisions/endpoint-v2" in context
    assert "endpoint bravo" in context

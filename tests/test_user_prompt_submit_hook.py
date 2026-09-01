"""The UserPromptSubmit per-turn recall hook — behavioral tests.

The hook is the delivery mechanism for implicit reads on Claude Code
(ADR-019): triggers + strict-threshold search, injected as
``additionalContext`` — which lands in the CONVERSATION, not the system
prompt, so it cannot invalidate the prompt-cache prefix.

These tests execute the real script under ``/bin/bash`` with a stub ``curl``
on PATH (same idiom as the SessionEnd hook tests in ``test_cli_init.py``).
The stub serves canned per-endpoint responses from ``$STUB_DIR``, records
every invocation, and honours ``CURL_FAIL=1`` — so the assertions cover the
script's actual behavior: what it calls, what it emits, and that every
failure mode degrades to silent exit 0. A hook that runs on every prompt
earns its keep by being invisible when it has nothing to say.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.init import USER_PROMPT_SUBMIT_HOOK_SCRIPT

_PROMPT = "how did we decide to handle the deploy rollback for the api?"

# The REAL response shape: /search returns a BARE ARRAY. The first version of
# this suite stubbed {"results": [...]} — a shape the server never sends — and
# the hook's `.results // .` jq died on real responses (indexing an array with
# a string is a hard jq error, not null), which fail-open turned into permanent
# silence. Caught live during the global rollout; the array shape is now the
# primary fixture and the object shape is covered separately below.
# raw_score (the cosine the THRESHOLD knob filters on) deliberately differs
# from score (the post-fusion rank value, ~1.0 for any top hit): the display
# assertions below prove the hook renders the knob's scale, not the rank's.
_SEARCH_HITS = [
    {"rel_path": "decisions/deploy-rollback.md", "score": 1.0, "raw_score": 0.62,
     "snippet": "Rollback is git revert + reindex, never manual file surgery."},
    {"rel_path": "insights/api-deploys.md", "score": 0.98, "raw_score": 0.55,
     "snippet": "Deploys pause the watcher; resume is automatic."},
]

# Forward-compat: an envelope shape some future API version might adopt.
_SEARCH_HITS_ENVELOPE = {"results": _SEARCH_HITS}

_FIRED_TRIGGER = [
    {"id": "t1", "description": "deploy discussion",
     "memory_file": "decisions/deploy-rollback.md", "score": 0.88},
]

_READ_BODY = {"content": "Full rollback decision body with rationale."}


def _run_hook(tmp_path: Path, *, prompt: str = _PROMPT, env: dict | None = None,
              triggers_response: object = (), search_response: object = None,
              read_response: object = None):
    """Render the script, run it under bash with a per-endpoint stub curl.

    Returns (CompletedProcess, curl_log_path). The stub matches the endpoint
    substring in its argv and cats the corresponding canned response file.
    """
    hook = tmp_path / "hook.sh"
    hook.write_text(USER_PROMPT_SUBMIT_HOOK_SCRIPT)

    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "resp-triggers.json").write_text(json.dumps(list(triggers_response)))
    (stub_dir / "resp-search.json").write_text(
        json.dumps(search_response if search_response is not None else []))
    (stub_dir / "resp-read.json").write_text(
        json.dumps(read_response if read_response is not None else {"content": ""}))
    (stub_dir / "curl").write_text(
        '#!/bin/bash\n'
        'echo "$@" >> "$STUB_DIR/curl-called"\n'
        '[ "${CURL_FAIL:-0}" = "1" ] && exit 22\n'
        'case "$*" in\n'
        '  *check-triggers*) cat "$STUB_DIR/resp-triggers.json" ;;\n'
        '  */search*) cat "$STUB_DIR/resp-search.json" ;;\n'
        '  */read*) cat "$STUB_DIR/resp-read.json" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (stub_dir / "curl").chmod(0o755)

    payload = json.dumps({"prompt": prompt, "session_id": "s1", "cwd": str(tmp_path)})
    full_env = {
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "STUB_DIR": str(stub_dir),
        "HOME": str(tmp_path),
    }
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["/bin/bash", str(hook)],
        input=payload, capture_output=True, text=True, env=full_env,
    )
    return proc, (stub_dir / "curl-called")


def _context_of(proc) -> str:
    """Parse the hook's stdout and return the injected additionalContext."""
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return out["hookSpecificOutput"]["additionalContext"]


# ---- Injection paths ----------------------------------------------------


def test_search_hits_injected_as_snippets(tmp_path):
    """Primary path: the bare-array response the API actually sends.

    Renders raw_score (62%), NOT the fused rank score (100%) — the rank
    value reads ~100% for the top hit of ANY query, including irrelevant
    ones, while raw_score is the scale the THRESHOLD knob filters on.
    """
    proc, _ = _run_hook(tmp_path, search_response=_SEARCH_HITS)
    assert proc.returncode == 0, proc.stderr
    ctx = _context_of(proc)
    assert "decisions/deploy-rollback.md" in ctx
    assert "(62% match)" in ctx, "should render raw_score, the knob's scale"
    assert "(100% match)" not in ctx, "must not render the fused rank score"
    assert "git revert + reindex" in ctx
    assert "Related memories" in ctx


def test_a_keyword_only_hit_claims_no_similarity(tmp_path):
    """raw_score is present and null: a BM25-only hit the ranker marked.

    There is no cosine to report, so the hook reports none. Falling back to
    the fused value here is the original bug wearing a fallback.
    """
    hits = [{"rel_path": "notes/a.md", "score": 1.0, "raw_score": None,
             "snippet": "body"}]
    proc, _ = _run_hook(tmp_path, search_response=hits)
    assert proc.returncode == 0, proc.stderr
    ctx = _context_of(proc)
    assert "(keyword match, rank 1.00)" in ctx
    assert "%" not in ctx.split("### Related memories")[1]


def test_an_absent_raw_score_is_not_the_same_as_a_null_one(tmp_path):
    """A pre-0.12 server never sent the field, so which arm hit is unknown."""
    absent = [{"rel_path": "notes/a.md", "score": 1.0, "snippet": "body"}]
    null = [{"rel_path": "notes/a.md", "score": 1.0, "raw_score": None,
             "snippet": "body"}]
    a_dir, n_dir = tmp_path / "absent", tmp_path / "null"
    a_dir.mkdir()
    n_dir.mkdir()
    absent_ctx = _context_of(_run_hook(a_dir, search_response=absent)[0])
    null_ctx = _context_of(_run_hook(n_dir, search_response=null)[0])
    assert "(rank 1.00)" in absent_ctx
    assert absent_ctx != null_ctx


def test_the_hook_renders_what_describe_match_would(tmp_path):
    """The hook is a jq copy of palinode/core/scoring.py and must agree with it.

    Same three cases, same wording. The Python surfaces and the hook drifting
    apart is how one of them starts lying again.
    """
    from palinode.core.scoring import describe_match

    cases = [
        {"score": 1.0, "raw_score": 0.421},
        {"score": 1.0, "raw_score": None},
        {"score": 1.0},
        {"score": 0.4},
        {"score": 0.07},
    ]
    hits = [dict(c, rel_path=f"notes/{i}.md", snippet="body")
            for i, c in enumerate(cases)]
    ctx = _context_of(_run_hook(tmp_path, search_response=hits)[0])
    for i, case in enumerate(cases):
        assert f"[notes/{i}.md] ({describe_match(case)})" in ctx


def test_envelope_response_shape_also_accepted(tmp_path):
    """Forward-compat: a {results: [...]} envelope renders identically."""
    proc, _ = _run_hook(tmp_path, search_response=_SEARCH_HITS_ENVELOPE)
    assert proc.returncode == 0, proc.stderr
    assert "decisions/deploy-rollback.md" in _context_of(proc)


def test_fired_trigger_injects_memory_content(tmp_path):
    proc, _ = _run_hook(
        tmp_path, triggers_response=_FIRED_TRIGGER, read_response=_READ_BODY)
    assert proc.returncode == 0, proc.stderr
    ctx = _context_of(proc)
    assert "Trigger fired: decisions/deploy-rollback.md" in ctx
    assert "Full rollback decision body" in ctx


def test_context_carries_staleness_caveat(tmp_path):
    """Injected memory must announce itself as retrieved-and-possibly-stale —
    the reader can't tell a memory from ground truth otherwise."""
    proc, _ = _run_hook(tmp_path, search_response=_SEARCH_HITS)
    ctx = _context_of(proc)
    assert "may be stale" in ctx
    assert "palinode_search" in ctx


def test_total_context_is_bounded(tmp_path):
    proc, _ = _run_hook(
        tmp_path,
        triggers_response=_FIRED_TRIGGER,
        read_response={"content": "x" * 50_000},
        search_response=_SEARCH_HITS,
        env={"PALINODE_HOOK_RECALL_MAX_CHARS": "500"},
    )
    assert len(_context_of(proc)) <= 500


# ---- Silence is the common case -----------------------------------------


def test_silent_when_nothing_recalled(tmp_path):
    proc, curl_called = _run_hook(tmp_path)  # empty triggers, empty search
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert curl_called.exists()  # it DID look — and said nothing


def test_silent_exit_zero_when_api_down(tmp_path):
    proc, _ = _run_hook(tmp_path, env={"CURL_FAIL": "1"},
                        search_response=_SEARCH_HITS)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_short_prompt_skipped_before_any_network(tmp_path):
    proc, curl_called = _run_hook(tmp_path, prompt="ok")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert not curl_called.exists(), "curl fired for a trivial prompt"


def test_dryrun_prints_plan_and_touches_nothing(tmp_path):
    proc, curl_called = _run_hook(tmp_path, env={"PALINODE_HOOK_DRYRUN": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "DRYRUN" in proc.stdout
    assert not curl_called.exists()


# ---- Channel switches ---------------------------------------------------


def test_search_channel_disabled_by_max_results_zero(tmp_path):
    proc, curl_called = _run_hook(
        tmp_path, search_response=_SEARCH_HITS,
        env={"PALINODE_HOOK_RECALL_MAX_RESULTS": "0"})
    assert proc.returncode == 0, proc.stderr
    calls = curl_called.read_text()
    assert "/search" not in calls
    assert "check-triggers" in calls  # other channel unaffected


def test_trigger_channel_disabled_by_env(tmp_path):
    proc, curl_called = _run_hook(
        tmp_path, triggers_response=_FIRED_TRIGGER,
        env={"PALINODE_HOOK_RECALL_TRIGGERS": "0"})
    assert proc.returncode == 0, proc.stderr
    assert "check-triggers" not in curl_called.read_text()


def test_search_request_carries_calibrated_defaults(tmp_path):
    """limit 3 / threshold 0.5 / max_chars 300.

    0.5 is the measured elbow, not a guess: SearchConfig's calibration (54
    real bge-m3 query/chunk pairs) has true matches clearing 0.5 at 98% but
    0.7 at only 28% — the original 0.75 default made this channel silently
    dead. Live sweep on a real store: 0.5 = full recall with zero
    nonsense-query passthrough.
    """
    proc, curl_called = _run_hook(tmp_path, search_response=_SEARCH_HITS)
    assert proc.returncode == 0, proc.stderr
    # The -d payload is multi-line (jq -n pretty-prints), so assert on the
    # whole recorded log, not a single line.
    log = curl_called.read_text()
    assert "/search" in log
    compact = log.replace(" ", "").replace("\n", "")
    assert '"limit":3' in compact
    assert '"threshold":0.5' in compact
    assert '"max_chars":300' in compact


# ---- Auth parity with the session hooks ---------------------------------


def test_bearer_token_sent_when_configured(tmp_path):
    _, curl_called = _run_hook(
        tmp_path, search_response=_SEARCH_HITS,
        env={"PALINODE_API_TOKEN": "sekrit"})
    assert "Authorization: Bearer sekrit" in curl_called.read_text()


def test_no_auth_header_by_default(tmp_path):
    _, curl_called = _run_hook(tmp_path, search_response=_SEARCH_HITS)
    assert "Authorization" not in curl_called.read_text()


# ---- Scaffolding integration --------------------------------------------


def test_init_writes_and_registers_the_recall_hook(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    script = tmp_path / ".claude" / "hooks" / "palinode-user-prompt-submit.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o111, "hook script not executable"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    events = settings.get("hooks", {})
    assert "UserPromptSubmit" in events
    commands = [
        h["command"]
        for entry in events["UserPromptSubmit"]
        for h in entry.get("hooks", [])
    ]
    assert any("palinode-user-prompt-submit.sh" in c for c in commands)


def test_embedded_copy_matches_canonical_example():
    """`palinode init` embeds the hook as a string constant because an
    installed package cannot read examples/. Pin the embedded copy
    byte-for-byte to examples/hooks/palinode-user-prompt-submit.sh so the two
    cannot silently drift — same guard as the session-start and session-end
    hooks, same failure mode it prevents."""
    canonical = (
        Path(__file__).parent.parent
        / "examples" / "hooks" / "palinode-user-prompt-submit.sh"
    )
    assert USER_PROMPT_SUBMIT_HOOK_SCRIPT == canonical.read_text(), (
        "palinode/cli/init.py USER_PROMPT_SUBMIT_HOOK_SCRIPT has drifted from "
        "examples/hooks/palinode-user-prompt-submit.sh — re-sync byte-for-byte."
    )


def test_settings_timeout_matches_canonical_example():
    """init.py's registered timeout must match examples/hooks/settings.json —
    the same parity the session hooks keep."""
    from palinode.cli.init import SETTINGS_HOOK_BLOCK

    canonical = json.loads(
        (Path(__file__).parent.parent / "examples" / "hooks" / "settings.json")
        .read_text()
    )
    canon_t = canonical["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"]
    init_t = SETTINGS_HOOK_BLOCK["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"]
    assert init_t == canon_t


def test_init_rerun_registers_recall_hook_exactly_once(tmp_path):
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--force"])
        assert result.exit_code == 0, result.output

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = [
        h["command"]
        for entry in settings["hooks"]["UserPromptSubmit"]
        for h in entry.get("hooks", [])
        if "palinode-user-prompt-submit.sh" in h.get("command", "")
    ]
    assert len(commands) == 1, f"expected exactly one registration, got {commands}"

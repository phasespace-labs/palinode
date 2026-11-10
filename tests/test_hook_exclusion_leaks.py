"""Run shipped capture hooks with real policy preflights and a recording curl stub."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from palinode.cli.init import HOOK_SCRIPT, POST_COMPACT_HOOK_SCRIPT
from palinode.core.capture_policy import update_capture_policy
from palinode.core.config import config

SECRET = "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789"
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("event,script", [
    ("SessionEnd", HOOK_SCRIPT), ("PostCompact", POST_COMPACT_HOOK_SCRIPT),
    ("SessionEnd", (ROOT / "examples/hooks/palinode-session-end.sh").read_text()),
    ("PostCompact", (ROOT / "examples/hooks/palinode-post-compact.sh").read_text()),
], ids=["installed-floor", "installed-checkpoint", "example-floor", "example-checkpoint"])
@pytest.mark.parametrize("scope", ["project", "path", "source-path", "allowed"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_capture_hooks_do_not_emit_excluded_content(tmp_path, monkeypatch, event, script, scope, dry_run, client):
    if client == "codex" and event != "SessionEnd":
        pytest.skip("Codex registers the shared session-end floor only (palinode init --codex-hook)")
    memory = tmp_path / "store"
    memory.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(memory))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.git, "auto_push", False)
    workspace = tmp_path / "private-project"
    workspace.mkdir()
    transcript = workspace / "session.jsonl"
    if client == "codex":
        # A Codex rollout (first line session_meta): the floor rewrites it into the Claude
        # Code shape in a temp file, which must not happen under an exclusion.
        def item(kind: str) -> dict:
            return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": kind, "content": [{"type": "text", "text": SECRET}]}}}
        lines = [{"type": "session_meta", "payload": {"cli_version": "0.165.0"}}]
        lines += [item("UserMessage") for _ in range(4)] + [item("AgentMessage")]
        transcript.write_text("\n".join(json.dumps(line) for line in lines))
    else:
        transcript.write_text("\n".join(json.dumps({
            "type": "user", "message": {"role": "user", "content": SECRET},
        }) for _ in range(4)))
    update_capture_policy(
        excluded_projects=["private-project"] if scope == "project" else [],
        excluded_paths=([str(workspace)] if scope == "path" else
                        [str(transcript)] if scope == "source-path" else []),
    )
    hook = tmp_path / "hook.sh"
    hook.write_text(script)
    stub = tmp_path / "bin"
    stub.mkdir()
    # The only intercepted boundary is curl: run the actual policy evaluator
    # against the persisted controls before returning its response to bash.
    (stub / "curl").write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
from palinode.core.capture_policy import evaluate_capture_policy
args = sys.argv[1:]
url = next(a for a in args if a.startswith("http"))
flag = next((f for f in ("-d", "--data-binary") if f in args), None)
data = args[args.index(flag) + 1] if flag else ""
if data == "@-":
    data = sys.stdin.read()
root = Path(os.environ["STUB_DIR"])
with (root / "requests.jsonl").open("a") as out:
    out.write(json.dumps({"url": url, "body": data}) + "\\n")
if url.endswith("/controls/check"):
    result = evaluate_capture_policy(**json.loads(data)).public()
    with (root / "decisions.jsonl").open("a") as out:
        out.write(json.dumps(result) + "\\n")
    print(json.dumps(result))
else:
    # Any content-bearing request is recorded, even an unexpected endpoint.
    (root / "model-request.txt").write_text(data)
    print('{"status":"ok"}')
''')
    (stub / "curl").chmod(0o755)
    # Catch an accidental local git fallback as well as content-bearing curl.
    (stub / "git").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$STUB_DIR/git-calls"\nexit 1\n')
    (stub / "git").chmod(0o755)
    env = {"PATH": f"{stub}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
           "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "STUB_DIR": str(stub),
           "PYTHONPATH": str(ROOT), "PALINODE_DIR": str(memory),
           "PALINODE_PROJECT": "private-project", "PALINODE_HOOK_DRYRUN": str(int(dry_run))}
    payload = {"session_id": "fixture-session", "cwd": str(workspace),
               "transcript_path": str(transcript), "reason": "other",
               "hook_event_name": event, "compact_summary": SECRET, "trigger": "auto"}
    proc = subprocess.run(["/bin/bash", str(hook)], input=json.dumps(payload),
                          text=True, capture_output=True, env=env, cwd=workspace, timeout=30)
    assert proc.returncode == 0, proc.stderr
    decisions = [json.loads(line) for line in (stub / "decisions.jsonl").read_text().splitlines()]
    assert len(decisions) == 1
    expected = None if scope == "allowed" else f"excluded_{'path' if scope == 'source-path' else scope}"
    assert decisions[0]["reason"] == expected
    assert decisions[0]["allowed"] is (scope == "allowed")
    assert not (stub / "git-calls").exists()
    assert not list(memory.rglob("*.md"))
    emitted = proc.stdout + proc.stderr
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != transcript:
            emitted += path.read_bytes().decode("utf-8", errors="replace")
    if scope == "allowed":
        # Positive control: the fixture really reaches the payload/diagnostic
        # without a denial, so a broken or inert hook cannot pass this test.
        assert SECRET in emitted
    else:
        assert SECRET not in emitted
        calls = [json.loads(line) for line in (stub / "requests.jsonl").read_text().splitlines()]
        assert [call["url"].split(":6340/", 1)[-1] for call in calls] == ["controls/check"]
        assert not (stub / "model-request.txt").exists()

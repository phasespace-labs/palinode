"""The shipped recall hooks send and inject nothing for an excluded scope.

The session-start and per-prompt hooks are the scripts Claude Code runs and that
``palinode init --codex-hook`` copies byte for byte for Codex (Cursor and
Antigravity run them through an adapter). Each is run with the real policy
evaluator behind a recording ``curl`` stub, as in test_hook_exclusion_leaks.py:
under a project or path exclusion, the only request is the controls check, the
prompt never leaves the hook, and no stored memory is injected. The allowed case
is the positive control.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from palinode.cli.init import SESSION_START_HOOK_SCRIPT, USER_PROMPT_SUBMIT_HOOK_SCRIPT
from palinode.core.capture_policy import update_capture_policy
from palinode.core.config import config

SECRET = "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789"
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="the hooks need jq on PATH")

# A stored core memory carrying the fixture: injected only if recall is allowed.
PRIME = {"core_memories": [{"file": "decisions/private.md", "summary": SECRET}]}


@pytest.mark.parametrize("hook", ["session-start", "user-prompt-submit"])
@pytest.mark.parametrize("scope", ["project", "path", "allowed"])
def test_recall_hooks_do_not_send_or_inject_for_an_excluded_scope(tmp_path, monkeypatch, hook, scope):
    memory = tmp_path / "store"
    memory.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(memory))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.git, "auto_push", False)
    workspace = tmp_path / "private-project"
    workspace.mkdir()
    update_capture_policy(
        excluded_projects=["private-project"] if scope == "project" else [],
        excluded_paths=[str(workspace)] if scope == "path" else [],
    )
    script = SESSION_START_HOOK_SCRIPT if hook == "session-start" else USER_PROMPT_SUBMIT_HOOK_SCRIPT
    (tmp_path / "hook.sh").write_text(script)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "curl").write_text(f"#!{sys.executable}\n" + f'''
import json, os, sys
from pathlib import Path
from palinode.core.capture_policy import evaluate_capture_policy
args = sys.argv[1:]
url = next(a for a in args if a.startswith("http"))
flag = next((f for f in ("-d", "--data-binary", "--data-urlencode") if f in args), None)
data = args[args.index(flag) + 1] if flag else ""
if data == "@-":
    data = sys.stdin.read()
root = Path(os.environ["STUB_DIR"])
with (root / "requests.jsonl").open("a") as out:
    out.write(json.dumps({{"url": url, "body": data, "args": args}}) + "\\n")
if url.endswith("/controls/check"):
    result = evaluate_capture_policy(**json.loads(data)).public()
    with (root / "decisions.jsonl").open("a") as out:
        out.write(json.dumps(result) + "\\n")
    print(json.dumps(result))
elif url.endswith("/context/prime"):
    print({json.dumps(json.dumps(PRIME))})
else:
    print("{{}}")
''')
    (stub / "curl").chmod(0o755)
    # The hooks ask git for the repository root; there is none here.
    (stub / "git").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$STUB_DIR/git-calls"\nexit 1\n')
    (stub / "git").chmod(0o755)
    env = {"PATH": f"{stub}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
           "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "STUB_DIR": str(stub), "PYTHONPATH": str(ROOT), "PALINODE_DIR": str(memory),
           "PALINODE_PROJECT": "private-project"}
    payload = {"session_id": "fixture-session", "cwd": str(workspace), "source": "startup",
               "hook_event_name": "SessionStart" if hook == "session-start" else "UserPromptSubmit",
               "prompt": f"Which batch did we agree on? The key is {SECRET}"}
    proc = subprocess.run(["/bin/bash", str(tmp_path / "hook.sh")], input=json.dumps(payload),
                          text=True, capture_output=True, env=env, cwd=workspace, timeout=30)

    assert proc.returncode == 0, proc.stderr
    decisions = [json.loads(line) for line in (stub / "decisions.jsonl").read_text().splitlines()]
    assert decisions[0]["reason"] == (None if scope == "allowed" else f"excluded_{scope}")
    calls =[json.loads(line) for line in (stub / "requests.jsonl").read_text().splitlines()]
    endpoints = [call["url"].split(":6340/", 1)[-1].split("?", 1)[0] for call in calls]
    sent = "".join(call["body"] + " ".join(call["args"]) for call in calls
                   if not call["url"].endswith("/controls/check"))
    emitted = proc.stdout + proc.stderr
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.parent != stub and path.name != "hook.sh":
            emitted += path.read_bytes().decode("utf-8", errors="replace")
    if scope == "allowed":
        # Positive control: the prompt reaches a recall endpoint (per prompt),
        # or the stored memory is injected (session start).
        assert endpoints[0] == "controls/check" and len(endpoints) > 1
        if hook == "session-start":
            assert SECRET in proc.stdout
        else:
            assert SECRET in sent
    else:
        assert endpoints == ["controls/check"]
        assert SECRET not in emitted
        assert SECRET not in "".join(call["body"] + " ".join(call["args"]) for call in calls)
        assert not list(memory.rglob("*.md"))

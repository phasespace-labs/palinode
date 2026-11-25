"""Every harness's hook spec recognises exactly the forms it writes (ADR-023 §4).

The recognisers are not worked out backwards from the writers' output: each
spec lists every command form a release wrote, and these tests enumerate that
list. For every form, ``owns`` returns that registration and form, and
``runs`` finds that registration's script, so ``owns`` is a subset of
``runs``. Only an owned handler is ever rewritten. A hand-written variant is
found by ``runs`` and reported, never rewritten. Real temp directories
throughout.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from palinode.hooks import SCRIPT_NAMES
from palinode.hooks.claude import CLAUDE
from palinode.hooks.harnesses import HARNESSES
from palinode.hooks.installed import (
    CHANGE_SYNCHRONOUS,
    FIX_ASYNC,
    FIX_UNQUOTED,
    plan_registrations,
    registrations_to_fix_by_hand,
    repair_registrations,
)

HOME = Path("/home/someone")
PROJECT = Path("/work/my project")

FORMS = [
    pytest.param(spec, reg, form, id=f"{spec.name}-{reg.event}-{i}")
    for spec in HARNESSES
    for reg in spec.registrations
    for i, form in enumerate(spec.commands(reg, PROJECT))
]


@pytest.mark.parametrize("spec, reg, form", FORMS)
def test_owns_recognises_every_form_it_lists(spec, reg, form):
    assert spec.owns(form.text, PROJECT) == (reg, form)
    assert spec.owns(f" \t{form.text}  ", PROJECT) == (reg, form)


@pytest.mark.parametrize("spec, reg, form", FORMS)
def test_runs_finds_the_script_of_every_owned_form(spec, reg, form):
    names = [p.name for p in spec.runs(form.text, HOME, PROJECT)]
    assert reg.script in names
    if spec.adapter:
        assert names[0] == spec.adapter


@pytest.mark.parametrize("spec, reg, form", FORMS)
def test_a_legacy_form_upgrades_to_a_current_form_of_the_same_registration(spec, reg, form):
    if form.current:
        assert form.upgrade is None
        return
    upgraded = spec.owns(form.upgrade, PROJECT)
    assert upgraded is not None
    assert upgraded[0] == reg and upgraded[1].current
    assert form.change


@pytest.mark.parametrize("spec", HARNESSES, ids=lambda s: s.name)
def test_render_writes_the_first_form_and_owns_it(spec):
    for reg in spec.registrations:
        handler = spec.render(reg, PROJECT)
        assert handler["command"] == spec.commands(reg, PROJECT)[0].text
        assert handler["timeout"] == reg.timeout
        assert ("type" in handler) is spec.typed
        assert "async" not in handler
        assert spec.owns(handler["command"], PROJECT)[1].current


@pytest.mark.parametrize("spec", HARNESSES, ids=lambda s: s.name)
def test_the_registration_block_lists_every_registration_and_nothing_else(spec):
    block = spec.registration_block(PROJECT)
    found = [(event, h["command"]) for event, h in spec.layout.handlers(block)]
    assert found == [(r.event, spec.render(r, PROJECT)["command"]) for r in spec.registrations]
    assert spec.missing(block, HOME, PROJECT) == []


def test_every_script_belongs_to_some_harness():
    assert {s for spec in HARNESSES for s in spec.scripts} == set(SCRIPT_NAMES)


# -- repair: only owned handlers change ----------------------------------------


def _settings(*handlers: dict, event: str = "PostCompact") -> dict:
    return {"hooks": {event: [{"hooks": list(handlers)}]}}


@pytest.mark.parametrize("reg", CLAUDE.registrations, ids=lambda r: r.event)
def test_each_legacy_claude_form_is_upgraded_and_a_second_repair_changes_nothing(reg):
    legacy = [f for f in CLAUDE.commands(reg, PROJECT) if not f.current]
    assert legacy, "the unquoted form init wrote before 0.24.1"
    for form in legacy:
        data = _settings({"type": "command", "command": form.text, "timeout": reg.timeout},
                         event=reg.event)
        assert repair_registrations(CLAUDE, data, PROJECT) == [form.change]
        assert data["hooks"][reg.event][0]["hooks"][0]["command"] == form.upgrade
        assert repair_registrations(CLAUDE, data, PROJECT) == []


@pytest.mark.parametrize("form", CLAUDE.commands(CLAUDE.registration("PostCompact"), PROJECT),
                         ids=lambda f: f.text)
def test_an_owned_async_handler_is_made_synchronous(form):
    data = _settings({"type": "command", "command": form.text, "timeout": 35, "async": True})
    changes = repair_registrations(CLAUDE, data, PROJECT)
    assert changes[0] == CHANGE_SYNCHRONOUS
    assert "async" not in data["hooks"]["PostCompact"][0]["hooks"][0]


#: Commands that run palinode's post-compact script but are not a form palinode
#: writes or documents. ``runs`` finds them; ``owns`` does not.
HAND_WRITTEN = [
    '"$HOME"/.claude/hooks/palinode-post-compact.sh',
    "$HOME/.claude/hooks/palinode-post-compact.sh",
    "${HOME}/.claude/hooks/palinode-post-compact.sh",
    "/Users/someone/.claude/hooks/palinode-post-compact.sh",
    '"${CLAUDE_PROJECT_DIR}/.claude/hooks/palinode-post-compact.sh"',
    "$CLAUDE_PROJECT_DIR/.claude/hooks/palinode-post-compact.sh",
    "${CLAUDE_PROJECT_DIR}/tools/palinode-post-compact.sh",
    "bash ~/.claude/hooks/palinode-post-compact.sh",
    "~/.claude/hooks/palinode-post-compact.sh 2>/dev/null",
]


@pytest.mark.parametrize("command", HAND_WRITTEN)
def test_a_hand_written_variant_is_found_but_never_rewritten(command):
    assert CLAUDE.owns(command, PROJECT) is None
    assert "palinode-post-compact.sh" in [p.name for p in CLAUDE.runs(command, HOME, PROJECT)]
    data = _settings({"type": "command", "command": command, "async": True})
    before = json.dumps(data)
    assert repair_registrations(CLAUDE, data, PROJECT) == []
    assert json.dumps(data) == before


@pytest.mark.parametrize("command", [
    "~/.claude/hooks/palinode-post-compact.sh.backup",
    "my-palinode-post-compact.sh",
    "other-tool.sh",
    "",
])
def test_another_tools_hook_is_neither_owned_nor_run(command):
    assert CLAUDE.owns(command, PROJECT) is None
    assert CLAUDE.runs(command, HOME, PROJECT) == []


def test_hand_written_defects_are_reported_for_fixing_by_hand(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {
        "PostCompact": [{"hooks": [
            {"type": "command", "command": "$HOME/.claude/hooks/palinode-post-compact.sh", "async": True},
        ]}],
        "UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/palinode-user-prompt-submit.sh"},
            # Quoted: no defect.
            {"type": "command", "command": '"$CLAUDE_PROJECT_DIR"/.claude/hooks/palinode-session-start.sh'},
            # Not palinode's.
            {"type": "command", "command": "$CLAUDE_PROJECT_DIR/tools/lint.sh", "async": True},
        ]}],
    }}), encoding="utf-8")

    fixes = registrations_to_fix_by_hand(home, project)
    assert [(f.command, f.problem) for f in fixes] == [
        ("$HOME/.claude/hooks/palinode-post-compact.sh", FIX_ASYNC),
        ("$CLAUDE_PROJECT_DIR/.claude/hooks/palinode-user-prompt-submit.sh", FIX_UNQUOTED),
    ]
    assert [p.repairs for p in plan_registrations(home, project) if p.repairs] == []


def test_codex_config_toml_is_read_but_never_repaired(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    toml = project / ".codex" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(
        '[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype = "command"\n'
        'command = "\\"$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.codex/hooks/'
        'palinode-session-start.sh\\""\nasync = true\n',
        encoding="utf-8",
    )
    from palinode.hooks.codex import CODEX

    assert CODEX.palinode_events(toml, home, project) == {"SessionStart"}
    assert [p.repairs for p in plan_registrations(home, project) if p.repairs] == []


# -- the package seam ----------------------------------------------------------


def test_the_hooks_package_imports_nothing_from_the_cli():
    code = (
        "import sys, palinode.hooks.harnesses, palinode.hooks.installed; "
        "print(sorted(m for m in sys.modules if m.startswith('palinode.cli')))"
    )
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    assert out.stdout.strip() == "[]"

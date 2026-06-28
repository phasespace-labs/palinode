"""
tests/test_deploy_systemd.py — Validate deploy/systemd/ templates and installer.

Checks:
  1. Each .template parses as valid systemd unit syntax (required sections present).
  2. All ${VARIABLE} placeholders are documented in the README.
  3. install.sh is executable and shellcheck-clean (skipped gracefully if shellcheck absent).
  4. envsubst substitution round-trip: substituted template contains no remaining ${...} tokens.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# ── paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy" / "systemd"
TEMPLATES = [
    DEPLOY_DIR / "palinode-api.service.template",
    DEPLOY_DIR / "palinode-mcp.service.template",
    DEPLOY_DIR / "palinode-watcher.service.template",
]
INSTALL_SH = DEPLOY_DIR / "install.sh"
README = DEPLOY_DIR / "README.md"

# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_variables(text: str) -> set[str]:
    """Return all ${VAR} placeholder names found in text."""
    return set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", text))


def _unit_sections(text: str) -> set[str]:
    """
    Return the set of [Section] header names in a systemd unit file.
    ConfigParser cannot parse systemd units because systemd allows repeated keys
    (e.g. multiple Environment= lines). We just scan for bracket headers instead.
    """
    return set(re.findall(r"^\[([A-Za-z]+)\]", text, re.MULTILINE))


# ── 1. Template syntax ────────────────────────────────────────────────────────

@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_template_required_sections(template_path: Path) -> None:
    """Each template must contain [Unit], [Service], and [Install] sections."""
    raw = template_path.read_text()
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in raw, f"{template_path.name} missing section {section}"


@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_template_service_section_keys(template_path: Path) -> None:
    """[Service] must declare Type=simple, Restart=always, RestartSec=5."""
    raw = template_path.read_text()
    for required in ("Type=simple", "Restart=always", "RestartSec=5"):
        assert required in raw, f"{template_path.name} missing {required!r} in [Service]"


@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_template_install_wantedby(template_path: Path) -> None:
    """[Install] WantedBy is templated so the scope (user→default.target,
    system→multi-user.target) is chosen by install.sh at render time (#252)."""
    raw = template_path.read_text()
    assert "WantedBy=${SYSTEMD_WANTED_BY}" in raw, (
        f"{template_path.name} should template WantedBy via ${{SYSTEMD_WANTED_BY}}"
    )


@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_template_no_hardcoded_ips(template_path: Path) -> None:
    """Templates must not contain site-specific IPs (private ranges, Tailscale, etc.).

    Bind addresses like 0.0.0.0 and loopback 127.0.0.1 are intentional and allowed.
    The test targets routable addresses that would be infrastructure leaks.
    """
    raw = template_path.read_text()
    # Match anything that looks like an IP but is not an obvious bind/loopback address
    ip_pattern = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
    allowed = {"0.0.0.0", "127.0.0.1"}
    matches = [ip for ip in ip_pattern.findall(raw) if ip not in allowed]
    assert not matches, (
        f"{template_path.name} contains hardcoded site-specific IPs (use variables): {matches}"
    )


@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_template_no_hardcoded_hostnames(template_path: Path) -> None:
    """Templates must not mention any specific deployment hostname or legacy name.

    Synthetic guard: catches site-specific hostnames, legacy-rename leftovers,
    and RFC 1918 IP literals slipping into install-time templates.
    """
    raw = template_path.read_text()
    forbidden = [
        "example-host",     # synthetic specific hostname
        "old-name-data",    # synthetic legacy-rename data-dir pattern
        "192.0.2.",         # RFC 5737 TEST-NET-1 — stands in for any hardcoded IP
    ]
    for word in forbidden:
        assert word not in raw, (
            f"{template_path.name} contains forbidden hostname/path {word!r}"
        )


# ── 2. README documents all variables ────────────────────────────────────────

def test_readme_documents_all_variables() -> None:
    """Every ${VARIABLE} used across all templates must appear in the README."""
    readme_text = README.read_text()
    all_vars: set[str] = set()
    for tmpl in TEMPLATES:
        all_vars |= _extract_variables(tmpl.read_text())

    missing = [v for v in sorted(all_vars) if v not in readme_text]
    assert not missing, (
        f"The following variables are used in templates but not documented in README.md: {missing}"
    )


# ── 3. install.sh is executable ──────────────────────────────────────────────

def test_install_sh_is_executable() -> None:
    """install.sh must have execute permission."""
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "deploy/systemd/install.sh is not user-executable (chmod +x needed)"


def test_install_sh_has_shebang() -> None:
    """install.sh must start with a #!/usr/bin/env bash shebang."""
    first_line = INSTALL_SH.read_text().splitlines()[0]
    assert first_line.startswith("#!/"), f"install.sh missing shebang, got: {first_line!r}"
    assert "bash" in first_line, f"install.sh shebang should reference bash, got: {first_line!r}"


@pytest.mark.skipif(
    shutil.which("shellcheck") is None,
    reason="shellcheck not installed — skipping shell lint (install with: apt install shellcheck / brew install shellcheck)",
)
def test_install_sh_shellcheck() -> None:
    """install.sh passes shellcheck (shell=bash, severity=warning)."""
    result = subprocess.run(
        ["shellcheck", "--shell=bash", "--severity=warning", str(INSTALL_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck found issues in install.sh:\n{result.stdout}\n{result.stderr}"
    )


# ── 4. envsubst substitution round-trip ──────────────────────────────────────

SAMPLE_ENV = {
    "PALINODE_HOME": "/tmp/test-palinode",
    "PALINODE_DATA_DIR": "/tmp/test-palinode-data",
    "OLLAMA_URL": "http://localhost:11434",
    "EMBEDDING_MODEL": "bge-m3",
    "API_PORT": "6340",
    "MCP_PORT": "6341",
    "SYSTEMD_WANTED_BY": "default.target",
    "PALINODE_API_BIND_INTENT": "",
}


@pytest.mark.skipif(
    shutil.which("envsubst") is None,
    reason="envsubst not installed — skipping substitution test",
)
@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_substitution_removes_all_placeholders(template_path: Path) -> None:
    """After envsubst with sample values, no ${VAR} tokens should remain."""
    env = {**os.environ, **SAMPLE_ENV}
    result = subprocess.run(
        ["envsubst"],
        input=template_path.read_text(),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"envsubst failed: {result.stderr}"
    remaining = _extract_variables(result.stdout)
    assert not remaining, (
        f"After substitution, {template_path.name} still contains unresolved vars: {remaining}"
    )


@pytest.mark.skipif(
    shutil.which("envsubst") is None,
    reason="envsubst not installed — skipping substitution parse test",
)
@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_substituted_template_parses_as_valid_unit(template_path: Path) -> None:
    """A fully substituted template must contain required systemd sections.

    Note: ConfigParser cannot parse systemd units because systemd allows repeated
    keys (multiple Environment= lines). We scan for section headers directly.
    """
    env = {**os.environ, **SAMPLE_ENV}
    result = subprocess.run(
        ["envsubst"],
        input=template_path.read_text(),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    sections = _unit_sections(result.stdout)
    for required in ("Unit", "Service", "Install"):
        assert required in sections, (
            f"{template_path.name}: missing [{required}] section after substitution"
        )


# ── 5. install.sh --help exits cleanly ───────────────────────────────────────

def test_install_sh_help_exits_zero() -> None:
    """install.sh --help should print usage and exit 0."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"install.sh --help exited {result.returncode}:\n{result.stderr}"
    )
    output = result.stdout + result.stderr
    assert "PALINODE_HOME" in output, "install.sh --help should mention PALINODE_HOME"
    assert "--enable" in output, "install.sh --help should mention --enable flag"


# ── 6. watcher unit name is reconcilable (#252) ──────────────────────────────

def test_install_sh_documents_watcher_unit_name() -> None:
    """install.sh --help must document WATCHER_UNIT_NAME so an existing deploy
    whose watcher unit is named differently (e.g. palinode-indexer) can be
    reconciled idempotently rather than duplicated (#252)."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert "WATCHER_UNIT_NAME" in output, (
        "install.sh --help should document the WATCHER_UNIT_NAME override (#252)"
    )


def test_install_sh_maps_watcher_unit_name() -> None:
    """install.sh must derive the watcher's installed unit name from
    WATCHER_UNIT_NAME (default palinode-watcher) rather than hardcoding it, so
    the installer can write/manage a renamed unit like palinode-indexer."""
    raw = INSTALL_SH.read_text()
    assert 'WATCHER_UNIT_NAME="${WATCHER_UNIT_NAME:-palinode-watcher}"' in raw, (
        "install.sh should default WATCHER_UNIT_NAME to palinode-watcher"
    )
    assert "unit_name_for" in raw, (
        "install.sh should map the watcher template to its configured unit name"
    )


def test_readme_documents_watcher_unit_name() -> None:
    """README must document the WATCHER_UNIT_NAME variable (#252)."""
    assert "WATCHER_UNIT_NAME" in README.read_text(), (
        "README.md should document WATCHER_UNIT_NAME"
    )


# ── 7. system scope (#252) ────────────────────────────────────────────────────

def test_install_sh_documents_system_flag() -> None:
    """install.sh --help must document the --system flag (#252). Production hosts
    (e.g. the dedicated palinode host) run system-scope units in
    /etc/systemd/system under multi-user.target; without --system the installer
    could only ever write --user units and so could not reconcile them."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert "--system" in output, "install.sh --help should document the --system flag (#252)"


def test_install_sh_system_scope_targets_etc() -> None:
    """In --system scope install.sh must write to /etc/systemd/system, render
    WantedBy=multi-user.target, drop the `--user` arg from systemctl, and require
    root. User scope must keep ~/.config/systemd/user + default.target (#252)."""
    raw = INSTALL_SH.read_text()
    assert "/etc/systemd/system" in raw, "system scope must target /etc/systemd/system"
    assert 'SYSTEMD_WANTED_BY="multi-user.target"' in raw, (
        "system scope must render WantedBy=multi-user.target"
    )
    assert 'SYSTEMD_WANTED_BY="default.target"' in raw, (
        "user scope must keep WantedBy=default.target"
    )
    assert "$HOME/.config/systemd/user" in raw, "user scope must target ~/.config/systemd/user"
    # systemctl is invoked through an array so --user is present in user scope only
    assert "SYSTEMCTL=( systemctl )" in raw, "system scope must call systemctl without --user"
    assert "SYSTEMCTL=( systemctl --user )" in raw, "user scope must call systemctl --user"
    assert "EUID" in raw, "system scope must enforce a root check"


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="install.sh exits at the Linux/systemd OS gate before the root check on non-Linux",
)
def test_install_sh_rejects_system_without_root(monkeypatch) -> None:
    """Running --system as a non-root user must fail fast with a root hint,
    rather than silently writing nothing or erroring obscurely (#252).

    Skips if the test runner is already root (CI containers sometimes are).
    """
    if (os.geteuid() if hasattr(os, "geteuid") else 1) == 0:
        pytest.skip("test runner is root; cannot exercise the non-root rejection path")
    env = {**os.environ, **SAMPLE_ENV, "PALINODE_HOME": "/tmp/test-palinode"}
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--system"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0, "--system without root should exit non-zero"
    assert "root" in (result.stdout + result.stderr).lower(), (
        "--system without root should explain it needs root"
    )


@pytest.mark.skipif(
    shutil.which("envsubst") is None,
    reason="envsubst not installed — skipping substitution test",
)
@pytest.mark.parametrize("template_path", TEMPLATES, ids=[t.name for t in TEMPLATES])
def test_system_scope_renders_multi_user_target(template_path: Path) -> None:
    """With SYSTEMD_WANTED_BY=multi-user.target the rendered unit must carry
    WantedBy=multi-user.target and leave no unresolved tokens (#252)."""
    env = {**os.environ, **SAMPLE_ENV, "SYSTEMD_WANTED_BY": "multi-user.target"}
    result = subprocess.run(
        ["envsubst"],
        input=template_path.read_text(),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"envsubst failed: {result.stderr}"
    assert "WantedBy=multi-user.target" in result.stdout, (
        f"{template_path.name} should render WantedBy=multi-user.target in system scope"
    )
    assert not _extract_variables(result.stdout), "no tokens should remain after substitution"


# ── 8. bind-intent is opt-in, not hardcoded public (#252) ─────────────────────

API_TEMPLATE = DEPLOY_DIR / "palinode-api.service.template"


def test_api_template_does_not_hardcode_public_bind_intent() -> None:
    """The API template must NOT hardcode PALINODE_API_BIND_INTENT=public.

    Regression guard for the failed 2026-06-27 reconciliation: the hardcoded
    value forced the app's mandatory-token path, so a token-less network-isolated
    host (which binds 0.0.0.0 behind Tailscale with no token) crash-looped on
    `REFUSING TO START`. Bind-intent must be a parameter (#252)."""
    raw = API_TEMPLATE.read_text()
    # Target the Environment= directive specifically — the substring also appears
    # in the explanatory comment, which is fine.
    assert 'Environment="PALINODE_API_BIND_INTENT=public"' not in raw, (
        "API template hardcodes bind-intent=public, which forces a mandatory token "
        "and breaks token-less hosts — make it the ${PALINODE_API_BIND_INTENT} parameter"
    )
    assert 'Environment="PALINODE_API_BIND_INTENT=${PALINODE_API_BIND_INTENT}"' in raw, (
        "API template should render bind-intent from the PALINODE_API_BIND_INTENT variable"
    )


def test_install_sh_defaults_bind_intent_empty() -> None:
    """install.sh must default PALINODE_API_BIND_INTENT to empty so a plain
    install/reconcile does not force the mandatory-token path (#252)."""
    raw = INSTALL_SH.read_text()
    assert 'export PALINODE_API_BIND_INTENT="${PALINODE_API_BIND_INTENT:-}"' in raw, (
        "install.sh must default PALINODE_API_BIND_INTENT to empty (token-less default)"
    )


@pytest.mark.skipif(
    shutil.which("envsubst") is None,
    reason="envsubst not installed — skipping substitution test",
)
def test_api_template_empty_bind_intent_is_not_public() -> None:
    """With the default empty PALINODE_API_BIND_INTENT, the rendered API unit must
    NOT set bind-intent to public (the app's value-based check treats empty as
    not-public, so the API starts without requiring a token) (#252)."""
    env = {**os.environ, **SAMPLE_ENV, "PALINODE_API_BIND_INTENT": ""}
    result = subprocess.run(
        ["envsubst"],
        input=API_TEMPLATE.read_text(),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"envsubst failed: {result.stderr}"
    assert 'Environment="PALINODE_API_BIND_INTENT=public"' not in result.stdout, (
        "empty bind-intent must not render the public directive"
    )
    assert 'Environment="PALINODE_API_BIND_INTENT="' in result.stdout, (
        "empty bind-intent should render an empty value (treated as not-public)"
    )
    # And the opt-in path still works when explicitly requested:
    env_public = {**os.environ, **SAMPLE_ENV, "PALINODE_API_BIND_INTENT": "public"}
    result_public = subprocess.run(
        ["envsubst"],
        input=API_TEMPLATE.read_text(),
        capture_output=True,
        text=True,
        env=env_public,
    )
    assert 'Environment="PALINODE_API_BIND_INTENT=public"' in result_public.stdout, (
        "setting PALINODE_API_BIND_INTENT=public must render the public directive"
    )

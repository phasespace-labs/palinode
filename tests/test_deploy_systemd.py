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
    """[Install] must have WantedBy=default.target (user units)."""
    raw = template_path.read_text()
    assert "WantedBy=default.target" in raw, (
        f"{template_path.name} missing WantedBy=default.target in [Install]"
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
    """Templates must not mention the production hostname."""
    raw = template_path.read_text()
    forbidden = ["clawdbot", "engram-data", "engram", "10.2.1"]
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

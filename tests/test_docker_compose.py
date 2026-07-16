"""
tests/test_docker_compose.py — Validate the Docker one-command install surface
(docker-compose.yml + Dockerfile + entrypoint) and the launchd templates.

Checks:
  1. docker-compose.yml parses and declares exactly the promised services:
     api + watcher + ollama + one-shot model pull. No Qdrant — the default
     deploy ships clean (the resolution of the unused-Qdrant-container report).
  2. The memory dir is a host bind mount on both palinode services
     (files-are-truth must survive the containers), and both point at the
     bundled Ollama by default while remaining env-overridable.
  3. The API publishes loopback-only by default.
  4. The entrypoint git-inits the data dir (the app itself never does) and the
     Dockerfile installs git for commit-on-save.
  5. launchd plists parse as XML, bind loopback, and use only ${VARIABLE}
     placeholders documented in their README (same contract as systemd's).
"""

import plistlib
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "deploy" / "docker" / "entrypoint.sh"
LAUNCHD_DIR = REPO_ROOT / "deploy" / "launchd"
LAUNCHD_TEMPLATES = [
    LAUNCHD_DIR / "com.phasespace.palinode-api.plist.template",
    LAUNCHD_DIR / "com.phasespace.palinode-watcher.plist.template",
]


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


# ── compose: service inventory ────────────────────────────────────────────────

def test_compose_declares_promised_services():
    services = set(_compose()["services"])
    assert services == {"palinode-api", "palinode-watcher", "ollama", "ollama-init"}


def test_compose_ships_without_qdrant():
    assert "qdrant" not in COMPOSE.read_text().lower()


def test_ollama_init_is_one_shot_model_pull():
    svc = _compose()["services"]["ollama-init"]
    assert svc["restart"] == "no"
    assert svc["entrypoint"] == ["ollama", "pull"]
    assert "bge-m3" in svc["command"][0]


# ── compose: memory dir + ollama wiring ───────────────────────────────────────

def test_memory_dir_is_host_bind_mount_on_both_services():
    services = _compose()["services"]
    for name in ("palinode-api", "palinode-watcher"):
        volumes = services[name]["volumes"]
        data_mounts = [v for v in volumes if v.endswith(":/data")]
        assert data_mounts, f"{name} must bind-mount the memory dir at /data"
        host_side = data_mounts[0].rsplit(":/data", 1)[0]
        assert "PALINODE_DATA_DIR" in host_side, (
            f"{name} memory mount must be a host path (env-overridable), "
            f"not a named volume: {data_mounts[0]}"
        )
        assert services[name]["environment"]["PALINODE_DIR"] == "/data"


def test_ollama_url_defaults_to_bundled_service_and_is_overridable():
    services = _compose()["services"]
    for name in ("palinode-api", "palinode-watcher"):
        url = services[name]["environment"]["OLLAMA_URL"]
        assert url == "${OLLAMA_URL:-http://ollama:11434}"


def test_palinode_services_do_not_hard_depend_on_ollama():
    """`docker compose up palinode-api palinode-watcher` with a host Ollama
    must not drag the bundled one in via depends_on."""
    services = _compose()["services"]
    for name in ("palinode-api", "palinode-watcher"):
        deps = services[name].get("depends_on") or []
        assert "ollama" not in deps


def test_api_publishes_loopback_only_by_default():
    ports = _compose()["services"]["palinode-api"]["ports"]
    assert any(p.startswith("${PALINODE_BIND:-127.0.0.1}:") for p in ports)


# ── entrypoint + Dockerfile ───────────────────────────────────────────────────

def test_entrypoint_git_inits_data_dir():
    text = ENTRYPOINT.read_text()
    assert text.startswith("#!/bin/sh")
    assert "set -eu" in text
    assert "git init" in text
    assert "user.email" in text  # commits need an identity inside the container
    assert 'exec "$@"' in text


def test_entrypoint_marks_bind_mount_safe_for_root_git():
    """The bind mount is host-user-owned while container git runs as root;
    without safe.directory every git command after init fails (exit-128
    crash loop — caught by the 2026-07-12 smoke on a real docker host)."""
    assert "safe.directory" in ENTRYPOINT.read_text()


def test_dockerfile_installs_git_and_wires_entrypoint():
    text = DOCKERFILE.read_text()
    assert re.search(r"apt-get install .*git", text), "image needs git for commit-on-save"
    assert "deploy/docker/entrypoint.sh" in text
    assert 'ENV PALINODE_DIR=/data' in text


def test_dockerignore_keeps_context_minimal():
    lines = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert "*" in lines, ".dockerignore must be allowlist-style"
    for needed in ("!pyproject.toml", "!palinode", "!deploy/docker"):
        assert needed in lines


# ── launchd templates ─────────────────────────────────────────────────────────

def test_launchd_plists_parse_and_keepalive():
    for template in LAUNCHD_TEMPLATES:
        plist = plistlib.loads(template.read_bytes())
        assert plist["Label"].startswith("com.phasespace.palinode-")
        assert plist["KeepAlive"] is True
        assert plist["RunAtLoad"] is True
        assert "PALINODE_DIR" in plist["EnvironmentVariables"]


def test_launchd_api_binds_loopback():
    plist = plistlib.loads(LAUNCHD_TEMPLATES[0].read_bytes())
    args = plist["ProgramArguments"]
    assert args[args.index("--host") + 1] == "127.0.0.1"


def test_launchd_placeholders_documented_in_readme():
    readme = (LAUNCHD_DIR / "README.md").read_text()
    for template in LAUNCHD_TEMPLATES:
        placeholders = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", template.read_text()))
        placeholders.discard("HOME")  # ambient, not operator-supplied
        undocumented = {p for p in placeholders if p not in readme}
        assert not undocumented, f"{template.name}: undocumented placeholders {undocumented}"

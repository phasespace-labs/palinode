# Palinode Pre-Launch Checklist

Checklist for v1.0 public launch readiness.

## Harness smoke (release blocker)

Every supported MCP harness must pass the canonical 5-call smoke sequence
before a release tag is cut. The smoke sequence is documented in
[docs/HARNESS-SMOKE.md](HARNESS-SMOKE.md); the `palinode mcp-smoke`
CLI subcommand prints the runbook and records results.

**Tier 1 — required per PR (CI) + every release:**

- [ ] Claude Code (Tier 1) -- `palinode mcp-smoke claude-code --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`
- [ ] Codex (Tier 1) -- `palinode mcp-smoke codex --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`
- [ ] Generic IDE (Tier 1) -- `palinode mcp-smoke generic-ide --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`

**Tier 2 — required every release:**

- [ ] Cursor (Tier 2) -- `palinode mcp-smoke cursor --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`
- [ ] Claude Desktop (Tier 2) -- `palinode mcp-smoke claude-desktop --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`
- [ ] Cline (Tier 2) -- `palinode mcp-smoke cline --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`
- [ ] Zed (Tier 2) -- `palinode mcp-smoke zed --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`
- [ ] Windsurf (Tier 2) -- `palinode mcp-smoke windsurf --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`
- [ ] Continue (Tier 2) -- `palinode mcp-smoke continue --record --operator <name>` on YYYY-MM-DD; logged to `.palinode/harness-smoke-runs.jsonl`

**Tier 3** (OpenClaw, Hermes AI, Pi) is future/best-effort and not
release-blocking.

**This section cannot be ticked complete unless every Tier 1+2 checkbox
is checked AND `mcp-tool-coverage` CI is green on the release SHA.**

## Release requirements
- [ ] PyPI package published (`pip install palinode` resolves)
- [ ] GitHub org migration complete (#118)
- [ ] Integration test suite green on CI
- [ ] Security test suite green on CI (#123)
- [ ] CI/CD pipeline active (#121)
- [ ] MCP audit log (#116)

## Docs + marketplace
- [ ] README polished (screenshots, install paths, quick-start)
- [ ] CHANGELOG complete (Unreleased → versioned)
- [ ] Marketplace description + screenshots ready
- [ ] MCP-SETUP.md up to date (tool count, all platforms)
- [ ] `palinode doctor` passes on a clean install

## Quality gates
- [ ] `palinode doctor` passes on fresh install (no warnings)
- [ ] Release checklist complete on main
- [ ] Pre-launch: resolve any open `P0`/`P1` labeled issues

## Hardening (M1)
- [ ] `palinode doctor` umbrella complete — all planned checks in (#190)
- [ ] MCP config consistency (`palinode mcp-config --diagnose`) working across all platforms (#189)
- [ ] Silent DB auto-creation prevented on mismatched `memory_dir` (#188)
- [ ] `/health` and `/status` accuracy confirmed (#187)
- [ ] Service unit files tested end-to-end on a clean Linux host (#185)

## Distribution
- [ ] `palinode init` idempotent on all supported platforms (macOS, Linux)
- [ ] `palinode deploy-systemd` smoke-tested on Ubuntu 22.04+
- [ ] Homebrew formula (M6) or documented manual install as fallback
- [ ] `palinode --version` returns a clean semver string

## Test coverage
- [ ] Fresh-session context loading test passing (#186)
- [ ] Integration test gap closed (#198)
- [ ] Security suite passing (#123)
- [ ] MCP tool count assertion test (`tests/test_mcp_tool_count.py`) green

## Platform parity
- [ ] All 25 MCP tools verified working on Claude Code, Claude Desktop, Cursor
- [ ] Codex CLI MCP config documented and smoke-tested
- [ ] Generic IDE MCP config documented
- [ ] `palinode mcp-config --diagnose` covers all supported client paths

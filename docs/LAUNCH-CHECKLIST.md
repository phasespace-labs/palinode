# Palinode Pre-Launch Checklist

Checklist for v1.0 public launch readiness.

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
- [ ] All 21 MCP tools verified working on Claude Code, Claude Desktop, Cursor
- [ ] Codex CLI MCP config documented and smoke-tested
- [ ] `palinode mcp-config --diagnose` covers all supported client paths

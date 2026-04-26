# Palinode Roadmap

The canonical roadmap lives in **GitHub milestones**. This document explains the milestone structure, themes, and order rationale so contributors can orient quickly.

## Where to find the issues

GitHub milestones page (filterable, links to all issues per milestone):

- Browse open milestones in the repo's Issues tab → "Milestones"

Each milestone groups related work toward a coherent outcome. The seven milestones together describe the path from current state to v1.0 and beyond.

## The seven milestones

### M1 — Distribution + Adoption

Get Palinode in front of users across multiple IDEs. Smooth the install, make operational failures loud and recoverable, build the deploy story for self-hosters and the marketplace story for everyone else.

Current focus: a `palinode doctor` command (umbrella for proactive misconfiguration detection), templated service deployment, MCP client config consistency, CI/CD discipline.

### M2 — Agent Intelligence

Sessions start smart, agents hand off cleanly, memory is scoped to the right audience. The largest milestone by issue count — covers cross-surface parity, integration tests, scope-chain layers, structured handoff tokens, and the auto-inject / dashboard / brief / tasks tools.

### M3 — Memory Quality

Improve what gets stored and how it evolves. Epistemic markers (fact vs inference vs open question), typed cross-links (supports/contradicts/refines), semantic contradiction detection, classification + smart prime, milestone dependency modeling. Smaller scope but high leverage on long-term store quality.

### M4 — Import + Ecosystem

Bring existing knowledge in. Generic markdown import, Obsidian integration (research stage — palinode is markdown-first by design, the substrate is already compatible), GitLab-aware entity linking, team scopes / agent-to-agent channels, OpenClaw profile migration.

### M5 — Cloud + Team Sharing

Hosted deployment, shared memory for teams, and the operational foundation for managed use cases. This milestone covers the path from single-user local workflows to collaborative and hosted environments.

### M6 — v1.0 Packaging

Homebrew, Nix, Mintlify docs site, package architecture restructure. Unblocked when M1 hardening is done — can't ship `brew install palinode` until silent-misconfiguration classes are caught proactively.

### M7 — Research + Speculative

Things worth thinking about that aren't yet committed: query-time delta compilation, prompt A/B testing, Cowork integration, multimodal embeddings (image/PDF), model step-change preparation, Palinode self-tracking, and rationalizing legacy memory workflows.

## Order rationale

The order is intentional, not alphabetical:

```
M1 (foundation) → M2 (intelligence) → M3 (quality) → M4 (ecosystem) → M5/M6 (productization) → M7 (research)
```

- **M1 first.** Hardening blocks adoption. If silent-misconfiguration bites every new self-hosted user, no amount of feature work matters. Foundation precedes capability.
- **M2 next.** The largest leverage point once foundation is solid is "make sessions actually smart" — handoff tokens, scope chains, parity contract enforcement.
- **M3 third.** Memory quality builds on the agent-intelligence APIs (you need the surfaces before you refine what flows through them).
- **M4 fourth.** Ecosystem integrations (Obsidian, GitLab, generic markdown import) work best on a stable, intelligent core. *Note: M4's research-stage items can begin in parallel with M1 — different code areas, different audiences, no conflict.*
- **M5/M6 productization.** Cloud and packaging assume the underlying product is solid. Order between them is parallel-safe; hosted and team-sharing work also carries additional product and deployment dependencies that affect timing.
- **M7 last.** Research items inform future direction but don't block shipping.

## How to contribute

- Browse the milestone you care about and look for `good first issue` labels
- Bug reports and reproduction details are always welcome — file an issue with `bug` label
- For larger proposals, open a discussion or draft an ADR before implementation
- Each PR that touches public-shipping code should add a `CHANGELOG.md` entry under Unreleased

## Related documents

- `docs/CHANGELOG.md` — what shipped when
- `ADR-001-tools-over-pipeline.md` and other ADRs in repo root — architectural decisions
- `PROGRAM.md` — behavioral spec for memory extraction and consolidation

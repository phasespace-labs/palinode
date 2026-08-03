# Roadmap

**Current version:** 0.10.x · **Status:** usable, actively developed, pre-1.0
**Last reviewed:** August 2026

This is a direction document, not a delivery schedule. Palinode has one maintainer,
so it carries themes and priorities rather than dates. Items move when they are
ready.

---

## What Palinode is trying to be

Palinode is the **reference implementation of auditable agent memory**.

Agent memory is becoming a commodity — model vendors, frameworks, and hosted services
all ship some version of it. *Auditable* agent memory is not. The specific bet here is
that as agents accumulate more consequential memory, the question stops being "can it
remember?" and becomes **"can you check what it remembered, and why it believes it?"**

Concretely, that means a memory record can carry:

- an explicit **epistemic status** — is this a fact, an inference, an open question, or
  merely unverified?
- **typed links to evidence** — declared `backed_by` and `contradicts` relationships,
  not co-occurrence inferred after the fact
- a **verifiable, quote-level citation** — this claim came from these exact words, and
  here is the hash proving the words have not changed
- a **git commit** recording who wrote it and when, so it can be diffed and reverted

…and that the model **never writes any of it directly**. It proposes operations; a
deterministic executor validates and applies them.

Being the *reference* implementation is the goal, not a consolation prize. Palinode is
not trying to out-scale hosted memory services or win on install base. It is trying to
be the clearest and most correct expression of this idea, so that the idea can be
adopted — including by other systems.

---

## Now

Work in progress or next up.

**External benchmark evidence.** Palinode's differentiating behaviours — contradiction
resolution, abstention, temporal reasoning — have historically had no external
scoreboard, because the widely used memory benchmarks do not test them. That is
changing, and running against a benchmark that *does* test them is the current
priority. Results will be published with methodology and with the cases Palinode loses,
not just the ones it wins.

**Interoperability of the record format.** The epistemic status, evidence links, and
citation structure are currently expressed as Palinode's frontmatter schema. They are
not Palinode-specific ideas, and the intent is to specify them separately so that other
memory systems can implement them without adopting Palinode. See *Direction* below.

**Contribution surface.** Documentation, contribution guidelines, and a supply of
well-scoped issues, so that the project is straightforward to help with.

---

## Next

Reasonably well understood; not yet started.

**[Bi-temporal claims](https://github.com/phasespace-labs/palinode/issues/76).** Palinode currently records *when we learned something* (git
gives this for free). It does not record *when that thing was true*. Separating event
time from record time is what turns "what did the file say on this date" into "what did
we believe was true as of this date" — the second being the question an auditor asks.

**[Idle-time consolidation](https://github.com/phasespace-labs/palinode/issues/77).** Compaction is currently invoked explicitly. Running it
during idle periods is low-risk here specifically because the deterministic executor
already gates every write.

**Abstention quality.** Knowing when to return *nothing* is a memory behaviour in its
own right. Weak-match recall where the honest answer is "no relevant memory" is a bug,
and it should be measured as one.

**Data lifecycle.** Immutable git history and a right to erasure are in genuine tension.
This needs a documented answer — tombstones, redaction in place, scoped key destruction
— rather than an improvised one.

---

## Direction

Longer-horizon intent. These shape decisions now even though nothing is scheduled.

**A portable specification for auditable memory records.** The most useful thing this
project can produce may not be the software. If epistemic status, typed evidence links,
and span-level citation are good ideas, they should be written down in a form any memory
system can implement — with conformance criteria and a test suite — rather than living
only inside one Python package. Palinode would then be the reference implementation of
that spec, and other systems adopting it would count as success.

**Staying local-first.** A directory of markdown, one SQLite file, and an embedding
endpoint. No required cloud dependency, no service you have to trust to read your own
memory. If every service is down, `cat` still works.

**Editor and harness neutrality.** One memory, reachable over MCP from whichever tool
you happen to be using.

---

## Explicit non-goals

Saying no clearly is more useful than leaving it ambiguous.

- **Not a hosted service.** No managed offering is planned. Self-hosting is the model.
- **Not competing on scale.** Systems built for millions of end-user profiles across a
  multi-tenant fleet are solving a different problem. Palinode optimizes for
  inspectability and correctness at the scale of a person, a project, or a team.
- **Not adding required infrastructure.** Postgres, Redis, a message broker, or a
  vector-database service will not become required components.
- **Not a general knowledge base or RAG pipeline.** Palinode stores what an agent
  learned and can justify, not everything an organization knows.
- **Not chasing benchmark leaderboards.** Benchmarks are used here as evidence, and
  results get published including losses. Tuning to a benchmark is not a goal.
- **No speculative abstractions.** Extension points arrive when there is a second
  caller, not in anticipation of one.

---

## Stability

Pre-1.0, so interfaces can still change, but in practice:

- **The file format is the most stable thing here** and is treated as such. Frontmatter
  fields are added, rarely repurposed, and schema versioning exists to migrate them.
- **The MCP tool surface** is stable in semantics; tools may gain optional parameters.
- **Python internals** are not a public API and change freely.
- Breaking changes are called out in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).

The 1.0 marker is about the record format and the audit surface being settled enough to
build on — not about feature completeness.

---

## Influencing this

Open an issue. Concrete use cases are far more persuasive than feature requests: "I
tried to do X and hit Y" will move something up this list much faster than "please add
X." If you are using Palinode for something, saying so is genuinely useful — with one
maintainer and no telemetry, the only signal available is what people say out loud.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) to get started.

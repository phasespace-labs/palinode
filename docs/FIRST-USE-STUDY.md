# First-use study — self-run protocol

**Status: NOT RUN — zero participants.** This protocol has five unfamiliar
volunteers each run the fictional Harbor Notes journey alone, on their own
machine, against the v0.21 release checkout named in the recruitment issue,
and report what the run left behind.
It is not product evidence; do not substitute unit tests, synthetic agents,
maintainer use, or mechanical client evidence for a human result.

There is no observer. The evidence class this produces is stated plainly in
[Evidence class](#evidence-class-read-before-quoting-a-result); read it before
quoting any number from this study.

This document is for maintainers and reviewers. Participants do not read it,
and they do not read the checkout's copy of the
[participant script](FIRST-USE-PARTICIPANT-CARDS.md) either: the candidate
they install predates this protocol, so the **recruitment issue carries the
complete participant script inline**, and the public report form carries the
commands and questions on its own. The shipped script file is the maintained
reference for the next release. The maintainer's scoring key for the
comprehension questions lives in the private kit and is never published;
nothing in this document is an answer key.

## Candidate, cohort, and clean start

Participants install **the v0.21 checkout named in the recruitment issue**
(`<CHECKOUT_POINTER>`), which is the "checkout selected by your maintainer"
that the [Quickstart](QUICKSTART.md) refers to. The Quickstart is the only
declared study resource; the participant may read all of it. Record the
pointer, the reported `palinode --version`, OS, Python version, client and
version, and explicit lexical retrieval mode from each report. A run on any
other checkout is a different candidate and is recorded as such.

Both flagship paths count and are reported separately: Claude Code with the
opt-in hook plus MCP, and Codex CLI with MCP plus project instructions. Hooks
are automatic behavior only on Claude; Codex session-init and search are
explicit MCP calls. Report actual counts per path; never generalize to editors
nobody ran.

Recruitment is **opt-in registration before starting**, not personal
invitation and not passive discovery. A volunteer who has never installed
Palinode posts a one-word "in" comment on the public recruitment issue before
they begin; the maintainer records that handle on the private register with
the date and the intended client path, and that comment is the moment of
**enrollment**. The fourteen-day no-report clock runs from it. The issue
carries the script and the intake form. A report from someone who never
registered is welcome and is recorded outside the cohort. Where the volunteers
come from — a short call for testers through the maintainer's own channels —
is a separate, authorized outreach step; the issue alone does not surface
them.

**Pilot first.** The first registered volunteer runs setup and one fresh
recall only (script steps 1–4), with a fifteen-minute stopping point; stopping
is a valid, reported result. Fix whatever that person hits before the next
volunteer starts, and widen to the full correction journey and the five-person
target from there. The pilot report is kept and counts toward nothing but the
protocol.

Eligibility is **never installed Palinode before**, asked once at recruitment
and again on the report form. A participant who has previously installed,
contributed to, or facilitated Palinode is recorded as ineligible, and any run
they submit is filed as a contributor report, outside the cohort denominator.
Each participant contributes one original attempt; a retest after a fix is a
new attempt linked to the original, and the original is never overwritten.

The participant prepares their own clean start by following the Quickstart:
a new Git-backed `PALINODE_DIR` outside the checkout, a test-only Git
identity, no remote, an empty Harbor Notes project, and their real client
profile as it is (recorded, not cleaned). Nobody pre-generates or merges the
MCP configuration for them.

## Consent and minimal data

The report form carries the consent text. It collects only: an anonymous
handle, the checkout pointer, `palinode --version`, OS, Python and client
versions, the store's Git log subjects, a filtered retrieval log, a handful of
timestamps, stage reached, free-text answers about fictional Harbor Notes
material, and a follow-up choice. It does not collect source, credentials,
real prompts, real memories, raw logs, screen recordings, or automatic
diagnostics. `palinode doctor --json` is deliberately **not** requested: it
contains the home directory path and therefore the local username. The
participant is asked to look over every pasted block before submitting and to
remove anything that is not fictional Harbor Notes material.

## What the run leaves behind

The study replaces the observer's timer and hint log with artifacts the
product writes on its own during an ordinary Quickstart run. Each was verified
against a throwaway lexical store before this protocol was written.

**The store's Git log.** Every write is a commit with a content-free subject.
A full rehearsal of the journey against a fresh install (2026-09-16) produced
this shape — ten commits, not five:

```text
2026-09-16 17:54:49 -0700 palinode update capture policy
2026-09-16 17:54:50 -0700 palinode update capture policy
2026-09-16 17:54:51 -0700 palinode auto-save: decisions/harbor-notes-storage.md
2026-09-16 17:54:54 -0700 palinode auto-save: decisions/harbor-notes-timestamps.md
2026-09-16 17:54:55 -0700 palinode auto-save: decisions/harbor-notes-concurrent-write-requirement.md
2026-09-16 17:54:55 -0700 palinode auto-save: decisions/harbor-notes-storage-shared.md
2026-09-16 17:54:55 -0700 palinode supersede: decisions/harbor-notes-storage.md -> decisions/harbor-notes-storage-shared.md
2026-09-16 17:54:56 -0700 palinode: auto-update cross_refs for decisions/harbor-notes-storage-history.md
2026-09-16 17:54:58 -0700 palinode update capture policy
```

Subjects name the file, never the body. **Match the five milestone subjects;
tolerate the rest.** The Quickstart's early controls step commits
`update capture policy` lines before any save, and the watcher adds an
`auto-update cross_refs` commit after the supersession. Save A is the first
`auto-save … harbor-notes-storage.md` commit, not the first commit; Correction
B is the `supersede` commit. The count of operational commits varies with
watcher timing and means nothing. Git init itself creates no commit, so the
setup start is the one moment the participant notes by hand.

**The retrieval audit log.** `PALINODE_DIR/.audit/retrievals.jsonl` records
one row per **delivered record**: a UTC timestamp, the calling surface as
labelled by the router (`palinode_search` for the client's MCP search tool,
`palinode_read` and `palinode_history` for reads, `cli_search` for the
Quickstart's own CLI checks, `api_search` for a bare API call), the query, the
record's store-relative ref, its disposition, and the scope the server
resolved. Three limits, each confirmed in rehearsal, bound what it can say:

- **A search that delivers nothing writes one call-level row, from the
  release after v0.21.0.** It carries the query, source, scope and receipt
  coverage, an empty ref, and disposition `none_delivered`, so "searched and
  found nothing" is distinguishable from "never searched". On the pinned
  `v0.21.0` tag the rehearsal's missing-evidence query returned an explicit
  `no_match` receipt and added zero rows; there, an absent row cannot make
  that distinction, the participant's one-line answer is the record of those
  outcomes, and the study never assigns a failure stage from a missing row
  alone.
- **Session-init plus read is a valid route with no search row.** A client
  that recovers the decision through `palinode_session_init` and then
  `palinode_read` leaves only a `palinode_read` row. Any client-sourced row
  after Save A counts as consultation. The absence of one tool's name is not
  evidence that Palinode was not consulted.
- **The `mode` column is delivery mode** (`explicit` or `passive`), not
  lexical versus hybrid retrieval. Retrieval mode is read from the reported
  environment, never from this log.

The raw `file_path` is absolute and would carry the home directory, so the
script prints a filtered view — timestamp, source, store-relative ref,
disposition, scope, query — with the `python3` the Quickstart already
requires, and prints a stated `NO RETRIEVAL LOG` line instead of failing when
the file does not exist yet. From it the maintainer reads when the
fresh-session recall happened, through which surface, **which record was
delivered** (the replacement or the retired one), and under which scope.

**Self-reported fields, and only these:** the setup start time; what the
client answered at fresh recall and at corrected recall, in one line each; the
stage reached and where it stuck; anything used beyond the declared resources;
and the four comprehension answers.

**The declared resources are the Quickstart and every document it links
inside the checkout's `docs/`** (the MCP install recipes, the config-homes
map, the privacy contract, and the others it names). Following a link the
Quickstart itself provides is part of the documented path, not assistance.
Assistance is anything outside those documents: a web search, another person,
the source code, or the coding client itself.

## Required journey and stages

The frozen order is the Quickstart's own: install and start → controls
walkthrough → Save A → connect and restart the client → fresh recall →
inspect → UTC neighbor, supporting requirement, and Correction B → fresh
corrected recall. The optional sections (abstention, conflict, controls
exercise, linked worktree) follow and are reported but do not gate
completion.

Required completion is install → Save A → client ready → fresh recall →
inspect → Correction B → fresh corrected recall, **unassisted by the
participant's own declaration**. The target is at least 4 of 5 enrolled
volunteers completing this journey. It is a target, never a prefilled result,
and it is not a trust, privacy, security, or retention rate.

| Stage | Evidence | Source |
| --- | --- | --- |
| Setup start | Timestamp | Self-reported (one line) |
| Save A | First `auto-save` commit | Store Git log |
| Client ready | Not directly evidenced; bounded by the first client-sourced retrieval | Retrieval log |
| Fresh recall | First retrieval from the client after Save A, plus what the client answered | Retrieval log + one self-reported line |
| Inspect | `palinode read … --meta` or inspector visit, stated by participant | Self-reported |
| Correction B | `supersede` commit preceded by the three `auto-save` commits | Store Git log |
| Fresh corrected recall | First client retrieval after the `supersede` commit, plus what the client answered | Retrieval log + one self-reported line |

Assistance means anything beyond the Quickstart and the participant script:
another document, a search engine, an AI assistant, a person, or the source
code. The participant declares it in one field. A run declared assisted is
recorded as assisted; a run declared unassisted is recorded as unassisted by
declaration. There is no way to verify the declaration, and this protocol
does not pretend otherwise.

## Comprehension and the trust checks

The three comprehension questions drawn from the trust acceptance work are
answered in the participant's own words on the report form: what Palinode is
configured to capture, where captured content can go, and what pausing
capture and recall does and does not stop. A fourth asks what remains stored
after a correction and who could still read it. Each question asks for **one
concrete example** from the participant's own run, not a general
explanation, because written answers on a form tend to be terse and a terse
general answer cannot be scored.

Using the coding client itself to explain an error or suggest a next step is
assistance. It is expected, it is fine, and it must be declared on the form;
the participant is sitting inside Claude Code or Codex for the whole run and
asking it is the reflex. A declared AI-assisted run is scored assisted, not
excluded.

The maintainer scores each answer against the private key as correct,
incorrect, uncertain, or not answered, and records the result **separately
from completion**. Before scoring an answer uncertain, the maintainer may post
**one** clarifying comment on the participant's report issue, after the run
is over; the reply is scored and the exchange stays on the issue. That is the
only follow-up, and it never touches the journey classification. These are comprehension observations. They are never
summed into a trust or security success rate, and a complete journey with an
incorrect comprehension answer is still a complete journey with an incorrect
comprehension answer.

## Failure, denominator, and decision

Classify the first blocking stage from the participant's report: eligibility,
clean install, configuration/tools, Save A, fresh recall, inspect, correction,
corrected recall, worktree/scope, controls, participant stop, or
environment/service. Preserve later failures as separate events. A pre-start
decliner is not enrolled; an enrolled participant who stops stays in the
denominator with the stated reason.

**The denominator is the enrolled cohort, never the count of reports that
arrived.** Frustrated participants close the tab and do not file; a
report-only denominator would turn ten attempts with six silent abandonments
into 4/4. An enrolled participant with no report after fourteen days is
recorded as **no report** on their own row and counts against the target
exactly as a stop would. The recruitment issue says that a two-minute partial
report ("I stopped at step 3, here is where") is the most useful thing a
participant can send, and the form makes that possible with only the stage
and the assistance field required. The participant's self-rated experience
with command-line developer tooling is recorded so a result can say who the
completions were; the documented source-checkout install is the path under
test, and a cohort that only experienced developers can get through is a
finding, not a sampling error to design away.

File every concrete failure against the owning issue, quoting the sanitized
report, and retest the corrected step with a new linked attempt. The original
attempt stays in the register beside the retest; never average, overwrite, or
call an assisted retest unassisted.

Report invited, enrolled, consented, started, eligible, completed
unassisted-by-declaration, completed assisted, stopped, no report, and
missing follow-up, separately by path. With zero participants every result is NOT RUN, not 0/5
failed and not 4/5 passed.

| Condition | Required disposition |
| --- | --- |
| Fewer than five enrolled, any enrolled participant with no report, missing data, or missed target | **Defer** broader promotion; list the gap and the remediation. |
| Disclosure, mutation, or trust failure in the promoted path | **Revise or disable/remove the path**; no completion count waives it. |
| Five attempts reported, target met, no open gate | **Candidate for a reviewed promotion decision**, not automatic approval. |

## Evidence class — read before quoting a result

This is a self-run, artifact-instrumented study. Compared with an observed
trial it keeps the timestamps and the outcome, and gives up three things:

- **"Unassisted" is self-declared.** Nobody watched. The declaration field is
  the whole record of assistance.
- **Hesitation and near-misses are unrecorded** unless the participant
  volunteers them in the free-text field. A stage that took ten confused
  minutes and a stage that took ten confident minutes look the same in the
  log.
- **Comprehension is written, not conversational.** The scorer cannot ask a
  follow-up; an ambiguous answer is scored uncertain, not resolved.

Release notes, README claims, and any public statement that cites this study
must say "self-reported, unassisted by declaration" and must not describe the
runs as observed. Command and fixture evidence establishes a mechanical path
only; this study establishes what five people reported, and no more.

## Optional two-week follow-up

Offer, never require, an opt-in follow-up after the initial report. No
outreach, enrollment, telemetry upload, or participant result is created by
this plan. Separate observed behavior, reported benefit, and counterfactual
estimate; do not invent time saved. A small cohort over two weeks does not
establish retention; make a distinct go/revise/defer decision.

## Reusable blank record

| Field | Blank value |
| --- | --- |
| Anonymous participant / attempt ID | NOT RUN |
| Checkout pointer, `palinode --version`, retrieval mode | NOT RUN |
| OS, Python, client/version, path (Claude hooks+MCP / Codex MCP) | NOT RUN |
| Setup start (self-reported) | NOT RUN |
| Save A commit time | NOT RUN |
| First client-sourced retrieval time and source | NOT RUN |
| Fresh recall answer (one line) | NOT RUN |
| `supersede` commit time | NOT RUN |
| First client retrieval after supersede, and answer | NOT RUN |
| Declared assistance beyond the Quickstart | NOT RUN |
| First failure stage / later event stages | NOT RUN |
| Required journey: unassisted by declaration / assisted / failed / stopped | NOT RUN |
| Original attempt / linked retest and change | NOT RUN |
| Follow-up choice / missing reason | NOT RUN |

| Comprehension question | Correct | Incorrect | Uncertain / not answered |
| --- | --- | --- | --- |
| What is configured to be captured | | | |
| Where captured content can go | | | |
| What pause does and does not stop | | | |
| What remains after a correction, and who can read it | | | |

| Cohort denominator | Claude | Codex | Total |
| --- | ---: | ---: | ---: |
| Started | 0 | 0 | 0 |
| Eligible for required journey | 0 | 0 | 0 |
| Completed, unassisted by declaration | 0 | 0 | 0 |
| Completed, assisted | 0 | 0 | 0 |
| Stopped / missing follow-up | NOT RUN | NOT RUN | NOT RUN |

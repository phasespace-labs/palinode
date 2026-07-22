---
id: project-my-app-status
category: project-status
entities: [project/my-app, person/alice, person/bob]
last_updated: 2026-03-28
consolidation_passes: 1
---

# My App — Status (Pass 1: after UPDATE + SUPERSEDE)

> This file shows the state after the nightly/debounced reflection pass. The executor applied 13 operations proposed by the LLM: 6 UPDATE, 4 SUPERSEDE, 2 KEEP, 1 NOOP. Nothing has been merged or archived yet — that's the next pass. See `pass-0-initial.md` for the raw state and the inline diff comments below for what changed.

## Current Work

- [2026-03-08] Kicked off checkout redesign. Stack TBD — evaluating React Native vs Flutter. <!-- fact: f-0308-1 supersede: f-0309-1 reason: stack was chosen 2026-03-09 -->
- [2026-03-09] Chose React Native (Expo). Bob has 2 years Expo experience. <!-- fact: f-0309-1 op: UPDATE from: "Bob has Expo experience" reason: tightened wording -->
- [2026-03-10] Starting API layer design. Considering GraphQL for flexibility. <!-- fact: f-0310-1 supersede: f-0315-1 reason: REST was chosen 2026-03-15 -->
- [2026-03-11] GraphQL exploration ongoing. Apollo Server looks promising. <!-- fact: f-0311-1 supersede: f-0315-1 reason: REST was chosen 2026-03-15 -->
- [2026-03-12] Leaning REST — Stripe webhooks are REST-native. <!-- fact: f-0312-1 op: UPDATE from: "Actually, leaning REST now" reason: removed hedge word -->
- [2026-03-13] tRPC spike: nice DX but Python service plans rule it out. <!-- fact: f-0313-1 op: UPDATE from: "Tried tRPC in a spike" reason: tightened wording -->
- [2026-03-15] **Decision: REST over GraphQL.** See `decisions/api-design.md`. <!-- fact: f-0315-1 op: KEEP reason: current canonical decision -->
- [2026-03-16] Stripe SDK integration started (Alice's track). <!-- fact: f-0316-1 op: UPDATE from: "Started Stripe SDK integration. Alice's track." reason: tightened -->
- [2026-03-17] Stripe integration blocked on webhook signing. Waiting on Bob. <!-- fact: f-0317-1 supersede: f-0318-1 reason: resolved the next day -->
- [2026-03-18] Webhook signing resolved — using raw body middleware. <!-- fact: f-0318-1 op: KEEP reason: still the implementation approach -->
- [2026-03-19] First end-to-end checkout succeeded in dev. <!-- fact: f-0319-1 op: NOOP reason: already concise -->
- [2026-03-20] QA caught three edge cases: expired cards, 3DS, address mismatch. <!-- fact: f-0320-1 op: UPDATE from: "QA caught three edge cases" reason: enumerated for clarity -->
- [2026-03-21] Expired card handling fixed. <!-- fact: f-0321-1 -->
- [2026-03-22] 3DS flow added; needs more testing. <!-- fact: f-0322-1 op: UPDATE from: "Need more testing" reason: tightened -->
- [2026-03-23] Address mismatch deferred to post-launch. <!-- fact: f-0323-1 -->
- [2026-03-24] Stripe integration complete. All tests passing. <!-- fact: f-0324-1 supersede: f-0324-2 reason: f-0324-2 corrected the date -->
- [2026-03-24] Stripe integration was complete on 2026-03-18; re-verified today. <!-- fact: f-0324-2 op: UPDATE from: "Actually, Stripe was already done on the 18th" reason: removed "actually" + clarified -->
- [2026-03-25] QA pass 1 complete. 2 blockers remaining. <!-- fact: f-0325-1 -->
- [2026-03-26] Blocker 1 fixed: cart persistence across app restarts. <!-- fact: f-0326-1 -->
- [2026-03-27] Blocker 2 fixed: Apple Pay sheet dismissing early on iOS 17. <!-- fact: f-0327-1 -->
- [2026-03-27] Apple Pay status query (resolved 2026-03-28: it's a launch requirement). <!-- fact: f-0327-2 op: UPDATE from: "Why is it a blocker?" reason: question was answered, recording the answer -->
- [2026-03-28] Apple Pay is a launch requirement per Alice's 2026-03-18 note. <!-- fact: f-0328-1 -->
- [2026-03-28] Launch target: 2026-04-15. On track. <!-- fact: f-0328-2 op: UPDATE from: "Launch target still April 15" reason: ISO date format -->

## Open Questions

- ~~Do we need Apple Pay / Google Pay for launch or can it wait?~~ <!-- resolved 2026-03-28 via f-0328-1 -->
- Error handling UX for declined cards — show inline or modal?
  <!-- still open -->

## Consolidation Log

### 2026-03-29 (nightly pass)
- [UPDATE] f-0309-1: tightened wording
- [UPDATE] f-0312-1: removed hedge word
- [UPDATE] f-0313-1: tightened wording
- [UPDATE] f-0316-1: tightened wording
- [UPDATE] f-0320-1: enumerated edge cases for clarity
- [UPDATE] f-0322-1: tightened wording
- [UPDATE] f-0324-2: removed "actually" + clarified
- [UPDATE] f-0327-2: replaced question with its answer
- [UPDATE] f-0328-2: ISO date format
- [SUPERSEDE] f-0308-1 → f-0309-1: stack was chosen the next day
- [SUPERSEDE] f-0310-1 → f-0315-1: REST was chosen 2026-03-15
- [SUPERSEDE] f-0311-1 → f-0315-1: REST was chosen 2026-03-15
- [SUPERSEDE] f-0317-1 → f-0318-1: resolved the next day
- [SUPERSEDE] f-0324-1 → f-0324-2: date corrected
- [KEEP] f-0315-1: current canonical decision
- [KEEP] f-0318-1: still the implementation approach
- [NOOP] f-0319-1: already concise

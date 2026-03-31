---
id: project-my-app
category: project
name: Mobile Checkout Redesign
status: active
core: true
entities: [person/alice, person/bob]
last_updated: 2026-03-31
summary: "Mobile checkout redesign for Acme Corp. React Native + Stripe. Q2 launch target April 15. Currently in integration testing."
---
# Mobile Checkout Redesign

## What This Is
Complete redesign of the mobile checkout flow for Acme Corp's e-commerce app. Goal: increase conversion rate from 2.1% to 3.5%.

## Architecture
- Frontend: React Native (Expo)
- Payment: Stripe SDK (replacing legacy PayPal integration)
- Backend: Node.js API on Render
- Database: Postgres + Prisma ORM

## Status
Integration testing phase. Core checkout flow works. Payment provider API integration in progress (Bob's track).

## Milestones
- [x] Design review complete (March 1)
- [x] Core UI components built (March 15)
- [ ] Stripe integration complete (target: April 1)
- [ ] QA pass (target: April 8)
- [ ] Launch (target: April 15)

## Key Decisions
- Chose Stripe over Square — better React Native SDK, lower per-transaction fees
- Single-page checkout (not multi-step) — A/B test showed 23% higher completion
- Guest checkout enabled by default — reduces friction for first-time buyers

## Open Questions
- [ ] Do we need Apple Pay / Google Pay for launch or can it wait?
- [ ] Error handling UX for declined cards — show inline or modal?

---
id: decision-api-design
category: decision
core: false
entities: [project/my-app]
last_updated: 2026-03-31
summary: "Decided on REST over GraphQL for the checkout API — simpler caching, team expertise, Stripe webhooks are REST-native."
---
# Decision: REST API for Checkout

## What Was Decided
Use REST endpoints for the checkout API instead of GraphQL.

## Why
- Team has 3 years of REST experience, 0 with GraphQL
- Stripe webhooks are REST-native — no translation layer needed
- CDN/edge caching is straightforward with REST (cache by URL)
- Checkout flow has predictable data shapes — GraphQL flexibility isn't needed

## What We Considered
- **GraphQL**: flexible queries, single endpoint, but steeper learning curve and no caching story for mutations
- **tRPC**: type-safe, but too coupled to TypeScript — we may need Python services later

## Trade-offs Accepted
- Multiple endpoints to maintain (vs single GraphQL endpoint)
- Over-fetching on some routes (acceptable for checkout — small payloads)
- If we add a mobile-specific BFF later, GraphQL might make more sense

## Date
2026-03-15 — decided in architecture review with Alice and Bob.

---
id: insight-testing-strategy
category: insight
core: false
entities: [project/my-app]
last_updated: 2026-03-31
summary: "Integration tests catch more real bugs than unit tests for API-heavy apps. Write fewer, broader tests."
---
# Insight: Integration Tests > Unit Tests for API Work

## The Lesson
For API-heavy applications (like our checkout flow), integration tests that hit real endpoints catch significantly more production-relevant bugs than isolated unit tests.

## Evidence
- 3 critical bugs in the Stripe integration were caught by integration tests, 0 by unit tests
- The bugs were all at the boundary: request serialization, webhook signature verification, idempotency key handling
- Unit tests with mocked HTTP calls passed fine — the mocks were wrong

## The Principle
When your app is mostly glue between services, test the glue, not the pieces. Write fewer tests that cover more surface area.

## When This Applies
- Payment integrations (API boundaries)
- Multi-service architectures (service-to-service contracts)
- Webhook handlers (real HTTP matters)

## When It Doesn't Apply
- Pure computation (math, parsing, algorithms) — unit tests are better
- UI components — snapshot/visual tests are better

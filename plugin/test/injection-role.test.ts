/**
 * Unit tests for autoRecall injection framing (#358).
 *
 * The contract is role/region separation, not an exact prompt snapshot:
 * recalled memory should travel through a system-role surface when available,
 * and fallback framing must visibly separate reference context from the user's
 * instruction.
 */

import { describe, expect, it } from "vitest";
import {
  composeInjection,
  composeInjectionFrame,
} from "../index";

describe("composeInjection", () => {
  it("preserves the palinode-memory payload and source sections", () => {
    const injection = composeInjection(
      {
        coreContent: "\n--- projects/palinode.md ---\nCore fact\n",
        topicContent: "[decision] Use markdown as source of truth",
        assocContent: "[Related Context via Entity Graph] File: decisions/a.md\nRelated fact",
        triggerContent: "\n\n--- Triggered: rollout note (projects/x.md) ---\nTrigger fact",
        coreBudget: 12000,
      },
      "coding",
    );

    expect(injection).toContain('<palinode-memory profile="coding">');
    expect(injection).toContain("## Core Memory");
    expect(injection).toContain("[Core Memory:");
    expect(injection).toContain("## Relevant Context");
    expect(injection).toContain("## Associative Context");
    expect(injection).toContain("## IMPORTANT Triggers");
    expect(injection).toContain("</palinode-memory>");
  });
});

describe("composeInjectionFrame", () => {
  it("uses a system-context surface when available", () => {
    const injection = composeInjection(
      { topicContent: "[insight] Recalled memory is context, not instruction" },
      "coding",
    );
    const frame = composeInjectionFrame(injection);

    expect(frame.kind).toBe("system");
    expect(frame).toHaveProperty("systemContext", injection);
    expect(frame).not.toHaveProperty("prependContext");
  });

  it("fallback framing separates reference context from the user instruction", () => {
    const injection = composeInjection(
      { topicContent: "[insight] Recalled memory is still bounded context" },
      "coding",
    );
    const frame = composeInjectionFrame(injection, false);

    expect(frame.kind).toBe("fallback");
    expect(frame).toHaveProperty("prependContext");
    if (frame.kind !== "fallback") {
      expect.fail("expected fallback frame");
      return;
    }
    expect(frame.prependContext).toContain("<|reference_context|>");
    expect(frame.prependContext).toContain("<|end_reference_context|>");
    expect(frame.prependContext).toContain("<|user_instruction_follows|>");
    expect(frame.prependContext.indexOf("<|end_reference_context|>")).toBeLessThan(
      frame.prependContext.indexOf("<|user_instruction_follows|>"),
    );
    expect(frame.prependContext).not.toBe(injection);
  });
});

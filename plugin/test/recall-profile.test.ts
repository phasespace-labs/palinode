/**
 * Unit tests for the recall-profile primitive (#391 / #394).
 *
 * Covers `resolveProfile` composition semantics:
 *   - autoRecall: false short-circuits to "off"
 *   - Each named preset returns the expected source list
 *   - recallProfileConfig shallow-merges over the preset
 *   - Unknown profile (shouldn't be reachable post-parse but defensive) → coding
 *   - Shape of every shipped preset (no missing required fields)
 */

import { describe, expect, it } from "vitest";
import {
  PROFILES,
  resolveProfile,
  type RecallProfileName,
  type RecallProfileConfig,
} from "../index";

const ALL_PROFILES: RecallProfileName[] = [
  "coding",
  "monitoring",
  "investigation",
  "writing",
  "conversation",
  "minimal",
  "off",
];

describe("PROFILES catalog", () => {
  it("ships every named profile", () => {
    for (const name of ALL_PROFILES) {
      expect(PROFILES[name]).toBeDefined();
    }
  });

  it("every profile has a sources array", () => {
    for (const name of ALL_PROFILES) {
      expect(Array.isArray(PROFILES[name].sources)).toBe(true);
    }
  });

  it("minimal and off have zero sources", () => {
    expect(PROFILES.minimal.sources).toEqual([]);
    expect(PROFILES.off.sources).toEqual([]);
  });

  it("coding is the broadest preset (all four sources)", () => {
    expect(PROFILES.coding.sources).toEqual([
      "core",
      "semantic",
      "associative",
      "triggers",
    ]);
  });

  it("monitoring denies past-tense memory types", () => {
    expect(PROFILES.monitoring.typeDeny).toBeDefined();
    expect(PROFILES.monitoring.typeDeny).toEqual(
      expect.arrayContaining(["RCA", "Postmortem", "Incident", "Reflection"]),
    );
  });

  it("investigation explicitly allows postmortem-class memories", () => {
    expect(PROFILES.investigation.typeAllow).toBeDefined();
    expect(PROFILES.investigation.typeAllow).toEqual(
      expect.arrayContaining(["RCA", "Postmortem", "Incident", "Decision"]),
    );
  });

  it("monitoring totalBudget is much smaller than coding", () => {
    expect(PROFILES.monitoring.totalBudget).toBeLessThan(
      PROFILES.coding.totalBudget ?? Infinity,
    );
  });
});

describe("resolveProfile", () => {
  it("autoRecall:false forces off regardless of profile name", () => {
    const out = resolveProfile({
      autoRecall: false,
      recallProfile: "coding",
    });
    expect(out.sources).toEqual([]);
    expect(out.totalBudget).toBe(0);
  });

  it("returns the named profile when autoRecall is true", () => {
    const out = resolveProfile({
      autoRecall: true,
      recallProfile: "monitoring",
    });
    expect(out.sources).toEqual(["triggers"]);
    expect(out.typeDeny).toEqual(PROFILES.monitoring.typeDeny);
  });

  it("recallProfileConfig overrides individual fields on the base preset", () => {
    const out = resolveProfile({
      autoRecall: true,
      recallProfile: "monitoring",
      recallProfileConfig: { triggersLimit: 5 },
    });
    // Override applied
    expect(out.triggersLimit).toBe(5);
    // Base preserved for non-overridden fields
    expect(out.sources).toEqual(["triggers"]);
    expect(out.typeDeny).toEqual(PROFILES.monitoring.typeDeny);
    expect(out.totalBudget).toBe(PROFILES.monitoring.totalBudget);
  });

  it("override can extend sources (e.g. monitoring + core)", () => {
    const out = resolveProfile({
      autoRecall: true,
      recallProfile: "monitoring",
      recallProfileConfig: { sources: ["core", "triggers"] },
    });
    expect(out.sources).toEqual(["core", "triggers"]);
  });

  it("falls back to coding when profile is unknown (defensive)", () => {
    const out = resolveProfile({
      autoRecall: true,
      // Cast through unknown to bypass the type guard — simulates a stale
      // config that ships an obsolete profile name.
      recallProfile: "no-such-profile" as unknown as RecallProfileName,
    });
    expect(out.sources).toEqual(PROFILES.coding.sources);
  });

  it("returns a fresh object — override does not mutate the catalog", () => {
    const before: RecallProfileConfig = { ...PROFILES.monitoring };
    resolveProfile({
      autoRecall: true,
      recallProfile: "monitoring",
      recallProfileConfig: { triggersLimit: 99 },
    });
    expect(PROFILES.monitoring.triggersLimit).toBe(before.triggersLimit);
  });
});

// frontend/src/lib/followUpSuggestions.fb.grammar-edges.test.ts
// PR #835 feedback F-5 (external, executed) + PRR-002 + PRR-012g + PRR-015:
// grammar edges the a07 check does not cover — single-word imperatives, the
// imperative-then-object shape ("Compare A and B" must keep its "A"), the
// word-boundary cap promise, the long-single-word fallback, and the 80-char
// suggestion budget.

import { describe, it, expect } from "vitest";
import { deriveFollowUps } from "./followUpSuggestions";

describe("follow-up chip grammar edges (fb F-5 / PRR-002 / PRR-015)", () => {
  it("a bare single-word imperative does not leak into chips (PRR-002)", () => {
    for (const q of ["Explain.", "List.", "Summarize.", "Explain"]) {
      const chips = deriveFollowUps(q, []);
      for (const chip of chips) {
        expect(chip.toLowerCase()).not.toContain("explain");
        expect(chip.toLowerCase()).not.toContain("list");
        expect(chip.toLowerCase()).not.toContain("summarize");
      }
    }
    // Positive control: the imperative is stripped but the topic survives.
    const chips = deriveFollowUps("Explain rules", []);
    expect(chips.length).toBeGreaterThan(0);
    expect(chips.some((c) => c.includes("rules"))).toBe(true);
    expect(chips.some((c) => /\bexplain\b/i.test(c))).toBe(false);
  });

  it("imperative + object keeps the object: 'Compare A and B' (F-5)", () => {
    const chips = deriveFollowUps("Compare A and B", []);
    const topicChip = chips.find((c) => c.includes("A and B"));
    expect(topicChip).toBeDefined();
    // The filler loop must not eat "A" after an imperative strip.
    expect(chips.some((c) => c.includes("around and B"))).toBe(false);
  });

  it("imperative + article object stays grammatical (F-5)", () => {
    const chips = deriveFollowUps("Explain the vendor onboarding process", []);
    expect(chips.length).toBeGreaterThan(0);
    for (const chip of chips) {
      expect(/\bexplain\b/i.test(chip)).toBe(false);
      expect(chip).not.toContain("…");
    }
  });

  it("the topic cap cuts on a word boundary (PRR-012g)", () => {
    const chips = deriveFollowUps(
      "explain alpha beta gamma delta epsilon zeta eta theta iota",
      []
    );
    expect(chips.length).toBeGreaterThan(0);
    for (const chip of chips) {
      // No partial word: the topic must end at one of the complete words.
      expect(chip).toMatch(/(alpha|beta|gamma|delta|epsilon|zeta|eta)(\?)?$/);
      expect(chip).not.toContain("thet ");
      expect(chip).not.toContain("theta iota");
    }
  });

  it("a single word longer than the cap is kept whole, not hard-cut (PRR-015)", () => {
    // 45 chars: the risks template stays within the 80-char budget with the
    // WHOLE word (30 + 45 + 1 = 76) but the old text.slice(0, 40) fallback
    // would have shipped a 40-char mid-word cut.
    const longWord = "a".repeat(45);
    const chips = deriveFollowUps(longWord, []);
    expect(chips.some((c) => c.includes(longWord))).toBe(true);
    for (const chip of chips) {
      expect(chip.length).toBeLessThanOrEqual(80);
      expect(chip).not.toContain("…");
    }
  });

  it("every chip stays within the 80-char suggestion budget", () => {
    const chips = deriveFollowUps(
      "explain the very specific coolant pressure valve replacement procedure steps for industrial units",
      []
    );
    for (const chip of chips) {
      expect(chip.length).toBeLessThanOrEqual(80);
    }
  });
});

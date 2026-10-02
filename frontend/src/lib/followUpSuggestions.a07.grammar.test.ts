// frontend/src/lib/followUpSuggestions.a07.grammar.test.ts
// A07 (AC11): follow-up chips embed the user's question as a topic. An
// imperative question ("Explain …") must not leak its imperative verb into
// the chip templates ("What are the key risks around Explain …?"), and
// mid-word truncation ellipses ("…") must not appear in a chip. The existing
// followUpSuggestions.test.ts pins today's cap behavior — this file pins
// grammar only and leaves that file untouched.

import { describe, it, expect } from "vitest";
import { deriveFollowUps } from "./followUpSuggestions";

describe("deriveFollowUps grammar (A07 AC11)", () => {
  it("imperative questions yield grammatical chips", () => {
    const chips = deriveFollowUps(
      "Explain the vendor onboarding process for new suppliers in the EU region",
      []
    );
    const ungrammatical = chips.filter(
      (chip) => /\bexplain\b/i.test(chip) || chip.includes("…")
    );
    expect(ungrammatical).toEqual([]);
  });
});

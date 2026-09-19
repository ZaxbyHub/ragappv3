// frontend/src/lib/followUpSuggestions.test.ts
// Issue #573 AC1 — PR-review PRR-009: direct unit coverage for the
// deriveFollowUps heuristic (the component tests only exercise hardcoded
// props). Deterministic mapping: filler stripping, topic truncation,
// length caps, dedupe, malformed-input safety.

import { describe, it, expect } from "vitest";
import { deriveFollowUps } from "./followUpSuggestions";

describe("deriveFollowUps (PRR-009)", () => {
  it("derives up to 3 suggestions from a question", () => {
    const out = deriveFollowUps("What did the manual say about coolant?", []);
    expect(out.length).toBeGreaterThanOrEqual(1);
    expect(out.length).toBeLessThanOrEqual(3);
    // The interrogative lead-in is stripped from the embedded topic (the
    // template itself legitimately opens with "What are...").
    expect(out[0]).toContain("manual say about coolant");
    expect(out[0]).not.toContain("around what did");
  });

  it("incorporates source titles when two or more are available", () => {
    const out = deriveFollowUps("coolant intervals", ["handbook.pdf", "spec.docx"]);
    expect(out.some((s) => s.includes("handbook.pdf") && s.includes("spec.docx"))).toBe(true);
  });

  it("falls back to a single-source question with one title", () => {
    const out = deriveFollowUps("coolant", ["handbook.pdf"]);
    expect(out.some((s) => s.includes("handbook.pdf"))).toBe(true);
  });

  it("returns [] when there is nothing to work from", () => {
    expect(deriveFollowUps("", [])).toEqual([]);
    expect(deriveFollowUps("   ", [])).toEqual([]);
  });

  it("strips leading fillers and caps long topics so suggestions stay in budget", () => {
    const long = "please tell me about the very specific coolant pressure valve replacement procedure steps";
    const out = deriveFollowUps(long, []);
    // Topic = at most 8 words AND 40 chars after filler-stripping, so the
    // embedded templates never exceed the 80-char suggestion budget (an
    // earlier topic cap produced zero suggestions for long queries).
    expect(out.length).toBeGreaterThan(0);
    for (const s of out) {
      expect(s.length).toBeLessThanOrEqual(80);
    }
  });

  it("truncates long source titles in comparisons", () => {
    const longTitle = "a".repeat(120) + ".pdf";
    const out = deriveFollowUps("coolant", [longTitle, "b.pdf"]);
    const comparison = out.find((s) => s.includes("a.pdf") || s.includes(".pdf and"));
    // The long title must not appear in full (truncated with ellipsis).
    expect(out.every((s) => !s.includes("a".repeat(120)))).toBe(true);
    expect(comparison !== undefined || out.length > 0).toBe(true);
  });

  it("dedupes identical candidates", () => {
    const out = deriveFollowUps("coolant", []);
    expect(new Set(out).size).toBe(out.length);
  });

  it("tolerates malformed source titles", () => {
    // Adversarial input beyond the typed contract: null/number entries.
    expect(() =>
      deriveFollowUps("coolant", [null as unknown as string, 42 as unknown as string, "ok.pdf"])
    ).not.toThrow();
    const out = deriveFollowUps("coolant", [
      null as unknown as string,
      42 as unknown as string,
      "ok.pdf",
    ]);
    expect(out.some((s) => s.includes("ok.pdf"))).toBe(true);
  });
});

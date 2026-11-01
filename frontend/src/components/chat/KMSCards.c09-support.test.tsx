// frontend/src/components/chat/KMSCards.c09-support.test.tsx
// Issue #714: KMSCards renders exactly one per-card support indicator from
// the done payload's citationConfidence, labelled "Support" and never
// "verified". RED at base, GREEN after the fix.
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { KMSCards } from "./KMSCards";
import type { KMSReference } from "@/lib/api";

const KMS: KMSReference = {
  kms_label: "K1",
  entry_id: 7,
  slug: "pythagorean-theorem",
  title: "Pythagorean theorem",
  summary: "Relates the sides of a right triangle.",
  excerpt: "The pythagorean theorem relates the sides of a right triangle.",
  tags: [],
  status: "active",
  source_type: "math",
  file_id: null,
  score: 1,
  score_type: "kms_fts",
};

describe("KMSCards support indicator (issue #714)", () => {
  it("renders a support score for a cited KMS ref", () => {
    render(<KMSCards kmsRefs={[KMS]} citationConfidence={{ K1: 0 }} />);

    expect(
      screen.getByRole("article", { name: /Knowledge K1: Pythagorean theorem/i })
    ).toBeInTheDocument();
    expect(screen.queryAllByText(/support/i)).toHaveLength(1);
    expect(screen.getByText("Support 0%")).toBeInTheDocument();
    expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
  });

  it("renders no support indicator when the entry has no score", () => {
    render(<KMSCards kmsRefs={[KMS]} />);

    expect(
      screen.getByRole("article", { name: /Knowledge K1: Pythagorean theorem/i })
    ).toBeInTheDocument();
    expect(screen.queryAllByText(/support/i)).toHaveLength(0);
  });
});

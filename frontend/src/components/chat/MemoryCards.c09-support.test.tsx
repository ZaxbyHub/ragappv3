// frontend/src/components/chat/MemoryCards.c09-support.test.tsx
// Issue #714: MemoryCards renders exactly one per-card support indicator from
// the done payload's citationConfidence, labelled "Support" and never
// "verified". RED at base, GREEN after the fix.
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryCards } from "./MemoryCards";
import type { UsedMemory } from "@/lib/api";

const MEMORY: UsedMemory = {
  id: "m1",
  memory_label: "M1",
  content: "Zebras roam the savanna in family groups.",
  category: "fauna",
  tags: null,
  source: "notes",
};

describe("MemoryCards support indicator (issue #714)", () => {
  it("renders a support score for a cited memory ref", () => {
    render(
      <MemoryCards memories={[MEMORY]} citationConfidence={{ M1: 0.5 }} />
    );

    expect(screen.getByRole("article", { name: "Memory M1" })).toBeInTheDocument();
    expect(screen.queryAllByText(/support/i)).toHaveLength(1);
    expect(screen.getByText("Support 50%")).toBeInTheDocument();
    expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
  });

  it("renders no support indicator when the memory has no score", () => {
    render(<MemoryCards memories={[MEMORY]} />);

    expect(screen.getByRole("article", { name: "Memory M1" })).toBeInTheDocument();
    expect(screen.queryAllByText(/support/i)).toHaveLength(0);
  });
});

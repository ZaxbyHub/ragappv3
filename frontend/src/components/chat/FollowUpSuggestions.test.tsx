// frontend/src/components/chat/FollowUpSuggestions.test.tsx
// Issue #573 (AC1) — acceptance check C1: follow-up suggestions after a
// completed assistant turn.
//
// DISCRIMINATING check: expected RED at base commit ae2e15a0 — the
// FollowUpSuggestions module does not exist (import resolution fails) and
// TranscriptPane.tsx contains no reference to it. GREEN once the implementer
// ships the component and wires it into the transcript.
//
// Frozen component contract:
//   File:    frontend/src/components/chat/FollowUpSuggestions.tsx
//   Export:  FollowUpSuggestions
//   Props:   suggestions: string[]
//            onSelect: (suggestion: string) => void
//   Renders: a labelled region (role="region", accessible name containing
//            "Suggested follow-ups") containing one button per suggestion
//            whose accessible name is the suggestion text; activating a
//            suggestion button calls onSelect exactly once with that string.
//            With an empty suggestions array the component renders nothing
//            (no empty labelled region).
//   Wiring:  frontend/src/components/chat/TranscriptPane.tsx must import and
//            render FollowUpSuggestions (source-scan guardrail below, cf.
//            the fonts-selfhosted-assets / SessionRail.data-index pattern).

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { FollowUpSuggestions } from "./FollowUpSuggestions";

const TRANSCRIPT_PANE_PATH = resolve(__dirname, "TranscriptPane.tsx");

const SUGGESTIONS = [
  "Summarize the key risks",
  "Draft a follow-up email",
  "List the open questions",
];

describe("FollowUpSuggestions component contract (issue #573 AC1 / C1)", () => {
  it("renders a labelled region with one button per suggestion", () => {
    render(<FollowUpSuggestions suggestions={SUGGESTIONS} onSelect={vi.fn()} />);

    const region = screen.getByRole("region", { name: /suggested follow-ups/i });
    expect(region).toBeInTheDocument();

    for (const suggestion of SUGGESTIONS) {
      expect(
        screen.getByRole("button", { name: suggestion }),
        `expected a suggestion button with accessible name "${suggestion}"`
      ).toBeInTheDocument();
    }
  });

  it("calls onSelect exactly once with the suggestion text when its button is clicked", () => {
    const onSelect = vi.fn();
    render(<FollowUpSuggestions suggestions={SUGGESTIONS} onSelect={onSelect} />);

    fireEvent.click(screen.getByRole("button", { name: SUGGESTIONS[1] }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(SUGGESTIONS[1]);
  });

  it("renders nothing when the suggestions array is empty", () => {
    render(<FollowUpSuggestions suggestions={[]} onSelect={vi.fn()} />);

    expect(
      screen.queryByRole("region", { name: /suggested follow-ups/i }),
      "an empty suggestions array must not render an empty labelled region"
    ).not.toBeInTheDocument();
  });

  it("wiring: TranscriptPane.tsx imports and renders FollowUpSuggestions", () => {
    const source = readFileSync(TRANSCRIPT_PANE_PATH, "utf-8");

    expect(
      /import\s*\{[^}]*FollowUpSuggestions[^}]*\}\s*from\s*["']\.\/FollowUpSuggestions["']/.test(
        source
      ),
      "TranscriptPane.tsx must import FollowUpSuggestions from ./FollowUpSuggestions (issue #573 AC1 wiring)"
    ).toBe(true);

    expect(
      source.includes("<FollowUpSuggestions"),
      "TranscriptPane.tsx must render <FollowUpSuggestions ...> in the transcript (issue #573 AC1 wiring)"
    ).toBe(true);
  });
});

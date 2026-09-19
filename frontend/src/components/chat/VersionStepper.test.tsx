// frontend/src/components/chat/VersionStepper.test.tsx
// Issue #573 (AC3) — acceptance check C3: version navigation for edited
// messages. At base, editing truncates in place (TranscriptPane handleEdit:
// truncateChatSession + removeMessagesFrom) and the pre-edit content is
// discarded — no sibling version data exists anywhere.
//
// DISCRIMINATING check: expected RED at base commit ae2e15a0 (module
// missing; TranscriptPane has no VersionStepper reference). GREEN once the
// implementer ships the component and wires it.
//
// Frozen component contract:
//   File:    frontend/src/components/chat/VersionStepper.tsx
//   Export:  VersionStepper (and the version item type below)
//   Props:   versions: { label: string; content: string }[]
//            activeIndex: number
//            onSelectIndex: (index: number) => void
//   Renders: the active version's content, a "1 / 2"-style position
//            indicator (activeIndex + 1 / total), and Previous / Next
//            controls with accessible names matching /previous version/i and
//            /next version/i. Clicking next/prev calls onSelectIndex with the
//            neighbouring index (bounded by the array). The parent swaps the
//            displayed version by re-rendering with the new activeIndex.
//   Wiring:  TranscriptPane.tsx references VersionStepper (source-scan).

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { VersionStepper } from "./VersionStepper";

const TRANSCRIPT_PANE_PATH = resolve(__dirname, "TranscriptPane.tsx");

const VERSIONS = [
  { label: "Original", content: "First draft of the answer" },
  { label: "Edited", content: "Revised answer after the edit" },
];

describe("VersionStepper component contract (issue #573 AC3 / C3)", () => {
  it("renders the active version's content and a '1 / 2' position indicator", () => {
    render(
      <VersionStepper versions={VERSIONS} activeIndex={0} onSelectIndex={vi.fn()} />
    );

    expect(screen.getByText("First draft of the answer")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
  });

  it("next control requests index 1 and previous control requests index 0", () => {
    const onSelectIndex = vi.fn();
    render(
      <VersionStepper versions={VERSIONS} activeIndex={0} onSelectIndex={onSelectIndex} />
    );

    fireEvent.click(screen.getByRole("button", { name: /next version/i }));
    expect(onSelectIndex).toHaveBeenCalledTimes(1);
    expect(onSelectIndex).toHaveBeenLastCalledWith(1);

    fireEvent.click(screen.getByRole("button", { name: /previous version/i }));
    expect(onSelectIndex).toHaveBeenCalledTimes(2);
    expect(onSelectIndex).toHaveBeenLastCalledWith(0);
  });

  it("stepping to the second version swaps the displayed content and position", () => {
    const { rerender } = render(
      <VersionStepper versions={VERSIONS} activeIndex={0} onSelectIndex={vi.fn()} />
    );

    rerender(
      <VersionStepper versions={VERSIONS} activeIndex={1} onSelectIndex={vi.fn()} />
    );

    expect(screen.getByText("Revised answer after the edit")).toBeInTheDocument();
    expect(screen.queryByText("First draft of the answer")).not.toBeInTheDocument();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
  });
});

describe("VersionStepper wiring (issue #573 AC3)", () => {
  it("TranscriptPane renders <VersionStepper onSelectIndex={...}> wired to the edit-version store", () => {
    const source = readFileSync(TRANSCRIPT_PANE_PATH, "utf-8");

    expect(
      source.includes("<VersionStepper"),
      "TranscriptPane.tsx must render <VersionStepper ...> on messages that have sibling versions (issue #573 AC3 wiring)"
    ).toBe(true);
    expect(
      source.includes("onSelectIndex={"),
      "TranscriptPane.tsx must pass an onSelectIndex handler to VersionStepper (issue #573 AC3 wiring)"
    ).toBe(true);
    expect(
      source.includes("messageEditVersions") ||
        source.includes("activeEditVersion") ||
        source.includes("recordEditVersion"),
      "the stepper must be backed by the client-side edit-version state (messageEditVersions/activeEditVersion/recordEditVersion) so stepping actually swaps the displayed sibling (issue #573 AC3 wiring)"
    ).toBe(true);
  });
});

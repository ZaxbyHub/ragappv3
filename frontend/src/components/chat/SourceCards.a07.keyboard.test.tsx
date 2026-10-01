// frontend/src/components/chat/SourceCards.a07.keyboard.test.tsx
// A07 (AC6): the source card renders as role="button" with tabIndex={0}, so
// it is reachable by keyboard — but its keyDown handler only reacts to
// "Enter". A focused card must also activate on Space (key " ").

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SourceCards } from "./SourceCards";
import type { Source } from "@/lib/api";

const source: Source = {
  id: "chunk-keyboard",
  filename: "keyboard-activation.pdf",
  source_label: "S1",
  snippet: "A snippet of the cited chunk.",
};

describe("SourceCards keyboard activation (A07 AC6)", () => {
  it("Space activates a source card", () => {
    const onSourceClick = vi.fn();

    render(
      <SourceCards sources={[source]} onSourceClick={onSourceClick} onViewAll={vi.fn()} />
    );

    const card = screen.getByRole("button", {
      name: "Source S1: keyboard-activation.pdf",
    });
    card.focus();
    expect(document.activeElement).toBe(card);

    fireEvent.keyDown(card, { key: " " });

    expect(onSourceClick).toHaveBeenCalledTimes(1);
  });
});

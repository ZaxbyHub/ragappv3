// frontend/src/components/chat/SourceCards.a07.keyboard.test.tsx
// A07 (AC6), migrated for the #866 card restructure: the source card is no
// longer a role="button" container — the FILENAME BUTTON carries the open
// action (native keyboard contract: Enter and Space both activate). The pin
// is unchanged in substance: a keyboard user reaches and activates the card.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceCards } from "./SourceCards";
import type { Source } from "@/lib/api";

const source: Source = {
  id: "chunk-keyboard",
  filename: "keyboard-activation.pdf",
  source_label: "S1",
  snippet: "A snippet of the cited chunk.",
};

describe("SourceCards keyboard activation (A07 AC6)", () => {
  it("Space activates the source card's filename button", async () => {
    const user = userEvent.setup();
    const onSourceClick = vi.fn();

    render(
      <SourceCards sources={[source]} onSourceClick={onSourceClick} onViewAll={vi.fn()} />
    );

    const button = screen.getByRole("button", {
      name: "Source S1: keyboard-activation.pdf",
    });
    button.focus();
    expect(document.activeElement).toBe(button);

    await user.keyboard(" ");

    expect(onSourceClick).toHaveBeenCalledTimes(1);
  });
});

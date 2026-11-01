// frontend/src/components/chat/SourceCards.fb.keyboard-nested.test.tsx
// PR #835 feedback F-1 (external review, execution-proven) + PRR-012a,
// migrated for the #866 card restructure: the filename button now owns the
// open action (native button semantics — Enter and Space activate exactly
// once, auto-repeat is the browser's business) and the More/Less expander is
// a SIBLING control, so no container handler can hijack its keydowns at all.
// The pins keep their original substance: expander keys toggle only; the
// open action fires exactly once per activation key; non-activation keys do
// nothing. A >120-char snippet mounts the expander.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceCards } from "./SourceCards";
import type { Source } from "@/lib/api";

const longSnippet =
  "This cited chunk runs well past one hundred and twenty characters so that " +
  "the SourceCard renders its More/Less expander button, which is now a " +
  "sibling interactive control of the filename open button (#866).";

const source: Source = {
  id: "chunk-nested-kb",
  filename: "nested-keyboard.pdf",
  source_label: "S1",
  snippet: longSnippet,
};

function renderCards(onSourceClick: ReturnType<typeof vi.fn>) {
  render(
    <SourceCards sources={[source]} onSourceClick={onSourceClick} onViewAll={vi.fn()} />
  );
  return screen.getByRole("button", {
    name: "Source S1: nested-keyboard.pdf",
  });
}

describe("SourceCards keyboard: nested expander + guard rails (fb F-1)", () => {
  it("Enter/Space on the focused More button toggle it and do NOT activate the card", async () => {
    const user = userEvent.setup();
    const onSourceClick = vi.fn();
    render(
      <SourceCards sources={[source]} onSourceClick={onSourceClick} onViewAll={vi.fn()} />
    );

    const more = screen.getByRole("button", { name: /more/i });
    more.focus();
    expect(document.activeElement).toBe(more);
    expect(more.getAttribute("aria-expanded")).toBe("false");

    await user.keyboard("{Enter}");
    expect(more.getAttribute("aria-expanded")).toBe("true");
    expect(onSourceClick).not.toHaveBeenCalled();

    await user.keyboard(" ");
    expect(more.getAttribute("aria-expanded")).toBe("false");
    expect(onSourceClick).not.toHaveBeenCalled();
  });

  it("a non-activation key on the focused filename button does nothing", async () => {
    const user = userEvent.setup();
    const onSourceClick = vi.fn();
    const button = renderCards(onSourceClick);
    button.focus();
    await user.keyboard("{Escape}");
    await user.keyboard("{Tab}");
    await user.keyboard("a");
    expect(onSourceClick).not.toHaveBeenCalled();
  });

  it("filename-button Enter/Space activate exactly once each", async () => {
    const user = userEvent.setup();
    const onSourceClick = vi.fn();
    const button = renderCards(onSourceClick);
    button.focus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onSourceClick).toHaveBeenCalledTimes(2);
  });
});

// frontend/src/components/chat/SourceCards.fb.keyboard-nested.test.tsx
// PR #835 feedback F-1 (external review, execution-proven) + PRR-012a:
// the card-level onKeyDown must NOT hijack keydowns that originate on the
// nested More/Less expander button (preventDefault cancelled the button's
// native activation), must ignore key auto-repeat, and must not activate on
// non-activation keys. The frozen a07 keyboard check uses a short snippet,
// so the nested button never renders there — this file uses a >120-char
// snippet to mount it. userEvent drives real activation semantics
// (Enter keydown -> click; Space keyup -> click), matching the browser.

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceCards } from "./SourceCards";
import type { Source } from "@/lib/api";

const longSnippet =
  "This cited chunk runs well past one hundred and twenty characters so that " +
  "the SourceCard renders its More/Less expander button, which is the nested " +
  "interactive control whose keyboard activation the card handler must not hijack.";

const source: Source = {
  id: "chunk-nested-kb",
  filename: "nested-keyboard.pdf",
  source_label: "S1",
  snippet: longSnippet,
};

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

  it("key auto-repeat does not machine-gun the card activation", () => {
    const onSourceClick = vi.fn();
    render(
      <SourceCards sources={[source]} onSourceClick={onSourceClick} onViewAll={vi.fn()} />
    );
    const card = screen.getByRole("button", {
      name: "Source S1: nested-keyboard.pdf",
    });
    card.focus();
    fireEvent.keyDown(card, { key: "Enter", repeat: true });
    fireEvent.keyDown(card, { key: " ", repeat: true });
    expect(onSourceClick).not.toHaveBeenCalled();
  });

  it("a non-activation key on the focused card does nothing", () => {
    const onSourceClick = vi.fn();
    render(
      <SourceCards sources={[source]} onSourceClick={onSourceClick} onViewAll={vi.fn()} />
    );
    const card = screen.getByRole("button", {
      name: "Source S1: nested-keyboard.pdf",
    });
    card.focus();
    fireEvent.keyDown(card, { key: "Escape" });
    fireEvent.keyDown(card, { key: "Tab" });
    fireEvent.keyDown(card, { key: "a" });
    expect(onSourceClick).not.toHaveBeenCalled();
  });

  it("card-level Enter/Space still activate exactly once each", () => {
    const onSourceClick = vi.fn();
    render(
      <SourceCards sources={[source]} onSourceClick={onSourceClick} onViewAll={vi.fn()} />
    );
    const card = screen.getByRole("button", {
      name: "Source S1: nested-keyboard.pdf",
    });
    card.focus();
    fireEvent.keyDown(card, { key: "Enter" });
    fireEvent.keyDown(card, { key: " " });
    expect(onSourceClick).toHaveBeenCalledTimes(2);
  });
});

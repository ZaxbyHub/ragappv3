// frontend/src/tests/ctrl-k-visible-owner.issue775.test.tsx
// Issue #775 review PRR-101 (HIGH) + PRR-307 — the Ctrl+K owner must be the
// VISIBLE ChatSearchInput instance.
//
// ChatShell keeps the desktop rail MOUNTED while hidden (display:none below
// md, or w-0/opacity-0 when collapsed) and it registers the document-level
// focusSearch listener first. Under the defaultPrevented ownership protocol
// that hidden instance claimed the combo and called focus() on an unrendered
// element — a browser no-op — so one keypress produced ZERO visible actions
// (regression vs master) while every jsdom suite stayed green (no layout
// engine ⇒ no hidden state). The fix: an instance whose input fails
// checkVisibility({checkOpacity:true}) does not claim; ownership resolves to
// the visible instance, or — with no visible claimant — the palette opens.
//
// jsdom has neither layout nor checkVisibility, so these tests drive the
// guard through per-element checkVisibility overrides (own properties shadow
// the prototype exactly as a real browser's implementation would bind).
//
// PRR-307 (which-owner): the flagship case also pins WHICH owner wins —
// the visible search input focuses and the palette stays closed.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { ChatSearchInput } from "@/components/chat/SessionRail";
import { CommandPalette } from "@/components/shared/CommandPalette";

// The palette consumes the react-query-backed Draft Room capability hook
// (same as the frozen l04 harnesses mock) — inert for these keydown tests.
vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => true,
}));

type Visibility = boolean | undefined;

function renderPair(hidden: Visibility, visible: Visibility) {
  render(
    <MemoryRouter>
      <ChatSearchInput value="" onChange={() => {}} />
      <ChatSearchInput value="" onChange={() => {}} />
      <CommandPalette />
    </MemoryRouter>
  );
  const inputs = screen.getAllByLabelText("Search chat sessions");
  const [first, second] = inputs;
  const define = (el: HTMLElement, v: Visibility) => {
    if (v === undefined) return;
    Object.defineProperty(el, "checkVisibility", {
      configurable: true,
      value: () => v,
    });
  };
  define(first, hidden);
  define(second, visible);
  return { first, second };
}

function countFocus(el: HTMLElement): () => number {
  let n = 0;
  el.addEventListener("focus", () => {
    n += 1;
  });
  return () => n;
}

describe("issue #775 review PRR-101 — the visible ChatSearchInput owns Ctrl+K", () => {
  afterEach(() => {
    cleanup();
    // Drop per-element checkVisibility overrides (elements are discarded by
    // cleanup; the explicit delete guards against prototype leakage if a
    // future refactor moves the override).
    try {
      // @ts-expect-error test-only teardown of test-defined own properties
      delete Element.prototype.checkVisibility;
    } catch {
      // prototype had no such property — nothing to undo
    }
  });

  it("a hidden desktop instance yields; the visible instance focuses and the palette stays closed", () => {
    const { first, second } = renderPair(false, true);
    const firstFocus = countFocus(first);
    const secondFocus = countFocus(second);

    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });

    expect(secondFocus(), "the VISIBLE search input must receive the shortcut").toBe(1);
    expect(firstFocus(), "the hidden instance must not claim the combo").toBe(0);
    expect(
      screen.queryByRole("dialog"),
      "with a visible search claimant the palette stays closed (PRR-307)"
    ).toBeNull();
  });

  it("with NO visible claimant (sheet closed on mobile) the palette opens", () => {
    // A single HIDDEN instance (the collapsed/closed-sheet mobile state):
    // it must not claim, so the combo falls through to the palette.
    render(
      <MemoryRouter>
        <ChatSearchInput value="" onChange={() => {}} />
        <CommandPalette />
      </MemoryRouter>
    );
    const only = screen.getByLabelText("Search chat sessions");
    Object.defineProperty(only, "checkVisibility", {
      configurable: true,
      value: () => false,
    });

    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
    expect(
      screen.queryByRole("dialog"),
      "a hidden-only rail must not swallow the combo — the palette opens"
    ).not.toBeNull();
  });

  it("without checkVisibility support the pre-review claim behavior is preserved", () => {
    // No per-element overrides: jsdom exposes no checkVisibility, mirroring
    // older browsers — the first instance claims exactly as before the fix.
    render(
      <MemoryRouter>
        <ChatSearchInput value="" onChange={() => {}} />
        <ChatSearchInput value="" onChange={() => {}} />
        <CommandPalette />
      </MemoryRouter>
    );
    const inputs = screen.getAllByLabelText("Search chat sessions");
    const firstFocus = countFocus(inputs[0]);

    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });

    expect(firstFocus(), "fallback keeps the first-registrant claim").toBe(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

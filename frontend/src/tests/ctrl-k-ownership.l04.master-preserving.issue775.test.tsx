// frontend/src/tests/ctrl-k-ownership.l04.test.tsx
// L04 / AC1 (frozen acceptance check) — Ctrl+K ownership: ONE keypress,
// ONE action.
//
// At master TWO independent listeners react to a single Ctrl+K keypress:
//   - ChatSearchInput's document-level listener
//     (frontend/src/components/chat/SessionRail.tsx) focuses the session
//     search on the effective focusSearch binding (default Ctrl+K);
//   - CommandPalette's window-level listener
//     (frontend/src/components/shared/CommandPalette.tsx) toggles the
//     palette open on Ctrl/Cmd+K.
// In the real shell both surfaces are mounted at once (the palette is
// app-wide; the search input lives in the chat rail), so one Ctrl-K press
// runs TWO actions: the session search is focused AND the palette opens.
//
// This check renders both surfaces together inside a MemoryRouter (the
// palette needs router context for useNavigate), dispatches Ctrl+K ONCE on
// document.body (bubbles through document- and window-level listeners),
// and requires exactly one of the two actions to have fired. Focus is
// counted via a focus-event listener attached to the search input BEFORE
// the keypress (not by reading final activeElement — the palette's
// autofocus would race that observation).
//
// Expected RED at master: "expected 2 to be 1" (both the session-search
// focus AND the palette open fire from one keypress).

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { ChatSearchInput } from "@/components/chat/SessionRail";
import { CommandPalette } from "@/components/shared/CommandPalette";

// AMEND (CHECK_WRONG, issue #775): the palette legitimately consumes the
// Draft Room capability hook since the fix (same as NavigationRail), which
// is react-query-backed and needs a provider this minimal harness never
// wrapped. Mock it the same way command-palette.l04.test.tsx does — at base
// the palette consumed no hooks, so the mock is inert there and the frozen
// RED assertion ("expected 2 to be 1") is unchanged.
vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => true,
}));

describe("L04 — Ctrl+K ownership (AC1)", () => {
  afterEach(() => {
    cleanup();
  });

  it("one Ctrl-K keypress runs exactly one action", () => {
    render(
      <MemoryRouter>
        <ChatSearchInput value="" onChange={() => {}} />
        <CommandPalette />
      </MemoryRouter>
    );

    // Count focus EVENTS on the session search (never the final
    // activeElement — the palette steals focus into its own input on open).
    const searchInput = screen.getByLabelText("Search chat sessions");
    let searchFocusedCount = 0;
    searchInput.addEventListener("focus", () => {
      searchFocusedCount += 1;
    });

    // ONE keypress, dispatched on document.body so both the document-level
    // (ChatSearchInput) and window-level (CommandPalette) keydown listeners
    // receive it.
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });

    const paletteDialogOpen = screen.queryByRole("dialog") !== null;
    const actionsFired =
      (searchFocusedCount > 0 ? 1 : 0) + (paletteDialogOpen ? 1 : 0);
    expect(
      actionsFired,
      "a single Ctrl-K keypress must run exactly one action (session-search focus XOR palette open)"
    ).toBe(1);
  });
});

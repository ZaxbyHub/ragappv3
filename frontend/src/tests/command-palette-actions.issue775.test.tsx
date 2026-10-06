// frontend/src/tests/command-palette-actions.issue775.test.tsx
// Issue #775 — palette v2 action-wiring pins (beyond the frozen C6/C7
// checks, which prove only ">=5 non-navigating commands" and "the search API
// is called"):
//
//   1. The "Show keyboard shortcuts" action is wired through the SINGLE
//      existing dispatcher: with the palette and the app-wide shortcuts
//      hook mounted the way App.tsx mounts them (issue #775 relocation),
//      running the action closes the palette and opens the shortcuts dialog.
//   2. Theme actions perform their side effect on the real useThemeStore.
//   3. Entity-hit rows navigate to their url_hint and close the palette.
//   4. Render order: every navigation destination precedes every action.
//
// Harness: CommandPalette + useKeyboardShortcuts/KeyboardShortcutsDialog
// mounted together (the App.tsx AppShortcutsMount shape) inside a
// MemoryRouter with a LocationProbe catch-all; auth/capability/search mocks
// per the frozen l04 file (static imports + hoisted vi.mock only).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useEffect } from "react";
import { render, screen, fireEvent, cleanup, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";

import { CommandPalette } from "@/components/shared/CommandPalette";
import {
  KeyboardShortcutsDialog,
  useKeyboardShortcuts,
} from "@/components/shared/KeyboardShortcuts";
import { useThemeStore } from "@/stores/useThemeStore";
import { useNavigationGuardStore } from "@/stores/useNavigationGuardStore";
import { unifiedSearch } from "@/lib/api/search";

const authState = vi.hoisted(() => ({
  user: { id: 1, username: "alice", role: "admin" },
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      user: authState.user,
      logout: vi.fn(),
      init: vi.fn().mockResolvedValue(undefined),
    }),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => true,
}));

const searchMock = vi.hoisted(() => ({
  unifiedSearch: vi.fn().mockResolvedValue({ results: [] }),
}));

vi.mock("@/lib/api/search", () => ({
  unifiedSearch: searchMock.unifiedSearch,
}));

/** The App.tsx AppShortcutsMount shape (issue #775): dialog + hook live at
 *  the shell level so the palette's dispatched combo reaches them. */
function AppShortcutsMount() {
  const { open, setOpen } = useKeyboardShortcuts();
  return <KeyboardShortcutsDialog open={open} onOpenChange={setOpen} />;
}

function mountShell() {
  // Box read by path() after render; the probe updates it from an effect
  // (never during render — react-compiler lint requires pure renders).
  const pathBox = { path: "/start" };
  const LocationProbe = () => {
    const location = useLocation();
    useEffect(() => {
      pathBox.path = location.pathname;
    }, [location.pathname]);
    return null;
  };
  render(
    <MemoryRouter initialEntries={["/start"]}>
      <CommandPalette />
      <AppShortcutsMount />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  );
  return { path: () => pathBox.path };
}

function openPalette(): HTMLElement {
  if (screen.queryByRole("dialog") === null) {
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
  }
  return screen.getByRole("dialog");
}

describe("issue #775 — palette action wiring", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The theme store is module-level zustand state shared across tests —
    // reset it to the shipped default before each case. Same for the
    // navigation guard (implementation-review round 1: palette navigation
    // must consult it).
    useThemeStore.getState().setTheme("system");
    useNavigationGuardStore.getState().setConfirmLeave(null);
  });

  afterEach(() => {
    cleanup();
  });

  it("the shortcuts action closes the palette and opens the dialog through the app-wide hook", () => {
    mountShell();
    const palette = openPalette();

    fireEvent.click(within(palette).getByRole("button", { name: "Show keyboard shortcuts" }));

    // Palette closes; the keyboard-shortcuts dialog (its own dialog surface)
    // opens through the dispatched bound combo, not a second channel.
    return waitFor(() => {
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByText("Keyboard Shortcuts")).toBeInTheDocument();
    });
  });

  it("theme actions change the real theme store", () => {
    mountShell();
    const palette = openPalette();

    fireEvent.click(within(palette).getByRole("button", { name: "Use high contrast theme" }));
    expect(useThemeStore.getState().theme).toBe("high-contrast");

    // Palette closed by the action; reopen for the second action.
    const palette2 = openPalette();
    fireEvent.click(within(palette2).getByRole("button", { name: "Use system theme" }));
    expect(useThemeStore.getState().theme).toBe("system");
  });

  it("entity-hit rows navigate to their url_hint and close the palette", async () => {
    searchMock.unifiedSearch.mockResolvedValue({
      results: [
        {
          type: "document",
          id: 5,
          title: "Alpha Report",
          snippet: "snippet",
          vault_id: 1,
          url_hint: "/documents/5",
          score: 1,
        },
      ],
    });
    const { path } = mountShell();
    const palette = openPalette();

    const input = within(palette).getByLabelText("Search commands");
    fireEvent.change(input, { target: { value: "alpha" } });

    const hit = await waitFor(() => screen.getByRole("button", { name: /alpha report/i }));
    fireEvent.click(hit);

    await waitFor(() => expect(path()).toBe("/documents/5"));
    await waitFor(() => expect(screen.queryByLabelText("Search commands")).toBeNull());
  });

  it("a declined unsaved-changes guard blocks palette navigation (route unchanged)", () => {
    const { path } = mountShell();
    const palette = openPalette();

    // The dirty page's confirmLeave declines: navigation must not fire —
    // the same contract App.tsx's handleItemSelect follows for the mobile
    // bottom nav (implementation-review round 1 finding).
    useNavigationGuardStore.getState().setConfirmLeave(() => false);
    fireEvent.click(within(palette).getByRole("button", { name: "Go to Documents" }));
    expect(path()).toBe("/start");

    // And the guard is actually consulted (a dirty page that confirms still
    // navigates) — fireEvent is act-wrapped, so the route is final here.
    useNavigationGuardStore.getState().setConfirmLeave(() => true);
    const palette2 = openPalette();
    fireEvent.click(within(palette2).getByRole("button", { name: "Go to Documents" }));
    expect(path()).toBe("/documents");
  });

  it("destinations render before every action in DOM order", () => {
    mountShell();
    const palette = openPalette();

    const buttons = within(palette)
      .getAllByRole("button")
      .filter((b) => b.getAttribute("aria-label") !== "Close");
    const labels = buttons.map((b) => b.textContent ?? "");

    const firstAction = labels.findIndex((l) => l.includes("Show keyboard shortcuts"));
    const lastDestination = labels.findIndex((l) => l.includes("Go to Profile"));
    // All 12 destinations for an admin with the Draft Room capability.
    expect(labels.filter((l) => l.startsWith("Go to ")).length).toBe(12);
    expect(firstAction).toBeGreaterThan(-1);
    expect(lastDestination).toBeGreaterThan(-1);
    expect(lastDestination).toBeLessThan(firstAction);
  });
});

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
import { toast } from "sonner";
import {
  loadShortcutBindings,
  SHORTCUT_BINDINGS_STORAGE_KEY,
} from "@/lib/shortcutBindings";

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

const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api/search", () => ({
  unifiedSearch: searchMock.unifiedSearch,
}));

vi.mock("sonner", () => ({
  toast: toastMock,
}));

// Issue #775 review PRR-413: mockReset in beforeEach so a per-test
// mockResolvedValue (the entity-hit case) cannot leak into later tests —
// clearMocks only clears CALLS, not implementations.
function reseedSearchDefault() {
  searchMock.unifiedSearch.mockReset();
  searchMock.unifiedSearch.mockResolvedValue({ results: [] });
}

// localStorage Map layer (KeyboardShortcuts.rebind pattern) — the PRR-314
// test seeds a persisted binding, so the global no-op stubs need a backing
// store.
const storage = new Map<string, string>();

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
    // must consult it) and the search mock's default implementation
    // (PRR-413: clearMocks does not reset mockResolvedValue).
    storage.clear();
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => storage.get(key) ?? null);
    vi.mocked(localStorage.setItem).mockImplementation((key: string, value: string) => {
      storage.set(key, value);
    });
    vi.mocked(localStorage.removeItem).mockImplementation((key: string) => {
      storage.delete(key);
    });
    useThemeStore.getState().setTheme("system");
    useNavigationGuardStore.getState().setConfirmLeave(null);
    reseedSearchDefault();
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
      .filter((b) => b.textContent?.trim() !== "Close");
    const labels = buttons.map((b) => b.textContent ?? "");

    const firstAction = labels.findIndex((l) => l.includes("Show keyboard shortcuts"));
    const lastDestination = labels.findIndex((l) => l.includes("Go to Profile"));
    // All 12 destinations for an admin with the Draft Room capability.
    expect(labels.filter((l) => l.startsWith("Go to ")).length).toBe(12);
    expect(firstAction).toBeGreaterThan(-1);
    expect(lastDestination).toBeGreaterThan(-1);
    expect(lastDestination).toBeLessThan(firstAction);
  });

  it("Ctrl+K toggle-close resets query and hits like every other close path", () => {
    // Issue #775 review PRR-103/PRR-306: the keyboard toggle-close is a
    // third close path — it must leave the same clean state as Escape. The
    // closure assertion between close and reopen pins the close DIRECTION
    // (a can-only-open palette fails here).
    mountShell();
    const palette = openPalette();
    fireEvent.change(within(palette).getByLabelText("Search commands"), {
      target: { value: "alpha" },
    });

    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true }); // toggle-close
    expect(
      screen.queryByRole("dialog"),
      "Ctrl+K while open must CLOSE the palette"
    ).toBeNull();

    const reopened = openPalette();
    const input = within(reopened).getByLabelText("Search commands") as HTMLInputElement;
    expect(input.value, "reopen must start from a clean query").toBe("");
  });

  it("the light/dark toggle resolves system+dark and dark to light", () => {
    // Issue #775 review PRR-301: the resolvedDark branch is now asserted —
    // dark -> light, and system(+OS dark) -> light.
    const mql = vi
      .spyOn(window, "matchMedia")
      .mockReturnValue({
        matches: true,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
      } as unknown as MediaQueryList);
    try {
      mountShell();

      useThemeStore.getState().setTheme("dark");
      const palette = openPalette();
      fireEvent.click(within(palette).getByRole("button", { name: "Toggle light/dark theme" }));
      expect(useThemeStore.getState().theme).toBe("light");

      useThemeStore.getState().setTheme("system");
      const palette2 = openPalette();
      fireEvent.click(within(palette2).getByRole("button", { name: "Toggle light/dark theme" }));
      expect(
        useThemeStore.getState().theme,
        "system with an OS-dark match resolves dark -> toggling goes light"
      ).toBe("light");
    } finally {
      mql.mockRestore();
    }
  });

  it("an entity-hit click also honors a declined unsaved-changes guard", async () => {
    // Issue #775 review PRR-305: the SECOND guardedNavigate site (entity
    // hits) must be pinned too — reverting it alone passed every test.
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
    fireEvent.change(within(palette).getByLabelText("Search commands"), {
      target: { value: "alpha" },
    });
    const hit = await waitFor(() => screen.getByRole("button", { name: /alpha report/i }));

    useNavigationGuardStore.getState().setConfirmLeave(() => false);
    fireEvent.click(hit);
    expect(path(), "declined guard blocks the entity-hit navigation too").toBe("/start");

    useNavigationGuardStore.getState().setConfirmLeave(() => true);
    const palette2 = openPalette();
    fireEvent.change(within(palette2).getByLabelText("Search commands"), {
      target: { value: "alpha" },
    });
    const hit2 = await waitFor(() => screen.getByRole("button", { name: /alpha report/i }));
    fireEvent.click(hit2);
    await waitFor(() => expect(path()).toBe("/documents/5"));
  });

  it("copy page link reports success and failure instead of failing silently", async () => {
    // Issue #775 review PRR-408: every outcome is user-visible.
    mountShell();

    // Success: clipboard API resolves.
    const clipWrite = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipWrite },
    });
    const palette = openPalette();
    fireEvent.click(within(palette).getByRole("button", { name: "Copy page link" }));
    await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith("Page link copied"));
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    cleanup();
    mountShell(); // fresh shell for the failure leg (cleanup above unmounted)

    // Failure: no clipboard API and execCommand refuses -> error toast
    // (never a silent no-op). jsdom does not define execCommand at all, so
    // assign a refusing implementation (exercises the false-return path).
    const originalExecCommand = document.execCommand;
    document.execCommand = vi.fn(() => false);
    try {
      toastMock.error.mockClear();
      const palette2 = openPalette();
      fireEvent.click(within(palette2).getByRole("button", { name: "Copy page link" }));
      await waitFor(() => expect(toastMock.error).toHaveBeenCalled());
    } finally {
      if (originalExecCommand === undefined) {
        // @ts-expect-error test-only teardown of a test-defined property
        delete document.execCommand;
      } else {
        document.execCommand = originalExecCommand;
      }
    }
  });

  it("queries shorter than two characters never hit the search API; dead queries show the empty state", () => {
    // Issue #775 review PRR-315: the min-char guard is pinned PAST the
    // debounce (a synchronous not-called assertion alone passes with the
    // guard deleted — the debounced call would land 300ms out).
    mountShell();
    const palette = openPalette();
    const input = within(palette).getByLabelText("Search commands");
    // Fake timers must be installed BEFORE the change — sinon cannot capture
    // the debounce timer that fireEvent already scheduled on native setTimeout
    // (delta-review r2: installing after made this pin vacuous).
    vi.useFakeTimers();
    try {
      fireEvent.change(input, { target: { value: "z" } });
      vi.advanceTimersByTime(400); // past the 300ms debounce
      expect(searchMock.unifiedSearch).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }

    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(screen.getByText("No matching commands")).toBeTruthy();
  });

  it("the shortcuts action drives the dispatcher through a NON-default rebind too", () => {
    // Issue #775 review PRR-314: seed showShortcuts=F9 and confirm the
    // palette action dispatches the BOUND combo (comboToKeyboardEventInit
    // path), not a hardcoded "?".
    window.localStorage.setItem(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({ showShortcuts: "F9" })
    );
    expect(loadShortcutBindings()).toEqual({ showShortcuts: "F9" });

    mountShell();
    const palette = openPalette();
    fireEvent.click(within(palette).getByRole("button", { name: "Show keyboard shortcuts" }));

    return waitFor(() => {
      expect(screen.getByText("Keyboard Shortcuts")).toBeTruthy();
    });
  });
});

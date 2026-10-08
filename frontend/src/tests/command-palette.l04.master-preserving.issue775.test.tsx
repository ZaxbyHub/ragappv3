// frontend/src/tests/command-palette.l04.test.tsx
// L04 (frozen acceptance checks) — command palette completeness.
//
// At master the palette (frontend/src/components/shared/CommandPalette.tsx)
// ships a static list of six navigation commands, ignores the signed-in
// user's role, ignores the Draft Room capability, and never queries the
// global search API. The NavigationRail (frontend/src/components/layout/
// NavigationRail.tsx navItems) offers TWELVE destinations — /chat
// /documents /memory /wiki /kms /draft-room /vaults /admin/groups
// /admin/users /admin/organizations /settings /profile — with the three
// /admin routes admin-only and draft-room capability-gated.
//
// Frozen checks:
//   C1 / AC4: an ADMIN palette reaches every NavigationRail destination.
//             Expected RED: "expected 6 to be 0".
//   C2 / AC5: a MEMBER palette (Draft Room visible) offers exactly the
//             non-admin destinations — 9 distinct paths, none under
//             /admin. Expected RED: "expected 6 to be 9".
//   C3 / AC6: the palette lists at least five ACTIONS — commands that do
//             not navigate. Expected RED: "expected 0 to be greater than
//             or equal to 5".
//   C4 / AC7: typing a query hits the global search API for entity hits.
//             Expected RED: "expected 0 to be greater than 0".
//
// Harness: CommandPalette rendered inside a MemoryRouter with a
// LocationProbe catch-all route reporting useLocation().pathname; the
// palette opens on a document.body Ctrl+K keydown (bubbles to the
// window-level listener). Static imports + hoisted vi.mock factories (the
// auth-store selector pattern from command-palette.issue258.test.tsx, with
// a vi.hoisted per-test role).

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from "react-router-dom";

import { CommandPalette } from "@/components/shared/CommandPalette";
import { unifiedSearch } from "@/lib/api/search";

// Per-test configurable auth state (vi.hoisted so the vi.mock factory —
// hoisted to the top of the file — can close over it).
const authState = vi.hoisted(() => ({
  user: { id: 1, username: "alice", role: "member" },
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      user: authState.user,
      logout: vi.fn(),
      init: vi.fn().mockResolvedValue(undefined),
    }),
}));

// Draft Room capability enabled for both role scenarios.
vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => true,
}));

// Entity-search API stub for C4 — an empty result set is fine; the check
// only requires the palette to CALL it.
vi.mock("@/lib/api/search", () => ({
  unifiedSearch: vi.fn().mockResolvedValue({ results: [] }),
}));

// The 12 NavigationRail destinations, frozen.
const DESTINATIONS = [
  "/chat",
  "/documents",
  "/memory",
  "/wiki",
  "/kms",
  "/draft-room",
  "/vaults",
  "/admin/groups",
  "/admin/users",
  "/admin/organizations",
  "/settings",
  "/profile",
];

/**
 * Mount the palette plus a catch-all route whose probe reports the router
 * location into `path()` and exposes a "reset-route" control that navigates
 * back to the fixed starting route.
 */
function mountPalette(): { path: () => string } {
  let currentPath = "/start";
  const LocationProbe = () => {
    const location = useLocation();
    const navigate = useNavigate();
    currentPath = location.pathname;
    return (
      <button type="button" aria-label="reset-route" onClick={() => navigate("/start")} />
    );
  };
  render(
    <MemoryRouter initialEntries={["/start"]}>
      <CommandPalette />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  );
  return { path: () => currentPath };
}

/** Open the palette (idempotent while closed) and return the dialog. */
function openPalette(): HTMLElement {
  if (screen.queryByRole("dialog") === null) {
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
  }
  return screen.getByRole("dialog");
}

/** Close the palette (Escape) and wait for it to leave the DOM. */
async function closePalette(): Promise<void> {
  if (screen.queryByRole("dialog") !== null) {
    fireEvent.keyDown(document.body, { key: "Escape" });
  }
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });
}

/**
 * The palette COMMANDS: the buttons inside the dialog, excluding the Dialog
 * primitive's built-in "Close" affordance (dialog chrome, not a command).
 */
function paletteCommandNames(dialog: HTMLElement): string[] {
  return within(dialog)
    .getAllByRole("button")
    .map((button) => (button.textContent ?? "").trim())
    .filter((name) => name.length > 0 && name !== "Close");
}

function clickCommand(dialog: HTMLElement, name: string): void {
  fireEvent.click(within(dialog).getByRole("button", { name }));
}

describe("L04 — command palette completeness", () => {
  afterEach(() => {
    cleanup();
  });

  it("admin palette reaches every NavigationRail destination", async () => {
    authState.user = { id: 1, username: "alice", role: "admin" };
    const harness = mountPalette();

    // Snapshot the command list from the open (empty-query) palette; the
    // palette closes after each execution, so reopen between commands.
    const names = paletteCommandNames(openPalette());

    const reached = new Set<string>();
    for (const name of names) {
      const dialog = openPalette();
      clickCommand(dialog, name);
      reached.add(harness.path());
      await closePalette();
    }

    const missing = DESTINATIONS.filter((destination) => !reached.has(destination));
    expect(missing.length).toBe(0);
  });

  it("member palette offers exactly the non-admin destinations", async () => {
    authState.user = { id: 1, username: "alice", role: "member" };
    const harness = mountPalette();

    const names = paletteCommandNames(openPalette());
    const offered = new Set<string>();
    for (const name of names) {
      const dialog = openPalette();
      clickCommand(dialog, name);
      offered.add(harness.path());
      await closePalette();
    }

    // Exactly the non-admin destinations: 12 NavigationRail destinations
    // minus the 3 admin-only routes.
    expect(offered.size).toBe(9);
    // And nothing admin-flavored may leak to a member.
    expect(Array.from(offered).filter((path) => path.startsWith("/admin"))).toHaveLength(0);
  });

  it("palette lists at least five actions", async () => {
    authState.user = { id: 1, username: "alice", role: "admin" };
    const harness = mountPalette();

    // EMPTY query: every command listed in the open palette, clicked from a
    // fixed starting route. A command is an ACTION iff executing it leaves
    // the route untouched.
    const names = paletteCommandNames(openPalette());

    let actionCount = 0;
    for (const name of names) {
      await closePalette();
      // Fixed starting route for every classification.
      fireEvent.click(screen.getByLabelText("reset-route"));
      expect(harness.path()).toBe("/start");
      const dialog = openPalette();
      clickCommand(dialog, name);
      if (harness.path() === "/start") actionCount += 1;
    }

    expect(actionCount).toBeGreaterThanOrEqual(5);
  });

  it("palette queries the global search API for entity hits", async () => {
    authState.user = { id: 1, username: "alice", role: "admin" };
    mountPalette();

    const dialog = openPalette();
    const input = within(dialog).getByLabelText("Search commands");
    fireEvent.change(input, { target: { value: "alpha" } });

    // Wait past any debounce before asserting the API was consulted.
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(vi.mocked(unifiedSearch).mock.calls.length).toBeGreaterThan(0);
  });
});

// Issue 258 / AC25 part (b) (legacy-14) — global command palette.
//
// Phase 2.5 acceptance check. At base the only shortcut surface is the
// `?`-triggered KeyboardShortcuts help dialog
// (src/components/shared/KeyboardShortcuts.tsx); its own table lists
// "Ctrl/Cmd + K" as "Focus session search" (a ChatShell-scoped listener).
// There is NO command palette: nothing opens on Ctrl/Cmd+K at the app
// shell level, and there is no command-execution surface.
//
// This check requires, at the app-shell level (real PageShell/nav, pages
// stubbed — mirroring the App.draft-room.test.tsx convention so this file
// tests only the palette wiring):
//   1. Ctrl+K (or Cmd+K) opens a dialog-role palette from a normal route;
//   2. the palette lists navigation commands (at least one destination
//      route among documents/chats/vaults/drafts/…);
//   3. executing that command navigates (window.location.pathname changes
//      from the starting route to the command's destination).
//
// At base step 1 fails → RED. The dialog ROLE is required (not a custom
// div): a palette is a modal surface, and role="dialog" is the Radix
// Dialog default — so a Phase 4 implementation using the repo's Dialog
// primitive satisfies this without tailoring.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      user: { id: 1, username: "alice", role: "member" },
      logout: vi.fn(),
      init: vi.fn().mockResolvedValue(undefined),
    }),
}));

vi.mock("@/components/auth/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({ backend: true, embeddings: true, chat: true, loading: false }),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => false,
}));

// Every routed page is stubbed to a marker (App.draft-room.test.tsx
// convention): this file verifies the PALETTE wiring in the shell, not any
// page's internals.
vi.mock("@/pages/ChatShell", () => ({ default: () => <div>Chat Page</div> }));
vi.mock("@/pages/DocumentsPage", () => ({ default: () => <div>Documents Page</div> }));
vi.mock("@/pages/DocumentDetailPage", () => ({ default: () => <div>Document Detail Page</div> }));
vi.mock("@/pages/MemoryPage", () => ({ default: () => <div>Memory Page</div> }));
vi.mock("@/pages/VaultsPage", () => ({ default: () => <div>Vaults Page</div> }));
vi.mock("@/pages/SettingsPage", () => ({ default: () => <div>Settings Page</div> }));
vi.mock("@/pages/LoginPage", () => ({ default: () => <div>Login Page</div> }));
vi.mock("@/pages/SetupPage", () => ({ default: () => <div>Setup Page</div> }));
vi.mock("@/pages/RegisterPage", () => ({ default: () => <div>Register Page</div> }));
vi.mock("@/pages/AdminUsersPage", () => ({ default: () => <div>Admin Users Page</div> }));
vi.mock("@/pages/AdminGroupsPage", () => ({ default: () => <div>Admin Groups Page</div> }));
vi.mock("@/pages/OrgsPage", () => ({ default: () => <div>Organizations Page</div> }));
vi.mock("@/pages/ProfilePage", () => ({ default: () => <div>Profile Page</div> }));
vi.mock("@/pages/ChangePasswordRequiredPage", () => ({ default: () => <div>Change Password Page</div> }));
vi.mock("@/pages/NotFoundPage", () => ({ default: () => <div>Not Found Page</div> }));
vi.mock("@/pages/WikiPage", () => ({ default: () => <div>Wiki Page</div> }));
vi.mock("@/pages/KMSPage", () => ({ default: () => <div>KMS Page</div> }));
vi.mock("@/pages/KMSDetailPage", () => ({ default: () => <div>KMS Detail Page</div> }));
vi.mock("@/pages/DraftRoomPage", () => ({ default: () => <div>Draft Room Page</div> }));
vi.mock("@/pages/DraftRoomDetailPage", () => ({ default: () => <div>Draft Room Detail Page</div> }));
vi.mock("@/pages/SearchPage", () => ({ default: () => <div>Search Page</div> }));

describe("AC25 — global command palette (legacy-14)", () => {
  beforeEach(() => {
    // Start AWAY from the palette command's destination so the navigation
    // assertion is discriminating.
    window.history.pushState({}, "", "/vaults");
  });

  it(
    "AC25: Ctrl/Cmd+K opens a command palette listing navigation commands; executing one navigates",
    { timeout: 30_000 },
    async () => {
      const { default: App } = await import("../App");
      render(<App />);

      // Shell mounted at the starting route before the palette interaction.
      expect(await screen.findByText("Vaults Page")).toBeTruthy();

      // Sentinel immediately before the discriminating assertion.
      console.log(
        'AC25 CHECK: FAIL — expecting Ctrl+K (or Cmd+K) to open a command palette (role="dialog") from the app shell'
      );

      // Dispatch on document.body so both window- and document-level
      // keydown listeners receive it. Accept either binding.
      fireEvent.keyDown(document.body, { key: "k", code: "KeyK", ctrlKey: true });
      let palette = screen.queryByRole("dialog");
      if (palette === null) {
        fireEvent.keyDown(document.body, { key: "k", code: "KeyK", metaKey: true });
        palette = screen.queryByRole("dialog");
      }
      // At base this is null (only the `?` help dialog exists, and it is
      // closed; Ctrl+K is ChatShell-scoped and that page is not mounted
      // here) → RED.
      expect(palette, "no dialog opened on Ctrl/Cmd+K — no command palette at base").toBeTruthy();

      // The palette itself lists navigation commands (scoped INSIDE the
      // dialog so a page marker behind it can never satisfy this).
      console.log("AC25 CHECK: FAIL — expecting the palette to list navigation commands (documents/chats/vaults/…)");
      const command = await waitFor(() => {
        const option = within(palette as HTMLElement).queryByText(/documents/i);
        expect(option, "no navigation command mentioning a destination inside the palette").toBeTruthy();
        return option as HTMLElement;
      });

      // Executing the command navigates from /vaults to the destination.
      console.log("AC25 CHECK: FAIL — expecting command execution to navigate to the documents route");
      fireEvent.click(command);
      await waitFor(() => expect(window.location.pathname).toBe("/documents"), { timeout: 5_000 });
    }
  );
});

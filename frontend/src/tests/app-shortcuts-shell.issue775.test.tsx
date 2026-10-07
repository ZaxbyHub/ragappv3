// frontend/src/tests/app-shortcuts-shell.issue775.test.tsx
// Issue #775 review PRR-302 — the "?" shortcut must be reachable through the
// REAL App render path (AppShortcutsMount inside MainAppShell), not only via
// component-level harnesses or the source-scan guardrail: render the actual
// App at a non-chat shell route, dispatch "?" on window, and require the
// keyboard-shortcuts dialog to open.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

vi.mock("@/lib/api/onboarding", () => ({
  getOnboardingMilestones: vi.fn().mockResolvedValue({
    vault_created: true,
    upload_indexed: true,
    first_question_asked: true,
    first_citation_opened: true,
    show_checklist: false,
  }),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

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

// Routed pages stubbed (App.draft-room.test.tsx convention): this file pins
// the SHELL's shortcuts surface, not any page's internals.
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

describe("issue #775 review PRR-302 — '?' through the real App shell", () => {
  beforeEach(() => {
    window.history.pushState({}, "", "/vaults");
  });

  it("pressing ? at a shell route opens the keyboard-shortcuts dialog", async () => {
    const { default: App } = await import("../App");
    render(<App />);

    expect(await screen.findByText("Vaults Page")).toBeTruthy();

    fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: false, metaKey: false });

    await waitFor(() => {
      expect(screen.getByText("Keyboard Shortcuts")).toBeTruthy();
    });
  });
});

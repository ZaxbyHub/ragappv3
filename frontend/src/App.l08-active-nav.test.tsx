import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Mirrors the mocking convention in App.draft-room.test.tsx: shell-level
// dependencies and every routed page are stubbed to a trivial marker so this
// file tests ONLY the routing wiring in App.tsx (route registration, active
// item resolution), not any page's internal behavior.

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: { init: () => Promise<void> }) => unknown) =>
    selector({ init: vi.fn().mockResolvedValue(undefined) }),
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

// Capture the activeItem PageShell receives so route->nav-highlight mapping
// (getActiveItemFromPath in App.tsx) is verifiable without rendering the real
// NavigationRail/MobileBottomNav (covered by their own dedicated test files).
vi.mock("@/components/layout/PageShell", () => ({
  PageShell: ({
    children,
    activeItem,
  }: {
    children: React.ReactNode;
    activeItem: string;
  }) => (
    <div>
      <div data-testid="active-item">{activeItem}</div>
      {children}
    </div>
  ),
}));

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
vi.mock("@/pages/SearchPage", () => ({ default: () => <div>Search Page</div> }));
vi.mock("@/pages/DraftRoomPage", () => ({ default: () => <div>Draft Room Page</div> }));
vi.mock("@/pages/DraftRoomDetailPage", () => ({ default: () => <div>Draft Room Detail Page</div> }));

async function renderAppAt(path: string) {
  window.history.pushState({}, "", path);
  const { default: App } = await import("./App");
  return render(<App />);
}

describe("App /search active nav item (issue #779, UI-R1-08)", { timeout: 30_000 }, () => {
  it("search route does not mark Documents active", async () => {
    await renderAppAt("/search");

    expect(await screen.findByText("Search Page")).toBeInTheDocument();
    // /search is owned by no nav item; falling through to "documents" wrongly
    // marks Documents as the current page in the rail and mobile bottom nav.
    const captured = screen.getByTestId("active-item").textContent;
    expect(captured === "documents").toBe(false);
  });
});

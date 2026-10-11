import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Issue #779 (UI-R2-06): a page-level render error must not take the whole
// shell down. This file renders App with the REAL PageShell/Navigation/
// ErrorBoundary and mocks @/pages/DocumentsPage to throw during render, then
// asserts the two acceptance criteria: navigation landmarks stay mounted, and
// popstate navigation away from the crashed page recovers to a live route.

// Gotcha §5 (docs/engineering/testing.md): the real NavigationRail imports
// useThemeStore, which calls window.matchMedia at module-load time, and jsdom
// does not implement matchMedia. Install the stub before the dynamic App
// import below.
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({ init: vi.fn().mockResolvedValue(undefined), user: undefined, isAuthenticated: false }),
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

// The real PageShell fetches settings on mount; keep @/lib/api real but give
// getSettings a stubbed resolved value so no network/axios path is exercised.
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getSettings: async () => ({ chat_configured: true }),
}));

// The crashing page — throws during render.
vi.mock("@/pages/DocumentsPage", () => ({
  default: () => {
    throw new Error("l08 boundary probe: documents render boom");
  },
}));

vi.mock("@/pages/ChatShell", () => ({ default: () => <div>Chat Page</div> }));
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
vi.mock("@/components/canvas/CanvasPage", () => ({ default: () => <div>Canvas Page</div> }));

async function renderAppAt(path: string) {
  window.history.pushState({}, "", path);
  const { default: App } = await import("./App");
  return render(<App />);
}

describe("App page render error containment (issue #779, UI-R2-06)", { timeout: 30_000 }, () => {
  it("a page render error keeps navigation mounted", async () => {
    await renderAppAt("/documents");

    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    // The rail/mobile-nav landmarks must survive the page crash: 0 landmarks
    // means the boundary wrapped the whole router and unmounted navigation.
    expect(screen.queryAllByRole("navigation").length).toBeGreaterThan(0);
  });

  it("navigating away from a crashed page recovers", async () => {
    await renderAppAt("/documents");
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();

    // Browser-style back/forward navigation away from the crashed page.
    window.history.pushState({}, "", "/vaults");
    window.dispatchEvent(new PopStateEvent("popstate"));

    // Give React Router a moment to react to the popstate.
    await new Promise((resolve) => setTimeout(resolve, 200));

    expect(screen.queryAllByText("Vaults Page").length).toBeGreaterThan(0);
  });
});

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Issue #779 (UI-R4-08): VITE_TEST_MODE must yield a usable app. This file
// keeps the REAL useAuthStore, ProtectedRoute, RoleGuard, PageShell and
// LoginPage so the demo-session seeding path is exercised end to end. Only
// sibling page modules (and shell data hooks) are stubbed to markers.

// Gotcha §5: stub the env BEFORE the dynamic App import — App.tsx and
// LoginPage.tsx evaluate TEST_MODE (import.meta.env.VITE_TEST_MODE) at module
// load. Stubbed here at top of file so both read "true".
vi.stubEnv("VITE_TEST_MODE", "true");

// Gotcha §5 (docs/engineering/testing.md): the real PageShell -> Navigation ->
// NavigationRail -> useThemeStore chain calls window.matchMedia at module-load
// time, and jsdom does not implement matchMedia. Install the stub before the
// dynamic App import.
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

// Admin pages mocked to markers; LoginPage stays REAL (it renders the
// "Welcome Back" card and the "Loading Meridian…" spinner we assert on).
// AdminUsersPage's marker is WRAPPED IN THE REAL AdminGuard — the guard's
// synchronous isAuthenticated read is the defect site, so it must stay real
// (a bare marker would skip it and the bounce could never happen).
vi.mock("@/pages/AdminUsersPage", async () => {
  const { AdminGuard } = await import("@/components/auth/RoleGuard");
  return { default: () => <AdminGuard><div>Admin Users Page</div></AdminGuard> };
});
vi.mock("@/pages/AdminGroupsPage", () => ({ default: () => <div>Admin Groups Page</div> }));
vi.mock("@/pages/OrgsPage", () => ({ default: () => <div>Organizations Page</div> }));
vi.mock("@/pages/DocumentsPage", () => ({ default: () => <div>Documents Page</div> }));
vi.mock("@/pages/ChatShell", () => ({ default: () => <div>Chat Page</div> }));
vi.mock("@/pages/DocumentDetailPage", () => ({ default: () => <div>Document Detail Page</div> }));
vi.mock("@/pages/MemoryPage", () => ({ default: () => <div>Memory Page</div> }));
vi.mock("@/pages/VaultsPage", () => ({ default: () => <div>Vaults Page</div> }));
vi.mock("@/pages/SettingsPage", () => ({ default: () => <div>Settings Page</div> }));
vi.mock("@/pages/SetupPage", () => ({ default: () => <div>Setup Page</div> }));
vi.mock("@/pages/RegisterPage", () => ({ default: () => <div>Register Page</div> }));
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

describe("App test-mode session seeding (issue #779, UI-R4-08)", { timeout: 30_000 }, () => {
  beforeEach(async () => {
    // Reset the real store so each test starts from the boot state
    // (test-mode seeding happens per mount, never via persisted state).
    const { useAuthStore } = await import("@/stores/useAuthStore");
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: false,
      needsSetup: null,
      isLoading: false,
      initializationFailed: false,
    });
  });

  it("test mode renders admin routes without bouncing to login", async () => {
    await renderAppAt("/admin/users");
    await new Promise((resolve) => setTimeout(resolve, 2000));

    // The admin page (behind the real AdminGuard) must own the screen — the
    // real LoginPage's "Welcome Back" card must never appear.
    expect(screen.queryAllByText("Welcome Back").length).toBe(0);
  });

  it("test mode login route does not spin forever", async () => {
    await renderAppAt("/login");
    await new Promise((resolve) => setTimeout(resolve, 5000));

    // Either the real login form rendered (its username field is labelled
    // via htmlFor="login-username") or the route redirected — anything but
    // the eternal "Loading Meridian…" spinner on /login.
    const hasUsernameField = screen.queryByLabelText(/username/i) !== null;
    const leftLogin = window.location.pathname !== "/login";
    expect(hasUsernameField || leftLogin).toBe(true);
  });
});

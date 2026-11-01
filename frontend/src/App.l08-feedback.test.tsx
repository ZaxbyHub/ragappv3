import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Issue #779 feedback round (F-2 / PRR-007): the frozen AC5 check asserts
// only the ABSENCE of the login form, so a blank shell or a crashed render
// would satisfy it. This companion pins the POSITIVE contract — the admin
// page itself owns the screen at the requested URL. Frozen AC5 text stays
// untouched by design.

// Gotcha §5: env stub at module scope, BEFORE the dynamic App import.
vi.stubEnv("VITE_TEST_MODE", "true");

if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false, media: query, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {},
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

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getSettings: async () => ({ chat_configured: true }),
}));

// Same convention as the frozen file: the admin marker stays wrapped in the
// REAL AdminGuard so the synchronous isAuthenticated read is exercised.
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
vi.mock("@/pages/LoginPage", () => ({ default: () => <div>Login Page</div> }));
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

describe("App test-mode admin route positive pin (issue #779 feedback F-2)", { timeout: 30_000 }, () => {
  beforeEach(async () => {
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

  it("admin page owns the screen at /admin/users (positive control)", async () => {
    window.history.pushState({}, "", "/admin/users");
    const { default: App } = await import("./App");
    render(<App />);

    // Positive: the admin page itself rendered (through the real AdminGuard)
    // — not merely "the login form is absent".
    expect(await screen.findByText("Admin Users Page")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/admin/users");
    expect(screen.queryAllByText("Welcome Back").length).toBe(0);
  });
});

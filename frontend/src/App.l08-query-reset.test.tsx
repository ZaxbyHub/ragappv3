import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Issue #779 feedback round (F-4/PRR-013): the PageShell route boundary
// resets on pathname + search, so a query-only navigation (the rail's
// /search re-submit) recovers a crashed /search without a path change.
// This file renders the REAL PageShell/ErrorBoundary — the capture-mock
// file (App.l08-search-reset.test.tsx) has no route boundary, so recovery
// cannot be exercised there.

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

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getSettings: async () => ({ chat_configured: true }),
}));

// The search page is the crash fixture: q=a throws, any other query renders
// its marker — so the recovery assertion proves the NEW query's content.
vi.mock("@/pages/SearchPage", () => ({
  default: function SearchPageProbe() {
    const query = new URLSearchParams(window.location.search);
    if (query.get("q") === "a") {
      throw new Error("query-reset probe: q=a boom");
    }
    return <div>Search Page recovered</div>;
  },
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
vi.mock("@/pages/DraftRoomPage", () => ({ default: () => <div>Draft Room Page</div> }));
vi.mock("@/pages/DraftRoomDetailPage", () => ({ default: () => <div>Draft Room Detail Page</div> }));
vi.mock("@/components/canvas/CanvasPage", () => ({ default: () => <div>Canvas Page</div> }));

describe("App query-only navigation resets a crashed route (issue #779 feedback F-4)", { timeout: 30_000 }, () => {
  it("/search?q=a -> /search?q=b recovers through the real route boundary", async () => {
    window.history.pushState({}, "", "/search?q=a");
    const { default: App } = await import("./App");
    render(<App />);

    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();

    window.history.pushState({}, "", "/search?q=b");
    window.dispatchEvent(new PopStateEvent("popstate"));

    // findBy* (not a fixed sleep): recovers as soon as the reset lands
    // (diagnostic measured ~118ms on this path).
    expect(await screen.findByText("Search Page recovered")).toBeInTheDocument();
    expect(screen.queryAllByText("Something went wrong").length).toBe(0);

    // Navigation landmarks survived (route-scoped containment held).
    expect(screen.queryAllByRole("navigation").length).toBeGreaterThan(0);
  });
});

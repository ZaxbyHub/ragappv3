import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Supplemental end-to-end test-mode coverage for #779. Keep the real auth
// store, ProtectedRoute, AdminGuard, SetupPage, and RegisterPage. Only sibling
// pages and shell data hooks are stubbed, so markers cannot bypass the guards.
vi.stubEnv("VITE_TEST_MODE", "true");
vi.stubEnv("VITE_DEMO_ROLE", "admin");

const authClientPost = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ data: {} }),
);

vi.mock("axios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("axios")>();
  const realCreate = actual.default.create.bind(actual.default);
  return {
    ...actual,
    default: Object.assign(actual.default, {
      create: (...args: Parameters<typeof realCreate>) => {
        const client = realCreate(...args);
        client.post = authClientPost as typeof client.post;
        return client;
      },
    }),
  };
});

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

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  authClient: { post: authClientPost },
  getSettings: async () => ({ chat_configured: true }),
}));

vi.mock("@/pages/AdminUsersPage", async () => {
  const { AdminGuard } = await import("@/components/auth/RoleGuard");
  return { default: () => <AdminGuard><div>Admin Users Page</div></AdminGuard> };
});
vi.mock("@/pages/DocumentsPage", () => ({ default: () => <div>Documents Page</div> }));
vi.mock("@/pages/ChatShell", () => ({ default: () => <div>Chat Page</div> }));
vi.mock("@/pages/DocumentDetailPage", () => ({ default: () => <div>Document Detail Page</div> }));
vi.mock("@/pages/MemoryPage", () => ({ default: () => <div>Memory Page</div> }));
vi.mock("@/pages/VaultsPage", () => ({ default: () => <div>Vaults Page</div> }));
vi.mock("@/pages/SettingsPage", () => ({ default: () => <div>Settings Page</div> }));
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
  await act(async () => {
    window.history.pushState({}, "", path);
  });
  const { default: App } = await import("./App");
  return render(<App />);
}

async function navigateTo(path: string) {
  await act(async () => {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

function hasCrossComponentUpdateWarning(calls: unknown[][]): boolean {
  return calls.some(([format]) =>
    typeof format === "string" &&
    format.includes("Cannot update a component") &&
    format.includes("while rendering a different component"),
  );
}

describe("App test-mode feedback coverage (#779)", { timeout: 30_000 }, () => {
  beforeEach(async () => {
    const { useAuthStore } = await import("@/stores/useAuthStore");
    await act(async () => {
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
    authClientPost.mockClear().mockResolvedValue({ data: {} });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
    vi.stubEnv("VITE_TEST_MODE", "true");
    vi.stubEnv("VITE_DEMO_ROLE", "admin");
  });

  it("matches the captured logout-red-3 backtick warning shape", () => {
    expect(hasCrossComponentUpdateWarning([[
      "Cannot update a component (`%s`) while rendering a different component (`%s`). To locate the bad setState() call inside `%s`, follow the stack trace.",
      "CommandPalette",
      "App",
      "App",
    ]])).toBe(true);
    expect(hasCrossComponentUpdateWarning([["ordinary diagnostic"]])).toBe(false);
  });

  it("seeds before the real admin guard and leaves the admin marker at the requested path", async () => {
    await renderAppAt("/admin/users");

    await waitFor(() => expect(screen.getByText("Admin Users Page")).toBeInTheDocument());
    expect(window.location.pathname).toBe("/admin/users");
    expect(screen.queryByText("Welcome Back")).not.toBeInTheDocument();
    const { useAuthStore } = await import("@/stores/useAuthStore");
    const state = useAuthStore.getState();
    expect(state.user?.username).toBe("demo");
    expect(state.user?.role).toBe("admin");
    expect(state.isAuthenticated).toBe(true);
    expect(state.isInitialized).toBe(true);
    expect(state.needsSetup).toBe(false);
    expect(state.isLoading).toBe(false);
  });

  it("keeps the public register page reachable before the first protected navigation", async () => {
    await renderAppAt("/register");

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /create account/i })).toBeInTheDocument(),
    );
    expect(window.location.pathname).toBe("/register");
    expect(screen.queryByText("Welcome Back")).not.toBeInTheDocument();
  });

  it.each(["/register/", "/REGISTER/", "/%72egister"])(
    "keeps the public register form reachable at route spelling %s",
    async (path) => {
      await renderAppAt(path);

      await waitFor(() =>
        expect(screen.getByRole("button", { name: /create account/i })).toBeInTheDocument(),
      );
      expect(window.location.pathname).toBe(path);
      const { useAuthStore } = await import("@/stores/useAuthStore");
      const state = useAuthStore.getState();
      expect(state.user).toBeNull();
      expect(state.accessToken).toBeNull();
      expect(state.isAuthenticated).toBe(false);
    },
  );

  it("preserves an existing authenticated principal during bootstrap", async () => {
    const { useAuthStore } = await import("@/stores/useAuthStore");
    await act(async () => {
      useAuthStore.setState({
        user: {
          id: 99,
          username: "existing-user",
          full_name: "Existing User",
          role: "admin",
          is_active: true,
        },
        accessToken: "existing-token",
        isAuthenticated: true,
        isInitialized: true,
        needsSetup: false,
        isLoading: false,
      });
    });
    await renderAppAt("/documents");
    await waitFor(() => expect(screen.getByText("Documents Page")).toBeInTheDocument());
    const state = useAuthStore.getState();
    expect(state.user?.username).toBe("existing-user");
    expect(state.accessToken).toBe("existing-token");
    expect(state.isAuthenticated).toBe(true);
  });
  it("seeds a same-mounted register to admin transition without a render-phase warning", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await renderAppAt("/register");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /create account/i })).toBeInTheDocument(),
    );

    await navigateTo("/admin/users");
    await waitFor(() => expect(screen.getByText("Admin Users Page")).toBeInTheDocument());
    expect(window.location.pathname).toBe("/admin/users");
    expect(hasCrossComponentUpdateWarning(errorSpy.mock.calls)).toBe(false);
  });

  it("exercises the real login route without marker-only substitutes", async () => {
    await renderAppAt("/login");
    await waitFor(() => expect(screen.getByPlaceholderText("Username")).toBeInTheDocument());
    expect(window.location.pathname).toBe("/login");
    expect(screen.queryByText("Documents Page")).not.toBeInTheDocument();
  });

  it("redirects completed setup to the real login route", async () => {
    await renderAppAt("/setup");
    await waitFor(() => {
      expect(window.location.pathname).toBe("/login");
      expect(screen.getByPlaceholderText("Username")).toBeInTheDocument();
    });
    const { useAuthStore } = await import("@/stores/useAuthStore");
    const state = useAuthStore.getState();
    expect(state.user?.username).toBe("demo");
    expect(state.user?.role).toBe("admin");
    expect(state.isAuthenticated).toBe(true);
    expect(state.isInitialized).toBe(true);
    expect(state.needsSetup).toBe(false);
    expect(state.isLoading).toBe(false);
  });

  it("clears the demo session on real logout and rerenders the terminal state", async () => {
    await renderAppAt("/admin/users");
    await waitFor(() => expect(screen.getByText("Admin Users Page")).toBeInTheDocument());
    const { useAuthStore } = await import("@/stores/useAuthStore");

    await act(async () => {
      await useAuthStore.getState().logout();
    });

    expect(authClientPost).toHaveBeenCalledWith(
      "/auth/logout",
      {},
      expect.objectContaining({ withCredentials: true, signal: expect.any(AbortSignal) }),
    );
    await waitFor(() => {
      const state = useAuthStore.getState();
      expect(state.user).toBeNull();
      expect(state.accessToken).toBeNull();
      expect(state.isAuthenticated).toBe(false);
      expect(state.isLoading).toBe(false);
      expect(screen.queryByText("Admin Users Page")).not.toBeInTheDocument();
    });
  });

  it("redirects desktop logout from documents to login with a cleared session", async () => {
    await renderAppAt("/documents");
    await waitFor(() => expect(screen.getByText("Documents Page")).toBeInTheDocument());
    const { useAuthStore } = await import("@/stores/useAuthStore");
    const desktopNavigation = screen.getByRole("navigation", { name: "Main navigation" });
    fireEvent.click(within(desktopNavigation).getByRole("button", { name: "Log out" }));
    await waitFor(() => {
      expect(window.location.pathname).toBe("/login");
      expect(screen.getByPlaceholderText("Username")).toBeInTheDocument();
    });
    const state = useAuthStore.getState();
    expect(state.user).toBeNull();
    expect(state.accessToken).toBeNull();
    expect(state.isAuthenticated).toBe(false);
    expect(screen.queryByText("Documents Page")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
  it("does not emit cross-component update warnings while StrictMode mounts the seeded session", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await act(async () => {
      window.history.pushState({}, "", "/documents");
    });
    const { StrictMode } = await import("react");
    const { default: App } = await import("./App");
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
    await waitFor(() => expect(screen.getByText("Documents Page")).toBeInTheDocument());
    expect(hasCrossComponentUpdateWarning(errorSpy.mock.calls)).toBe(false);
  });
});

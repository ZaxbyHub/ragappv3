import { useEffect, useRef } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ProtectedRoute } from "./ProtectedRoute";
import { resetInitState, useAuthStore } from "@/stores/useAuthStore";

const providerMocks = vi.hoisted(() => ({
  authGet: vi.fn(),
  authPost: vi.fn(),
  authPatch: vi.fn(),
  refreshAccessToken: vi.fn(),
  ensureCsrfToken: vi.fn(),
  setJwtAccessToken: vi.fn(),
  resetCsrfToken: vi.fn(),
  resetSubpathRefreshDiagnostic: vi.fn(),
  fetchVaults: vi.fn(),
}));

// Axios is the network boundary for useAuthStore's auth client. The store and
// its public init action remain real; only transport responses are controlled.
vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: providerMocks.authGet,
      post: providerMocks.authPost,
      patch: providerMocks.authPatch,
      put: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    })),
  },
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    refreshAccessToken: providerMocks.refreshAccessToken,
    ensureCsrfToken: providerMocks.ensureCsrfToken,
    setJwtAccessToken: providerMocks.setJwtAccessToken,
    resetCsrfToken: providerMocks.resetCsrfToken,
    resetSubpathRefreshDiagnostic: providerMocks.resetSubpathRefreshDiagnostic,
    attachCsrfInterceptor: vi.fn(),
  };
});

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: {
    getState: () => ({ fetchVaults: providerMocks.fetchVaults }),
  },
}));

// Keep the loader's role observable without the jsdom live-region cleanup crash
// covered by the existing ProtectedRoute preserving tests.
vi.mock("lucide-react", () => ({
  Loader2: ({ role, className }: { role?: string; className?: string }) => (
    <div role={role} className={className} data-testid="loader2" />
  ),
}));

function resetAuthState() {
  useAuthStore.setState({
    user: null,
    accessToken: null,
    isAuthenticated: false,
    isInitialized: false,
    isLoading: false,
    needsSetup: null,
    authMode: "unknown",
  });
}

function BootedProtectedRoute() {
  const init = useAuthStore((state) => state.init);
  const started = useRef(false);

  useEffect(() => {
    if (!started.current) {
      started.current = true;
      void init();
    }
  }, [init]);

  return (
    <ProtectedRoute>
      <div>Authoritative protected content</div>
    </ProtectedRoute>
  );
}

function renderBootedRoute() {
  return render(
    <MemoryRouter initialEntries={["/documents"]}>
      <Routes>
        <Route path="/login" element={<div>Login sentinel</div>} />
        <Route path="*" element={<BootedProtectedRoute />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("C25 real setup status through the ProtectedRoute public boundary", () => {
  let originalAuthState: ReturnType<typeof useAuthStore.getState>;

  beforeEach(() => {
    originalAuthState = useAuthStore.getState();
    vi.clearAllMocks();
    resetInitState();
    resetAuthState();
    providerMocks.refreshAccessToken.mockResolvedValue(null);
    providerMocks.ensureCsrfToken.mockResolvedValue("csrf-token");
    providerMocks.fetchVaults.mockResolvedValue(undefined);
  });

  afterEach(() => {
    cleanup();
    resetInitState();
    useAuthStore.setState(originalAuthState, true);
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("retries a real failed setup-status read and publishes the authoritative false route", async () => {
    providerMocks.authGet
      .mockRejectedValueOnce(new Error("setup status unavailable"))
      .mockResolvedValueOnce({ data: { needs_setup: false, auth_mode: "jwt" } });

    renderBootedRoute();

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(useAuthStore.getState().needsSetup).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText("Login sentinel")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.getByText("Login sentinel")).toBeInTheDocument());
    expect(providerMocks.authGet).toHaveBeenNthCalledWith(1, "/auth/setup-status");
    expect(providerMocks.authGet).toHaveBeenNthCalledWith(2, "/auth/setup-status");
    expect(useAuthStore.getState()).toMatchObject({
      isInitialized: true,
      isLoading: false,
      isAuthenticated: false,
      needsSetup: false,
      authMode: "jwt",
    });
    expect(screen.queryByText("Authoritative protected content")).not.toBeInTheDocument();
  });
});

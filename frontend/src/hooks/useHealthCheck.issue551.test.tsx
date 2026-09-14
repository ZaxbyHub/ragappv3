/**
 * Issue #551 acceptance check — C12 frontend half.
 *
 * Pins `useHealthCheck` (frontend/src/hooks/useHealthCheck.ts): the mount
 * check (and the >=90s deep backstop) may send `deep=true` ONLY when the user
 * is authenticated (`useAuthStore` state `isAuthenticated` from
 * frontend/src/stores/useAuthStore.ts). Today the hook ALWAYS sends deep=true
 * on the first check regardless of auth — an unauthenticated visitor triggers
 * the expensive, provider-probing deep collection on the open backend route.
 *
 * Expected status at the PRE-FIX code (intentional — the discriminating check
 * proves the fix is required):
 *   FAIL (RED): "does not send deep=true when unauthenticated"
 *               (the mount check is sent with { params: { deep: true } })
 *   PASS (GREEN at base): "sends deep=true on first check when authenticated"
 *
 * The api module is mocked exactly like the existing useHealthCheck suites
 * (spread-importActual so the real `useAuthStore` — imported for its state —
 * still sees the real named exports it wires at module load). The auth store
 * itself is the REAL zustand store: state is set with `useAuthStore.setState`
 * and the prior state is restored after each test.
 */
import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockGet = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    default: { get: mockGet },
  };
});

import { useHealthCheck } from "@/hooks/useHealthCheck";
import { useAuthStore } from "@/stores/useAuthStore";

const okResponse = {
  data: {
    status: "ok",
    services: { backend: true },
  },
};

describe("useHealthCheck issue #551 — deep probe only for authenticated users", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;

  beforeEach(() => {
    vi.useFakeTimers();
    // Real store (not mocked): the fixed hook must gate deep on this flag.
    priorAuthState = useAuthStore.getState();
  });

  afterEach(() => {
    vi.useRealTimers();
    useAuthStore.setState(priorAuthState);
    mockGet.mockReset();
    vi.clearAllMocks();
    cleanup();
  });

  it("does not send deep=true when unauthenticated", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    mockGet.mockResolvedValue(okResponse as never);

    renderHook(() => useHealthCheck());

    // Mount check fires immediately (no pollInterval needed for this pin).
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));

    expect(mockGet.mock.calls[0][0]).toBe("/health");
    const config = mockGet.mock.calls[0][1] as
      | { params?: Record<string, unknown> }
      | undefined;
    expect(
      config?.params ?? {},
      "unauthenticated users must never trigger the deep (provider-probing) health check",
    ).not.toHaveProperty("deep");
  });

  it("sends deep=true on first check when authenticated", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    mockGet.mockResolvedValue(okResponse as never);

    renderHook(() => useHealthCheck());

    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));

    expect(mockGet.mock.calls[0][0]).toBe("/health");
    const config = mockGet.mock.calls[0][1] as
      | { params?: Record<string, unknown> }
      | undefined;
    expect(config?.params).toEqual({ deep: true });
  });
});

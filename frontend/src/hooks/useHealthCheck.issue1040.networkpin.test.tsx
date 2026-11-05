/**
 * Issue #1040 acceptance check — C2 (AC2, PRESERVING), split into its own file
 * so the file-level exit code reflects this check alone (its DISCRIMINATING
 * sibling C1 lives in useHealthCheck.issue1040.test.tsx and is RED at base).
 * This file is FROZEN for trace 1040-991-health-401-chat-cap-sync.
 *
 * C2: consecutive rejections with a NON-auth failure shape keep today's
 * behavior and still publish the all-down state, on both legs:
 *   - a network-style error (no `status` — e.g. `new Error("Network Error")`);
 *   - a server error carrying a 5xx top-level `status` (e.g. status 500) —
 *     the #1040 fix must special-case 401 only, never "any status-bearing
 *     rejection".
 * Pins current behavior: GREEN at base, must stay green.
 *
 * The api module is mocked exactly like the existing useHealthCheck suites
 * (spread-importActual so the real `useAuthStore` still sees the named
 * exports it wires at module load); the auth store is the REAL zustand store,
 * state set with `useAuthStore.setState` and restored after each test.
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

describe("useHealthCheck issue #1040 pin — network failures still reach down", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;

  beforeEach(() => {
    vi.useFakeTimers();
    priorAuthState = useAuthStore.getState();
  });

  afterEach(() => {
    vi.useRealTimers();
    useAuthStore.setState(priorAuthState);
    mockGet.mockReset();
    vi.clearAllMocks();
    cleanup();
  });

  it("C2: two consecutive network-style rejections (no status) still publish the all-down state (GREEN at base; must stay green)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    mockGet.mockImplementation(() => Promise.reject(new Error("Network Error")));

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(0);

    const health = result.current;
    expect(health.loading).toBe(false);
    expect(health.backend).toBe(false);
    expect(health.embeddings).toBe(false);
    expect(health.chat).toBe(false);
    expect(health.lastChecked).toBeInstanceOf(Date);
  });

  it("C2: two consecutive 5xx-status rejections (status 500) still publish the all-down state — only 401 is an auth event (GREEN at base; must stay green)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    // A server error carrying a top-level status, the same normalized shape
    // as the #1040 401 but with a 5xx code: the outage semantics must not
    // change for it.
    mockGet.mockImplementation(() =>
      Promise.reject(Object.assign(new Error("Server Error"), { status: 500 })),
    );

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(0);

    const health = result.current;
    expect(health.loading).toBe(false);
    expect(health.backend).toBe(false);
    expect(health.embeddings).toBe(false);
    expect(health.chat).toBe(false);
    expect(health.lastChecked).toBeInstanceOf(Date);
  });
});

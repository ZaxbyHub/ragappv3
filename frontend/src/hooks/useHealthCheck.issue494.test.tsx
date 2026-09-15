/**
 * Issue #494 acceptance check — AC6 / OPS-008 (PRESERVING).
 *
 * Pins `useHealthCheck` (frontend/src/hooks/useHealthCheck.ts, landed via
 * PR #493): a healthy deep check followed by lightweight (shallow) polls that
 * return HTTP 200 with `null` service values ("not checked this cycle") must
 * RETAIN the last-known indicators — null must never be coerced to `false`.
 *
 * Gap analysis (why this file exists): the existing suite
 * `frontend/src/tests/useHealthCheck.test.tsx` covers the fetch-REJECTION
 * contract (one failure after a success retains; two consecutive failures
 * flip down; cold-start failure surfaces immediately) but never feeds a 200
 * response with `null`/absent service values, so the
 * `services?.embeddings ?? prev.embeddings` retention path was unasserted.
 * The two-consecutive-failures-DO half of the AC remains covered by the
 * existing file's first node ("retains last-known on one failure after a
 * success; flips down on two consecutive"); this node covers exactly the
 * healthy-then-null half, plus the null-vs-false contrast.
 *
 * Wire-level facts asserted alongside the state: the mount check is sent with
 * `{ params: { deep: true } }` and the interval polls with `{ params: {} }`
 * (lightweight), read from the mocked api client's call log. Since issue
 * #551 the mount check carries deep=true only for an authenticated user, so
 * this scenario runs with the auth store set to authenticated.
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

const healthyDeepResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: true, chat: true },
  },
};

/** Lightweight poll body: backend reachable, services "not checked". */
const nullServicesResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: null, chat: null },
  },
};

/** Explicit-false poll body: backend authoritative "down" verdicts. */
const explicitFalseResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: false, chat: false },
  },
};

describe("useHealthCheck — last-known retention on null probes (issue #494 AC6/OPS-008, PRESERVING)", () => {
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

  it("never sends deep=true on the 90s backstop when unauthenticated (PRR-005)", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    // Real service booleans: keeps the cold-cache re-check (PRR-002) out of
    // this pin so the backstop scheduling is isolated.
    mockGet.mockResolvedValue(healthyDeepResponse as never);
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 30_000 }));

    // Three heartbeat ticks past the 90s deep-recheck interval: an
    // unauthenticated session must stay shallow-only on every one of them.
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.advanceTimersByTimeAsync(31_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(4));
    for (let i = 0; i < 4; i++) {
      expect(mockGet.mock.calls[i][1]).toEqual({ params: {} });
    }
    expect(result.current.backend).toBe(true);
  });

  it("sends deep=true on the 90s backstop when authenticated (PRR-005 symmetric pin)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    mockGet.mockResolvedValue(healthyDeepResponse as never);
    renderHook(() => useHealthCheck({ pollInterval: 30_000 }));

    // Mount check is deep; ticks at 30s/60s are shallow; the 90s tick is the
    // deep backstop again.
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.advanceTimersByTimeAsync(31_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(4));
    expect(mockGet.mock.calls[0][1]).toEqual({ params: { deep: true } });
    expect(mockGet.mock.calls[1][1]).toEqual({ params: {} });
    expect(mockGet.mock.calls[2][1]).toEqual({ params: {} });
    expect(mockGet.mock.calls[3][1]).toEqual({ params: { deep: true } });
  });

  it("stays in checking state (no false down) on a cold-cache shallow poll, then resolves on re-check (PRR-002)", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    // Cold cache: shallow response carries NO embeddings/chat keys (unknown).
    mockGet.mockResolvedValueOnce({
      data: { status: "ok", services: { backend: true } },
    } as never);
    mockGet.mockResolvedValue({
      data: {
        status: "ok",
        services: { backend: true, embeddings: true, chat: true },
      },
    } as never);
    const { result } = renderHook(() => useHealthCheck());

    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    // Unknown services must NOT be published as down: stay in checking state.
    expect(result.current.loading).toBe(true);

    // The hook re-polls ~2s later (bounded) and resolves with real booleans.
    await vi.advanceTimersByTimeAsync(2_100);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.embeddings).toBe(true);
    expect(result.current.chat).toBe(true);
    // The re-check (unauthenticated) is also shallow.
    expect(mockGet.mock.calls[1][1]).toEqual({ params: {} });
  });

  it("falls back to loading=false after the bounded re-check budget is exhausted with services still unknown (PRR-002 exhaustion)", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    // Every poll returns unknown services (backend up, embeddings/chat never
    // populated): initial + 5 re-checks, then the hook must publish loading
    // = false instead of spinning forever.
    mockGet.mockResolvedValue({
      data: { status: "ok", services: { backend: true } },
    } as never);
    const { result } = renderHook(() => useHealthCheck());

    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    expect(result.current.loading).toBe(true);

    // Re-checks at ~2s intervals: attempts 1..5.
    await vi.advanceTimersByTimeAsync(10_500);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(6));
    // Budget exhausted: loading resolves (no infinite spinner).
    await vi.waitFor(() => expect(result.current.loading).toBe(false));
    // Bounded: no further polls after the 6th (initial + 5 re-checks).
    await vi.advanceTimersByTimeAsync(10_000);
    expect(mockGet).toHaveBeenCalledTimes(6);
    // Unknown services retained as the initial false (truthful fallback:
    // amber banner may show, since the server genuinely never probed).
    expect(result.current.backend).toBe(true);
    expect(result.current.embeddings).toBe(false);
  });

  it("retains indicators when lightweight polls return null services; explicit false still flips down", async () => {
    // Deep probing requires credentials since issue #551; this scenario's
    // mount check is the authenticated deep check.
    useAuthStore.setState({ isAuthenticated: true });
    mockGet.mockResolvedValue(healthyDeepResponse as never);
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    // Mount check is deep (first check): sent as /health with params deep=true.
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(result.current.backend).toBe(true));
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet.mock.calls[0][0]).toBe("/health");
    expect(mockGet.mock.calls[0][1]).toEqual({ params: { deep: true } });
    expect(result.current.embeddings).toBe(true);
    expect(result.current.chat).toBe(true);
    expect(result.current.loading).toBe(false);

    // Lightweight poll returns 200 with null services: indicators must NOT
    // flip down; the request itself must be lightweight (params: {}).
    mockGet.mockResolvedValueOnce(nullServicesResponse as never);
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(mockGet.mock.calls[1][1]).toEqual({ params: {} });
    expect(result.current.backend).toBe(true);
    expect(result.current.embeddings).toBe(true); // retained, not coerced to false
    expect(result.current.chat).toBe(true); // retained, not coerced to false
    expect(result.current.loading).toBe(false);

    // A second consecutive null lightweight poll STILL retains: null is
    // "not checked", not a failure — no 2-failure threshold applies to it.
    mockGet.mockResolvedValueOnce(nullServicesResponse as never);
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(3));
    expect(result.current.embeddings).toBe(true);
    expect(result.current.chat).toBe(true);
    expect(result.current.backend).toBe(true);

    // Contrast: an explicit false from the backend is authoritative and flips
    // the indicators down immediately (pins null != false).
    mockGet.mockResolvedValueOnce(explicitFalseResponse as never);
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(result.current.embeddings).toBe(false));
    expect(result.current.chat).toBe(false);
    expect(result.current.backend).toBe(true);

    console.log(
      "PRESERVING GREEN: AC6/OPS-008 useHealthCheck last-known retention — " +
        "healthy deep check (params deep=true) then lightweight polls " +
        "(params {}) returning null services retain embeddings/chat " +
        "indicators across consecutive polls; explicit false flips them down",
    );
  });
});

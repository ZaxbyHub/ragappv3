import { renderHook } from "@testing-library/react";
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

const okResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: true, chat: true },
  },
};

describe("useHealthCheck — consecutive-failure contract (F-003)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    mockGet.mockReset();
    vi.clearAllMocks();
  });

  it("retains last-known on one failure after a success; flips down on two consecutive", async () => {
    mockGet.mockResolvedValue(okResponse as never);
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));
    // mount check (first = deep) — settle the success state fully
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(result.current.backend).toBe(true));
    expect(result.current.loading).toBe(false);

    // First failure after a success: retains last-known (streak 1 < 2)
    mockGet.mockRejectedValueOnce(new Error("net down"));
    await vi.advanceTimersByTimeAsync(5_000);
    // settle the rejection (state unchanged by design)
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(result.current.backend).toBe(true);

    // Second consecutive failure: flips down, loading cleared
    mockGet.mockRejectedValueOnce(new Error("net down"));
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(result.current.backend).toBe(false));
    expect(result.current.loading).toBe(false);
  });

  it("resets the failure streak after an intervening success", async () => {
    mockGet.mockResolvedValue(okResponse as never);
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(result.current.backend).toBe(true));

    mockGet.mockRejectedValueOnce(new Error("blip"));
    await vi.advanceTimersByTimeAsync(5_000);
    mockGet.mockResolvedValueOnce(okResponse as never);
    await vi.advanceTimersByTimeAsync(5_000);
    mockGet.mockRejectedValueOnce(new Error("blip2"));
    await vi.advanceTimersByTimeAsync(5_000);
    // streak reset by the success; single new failure retains last-known
    expect(result.current.backend).toBe(true);
  });

  it("marks down immediately when the backend is down from cold start", async () => {
    vi.useRealTimers();
    mockGet.mockReset();
    mockGet.mockRejectedValue(new Error("cold down"));
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));
    // hadSuccess=false -> no threshold grace on the very first check.
    // Wait on loading-clearing: it is only produced by the catch path (the
    // INITIAL state already has backend=false, so waiting on backend would
    // pass before the rejection settles).
    await vi.waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backend).toBe(false);
  });
});

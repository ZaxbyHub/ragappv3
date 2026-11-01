import { StrictMode, type ReactNode } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
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

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

type PendingRequest = {
  reject: (reason?: unknown) => void;
};

const deferred = <T,>(pending: Set<PendingRequest>): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const entry = {
    promise: new Promise<T>((promiseResolve, promiseReject) => {
      resolve = promiseResolve;
      reject = promiseReject;
    }),
    resolve: (value: T) => resolve(value),
    reject: (reason?: unknown) => reject(reason),
  };
  void entry.promise.catch(() => undefined);
  pending.add(entry);
  return entry;
};

const unknownResponse = {
  data: { status: "ok", services: { backend: true } },
};

const healthyResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: true, chat: true },
  },
};

describe("useHealthCheck issue #774 lifecycle ownership", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;
  let pending: Set<PendingRequest>;

  beforeEach(() => {
    vi.useFakeTimers();
    priorAuthState = useAuthStore.getState();
    pending = new Set();
  });

  afterEach(async () => {
    cleanup();
    await act(async () => {
      for (const request of pending) request.reject(new Error("test cleanup"));
      await Promise.resolve();
    });
    pending.clear();
    vi.clearAllTimers();
    useAuthStore.setState(priorAuthState);
    mockGet.mockReset();
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  it("does not schedule an unknown-service recheck after unmount", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    const first = deferred<typeof unknownResponse>(pending);
    mockGet.mockReturnValueOnce(first.promise);

    const { unmount } = renderHook(() => useHealthCheck());
    expect(mockGet).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => {
      first.resolve(unknownResponse);
      await first.promise;
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("ignores a late rejection from an obsolete auth generation", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    const stale = deferred<typeof healthyResponse>(pending);
    const fresh = deferred<typeof healthyResponse>(pending);
    mockGet.mockReturnValueOnce(stale.promise).mockReturnValueOnce(fresh.promise);

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));
    expect(mockGet).toHaveBeenCalledTimes(1);

    await act(async () => {
      useAuthStore.setState({ isAuthenticated: true });
    });
    expect(mockGet).toHaveBeenCalledTimes(2);

    await act(async () => {
      fresh.resolve(healthyResponse);
      await fresh.promise;
    });
    expect(result.current.backend).toBe(true);

    await act(async () => {
      stale.reject(new Error("obsolete request"));
      await stale.promise.catch(() => undefined);
    });
    mockGet.mockRejectedValueOnce(new Error("current transient failure"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(mockGet).toHaveBeenCalledTimes(3);

    // The stale rejection must not consume the first failure slot belonging to
    // the live generation; one current failure therefore retains success.
    expect(result.current.backend).toBe(true);
  });

  it("keeps only the live generation's bounded unknown-service timer", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    const stale = deferred<typeof unknownResponse>(pending);
    const live = deferred<typeof unknownResponse>(pending);
    const current = deferred<typeof healthyResponse>(pending);
    mockGet
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(live.promise)
      .mockReturnValueOnce(current.promise);

    renderHook(() => useHealthCheck());
    expect(mockGet).toHaveBeenCalledTimes(1);

    await act(async () => {
      useAuthStore.setState({ isAuthenticated: true });
    });
    expect(mockGet).toHaveBeenCalledTimes(2);

    await act(async () => {
      live.resolve(unknownResponse);
      await live.promise;
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(mockGet).toHaveBeenCalledTimes(3);

    // The old request resolves while the live recheck is still in flight. It
    // must not enqueue a second generation-owned timer.
    await act(async () => {
      stale.resolve(unknownResponse);
      await stale.promise;
    });
    await act(async () => {
      current.resolve(healthyResponse);
      await current.promise;
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(mockGet).toHaveBeenCalledTimes(3);
  });

  it("keeps the live poll bounded through StrictMode cleanup", async () => {
    useAuthStore.setState({ isAuthenticated: false });
    mockGet.mockResolvedValue(healthyResponse);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <StrictMode>{children}</StrictMode>
    );
    const { result, unmount } = renderHook(
      () => useHealthCheck({ pollInterval: 5_000 }),
      { wrapper },
    );

    await act(async () => { await Promise.resolve(); });
    expect(result.current.backend).toBe(true);
    const callsBeforeUnmount = mockGet.mock.calls.length;
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(mockGet).toHaveBeenCalledTimes(callsBeforeUnmount);
  });
});

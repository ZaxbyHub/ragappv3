import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/useAuthStore";
import {
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";

const mockGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, default: { get: mockGet } };
});

import { useHealthCheck } from "@/hooks/useHealthCheck";

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; settled: boolean };
const liveDeferreds = new Set<Deferred<unknown>>();
function deferred<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void;
  const state = { promise: undefined as unknown as Promise<T>, resolve: undefined as unknown as (value: T) => void, settled: false };
  state.promise = new Promise<T>((resolve) => {
    resolvePromise = (value) => {
      state.settled = true;
      resolve(value);
    };
  });
  state.resolve = resolvePromise;
  liveDeferreds.add(state as Deferred<unknown>);
  return state;
}

const healthy = {
  data: { status: "ok", services: { backend: true, embeddings: true, chat: true } },
};
const unknown = {
  data: { status: "ok", services: { backend: true, embeddings: null, chat: null } },
};
const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

describe("useHealthCheck issue 775 supplemental ownership", () => {
  let authSnapshot: ReturnType<typeof useAuthStore.getState>;
  let principalSnapshot: typeof authSnapshot.user;

  beforeEach(() => {
    authSnapshot = useAuthStore.getState();
    principalSnapshot = authSnapshot.user;
    vi.useFakeTimers();
    mockGet.mockReset();
    publishAuthPrincipal({ id: 101, role: "member" });
    act(() => {
      useAuthStore.setState({ ...authSnapshot, isAuthenticated: true });
    });
  });

  afterEach(async () => {
    cleanup();
    for (const pending of liveDeferreds) {
      if (!pending.settled) pending.resolve(undefined as never);
    }
    await act(async () => {
      await flush();
    });
    liveDeferreds.clear();
    act(() => {
      useAuthStore.setState(authSnapshot);
      publishAuthPrincipal(
        principalSnapshot
          ? { id: principalSnapshot.id, role: principalSnapshot.role }
          : null,
      );
    });
    reserveReplacementAuthOwner();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("publishes B and ignores A after an owner/principal replacement", async () => {
    const readA = deferred<typeof healthy>();
    const readB = deferred<typeof healthy>();
    mockGet.mockReturnValueOnce(readA.promise).mockReturnValueOnce(readB.promise);
    const view = renderHook((props: { interval: number }) => useHealthCheck({ pollInterval: props.interval }), {
      initialProps: { interval: 5_000 },
    });
    await act(async () => {
      reserveReplacementAuthOwner();
      publishAuthPrincipal({ id: 202, role: "member" });
      view.rerender({ interval: 5_000 });
      await flush();
    });
    expect(mockGet).toHaveBeenCalledTimes(2);
    await act(async () => {
      readA.resolve({ data: { status: "down", services: { backend: false, embeddings: false, chat: false } } });
      readB.resolve(healthy);
      await flush();
    });
    expect(view.result.current.backend).toBe(true);
    expect(view.result.current.embeddings).toBe(true);
  });

  it("lets the newest overlapping interval read own bookkeeping and state", async () => {
    const readA = deferred<typeof healthy>();
    const readB = deferred<typeof healthy>();
    mockGet.mockReturnValueOnce(readA.promise).mockReturnValueOnce(readB.promise);
    const view = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
      await flush();
    });
    expect(mockGet).toHaveBeenCalledTimes(2);
    await act(async () => {
      readA.resolve({
        data: { status: "down", services: { backend: false, embeddings: false, chat: false } },
      });
      await flush();
    });
    expect(view.result.current.loading).toBe(true);
    await act(async () => {
      readB.resolve(healthy);
      await flush();
    });
    expect(view.result.current.backend).toBe(true);
  });

  it("keeps anonymous checks shallow and forces a deep check for a replacement lifetime", async () => {
    act(() => useAuthStore.setState({ ...useAuthStore.getState(), isAuthenticated: false }));
    mockGet.mockResolvedValue(healthy);
    const view = renderHook(() => useHealthCheck());
    await act(async () => {
      await flush();
    });
    expect(mockGet.mock.calls[0]?.[1]).toEqual({ params: {} });

    act(() => useAuthStore.setState({ ...useAuthStore.getState(), isAuthenticated: true }));
    reserveReplacementAuthOwner();
    publishAuthPrincipal({ id: 303, role: "member" });
    view.rerender();
    await act(async () => {
      await flush();
    });
    expect(mockGet.mock.calls.at(-1)?.[1]).toEqual({ params: { deep: true } });
    expect(view.result.current.loading).toBe(false);
  });

  it("does not let A's bounded unknown recheck invoke B", async () => {
    const firstA = deferred<typeof unknown>();
    mockGet.mockReturnValueOnce(firstA.promise).mockResolvedValue(healthy);
    const view = renderHook(() => useHealthCheck());
    await act(async () => {
      firstA.resolve(unknown);
      await flush();
      reserveReplacementAuthOwner();
      publishAuthPrincipal({ id: 404, role: "member" });
      view.rerender();
      await flush();
    });
    const callsAfterB = mockGet.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      await flush();
    });
    expect(mockGet.mock.calls.length).toBe(callsAfterB);
    expect(view.result.current.backend).toBe(true);
  });

  it("records deep cadence at dispatch so a held request is shallow at the next cadence tick", async () => {
    const held = deferred<typeof healthy>();
    mockGet.mockReturnValue(held.promise);
    renderHook(() => useHealthCheck({ pollInterval: 5_000 }));
    await act(async () => {
      await flush();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(95_000);
      await flush();
    });

    const params = mockGet.mock.calls.map(([, config]) => config);
    expect(params[0]).toEqual({ params: { deep: true } });
    expect(params.at(-2)).toEqual({ params: { deep: true } });
    expect(params.at(-1)).toEqual({ params: {} });
  });

  it("handles malformed health data and recovers on the replacement lifetime", async () => {
    mockGet.mockResolvedValueOnce({ data: null }).mockResolvedValueOnce(healthy);
    const view = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));
    await act(async () => {
      await flush();
    });
    expect(view.result.current.loading).toBe(false);

    act(() => {
      reserveReplacementAuthOwner();
      publishAuthPrincipal({ id: 505, role: "member" });
      view.rerender();
    });
    await act(async () => {
      await flush();
    });
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(view.result.current.backend).toBe(true);
    expect(view.result.current.loading).toBe(false);
  });
});

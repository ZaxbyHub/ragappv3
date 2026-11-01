import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/useAuthStore";
import { publishAuthPrincipal, reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";

const mockGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, default: { get: mockGet } };
});

import { useHealthCheck } from "@/hooks/useHealthCheck";

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
  settled: boolean;
};
const liveDeferreds = new Set<Deferred<unknown>>();

function deferred<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (error: unknown) => void;
  const state = {
    promise: undefined as unknown as Promise<T>,
    resolve: undefined as unknown as (value: T) => void,
    reject: undefined as unknown as (error: unknown) => void,
    settled: false,
  };
  state.promise = new Promise<T>((resolve, reject) => {
    resolvePromise = (value) => {
      state.settled = true;
      resolve(value);
    };
    rejectPromise = (error) => {
      state.settled = true;
      reject(error);
    };
  });
  state.resolve = resolvePromise;
  state.reject = rejectPromise;
  liveDeferreds.add(state as Deferred<unknown>);
  return state;
}

const healthy = {
  data: { status: "ok", services: { backend: true, embeddings: true, chat: true } },
};
const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

describe("useHealthCheck issue 863 overlapping outcomes", () => {
  let authSnapshot: ReturnType<typeof useAuthStore.getState>;
  let principalSnapshot: typeof authSnapshot.user;
  let requests: Deferred<typeof healthy>[];

  beforeEach(() => {
    authSnapshot = useAuthStore.getState();
    principalSnapshot = authSnapshot.user;
    vi.useFakeTimers();
    mockGet.mockReset();
    requests = [];
    mockGet.mockImplementation(() => {
      const request = deferred<typeof healthy>();
      requests.push(request);
      return request.promise;
    });
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

  const poll = async (): Promise<void> => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
      await flush();
    });
  };

  it("publishes down after consecutive slow failures are superseded by interval reads", async () => {
    const view = renderHook(() => useHealthCheck({ pollInterval: 100 }));
    await act(async () => {
      await flush();
    });
    expect(requests).toHaveLength(1);

    await poll();
    expect(requests).toHaveLength(2);
    await act(async () => {
      requests[0].reject(new Error("slow request timed out"));
      await flush();
    });
    expect(view.result.current.backend).toBe(false);

    await poll();
    expect(requests).toHaveLength(3);
    await act(async () => {
      requests[1].reject(new Error("slow request timed out"));
      await flush();
    });
    expect(view.result.current.backend).toBe(false);
    expect(view.result.current.loading).toBe(false);
  });

  it("ignores failures older than a newer success before counting later failures", async () => {
    const view = renderHook(() => useHealthCheck({ pollInterval: 100 }));
    await act(async () => {
      await flush();
    });
    await poll();
    await poll();
    expect(requests).toHaveLength(3);

    await act(async () => {
      requests[2].resolve(healthy);
      await flush();
    });
    expect(view.result.current.backend).toBe(true);

    await act(async () => {
      requests[0].reject(new Error("late old failure"));
      requests[1].reject(new Error("late old failure"));
      await flush();
    });
    expect(view.result.current.backend).toBe(true);

    await poll();
    await act(async () => {
      requests[3].reject(new Error("first current failure"));
      await flush();
    });
    expect(view.result.current.backend).toBe(true);

    await poll();
    await act(async () => {
      requests[4].reject(new Error("second current failure"));
      await flush();
    });
    expect(view.result.current.backend).toBe(false);
  });
});
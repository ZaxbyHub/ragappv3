import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthStore } from "@/stores/useAuthStore";

let publicAuthSnapshot: ReturnType<typeof useAuthStore.getState>;
const publicUserA = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "member" as const,
  is_active: true,
};
const publicUserSameAccount = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "member" as const,
  is_active: true,
};

function setPublicAuthUser(user: typeof publicUserA, token: string) {
  act(() => {
    useAuthStore.setState({
      user,
      accessToken: token,
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
    });
  });
}

function restorePublicAuthState() {
  act(() => useAuthStore.setState(publicAuthSnapshot, true));
}
import { draftRoomKeys, type DraftDetail } from "@/lib/api/draftRoom";

const apiMocks = vi.hoisted(() => ({
  getJwtAccessToken: vi.fn(() => "A-token"),
  refreshAccessToken: vi.fn(async () => "B-token" as string | null),
}));

// These are the existing API seams. The hooks and stream readers remain the
// production modules under test; no parser or ownership implementation is
// recreated by this mock.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    API_BASE_URL: "/api",
    getJwtAccessToken: apiMocks.getJwtAccessToken,
    refreshAccessToken: apiMocks.refreshAccessToken,
  };
});

type ReadResult = ReadableStreamReadResult<Uint8Array>;
type CancelMode = "resolve" | "hang" | "reject";

function deferred<T>() {
  let settled = false;
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolveInner, rejectInner) => {
    resolvePromise = resolveInner;
    rejectPromise = rejectInner;
  });
  // Attach a rejection observer immediately so cleanup cannot create an
  // unhandled rejection when a production await is disposed mid-flight.
  promise.catch(() => undefined);
  const resolve = (value: T) => {
    if (settled) return;
    settled = true;
    resolvePromise(value);
  };
  const reject = (reason?: unknown) => {
    if (settled) return;
    settled = true;
    rejectPromise(reason);
  };
  const cleanup = () => resolve(undefined as T);
  return { promise, resolve, reject, cleanup };
}

function controlledSse(cancelMode: CancelMode = "resolve") {
  const encoder = new TextEncoder();
  const queue: ReadResult[] = [];
  let pendingResolve: ((result: ReadResult) => void) | null = null;

  const reader = {
    read: vi.fn(
      () =>
        new Promise<ReadResult>((resolve) => {
          const next = queue.shift();
          if (next) {
            resolve(next);
            return;
          }
          pendingResolve = resolve;
        }),
    ),
    cancel: vi.fn(() => {
      if (cancelMode === "hang") return new Promise<void>(() => undefined);
      if (cancelMode === "reject") return Promise.reject(new Error("cancel failed"));
      return Promise.resolve();
    }),
  } as unknown as ReadableStreamDefaultReader<Uint8Array> & {
    read: ReturnType<typeof vi.fn>;
    cancel: ReturnType<typeof vi.fn>;
  };

  const settlePending = (result: ReadResult) => {
    const resolve = pendingResolve;
    pendingResolve = null;
    resolve?.(result);
  };

  const emit = (text: string) => {
    const result: ReadResult = { value: encoder.encode(text), done: false };
    if (pendingResolve) settlePending(result);
    else queue.push(result);
  };

  const finish = () => {
    const result: ReadResult = { value: undefined, done: true };
    if (pendingResolve) settlePending(result);
    else queue.push(result);
  };

  const response = {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  } as unknown as Response;
  return { response, reader, emit, finish };
}

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ detail }),
  } as unknown as Response;
}

function createQueryWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("#774 Wiki stream ownership controls", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUserA, "A-token");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    apiMocks.getJwtAccessToken.mockReset();
    apiMocks.getJwtAccessToken.mockReturnValue("A-token");
    apiMocks.refreshAccessToken.mockReset();
    apiMocks.refreshAccessToken.mockResolvedValue("B-token");
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    restorePublicAuthState();
  });

  it("stops callbacks and reconnects when owner replacement interrupts a held read", async () => {
    vi.useFakeTimers();
    const stream = controlledSse("hang");
    fetchMock.mockResolvedValue(stream.response);
    const onTerminal = vi.fn();
    const { useWikiEventStream } = await import("./useWikiEventStream");
    const { unmount } = renderHook(() => useWikiEventStream(42, onTerminal));

    try {
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      setPublicAuthUser(publicUserSameAccount, "B-token");
      await vi.waitFor(() => expect(stream.reader.cancel).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(30_000);
      stream.emit('data: {"type":"job_completed"}\n\n');
      expect(onTerminal).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      stream.finish();
      unmount();
    }
  });

  it("does not refresh after disposal while a Wiki 401 body is pending", async () => {
    const body = deferred<{ detail: string }>();
    fetchMock.mockResolvedValue({
      ...errorResponse(401, "token_expired"),
      json: () => body.promise,
    } as unknown as Response);
    const { useWikiEventStream } = await import("./useWikiEventStream");
    const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));

    try {
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      unmount();
      body.resolve({ detail: "token_expired" });
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
      expect(apiMocks.refreshAccessToken).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      unmount();
      body.cleanup();
    }
  });

  it.each(["token_invalid", "user_inactive"] as const)(
    "keeps the fatal %s policy without refresh or reconnect",
    async (detail) => {
      vi.useFakeTimers();
      fetchMock.mockResolvedValue(errorResponse(401, detail));
      const { useWikiEventStream } = await import("./useWikiEventStream");
      const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));

      try {
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
        await vi.advanceTimersByTimeAsync(30_000);
        expect(apiMocks.refreshAccessToken).not.toHaveBeenCalled();
        expect(fetchMock).toHaveBeenCalledTimes(1);
      } finally {
        unmount();
      }
    },
  );
});

describe("#774 Draft Room stream ownership controls", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let queryClient: QueryClient;

  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUserA, "A-token");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    apiMocks.getJwtAccessToken.mockReset();
    apiMocks.getJwtAccessToken.mockReturnValue("A-token");
    apiMocks.refreshAccessToken.mockReset();
    apiMocks.refreshAccessToken.mockResolvedValue("B-token");
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    queryClient.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    restorePublicAuthState();
  });

  it("clears private state on owner replacement and never reconnects with B", async () => {
    vi.useFakeTimers();
    const stream = controlledSse("hang");
    fetchMock.mockResolvedValue(stream.response);
    apiMocks.getJwtAccessToken.mockReturnValueOnce("A-token").mockReturnValue("B-token");
    const { useDraftRoomEvents } = await import("./useDraftRoomEvents");
    const { result, unmount } = renderHook(() => useDraftRoomEvents(42), {
      wrapper: createQueryWrapper(queryClient),
    });

    try {
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      stream.emit('data: {"type":"job_started","job_id":774}\n\n');
      await vi.waitFor(() => expect(result.current.lastEvent?.type).toBe("job_started"));
      setPublicAuthUser(publicUserSameAccount, "B-token");
      await vi.waitFor(() => {
        expect(result.current.lastEvent).toBeNull();
        expect(result.current.connected).toBe(false);
        expect(result.current.pollingFallback).toBe(false);
      });
      await vi.advanceTimersByTimeAsync(30_000);
      expect(stream.reader.cancel).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(apiMocks.getJwtAccessToken).toHaveBeenCalledTimes(1);
      expect(apiMocks.getJwtAccessToken.mock.results[0]?.value).toBe("A-token");
    } finally {
      stream.finish();
      unmount();
    }
  });

  it("resets Draft Room state when disabled or when draft id becomes null", async () => {
    const stream = controlledSse();
    fetchMock.mockResolvedValue(stream.response);
    const { useDraftRoomEvents } = await import("./useDraftRoomEvents");
    const { result, rerender, unmount } = renderHook(
      ({ id, enabled }: { id: number | null; enabled?: boolean }) => useDraftRoomEvents(id, { enabled }),
      {
        initialProps: { id: 42 } as { id: number | null; enabled?: boolean },
        wrapper: createQueryWrapper(queryClient),
      },
    );

    try {
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      stream.emit('data: {"type":"job_started","job_id":775}\n\n');
      await waitFor(() => expect(result.current.lastEvent?.job_id).toBe(775));
      rerender({ id: null });
      await waitFor(() => {
        expect(result.current.lastEvent).toBeNull();
        expect(result.current.connected).toBe(false);
        expect(result.current.pollingFallback).toBe(false);
      });
      rerender({ id: 42, enabled: false });
      await waitFor(() => expect(result.current.lastEvent).toBeNull());
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      stream.finish();
      unmount();
    }
  });

  it.each(["hang", "reject"] as const)(
    "cleans timers and catches %s reader cancellation on unmount",
    async (cancelMode) => {
      vi.useFakeTimers();
      const stream = controlledSse(cancelMode);
      fetchMock.mockResolvedValue(stream.response);
      const { useDraftRoomEvents } = await import("./useDraftRoomEvents");
      const unhandled: unknown[] = [];
      const onUnhandled = (reason: unknown) => unhandled.push(reason);
      process.on("unhandledRejection", onUnhandled);
      const { unmount } = renderHook(() => useDraftRoomEvents(42), {
        wrapper: createQueryWrapper(queryClient),
      });

      try {
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
        unmount();
        await vi.waitFor(() => expect(stream.reader.cancel).toHaveBeenCalledTimes(1));
        expect(vi.getTimerCount()).toBe(0);
        await vi.advanceTimersByTimeAsync(1);
      } finally {
        stream.finish();
        process.off("unhandledRejection", onUnhandled);
        unmount();
      }
      expect(unhandled).toEqual([]);
    },
  );

  it("does not reconnect after Draft Room disposal during backoff", async () => {
    vi.useFakeTimers();
    fetchMock.mockRejectedValue(new Error("network down"));
    const { useDraftRoomEvents } = await import("./useDraftRoomEvents");
    const { unmount } = renderHook(() => useDraftRoomEvents(42), {
      wrapper: createQueryWrapper(queryClient),
    });

    try {
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      unmount();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      unmount();
    }
  });

  it("preserves fatal 403 handling without entering the polling reconnect loop", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue({ ok: false, status: 403, json: async () => ({ detail: "vault_access_revoked" }) });
    const { useDraftRoomEvents } = await import("./useDraftRoomEvents");
    const { result, unmount } = renderHook(() => useDraftRoomEvents(42), {
      wrapper: createQueryWrapper(queryClient),
    });

    try {
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(30_000);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result.current.pollingFallback).toBe(false);
    } finally {
      unmount();
    }
  });

  it("keeps the polling helper alive while parse work is active", async () => {
    vi.useFakeTimers();
    queryClient.setQueryData(
      draftRoomKeys.detail(42),
      { active_compile_job: null, inputs: [{ active_parse_job_id: 19 }] } as unknown as DraftDetail,
    );
    const pendingResponse = deferred<Response>();
    fetchMock
      .mockResolvedValueOnce({ ok: false, status: 500 })
      .mockResolvedValueOnce({ ok: false, status: 500 })
      .mockResolvedValueOnce({ ok: false, status: 500 })
      .mockResolvedValue(pendingResponse.promise);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { useDraftRoomEvents } = await import("./useDraftRoomEvents");
    const { result, unmount } = renderHook(() => useDraftRoomEvents(42), {
      wrapper: createQueryWrapper(queryClient),
    });

    try {
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(1_100);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      await vi.advanceTimersByTimeAsync(2_100);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      await vi.waitFor(() => expect(result.current.pollingFallback).toBe(true));
      await vi.advanceTimersByTimeAsync(2_100);
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(42) });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.jobs(42) });
    } finally {
      unmount();
      pendingResponse.cleanup();
    }
  });
});

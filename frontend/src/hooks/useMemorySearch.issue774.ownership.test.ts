import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryResult } from "@/lib/api";
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
import { useMemorySearch } from "@/hooks/useMemorySearch";

const fixture = vi.hoisted(() => ({
  listMemories: vi.fn(),
  searchMemories: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listMemories: fixture.listMemories,
    searchMemories: fixture.searchMemories,
  };
});
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: vi.fn(() => false) }));
vi.mock("sonner", () => ({ toast: { error: fixture.toastError, success: vi.fn() } }));

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
  settled: boolean;
};

const liveDeferreds = new Set<Deferred<unknown>>();

function deferred<T>(): Deferred<T> {
  let resolvePromise!: Deferred<T>["resolve"];
  let rejectPromise!: Deferred<T>["reject"];
  const state: Deferred<T> = {
    promise: undefined as unknown as Promise<T>,
    resolve: undefined as unknown as Deferred<T>["resolve"],
    reject: undefined as unknown as Deferred<T>["reject"],
    settled: false,
  };
  state.promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  state.promise.catch(() => undefined);
  state.resolve = (value) => { state.settled = true; resolvePromise(value); };
  state.reject = (reason) => { state.settled = true; rejectPromise(reason); };
  liveDeferreds.add(state as Deferred<unknown>);
  return state;
}

const flush = async () => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

function memory(id: number, content: string): MemoryResult {
  return { id, content, metadata: { category: "fact", source: "fixture" } } as MemoryResult;
}

describe("useMemorySearch ownership controls", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUserA, "A-token");
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(async () => {
    cleanup();
    for (const pending of liveDeferreds) {
      if (!pending.settled) pending.resolve(undefined);
    }
    liveDeferreds.clear();
    await act(async () => { await flush(); });
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    restorePublicAuthState();
  });

  it("rejects retained A query, search, and retry callbacks while B publishes", async () => {
    const oldRead = deferred<{ memories: MemoryResult[] }>();
    const currentRead = deferred<{ memories: MemoryResult[] }>();
    fixture.listMemories.mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(currentRead.promise);

    const view = renderHook((props: { vault: number }) => useMemorySearch(props.vault), {
      initialProps: { vault: 101 },
    });
    const oldSetSearchQuery = view.result.current.setSearchQuery;
    const oldHandleSearch = view.result.current.handleSearch;
    const oldRetry = (view.result.current as typeof view.result.current & { retry?: () => Promise<void> }).retry;
    expect(oldRetry).toBeTypeOf("function");
    await act(async () => oldRead.resolve({ memories: [memory(101, "private A")] }));
    await act(async () => { await flush(); });

    setPublicAuthUser(publicUserSameAccount, "B-token");
    view.rerender({ vault: 202 });
    await act(async () => { await flush(); });
    expect(fixture.listMemories).toHaveBeenCalledTimes(2);

    await act(async () => {
      oldSetSearchQuery("private A query");
      await oldHandleSearch();
      await oldRetry!();
      await flush();
    });
    expect(fixture.listMemories).toHaveBeenCalledTimes(2);
    expect(fixture.searchMemories).not.toHaveBeenCalled();
    expect(view.result.current.searchQuery).toBe("");

    await act(async () => currentRead.resolve({ memories: [memory(202, "current B")] }));
    await act(async () => { await flush(); });
    expect(view.result.current.memories.map((row) => row.id)).toEqual([202]);
    expect(view.result.current.searchQuery).toBe("");
  });
});

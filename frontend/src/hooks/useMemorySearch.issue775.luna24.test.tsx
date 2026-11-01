import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryResult } from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import {
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";

const fixture = vi.hoisted(() => ({
  listMemories: vi.fn(),
  searchMemories: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, listMemories: fixture.listMemories, searchMemories: fixture.searchMemories };
});
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: vi.fn(() => false) }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import { useMemorySearch } from "@/hooks/useMemorySearch";

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  settled: boolean;
};

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

const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

function memory(id: number, content: string): MemoryResult {
  return { id, content, metadata: { category: "fact", source: "fixture" } } as MemoryResult;
}

describe("useMemorySearch issue 775 supplemental ownership", () => {
  let authSnapshot: ReturnType<typeof useAuthStore.getState>;
  let principalSnapshot: typeof authSnapshot.user;

  beforeEach(() => {
    authSnapshot = useAuthStore.getState();
    principalSnapshot = authSnapshot.user;
    vi.useFakeTimers();
    fixture.listMemories.mockReset();
    fixture.searchMemories.mockReset();
    publishAuthPrincipal({ id: 101, role: "member" });
  });

  afterEach(async () => {
    cleanup();
    for (const pending of liveDeferreds) {
      if (!pending.settled) pending.resolve(undefined as never);
    }
    liveDeferreds.clear();
    await act(async () => {
      await flush();
    });
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

  it("retires a retained A handler and publishes the held B read", async () => {
    const readA = deferred<{ memories: MemoryResult[] }>();
    const readB = deferred<{ memories: MemoryResult[] }>();
    fixture.listMemories.mockReturnValueOnce(readA.promise).mockReturnValueOnce(readB.promise);

    const view = renderHook((props: { vault: number }) => useMemorySearch(props.vault), {
      initialProps: { vault: 1 },
    });
    await act(async () => { await flush(); });
    expect(fixture.listMemories).toHaveBeenCalledTimes(1);
    const retainedA = view.result.current;
    await act(async () => {
      reserveReplacementAuthOwner();
      publishAuthPrincipal({ id: 202, role: "member" });
      view.rerender({ vault: 2 });
      await flush();
    });
    expect(view.result.current.memories).toEqual([]);
    expect(view.result.current.loading).toBe(true);

    await act(async () => {
      retainedA.setSearchQuery("private A");
      await retainedA.handleSearch();
      await flush();
    });
    expect(fixture.listMemories).toHaveBeenCalledTimes(2);
    expect(view.result.current.searchQuery).toBe("");

    await act(async () => {
      readA.resolve({ memories: [memory(1, "A")] });
      readB.resolve({ memories: [memory(2, "B")] });
      await flush();
    });
    expect(view.result.current.memories.map((row) => row.id)).toEqual([2]);
  });

  it("lets the newest same-context read win when the older promise ignores abort", async () => {
    const oldRead = deferred<{ memories: MemoryResult[] }>();
    const newRead = deferred<{ memories: MemoryResult[] }>();
    fixture.listMemories.mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(newRead.promise);
    const view = renderHook(() => useMemorySearch(3));
    await act(async () => {
      await flush();
      void view.result.current.handleSearch();
      await flush();
    });

    await act(async () => {
      oldRead.resolve({ memories: [memory(3, "old")] });
      await flush();
    });
    expect(view.result.current.memories).toEqual([]);
    await act(async () => {
      newRead.resolve({ memories: [memory(4, "new")] });
      await flush();
    });
    expect(view.result.current.memories.map((row) => row.id)).toEqual([4]);
  });

  it("reissues the restored query after an A-to-B-to-A raw cycle without publishing the retired read", async () => {
    const initialRead = deferred<{ memories: MemoryResult[] }>();
    const currentRead = deferred<{ memories: MemoryResult[] }>();
    const manualRead = deferred<{ memories: MemoryResult[] }>();
    fixture.listMemories
      .mockReturnValueOnce(initialRead.promise)
      .mockReturnValueOnce(currentRead.promise)
      .mockReturnValueOnce(manualRead.promise);
    const view = renderHook(() => useMemorySearch(11));

    await act(async () => {
      view.result.current.setSearchQuery("B");
      await flush();
    });
    await act(async () => {
      view.result.current.setSearchQuery("");
      await flush();
    });
    expect(fixture.listMemories).toHaveBeenCalledTimes(2);

    await act(async () => {
      initialRead.resolve({ memories: [memory(11, "stale A")] });
      await flush();
    });
    expect(view.result.current.memories).toEqual([]);
    expect(view.result.current.loading).toBe(true);

    await act(async () => {
      void view.result.current.handleSearch();
      await flush();
    });
    expect(fixture.listMemories).toHaveBeenCalledTimes(3);
    expect(fixture.listMemories).toHaveBeenLastCalledWith(11);

    await act(async () => {
      currentRead.resolve({ memories: [memory(12, "retired current A")] });
      await flush();
    });
    expect(view.result.current.memories).toEqual([]);
    expect(view.result.current.loading).toBe(true);
    await act(async () => {
      manualRead.resolve({ memories: [memory(13, "manual current A")] });
      await flush();
    });
    expect(view.result.current.memories.map((row) => row.id)).toEqual([13]);
  });

  it("does not publish a held response after unmount", async () => {
    const pending = deferred<{ memories: MemoryResult[] }>();
    fixture.listMemories.mockReturnValueOnce(pending.promise);
    const view = renderHook(() => useMemorySearch(9));
    await act(async () => { await flush(); });
    expect(fixture.listMemories).toHaveBeenCalledTimes(1);
    view.unmount();
    await act(async () => {
      pending.resolve({ memories: [memory(9, "retired")] });
      await flush();
    });
    expect(fixture.listMemories).toHaveBeenCalledTimes(1);
  });

  it("restarts the current query when raw input reverts before the debounce fires", async () => {
    fixture.listMemories.mockResolvedValue({ memories: [] });
    const firstSearch = deferred<{ results: MemoryResult[] }>();
    fixture.searchMemories
      .mockReturnValueOnce(firstSearch.promise)
      .mockResolvedValueOnce({ results: [memory(20, "latest needle result")] });
    const view = renderHook(() => useMemorySearch(19));

    await act(async () => {
      await flush();
    });
    expect(fixture.listMemories).toHaveBeenCalledOnce();

    await act(async () => {
      view.result.current.setSearchQuery("needle");
      await flush();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
      await flush();
    });
    expect(fixture.searchMemories).toHaveBeenCalledTimes(1);
    const firstSearchSignal = fixture.searchMemories.mock.calls[0][1] as AbortSignal;

    await act(async () => {
      view.result.current.setSearchQuery("different");
      await flush();
    });
    expect(fixture.searchMemories).toHaveBeenCalledTimes(1);
    expect(firstSearchSignal.aborted).toBe(true);

    await act(async () => {
      view.result.current.setSearchQuery("needle");
      await flush();
    });
    expect(fixture.searchMemories).toHaveBeenCalledTimes(2);
    expect(fixture.searchMemories).toHaveBeenLastCalledWith(
      { query: "needle", limit: 50 },
      expect.any(AbortSignal),
      19,
    );
    expect(view.result.current.memories.map((row) => row.id)).toEqual([20]);

    await act(async () => {
      firstSearch.resolve({ results: [memory(19, "stale needle result")] });
      await flush();
    });
    expect(view.result.current.memories.map((row) => row.id)).toEqual([20]);
  });

  it("keeps the mounted lease usable for manual search while raw input waits for debounce", async () => {
    fixture.listMemories.mockResolvedValue({ memories: [] });
    fixture.searchMemories.mockResolvedValue({ results: [] });
    const view = renderHook(() => useMemorySearch(20));
    await act(async () => {
      await flush();
    });

    await act(async () => {
      view.result.current.setSearchQuery("pending needle");
      await flush();
    });
    expect(fixture.searchMemories).not.toHaveBeenCalled();

    await act(async () => {
      await view.result.current.handleSearch();
      await flush();
    });
    expect(fixture.listMemories).toHaveBeenCalledTimes(2);
    expect(fixture.listMemories).toHaveBeenLastCalledWith(20);
    expect(fixture.searchMemories).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
      await flush();
    });
    expect(fixture.searchMemories).toHaveBeenCalledOnce();
    expect(fixture.searchMemories).toHaveBeenLastCalledWith(
      { query: "pending needle", limit: 50 },
      expect.any(AbortSignal),
      20,
    );
  });
});

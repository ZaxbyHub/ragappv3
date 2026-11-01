import * as React from "react";

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Document, DocumentStatsResponse } from "@/lib/api";
import { getJwtAccessToken, setJwtAccessToken } from "@/lib/api";
import {
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "@/stores/useAuthStore";

const fixture = vi.hoisted(() => ({
  listDocuments: vi.fn(),
  getDocumentStats: vi.fn(),
  getDocumentWikiStatus: vi.fn(),
  compileDocumentWiki: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listDocuments: fixture.listDocuments,
    getDocumentStats: fixture.getDocumentStats,
    getDocumentWikiStatus: fixture.getDocumentWikiStatus,
    compileDocumentWiki: fixture.compileDocumentWiki,
  };
});

vi.mock("sonner", () => ({
  toast: {
    error: fixture.toastError,
    success: fixture.toastSuccess,
  },
}));

import { useDocumentPolling } from "./useDocumentPolling";

type PollingOptions = Parameters<typeof useDocumentPolling>[0];

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
  settled: boolean;
};

const pending = new Set<Deferred<unknown>>();

function defer<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (reason?: unknown) => void;
  const deferred: Deferred<T> = {
    promise: new Promise<T>((resolve, reject) => {
      resolvePromise = resolve;
      rejectPromise = reject;
    }),
    resolve: (value) => {
      deferred.settled = true;
      resolvePromise(value);
    },
    reject: (reason) => {
      deferred.settled = true;
      rejectPromise(reason);
    },
    settled: false,
  };
  deferred.promise.catch(() => undefined);
  pending.add(deferred as Deferred<unknown>);
  return deferred;
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

const baseUser = {
  id: 101,
  username: "poll-owner-a",
  email: "poll-a@example.test",
  role: "member" as const,
  is_active: true,
};

const authSnapshot = useAuthStore.getState();
const jwtSnapshot = getJwtAccessToken();

function row(id: number): Document {
  return {
    id,
    filename: `document-${id}.pdf`,
    original_filename: `document-${id}.pdf`,
    file_size: 12,
    file_type: "application/pdf",
    upload_date: "2026-10-01T00:00:00Z",
    metadata: { status: "indexed" },
  } as Document;
}

function stats(total: number): DocumentStatsResponse {
  return { total_documents: total } as DocumentStatsResponse;
}

function options(
  activeVaultId: number,
  overrides: Partial<PollingOptions> = {},
): PollingOptions {
  return {
    activeVaultId,
    search: "",
    sortBy: "upload_date",
    sortOrder: "desc",
    tagFilterId: null,
    folderFilterId: null,
    uploads: [],
    ...overrides,
  };
}

function setUser(id: number, token: string): void {
  const user = { ...baseUser, id, username: `poll-owner-${id}` };
  act(() => {
    reserveReplacementAuthOwner();
    setJwtAccessToken(token);
    useAuthStore.setState({
      user,
      accessToken: token,
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
    });
    publishAuthPrincipal({ id, role: "member" });
  });
}

async function renderSettled(next: PollingOptions) {
  const view = renderHook((current) => useDocumentPolling(current), {
    initialProps: next,
  });
  await act(async () => {
    await flush();
  });
  return view;
}

describe("useDocumentPolling issue 775 supplemental ownership", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    reserveReplacementAuthOwner();
    setUser(baseUser.id, "poll-token-a");
    fixture.listDocuments.mockResolvedValue({ documents: [row(1)], total: 1 });
    fixture.getDocumentStats.mockResolvedValue(stats(1));
    fixture.getDocumentWikiStatus.mockResolvedValue({ wiki_status: "not_compiled" });
    fixture.compileDocumentWiki.mockResolvedValue({});
  });

  afterEach(async () => {
    cleanup();
    for (const operation of pending) {
      if (!operation.settled) operation.resolve(undefined);
    }
    pending.clear();
    await act(async () => {
      await flush();
    });
    vi.clearAllTimers();
    vi.useRealTimers();
    act(() => {
      reserveReplacementAuthOwner();
      useAuthStore.setState(authSnapshot, true);
      setJwtAccessToken(jwtSnapshot);
      const previousUser = authSnapshot.user as
        | { id: number; role: "admin" | "member" }
        | null;
      publishAuthPrincipal(
        previousUser === null || previousUser === undefined
          ? null
          : { id: previousUser.id, role: previousUser.role },
      );
    });
  });

  it("surfaces malformed list data and recovers through the real fetch callback", async () => {
    fixture.listDocuments
      .mockReset()
      .mockResolvedValueOnce({ total: 1 })
      .mockResolvedValueOnce({ documents: [], total: 0 });

    const view = await renderSettled(options(1));
    expect(view.result.current.listError).toBe(true);
    expect(view.result.current.documents).toEqual([]);

    await act(async () => {
      void view.result.current.fetchDocuments();
      await flush();
    });
    expect(view.result.current.listError).toBe(false);
    expect(view.result.current.documents).toEqual([]);
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);
  });

  it("retires a held A list and stats request before B can publish its result", async () => {
    const aList = defer<unknown>();
    const aStats = defer<unknown>();
    fixture.listDocuments.mockReset().mockReturnValueOnce(aList.promise).mockResolvedValue({
      documents: [row(2)],
      total: 1,
    });
    fixture.getDocumentStats.mockReset().mockReturnValueOnce(aStats.promise).mockResolvedValue(stats(2));

    const view = renderHook((current) => useDocumentPolling(current), {
      initialProps: options(1),
    });
    await act(async () => {
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(1);
    const oldFetch = view.result.current.fetchDocuments;
    const oldListCalls = fixture.listDocuments.mock.calls.length;

    setUser(202, "poll-token-b");
    view.rerender(options(2));
    await act(async () => {
      await flush();
    });
    expect(view.result.current.documents).toEqual([row(2)]);

    await act(async () => {
      aList.resolve({ documents: [row(1)], total: 1 });
      aStats.resolve(stats(1));
      await flush();
    });
    await act(async () => {
      void oldFetch();
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(oldListCalls + 1);
    expect(view.result.current.documents).toEqual([row(2)]);
    expect(view.result.current.stats).toEqual(stats(2));
  });

  it("drops a held A result across query ABA even when the visible query returns to A", async () => {
    const staleA = defer<unknown>();
    fixture.listDocuments.mockReset().mockReturnValueOnce(staleA.promise).mockResolvedValue({
      documents: [row(3)],
      total: 1,
    });
    const view = renderHook((current) => useDocumentPolling(current), {
      initialProps: options(1, { search: "A" }),
    });
    await act(async () => {
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(1);
    await act(async () => {
      view.rerender(options(1, { search: "B" }));
      await flush();
    });
    await act(async () => {
      view.rerender(options(1, { search: "A" }));
      await flush();
    });

    await act(async () => {
      staleA.resolve({ documents: [row(99)], total: 1 });
      await flush();
    });
    expect(view.result.current.documents).toEqual([row(3)]);
    expect(view.result.current.documents).not.toEqual([row(99)]);
  });

  it("does not let a held stale loadMore response mutate a fresh owner or query window", async () => {
    fixture.listDocuments.mockReset().mockResolvedValueOnce({ documents: [row(1)], total: 100 });
    const view = await renderSettled(options(1));
    const stalePage = defer<unknown>();
    fixture.listDocuments.mockReturnValueOnce(stalePage.promise).mockResolvedValue({
      documents: [row(2)],
      total: 1,
    });
    act(() => view.result.current.loadMore());
    await act(async () => {
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);
    expect(fixture.listDocuments.mock.calls[1]?.[0]).toEqual(
      expect.objectContaining({ perPage: 100 }),
    );

    setUser(303, "poll-token-b");
    view.rerender(options(2, { search: "fresh" }));
    await act(async () => {
      await flush();
    });
    await act(async () => {
      stalePage.resolve({ documents: [row(99)], total: 100 });
      await flush();
    });
    expect(view.result.current.documents).toEqual([row(2)]);
    expect(view.result.current.documents).not.toContainEqual(row(99));
    expect(view.result.current.hasMore).toBe(false);
  });

  it("uses a fresh owner page window of fifty and permits positive recovery", async () => {
    fixture.listDocuments.mockReset().mockResolvedValueOnce({
      documents: Array.from({ length: 50 }, (_, index) => row(index + 1)),
      total: 51,
    });
    fixture.listDocuments.mockResolvedValueOnce({
      documents: Array.from({ length: 51 }, (_, index) => row(index + 1)),
      total: 51,
    });
    const view = await renderSettled(options(2));
    expect(fixture.listDocuments.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({ perPage: 50 }),
    );
    act(() => view.result.current.loadMore());
    await act(async () => {
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);
    expect(fixture.listDocuments.mock.calls[1]?.[0]).toEqual(
      expect.objectContaining({ perPage: 100 }),
    );
    expect(view.result.current.hasMore).toBe(false);
  });

  it("retires retained callbacks after unmount without transport or timer work", async () => {
    const view = renderHook(
      (current) => useDocumentPolling(current),
      {
        initialProps: options(1),
        wrapper: ({ children }) => React.createElement(React.StrictMode, null, children),
      },
    );
    await act(async () => {
      await flush();
    });
    const fetch = view.result.current.fetchDocuments;
    const calls = fixture.listDocuments.mock.calls.length;
    view.unmount();
    await act(async () => {
      void fetch();
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(calls);
  });

  it("keeps two transient indexed wiki hydrations stable until the five second poll", async () => {
    fixture.listDocuments.mockResolvedValue({ documents: [row(1), row(2)], total: 2 });
    fixture.getDocumentWikiStatus.mockResolvedValue({ wiki_status: "compiling" });
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    const view = await renderSettled(options(1));
    expect(fixture.getDocumentWikiStatus).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4999);
    });
    expect(fixture.getDocumentWikiStatus).toHaveBeenCalledTimes(2);
    fixture.getDocumentWikiStatus.mockResolvedValue({ wiki_status: "compiled" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      await flush();
    });
    expect(fixture.getDocumentWikiStatus).toHaveBeenCalledTimes(4);
    expect(view.result.current.wikiStatusMap["1"]?.wiki_status).toBe("compiled");
    expect(view.result.current.wikiStatusMap["2"]?.wiki_status).toBe("compiled");
  });

  it("does not re-fetch terminal wiki rows on same-query list refresh", async () => {
    fixture.listDocuments.mockResolvedValue({ documents: [row(1)], total: 1 });
    fixture.getDocumentWikiStatus.mockResolvedValue({ wiki_status: "compiled" });
    const view = await renderSettled(options(1));
    const wikiCalls = fixture.getDocumentWikiStatus.mock.calls.length;
    await act(async () => {
      void view.result.current.fetchDocuments();
      await flush();
    });
    expect(fixture.getDocumentWikiStatus).toHaveBeenCalledTimes(wikiCalls);
  });

  it("coalesces duplicate compile callbacks and releases busy after failure for the next request", async () => {
    const first = defer<unknown>();
    const second = defer<unknown>();
    fixture.compileDocumentWiki.mockReset().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const view = await renderSettled(options(1));
    await act(async () => {
      void view.result.current.handleCompileDocument("1");
      void view.result.current.handleCompileDocument("1");
      await flush();
    });
    expect(fixture.compileDocumentWiki).toHaveBeenCalledTimes(1);
    expect(view.result.current.compilingDocIds.has("1")).toBe(true);
    await act(async () => {
      first.reject(new Error("compile failed"));
      await flush();
    });
    expect(view.result.current.compilingDocIds.has("1")).toBe(false);
    await act(async () => {
      void view.result.current.handleCompileDocument("1");
      await flush();
    });
    expect(fixture.compileDocumentWiki).toHaveBeenCalledTimes(2);
    await act(async () => {
      second.resolve({});
      await flush();
    });
    expect(view.result.current.compilingDocIds.has("1")).toBe(false);
    expect(fixture.toastError).toHaveBeenCalledTimes(1);
    expect(fixture.toastSuccess).toHaveBeenCalledTimes(1);
  });

  it("drops delayed compile refresh after owner retirement while B remains usable", async () => {
    const compile = defer<unknown>();
    fixture.compileDocumentWiki.mockReset().mockReturnValueOnce(compile.promise).mockResolvedValue({});
    const view = await renderSettled(options(1));
    await act(async () => {
      void view.result.current.handleCompileDocument("1");
      await flush();
    });
    setUser(404, "poll-token-b");
    view.rerender(options(2));
    await act(async () => {
      await flush();
    });
    const wikiCalls = fixture.getDocumentWikiStatus.mock.calls.length;
    await act(async () => {
      compile.resolve({});
      await flush();
    });
    expect(fixture.getDocumentWikiStatus.mock.calls.length).toBe(wikiCalls);
    expect(view.result.current.compilingDocIds.has("1")).toBe(false);
    await act(async () => {
      void view.result.current.handleCompileDocument("2");
      await flush();
    });
    expect(fixture.compileDocumentWiki).toHaveBeenCalledTimes(2);
  });
});

import * as React from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Document, DocumentStatsResponse } from "@/lib/api";
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
import { useDocumentPolling } from "@/components/documents/useDocumentPolling";

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
vi.mock("sonner", () => ({ toast: { error: fixture.toastError, success: fixture.toastSuccess } }));

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

function documentRow(id: number, status: "processing" | "indexed" = "indexed"): Document {
  return {
    id,
    filename: `document-${id}.txt`,
    original_filename: `document-${id}.txt`,
    file_size: 12,
    content_type: "text/plain",
    upload_date: "2024-01-01T00:00:00Z",
    status,
    metadata: { status },
    vault_id: 1,
  } as unknown as Document;
}

const stats = {} as DocumentStatsResponse;
const statsFor = (vault: number) => ({ total: vault } as unknown as DocumentStatsResponse);
const args = (activeVaultId: number | null, search = "") => ({
  activeVaultId,
  search,
  sortBy: "upload_date" as const,
  sortOrder: "desc" as const,
  tagFilterId: null,
  folderFilterId: null,
  uploads: [],
});
const strictWrapper = ({ children }: { children?: React.ReactNode }) => React.createElement(React.StrictMode, null, children);

describe("useDocumentPolling ownership controls", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUserA, "A-token");
    vi.useFakeTimers();
    vi.clearAllMocks();
    fixture.getDocumentStats.mockResolvedValue(stats);
    fixture.getDocumentWikiStatus.mockResolvedValue({ wiki_status: "not_compiled" });
    fixture.compileDocumentWiki.mockResolvedValue({});
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

  it("ignores captured A fetchDocuments and fetchStats callbacks while B is pending", async () => {
    const oldDocuments = deferred<{ documents: Document[]; total: number }>();
    const currentDocuments = deferred<{ documents: Document[]; total: number }>();
    const oldStats = deferred<DocumentStatsResponse>();
    const currentStats = deferred<DocumentStatsResponse>();
    fixture.listDocuments.mockReturnValueOnce(oldDocuments.promise).mockReturnValueOnce(currentDocuments.promise);
    fixture.getDocumentStats.mockReturnValueOnce(oldStats.promise).mockReturnValueOnce(currentStats.promise);

    const view = renderHook((props: { vault: number; search: string }) => useDocumentPolling(args(props.vault, props.search)), {
      initialProps: { vault: 101, search: "private-a" },
    });
    await act(async () => { await flush(); });
    const oldFetchDocuments = view.result.current.fetchDocuments;
    const oldFetchStats = view.result.current.fetchStats;

    setPublicAuthUser(publicUserSameAccount, "B-token");
    view.rerender({ vault: 202, search: "current-b" });
    await act(async () => { await flush(); });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);
    expect(fixture.getDocumentStats).toHaveBeenCalledTimes(2);

    await act(async () => {
      await Promise.all([oldFetchDocuments(), oldFetchStats()]);
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);
    expect(fixture.getDocumentStats).toHaveBeenCalledTimes(2);

    await act(async () => {
      currentDocuments.resolve({ documents: [documentRow(202)], total: 1 });
      currentStats.resolve(statsFor(202));
      await flush();
    });
    expect(view.result.current.documents.map((row) => row.id)).toEqual([202]);
    expect(view.result.current.stats).toEqual(statsFor(202));
  });

  it("clears a StrictMode compile busy ID after the owned request resolves", async () => {
    const compile = deferred<unknown>();
    fixture.listDocuments.mockResolvedValue({ documents: [documentRow(1)], total: 1 });
    fixture.compileDocumentWiki.mockReturnValue(compile.promise);
    const view = renderHook(() => useDocumentPolling(args(303)), { wrapper: strictWrapper });
    await act(async () => { await flush(); });

    const compileRun = view.result.current.handleCompileDocument("303");
    await act(async () => { await flush(); });
    expect([...view.result.current.compilingDocIds]).toEqual(["303"]);

    await act(async () => {
      compile.resolve({});
      await compileRun;
      await flush();
    });
    expect(view.result.current.compilingDocIds.has("303")).toBe(false);
  });

  it("clears the owned wiki refresh timer when its scope unmounts", async () => {
    const compile = deferred<unknown>();
    fixture.listDocuments.mockResolvedValue({ documents: [documentRow(1)], total: 1 });
    fixture.compileDocumentWiki.mockReturnValue(compile.promise);
    const view = renderHook(() => useDocumentPolling(args(303)));
    await act(async () => { await flush(); });

    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    const compileRun = view.result.current.handleCompileDocument("303");
    await act(async () => { await flush(); });
    await act(async () => {
      compile.resolve({});
      await compileRun;
      await flush();
    });

    const refreshTimers = setTimeoutSpy.mock.calls.flatMap((call, index) =>
      call[1] === 2_000 ? [setTimeoutSpy.mock.results[index]?.value] : [],
    );
    expect(refreshTimers).toHaveLength(1);

    view.unmount();

    expect(clearTimeoutSpy).toHaveBeenCalledWith(refreshTimers[0]);
  });
  it("coalesces duplicate compile requests for one document while the first is pending", async () => {
    const compile = deferred<unknown>();
    fixture.listDocuments.mockResolvedValue({ documents: [documentRow(2)], total: 1 });
    fixture.compileDocumentWiki.mockReturnValue(compile.promise);
    const view = renderHook(() => useDocumentPolling(args(404)), { wrapper: strictWrapper });
    await act(async () => { await flush(); });

    const first = view.result.current.handleCompileDocument("404");
    const duplicate = view.result.current.handleCompileDocument("404");
    await act(async () => { await flush(); });
    expect(fixture.compileDocumentWiki).toHaveBeenCalledTimes(1);

    await act(async () => {
      compile.resolve({});
      await Promise.all([first, duplicate]);
      await flush();
    });
    expect(view.result.current.compilingDocIds.has("404")).toBe(false);
  });

  it("keeps replacement B busy when the old A compile finally settles", async () => {
    const oldCompile = deferred<unknown>();
    const currentCompile = deferred<unknown>();
    fixture.listDocuments.mockResolvedValue({ documents: [documentRow(7)], total: 1 });
    fixture.compileDocumentWiki.mockReturnValueOnce(oldCompile.promise).mockReturnValueOnce(currentCompile.promise);
    const view = renderHook((props: { vault: number }) => useDocumentPolling(args(props.vault)), {
      initialProps: { vault: 505 },
      wrapper: strictWrapper,
    });
    await act(async () => { await flush(); });

    const oldRun = view.result.current.handleCompileDocument("7");
    await act(async () => { await flush(); });
    setPublicAuthUser(publicUserSameAccount, "B-token");
    view.rerender({ vault: 606 });
    await act(async () => { await flush(); });
    const currentRun = view.result.current.handleCompileDocument("7");
    await act(async () => { await flush(); });
    expect(fixture.compileDocumentWiki).toHaveBeenCalledTimes(2);

    await act(async () => {
      oldCompile.resolve({});
      await oldRun;
      await flush();
    });
    expect(view.result.current.compilingDocIds.has("7")).toBe(true);

    await act(async () => {
      currentCompile.resolve({});
      await currentRun;
      await flush();
    });
    expect(view.result.current.compilingDocIds.has("7")).toBe(false);
  });
});

// K01 acceptance checks — useDocumentPolling stale-vault guards (AC2/AC3)
// and poll-rejection row preservation (AC8/AC9).
//
// AC2 / AC3 are PRESERVING regression pins over the current Scope/Lease/
// ReadToken guard architecture in useDocumentPolling.ts: a fetchDocuments /
// fetchStats closure captured under vault 1 must never publish vault-1 rows
// or stats once the hook has moved to vault 2. Both must PASS at base.
//
// AC8 / AC9 are DISCRIMINATING checks for the poll-rejection wipe. At base
// the catch branch of fetchDocuments commits `[]` / total 0 when an adaptive
// poll rejects (useDocumentPolling.ts catch path), which both destroys the
// previously loaded rows (AC8) and disarms the adaptive poll because the
// emptied list no longer reports a processing document (AC9). Both must FAIL
// at base; the fix keeps the previously loaded rows on rejection, which also
// leaves the processing-driven poll armed.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Document, DocumentStatsResponse } from "@/lib/api";
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
  toast: { error: fixture.toastError, success: fixture.toastSuccess },
}));

import { useDocumentPolling } from "@/components/documents/useDocumentPolling";

type PollingOptions = Parameters<typeof useDocumentPolling>[0];

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
  state.resolve = (value) => {
    state.settled = true;
    resolvePromise(value);
  };
  state.reject = (reason) => {
    state.settled = true;
    rejectPromise(reason);
  };
  liveDeferreds.add(state as Deferred<unknown>);
  return state;
}

async function flush(): Promise<void> {
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve();
  }
}

let publicAuthSnapshot: ReturnType<typeof useAuthStore.getState>;
const publicUser = {
  id: 101,
  username: "k01-owner",
  full_name: "K01 Owner",
  role: "member" as const,
  is_active: true,
};

function setPublicAuthUser(user: typeof publicUser, token: string): void {
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

function restorePublicAuthState(): void {
  act(() => useAuthStore.setState(publicAuthSnapshot, true));
}

function documentRow(
  id: string,
  status: "indexed" | "processing" = "indexed",
): Document {
  return {
    id,
    filename: `${id}.txt`,
    content_type: "text/plain",
    size: 12,
    created_at: "2026-10-01T00:00:00Z",
    metadata: { status },
    vault_id: 1,
  } as unknown as Document;
}

function statsFor(totalDocuments: number): DocumentStatsResponse {
  return {
    total_documents: totalDocuments,
    total_chunks: 0,
    total_size_bytes: 0,
    documents_by_status: {},
  };
}

function options(activeVaultId: number | null): PollingOptions {
  return {
    activeVaultId,
    search: "",
    sortBy: "created_at",
    sortOrder: "desc",
    tagFilterId: null,
    folderFilterId: null,
    uploads: [],
  };
}

function vaultAnsweringList(request?: { vaultId?: number }): {
  documents: Document[];
  total: number;
} {
  const row = request?.vaultId === 1 ? documentRow("v1-a") : documentRow("v2-a");
  return { documents: [row], total: 1 };
}

describe("useDocumentPolling K01 acceptance checks", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUser, "k01-token");
    vi.useRealTimers();
    // vi.clearAllMocks() does NOT reset implementations — reset each fixture
    // fn explicitly so no previous test's behavior leaks in.
    fixture.listDocuments.mockReset();
    fixture.getDocumentStats.mockReset();
    fixture.getDocumentWikiStatus.mockReset();
    fixture.compileDocumentWiki.mockReset();
    fixture.toastError.mockReset();
    fixture.toastSuccess.mockReset();
    fixture.getDocumentStats.mockResolvedValue(statsFor(0));
    fixture.getDocumentWikiStatus.mockResolvedValue({ wiki_status: "not_compiled" });
    fixture.compileDocumentWiki.mockResolvedValue({});
    vi.spyOn(console, "error").mockImplementation(() => undefined);
  });

  afterEach(async () => {
    cleanup();
    for (const pending of liveDeferreds) {
      if (!pending.settled) pending.resolve(undefined);
    }
    liveDeferreds.clear();
    await act(async () => {
      await flush();
    });
    if (vi.isFakeTimers()) vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    restorePublicAuthState();
  });

  it("stale vault closure never commits previous vault rows", { timeout: 30_000 }, async () => {
    fixture.listDocuments.mockImplementation((request?: { vaultId?: number }) =>
      Promise.resolve(vaultAnsweringList(request)),
    );

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: vault 1 settled with its own row.
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["v1-a"]);
    const staleFetchDocuments = view.result.current.fetchDocuments;

    await act(async () => {
      view.rerender({ vault: 2 });
      await flush();
    });
    // Sentinel: the vault-2 fetch settled and is visible.
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["v2-a"]);

    await act(async () => {
      await staleFetchDocuments();
      await flush();
    });

    const ids = view.result.current.documents.map((row) => row.id);
    expect(ids).not.toContain("v1-a");
    expect(ids).toEqual(["v2-a"]);
  });

  it("late previous vault stats never overwrite current vault stats", { timeout: 30_000 }, async () => {
    const vault1Stats = deferred<DocumentStatsResponse>();
    fixture.listDocuments.mockImplementation((request?: { vaultId?: number }) =>
      Promise.resolve(vaultAnsweringList(request)),
    );
    fixture.getDocumentStats.mockImplementation((vaultId?: number) =>
      vaultId === 1 ? vault1Stats.promise : Promise.resolve(statsFor(7)),
    );

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: vault 1 list settled, its stats are still held pending.
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["v1-a"]);
    expect(view.result.current.stats).toBe(null);

    await act(async () => {
      view.rerender({ vault: 2 });
      await flush();
    });
    // Sentinel: vault 2 stats committed.
    expect(view.result.current.stats?.total_documents).toBe(7);

    await act(async () => {
      vault1Stats.resolve(statsFor(3));
      await flush();
    });

    expect(view.result.current.stats?.total_documents).toBe(7);
  });

  it("poll rejection keeps previously loaded rows", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    const processing = documentRow("p1", "processing");
    fixture.listDocuments
      .mockImplementationOnce(() =>
        Promise.resolve({ documents: [processing], total: 1 }),
      )
      .mockImplementation(() => Promise.reject(new Error("Network down")));

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial load settled with the processing row.
    expect(view.result.current.documents).toHaveLength(1);

    // Fire the 2s adaptive poll; its listDocuments call rejects.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);

    // Discriminating assertion: the previously loaded row must survive the
    // rejected poll. At base the catch branch commits [] and this fails.
    expect(view.result.current.documents.length).toBe(1);
  });

  it("poll rejection keeps adaptive polling armed", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    const processing = documentRow("p1", "processing");
    const processingList = () =>
      Promise.resolve({ documents: [processing], total: 1 });
    fixture.listDocuments
      .mockImplementationOnce(processingList)
      .mockImplementationOnce(() => Promise.reject(new Error("Network down")))
      .mockImplementation(processingList);

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: initial load settled with the processing row visible.
    expect(view.result.current.documents).toHaveLength(1);
    expect(fixture.listDocuments).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    // Discriminating assertion: the processing-driven poll must still be
    // firing after a rejected poll. At base the rejection wipes the rows,
    // the processing doc disappears, and the poll disarms at 2 calls.
    expect(fixture.listDocuments.mock.calls.length).toBeGreaterThanOrEqual(3);
  });
});

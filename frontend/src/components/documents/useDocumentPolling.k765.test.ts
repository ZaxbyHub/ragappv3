// K765 acceptance checks (issue #765, frozen) — useDocumentPolling wiki
// status hydration over an unchanged id set (C3), retry of a failed
// per-document wiki fetch (C4), and per-document commit of a superseded wiki
// batch (C5).
//
// All three are DISCRIMINATING checks: RED at base, GREEN after the fix.
//
//   C3 — when the status poll converges a document from "processing" to
//        "indexed" WITHOUT changing the id set, the hydration gate keyed on
//        the joined id string must not suppress the wiki fetch: the newly
//        indexed document still needs its wiki status hydrated.
//   C4 — a failed per-document wiki fetch must be retried; at base the
//        failure leaves the document permanently absent from wikiStatusMap.
//   C5 — an older wiki batch that is superseded by a newer attempt (the
//        compile follow-up refresh) must still commit its per-document
//        results for documents the newer attempt FAILED to fetch; at base
//        the whole older batch is dropped when its token goes stale.
//
// Harness copied from useDocumentPolling.k01.test.ts (proven in this repo):
// the vi.importActual-spread "@/lib/api" mock, the sonner mock, the
// useAuthStore owner setup, the options() helper, and the deferred/flush
// microtask machinery.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  Document,
  DocumentStatsResponse,
  DocumentWikiStatus,
} from "@/lib/api";
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
  username: "k765-owner",
  full_name: "K765 Owner",
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

function wikiStatus(
  status: DocumentWikiStatus["wiki_status"],
  fileId: number,
): DocumentWikiStatus {
  return {
    file_id: fileId,
    wiki_status: status,
    pages_count: 0,
    claims_count: 0,
    active_claims: 0,
    lint_count: 0,
    pages: [],
    latest_job: null,
    job_count: 1,
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

describe("useDocumentPolling K765 acceptance checks", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUser, "k765-token");
    vi.useRealTimers();
    fixture.listDocuments.mockReset();
    fixture.getDocumentStats.mockReset();
    fixture.getDocumentWikiStatus.mockReset();
    fixture.compileDocumentWiki.mockReset();
    fixture.toastError.mockReset();
    fixture.toastSuccess.mockReset();
    fixture.getDocumentStats.mockResolvedValue(statsFor(0));
    fixture.getDocumentWikiStatus.mockResolvedValue(wikiStatus("compiled", 5));
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

  it("processing to indexed transition with an unchanged id set hydrates wiki status", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    let listCalls = 0;
    fixture.listDocuments.mockImplementation(() => {
      listCalls += 1;
      const row =
        listCalls === 1 ? documentRow("5", "processing") : documentRow("5", "indexed");
      return Promise.resolve({ documents: [row], total: 1 });
    });
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) =>
      Promise.resolve(wikiStatus("compiled", fileId)),
    );

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial load settled with the processing row (poll armed).
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);
    expect(view.result.current.documents[0]?.metadata?.status).toBe("processing");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    // Sentinel: the adaptive poll converged the row to indexed (same id set).
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);
    expect(view.result.current.documents[0]?.metadata?.status).toBe("indexed");

    // Discriminating assertion: the processing->indexed transition must run
    // the wiki hydration for the newly indexed document even though the id
    // set is unchanged. At base the hydration gate keys on the joined id
    // string and never fires: zero calls with first arg 5.
    const callsForFive = fixture.getDocumentWikiStatus.mock.calls.filter(
      ([fileId]) => fileId === 5,
    ).length;
    expect(callsForFive).toBeGreaterThan(0);
  });

  it("failed per document wiki fetch is retried", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );
    let wikiCalls = 0;
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) => {
      wikiCalls += 1;
      return wikiCalls === 1
        ? Promise.reject(new Error("wiki status unavailable"))
        : Promise.resolve(wikiStatus("compiled", fileId));
    });

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial hydration attempt ran and failed.
    expect(fixture.getDocumentWikiStatus).toHaveBeenCalledTimes(1);
    expect(view.result.current.wikiStatusMap["5"]).toBeUndefined();

    for (let index = 0; index < 6; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
    }

    // Discriminating assertion: the failed per-document fetch must be
    // retried until it resolves; the resolved status must be committed.
    // At base nothing retries and wikiStatusMap["5"] stays undefined.
    expect(view.result.current.wikiStatusMap["5"]?.wiki_status).toBe("compiled");
  });

  it("superseded wiki batch still commits its per document results", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({
        documents: [documentRow("5", "indexed"), documentRow("6", "indexed")],
        total: 2,
      }),
    );
    const heldFirstFive = deferred<DocumentWikiStatus>();
    let callsForFive = 0;
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) => {
      if (fileId === 5) {
        callsForFive += 1;
        return callsForFive === 1
          ? heldFirstFive.promise
          : Promise.reject(new Error("wiki status unavailable"));
      }
      return Promise.resolve(wikiStatus("not_compiled", fileId));
    });

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial batch started; document 5's first fetch is held.
    expect(callsForFive).toBe(1);

    await act(async () => {
      await view.result.current.handleCompileDocument("6");
    });
    // Sentinel: the compile succeeded and armed the 2s follow-up refresh.
    expect(fixture.compileDocumentWiki).toHaveBeenCalledWith(6, 1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    // Sentinel: the newer wiki attempt (the compile follow-up refresh) ran,
    // and ITS fetch for document 5 rejected.
    expect(callsForFive).toBe(2);

    // NOW the older batch's held request for 5 resolves with "compiled".
    await act(async () => {
      heldFirstFive.resolve(wikiStatus("compiled", 5));
      await flush();
    });

    // Discriminating assertion: the older batch's per-document result for 5
    // must still be committed — the newer attempt failed to fetch 5, so
    // dropping the older batch leaves 5 without any wiki status. At base the
    // stale-token guard discards the whole older batch.
    expect(view.result.current.wikiStatusMap["5"]?.wiki_status).toBe("compiled");
  });
});

// K02 feedback-round pins — useDocumentPolling keep-rows read-context
// ownership (third-review #5), listError contract, backoff ramp, toast id,
// retry cancellation (PR #1012 swarm-pr-feedback round).
//
// Four preserving pins over the uncommitted working-tree fix, all expected
// GREEN against the current tree:
//   #5  a keep-rows poll failure that belongs to a NEW query context (after a
//       search change) must NOT keep the previous query's rows — the
//       documentsContextRef half of the keepRows decision wipes instead. RED
//       against the pre-feedback code (scope-only keep decision).
//   PRR-010 a keep-rows poll failure over the SAME context keeps the rows and
//       does NOT flip listError (kept rows are valid last-known-good data).
//   ramp  the adaptive interval advances x1.5 per fired tick (2s → 3s → 4.5s):
//         after a poll at +2000 the next is due at +3000, not +2000.
//   toast every poll-failure toast.error carries { id: "documents-list-failure" }
//         so consecutive failures dedupe instead of stacking.
//
// Harness mirrors useDocumentPolling.k01.test.ts (proven in this repo): the
// vi.hoisted fixture + vi.mock("@/lib/api") with importActual spread, the real
// useAuthStore seeding, renderHook, fake timers narrowed to timers only, and
// the flush() microtask helper.

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
  username: "k02-owner",
  full_name: "K02 Owner",
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

function options(activeVaultId: number | null, search = ""): PollingOptions {
  return {
    activeVaultId,
    search,
    sortBy: "created_at",
    sortOrder: "desc",
    tagFilterId: null,
    folderFilterId: null,
    uploads: [],
  };
}

function useFakeTimerClock(): void {
  vi.useFakeTimers({
    toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
  });
}

describe("useDocumentPolling K02 feedback-round pins", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUser, "k02-token");
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

  it("poll failure after a query change does not keep the previous query's rows", { timeout: 30_000 }, async () => {
    useFakeTimerClock();
    const processing = documentRow("p1", "processing");
    // Call 1: the "" foreground load (settles, commits the processing row and
    // owns the rows for the "" read context). Call 2: the "q2" FOREGROUND
    // load, held pending so the q2 window never settles on its own. Call 3:
    // the q2 POLL, which rejects. Later calls: harmless safety net.
    const q2Foreground = deferred<{ documents: Document[]; total: number }>();
    fixture.listDocuments
      .mockImplementationOnce(() =>
        Promise.resolve({ documents: [processing], total: 1 }),
      )
      .mockImplementationOnce(() => q2Foreground.promise)
      .mockImplementationOnce(() => Promise.reject(new Error("Network down")))
      .mockImplementation(() => Promise.resolve({ documents: [], total: 0 }));

    const view = renderHook(
      (props: { search: string }) => useDocumentPolling(options(1, props.search)),
      { initialProps: { search: "" } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the "" query settled with its processing row.
    expect(view.result.current.documents).toHaveLength(1);
    expect(view.result.current.documents[0]?.id).toBe("p1");

    // Change the query: the q2 foreground load issues (call 2, held) and the
    // poll re-arms under the NEW read context.
    await act(async () => {
      view.rerender({ search: "q2" });
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);

    // Fire the 2s poll; its listDocuments call (3rd overall) rejects. The
    // failing fetch belongs to the q2 context, so the ""-context rows must
    // NOT be kept — the documentsContextRef guard wipes them.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(3);

    expect(view.result.current.documents.length).toBe(0);

    // Settle the held q2 foreground load (its token is no longer latest, so
    // it commits nothing) so no flight outlives the test.
    await act(async () => {
      q2Foreground.resolve({ documents: [documentRow("q2-a")], total: 1 });
      await flush();
    });
  });

  it("kept-rows poll failure keeps rows and does not flip listError", { timeout: 30_000 }, async () => {
    useFakeTimerClock();
    const processing = documentRow("p1", "processing");
    fixture.listDocuments
      .mockImplementationOnce(() =>
        Promise.resolve({ documents: [processing], total: 1 }),
      )
      .mockImplementationOnce(() => Promise.reject(new Error("Network down")))
      .mockImplementation(() =>
        Promise.resolve({ documents: [processing], total: 1 }),
      );

    const view = renderHook(
      (props: { search: string }) => useDocumentPolling(options(1, props.search)),
      { initialProps: { search: "" } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial load settled with the processing row and no error.
    expect(view.result.current.documents).toHaveLength(1);
    expect(view.result.current.listError).toBe(false);

    // Fire the 2s adaptive poll; its listDocuments call rejects over the SAME
    // query context — the rows must survive and listError must stay false.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);

    expect(view.result.current.documents.length).toBe(1);
    expect(view.result.current.listError).toBe(false);
  });

  it("poll backoff advances 1.5x per fired tick", { timeout: 30_000 }, async () => {
    useFakeTimerClock();
    const processing = documentRow("p1", "processing");
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [processing], total: 1 }),
    );

    const view = renderHook(
      (props: { search: string }) => useDocumentPolling(options(1, props.search)),
      { initialProps: { search: "" } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: only the initial foreground load has fired (t=0).
    expect(fixture.listDocuments).toHaveBeenCalledTimes(1);

    // First poll fires at +2000 (base interval) and re-arms at 2000 * 1.5.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);

    // 2900ms more (t=4900): the next poll is due at +3000, so nothing fires.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_900);
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);

    // The last 100ms (t=5000) reaches the +3000 mark: the second poll fires.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(3);
  });

  it("poll failure toast carries a stable dedupe id", { timeout: 30_000 }, async () => {
    useFakeTimerClock();
    const processing = documentRow("p1", "processing");
    fixture.listDocuments
      .mockImplementationOnce(() =>
        Promise.resolve({ documents: [processing], total: 1 }),
      )
      .mockImplementationOnce(() => Promise.reject(new Error("Network down")))
      .mockImplementationOnce(() => Promise.reject(new Error("Network down")))
      .mockImplementation(() =>
        Promise.resolve({ documents: [processing], total: 1 }),
      );

    const view = renderHook(
      (props: { search: string }) => useDocumentPolling(options(1, props.search)),
      { initialProps: { search: "" } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial load settled with the processing row visible.
    expect(view.result.current.documents).toHaveLength(1);

    // Two consecutive poll rejections: the first at the base 2000ms; the
    // kept-rows failure re-arms itself (no state changed for the effect to
    // observe), so the second fires at the ramped +3000ms.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      await flush();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
      await flush();
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(3);
    expect(fixture.toastError).toHaveBeenCalledTimes(2);

    // Both failures surface through the SAME toast id so sonner dedupes them.
    expect(fixture.toastError).toHaveBeenLastCalledWith(expect.any(String), {
      id: "documents-list-failure",
    });
  });
});

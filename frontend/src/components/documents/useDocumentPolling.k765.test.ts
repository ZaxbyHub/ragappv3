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
// Amended after review round 1 (falsifiability gaps):
//   C4-BOUND — the retry path is capped at five total attempts per document
//        (initial + four retries at skip ticks 0,1,3,7); a permanently
//        failing document must not be re-requested forever.
//   VAULT-SWITCH — per-document wiki failure bookkeeping is scope-owned: a
//        vault switch clears it, so the old vault's failing document earns
//        no further fetches and the new vault hydrates fresh.
//
// Review round 2 pins (final critic): PRR-004 (an exhausted failed id is
// skipped by hydration on a key change), PRR-009 (a superseded wiki batch
// still fetches its remaining documents), PRR-035 (a stale failure does not
// mark a document failed after a newer success), and PRR-011 (a failed
// upload-transition refetch keeps the loaded rows — relocated here from
// useDocumentPolling.k766.test.ts, which is at its 500-line cap).
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
import type { UploadFile } from "@/stores/useUploadStore";
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

function options(
  activeVaultId: number | null,
  overrides?: Partial<PollingOptions>
): PollingOptions {
  return {
    activeVaultId,
    search: "",
    sortBy: "created_at",
    sortOrder: "desc",
    tagFilterId: null,
    folderFilterId: null,
    uploads: [],
    ...overrides,
  };
}

/** Fresh upload-monitor fixture row (a NEW object every call, like the store). */
function uploadRow(id: string, status: string, progress = 0): UploadFile {
  return {
    id,
    file: { name: `${id}.bin` } as File,
    uploadProgress: progress,
    progress,
    status,
  } as unknown as UploadFile;
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

  it("permanently failing wiki fetch is retried at most five times in total", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );
    // ALWAYS reject: the document can never succeed, so the only observable
    // bound is the attempt cap (review round 1 B-4/R02).
    fixture.getDocumentWikiStatus.mockRejectedValue(
      new Error("wiki status unavailable"),
    );

    renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });

    // The bounded schedule is measured in retry-interval ticks with skip
    // credits [0, 1, 3, 7]: the retries ride the ticks at 5s, 15s, 35s and
    // 75s after the initial hydration failure — the schedule is exhausted
    // only at t=75s, so advance well past it (16 ticks = 80s).
    for (let index = 0; index < 16; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
    }
    await act(async () => {
      await flush();
    });

    // Bounded-retry assertion: FIVE attempts total (initial + four retries).
    // An uncapped implementation keeps fetching every eligible tick past
    // this point. (Six ticks alone cannot exhaust the [0,1,3,7] skip
    // schedule — the cap is only observable after 15 ticks.)
    expect(fixture.getDocumentWikiStatus).toHaveBeenCalledTimes(5);
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 5),
    ).toHaveLength(5);
  });

  it("failed document state resets on a vault switch", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // Vault 1 lists the failing document; vault 2 lists a fresh one.
    fixture.listDocuments.mockImplementation(({ vaultId }: { vaultId?: number }) =>
      vaultId === 2
        ? Promise.resolve({ documents: [documentRow("9", "indexed")], total: 1 })
        : Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) =>
      fileId === 5
        ? Promise.reject(new Error("wiki status unavailable"))
        : Promise.resolve(wikiStatus("compiled", fileId)),
    );

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(options(props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial hydration for 5 ran and failed...
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 5),
    ).toHaveLength(1);
    // ...and one retry tick fired for it (skip credit 0 rides the next tick).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 5),
    ).toHaveLength(2);

    // Switch to vault 2: the scope (and with it every per-document wiki
    // bookkeeping map) is retired.
    await act(async () => {
      view.rerender({ vault: 2 });
    });
    await act(async () => {
      await flush();
    });
    // The old document would now resolve — if its retry schedule had leaked
    // across the switch, later ticks would still fetch it.
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) =>
      Promise.resolve(wikiStatus("compiled", fileId)),
    );
    const callsForFiveAtSwitch = fixture.getDocumentWikiStatus.mock.calls.filter(
      ([fileId]) => fileId === 5,
    ).length;

    for (let index = 0; index < 6; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
    }
    await act(async () => {
      await flush();
    });

    // Discriminating assertions: no further fetch for the old vault's doc...
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 5),
    ).toHaveLength(callsForFiveAtSwitch);
    // ...the new vault's document hydrated fresh...
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 9),
    ).toHaveLength(1);
    expect(view.result.current.wikiStatusMap["9"]?.wiki_status).toBe("compiled");
    // ...and the old failure is not rendered into the new vault's view.
    expect(view.result.current.wikiFetchFailedIds.has("5")).toBe(false);
    expect(view.result.current.wikiStatusMap["5"]).toBeUndefined();
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

  it("an exhausted failed id is not re-requested when the list key changes", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // The empty search lists only the permanently failing doc 5; the "foo"
    // search additionally returns doc 6 (a fresh, hydratable doc).
    fixture.listDocuments.mockImplementation(({ search }: { search?: string }) =>
      search === "foo"
        ? Promise.resolve({
            documents: [documentRow("5", "indexed"), documentRow("6", "indexed")],
            total: 2,
          })
        : Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );
    fixture.getDocumentWikiStatus.mockRejectedValue(
      new Error("wiki status unavailable"),
    );

    const view = renderHook(
      (props: { search: string }) =>
        useDocumentPolling(options(1, { search: props.search })),
      { initialProps: { search: "" } },
    );
    await act(async () => {
      await flush();
    });
    // Exhaust the capped schedule exactly as the bounded check above:
    // 16 ticks = 80 s, the [0,1,3,7] skip credits land all 5 attempts.
    for (let index = 0; index < 16; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
    }
    await act(async () => {
      await flush();
    });
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 5),
    ).toHaveLength(5);

    // Change the list key: the query context changes and the hydration pass
    // re-runs — it must hydrate the FRESH doc 6 while skipping the
    // exhausted doc 5 (review round 2 PRR-004: a failed id is owned
    // exclusively by its capped retry schedule).
    await act(async () => {
      view.rerender({ search: "foo" });
    });
    await act(async () => {
      await flush();
    });
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 6).length,
    ).toBeGreaterThanOrEqual(1);

    // More retry ticks: doc 6 rides its own schedule; doc 5 stays skipped.
    for (let index = 0; index < 6; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
    }
    await act(async () => {
      await flush();
    });
    // Discriminating assertion: the key-change hydration and every later
    // tick leave the exhausted id alone. At base the hydration pass
    // re-included it on every key change and the fetches never stopped.
    expect(
      fixture.getDocumentWikiStatus.mock.calls.filter(([fileId]) => fileId === 5),
    ).toHaveLength(5);
    expect(view.result.current.wikiFetchFailedIds.has("5")).toBe(true);
  });

  it("a superseded batch still fetches its remaining documents", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // SEVEN indexed docs exhaust the six batch workers: docs 5..10 occupy
    // the first worker iterations (each HELD in flight); doc 11 — LAST in
    // cursor order — is only reachable by a worker's SECOND loop iteration,
    // making it the run-to-completion witness (review round 2 PRR-009).
    const docs = ["5", "6", "7", "8", "9", "10", "11"].map((id) =>
      documentRow(id, "indexed"),
    );
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: docs, total: docs.length }),
    );
    const held = docs.slice(0, 6).map(() => deferred<DocumentWikiStatus>());
    const callsPerDoc = new Map<number, number>();
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) => {
      const calls = (callsPerDoc.get(fileId) ?? 0) + 1;
      callsPerDoc.set(fileId, calls);
      if (fileId <= 10) {
        // Old batch (call 1): held. Newer batch (call 2+): rejects.
        return calls === 1
          ? held[fileId - 5].promise
          : Promise.reject(new Error("wiki status unavailable"));
      }
      // Doc 11: the NEWER batch reaches it first (call 1 rejects); the old
      // batch's late loop iteration is call 2 → compiled.
      return calls === 1
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
    // Sentinel: the initial batch started; all six workers are held.
    expect([...callsPerDoc.keys()].filter((fileId) => fileId <= 10)).toHaveLength(6);

    // A NEWER wiki read: compile doc 7; the 2s follow-up refresh re-reads
    // every indexed doc, superseding the initial batch's read token.
    await act(async () => {
      await view.result.current.handleCompileDocument("7");
    });
    expect(fixture.compileDocumentWiki).toHaveBeenCalledWith(7, 1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await act(async () => {
      await flush();
    });
    // Sentinel: the newer batch ran (second calls for 5..10) and itself
    // reached doc 11 (its call 1 rejected — only the OLD batch's continued
    // loop can still give 11 a successful fetch).
    expect(callsPerDoc.get(5)).toBe(2);
    expect(callsPerDoc.get(11)).toBe(1);

    // Release the old batch's held workers.
    await act(async () => {
      held.forEach((pending, index) =>
        pending.resolve(wikiStatus("compiled", index + 5)),
      );
      await flush();
    });

    // Discriminating assertions: the old batch's workers looped PAST the
    // supersession and fetched the remaining doc 11 (call 2), whose result
    // committed. At base the whole-batch currency gate stopped every worker
    // at its next loop iteration: call 2 never happened and the newer
    // batch's rejection left 11 with no status at all.
    expect(callsPerDoc.get(11)).toBe(2);
    expect(view.result.current.wikiStatusMap["11"]?.wiki_status).toBe("compiled");
    expect(view.result.current.wikiFetchFailedIds.has("11")).toBe(false);
    // The old batch's held successes committed as well (the doc AFTER the
    // held run did not abandon the batch's earlier documents either).
    expect(view.result.current.wikiStatusMap["5"]?.wiki_status).toBe("compiled");
  });

  it("a stale failure does not mark a document failed after a newer success", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({
        documents: [documentRow("5", "indexed"), documentRow("6", "indexed")],
        total: 2,
      }),
    );
    // Doc 5 attempt 1 (initial batch) is HELD and will REJECT late; the
    // newer attempt 2 (key-change batch) resolves compiled first. Doc 6
    // always resolves.
    const staleFailure = deferred<DocumentWikiStatus>();
    let callsForFive = 0;
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) => {
      if (fileId === 5) {
        callsForFive += 1;
        return callsForFive === 1
          ? staleFailure.promise
          : Promise.resolve(wikiStatus("compiled", fileId));
      }
      return Promise.resolve(wikiStatus("not_compiled", fileId));
    });

    const view = renderHook(
      (props: { search: string }) =>
        useDocumentPolling(options(1, { search: props.search })),
      { initialProps: { search: "" } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial batch started; doc 5's first attempt is held.
    expect(callsForFive).toBe(1);

    // Key change: the new hydration batch re-requests doc 5 (still
    // uncached — its first attempt has neither committed nor failed) and
    // attempt 2 SUCCEEDS and commits first.
    await act(async () => {
      view.rerender({ search: "foo" });
    });
    await act(async () => {
      await flush();
    });
    expect(callsForFive).toBe(2);
    expect(view.result.current.wikiStatusMap["5"]?.wiki_status).toBe("compiled");

    // NOW the older attempt rejects (a response that lost the race).
    await act(async () => {
      staleFailure.reject(new Error("wiki status unavailable"));
      await flush();
    });

    // Discriminating assertion (review round 2 PRR-035): the attempt-
    // recency watermark suppresses the stale failure — doc 5 is NOT marked
    // failed and the newer success stays committed. At base the failure
    // path had no guard and flipped the cell to "unavailable".
    expect(view.result.current.wikiFetchFailedIds.has("5")).toBe(false);
    expect(view.result.current.wikiStatusMap["5"]?.wiki_status).toBe("compiled");
  });

  // Relocated from useDocumentPolling.k766.test.ts (at its 500-line cap):
  // the upload-transition refetch must not wipe a loaded list when it fails.
  it("a failed transition refetch keeps the loaded rows", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // The initial load commits row 5; the upload-transition refetch REJECTS.
    fixture.listDocuments
      .mockImplementationOnce(() =>
        Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
      )
      .mockRejectedValue(new Error("transient list failure"));

    const view = renderHook(
      (props: { uploads: UploadFile[] }) =>
        useDocumentPolling(options(1, { uploads: props.uploads })),
      { initialProps: { uploads: [] as UploadFile[] } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial load settled with row 5 committed.
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);

    // The indexed transition arms the 1s refetch; its list call rejects.
    await act(async () => {
      view.rerender({ uploads: [uploadRow("a", "indexed")] });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    await act(async () => {
      await flush();
    });

    // Discriminating assertions (review round 2 PRR-011,
    // fetchDocuments({keepRowsOnFailure:true})): the refetch ran and failed
    // (a toast firing is fine), but the loaded rows SURVIVE and no error
    // state is set — the transition was consumed, so a wipe to the error
    // state would never self-heal. At base the failure wiped the rows.
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);
    expect(view.result.current.listError).toBe(false);
  });
});

// K766 acceptance checks (issue #766, frozen) — useDocumentPolling document
// timers: compile follow-up refresh vs unmount (C14, PRESERVING), the
// indexed-upload refetch vs monitor churn (C15/C16), the adaptive status-poll
// interval vs progress changes (C17), and Authorization-header leakage in
// failure logs (C18). C14 GREEN at base/after the fix; C15-C18 DISCRIMINATING.
//
// Amended after review round 1: C14 also pins the timer-clear mechanism via
// vi.getTimerCount; new PRR-013/PRR-023 checks (acceptance refetch fires once
// per NEW upload id; the seen-indexed set reconciles); C17 drives production
// top-level progress_percent; C18 uses the NORMALIZED error shape.
//
// Harness copied from useDocumentPolling.k01.test.ts (proven in this repo):
// the vi.importActual-spread "@/lib/api" mock, the sonner mock, the
// useAuthStore owner setup, the options() helper, and the flush machinery.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";

import type { Document, DocumentStatsResponse, DocumentWikiStatus } from "@/lib/api";
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

async function flush(): Promise<void> {
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve();
  }
}

let publicAuthSnapshot: ReturnType<typeof useAuthStore.getState>;
const publicUser = {
  id: 101,
  username: "k766-owner",
  full_name: "K766 Owner",
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

function documentRow(id: string, status: "indexed" | "processing" = "indexed"): Document {
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

function optionsWith(uploads: UploadFile[], activeVaultId: number | null): PollingOptions {
  return {
    activeVaultId,
    search: "",
    sortBy: "created_at",
    sortOrder: "desc",
    tagFilterId: null,
    folderFilterId: null,
    uploads,
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

/** The apiClient interceptor's NORMALIZED error (review round 1): top-level
 * status, raw AxiosError under originalError whose serialized config still
 * carries the Authorization header and the request URL (the leak shape). */
function normalizedBearerError(): Error & { status?: number; originalError?: unknown } {
  const config = {
    headers: { Authorization: "Bearer probe-token" },
    url: "/documents",
  } as unknown as InternalAxiosRequestConfig;
  const err = new Error("Request failed with status code 500") as Error & {
    status?: number;
    originalError?: unknown;
  };
  err.status = 500;
  err.originalError = new AxiosError("Request failed with status code 500", "500", config);
  return err;
}

/** Serialize every logged argument; JSON.stringify failures become "". */
function serializedLoggedArgs(spies: Array<{ mock: { calls: unknown[][] } }>): string[] {
  return spies
    .flatMap((spy) => spy.mock.calls)
    .flatMap((args) =>
      args.map((arg) => {
        try {
          const serialized = JSON.stringify(arg);
          return serialized === undefined ? "" : serialized;
        } catch {
          return "";
        }
      }),
    );
}

describe("useDocumentPolling K766 acceptance checks", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUser, "k766-token");
    vi.useRealTimers();
    fixture.listDocuments.mockReset();
    fixture.getDocumentStats.mockReset();
    fixture.getDocumentWikiStatus.mockReset();
    fixture.compileDocumentWiki.mockReset();
    fixture.toastError.mockReset();
    fixture.toastSuccess.mockReset();
    fixture.getDocumentStats.mockResolvedValue(statsFor(0));
    fixture.getDocumentWikiStatus.mockImplementation((fileId: number) =>
      Promise.resolve(wikiStatus("compiled", fileId)),
    );
    fixture.compileDocumentWiki.mockResolvedValue({});
    vi.spyOn(console, "error").mockImplementation(() => undefined);
  });

  afterEach(async () => {
    cleanup();
    await act(async () => {
      await flush();
    });
    if (vi.isFakeTimers()) vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    restorePublicAuthState();
  });

  it("compile follow-up refresh does not fire after unmount", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(optionsWith([], props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial hydration cached the compiled wiki status for 5.
    expect(view.result.current.wikiStatusMap["5"]?.wiki_status).toBe("compiled");

    await act(async () => {
      await view.result.current.handleCompileDocument("5");
    });
    // Sentinel: the compile succeeded and armed the 2s follow-up refresh.
    expect(fixture.compileDocumentWiki).toHaveBeenCalledWith(5, 1);
    // Mechanism pin: a timer is genuinely armed, and drives the follow-up refresh.
    expect(vi.getTimerCount()).toBeGreaterThanOrEqual(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(fixture.getDocumentWikiStatus.mock.calls.length).toBeGreaterThan(0);

    // Re-arm via a second compile; unmount must CLEAR it (getTimerCount → 0).
    await act(async () => {
      await view.result.current.handleCompileDocument("5");
    });
    expect(vi.getTimerCount()).toBeGreaterThanOrEqual(1);
    fixture.getDocumentWikiStatus.mockClear();
    act(() => {
      view.unmount();
    });
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });

    // Preserving pin: the armed compile follow-up refresh is cancelled on
    // unmount — no wiki status fetch happens after the page is gone.
    // GREEN at base and after the fix.
    expect(fixture.getDocumentWikiStatus).toHaveBeenCalledTimes(0);
  });

  it("an accepted upload triggers a prompt refetch", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );

    const view = renderHook(
      (props: { uploads: UploadFile[] }) => useDocumentPolling(optionsWith(props.uploads, 1)),
      { initialProps: { uploads: [] } },
    );
    await act(async () => {
      await flush();
    });
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);

    fixture.listDocuments.mockClear();
    // A NEW pending upload id — the acceptance trigger (PRR-013) refetches once.
    view.rerender({ uploads: [uploadRow("a", "pending")] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(fixture.listDocuments.mock.calls.length).toBeGreaterThanOrEqual(1);

    // Five monitor ticks, FRESH arrays, same pending id: acceptance fires
    // once per NEW id, never per array identity.
    for (let tick = 1; tick <= 5; tick += 1) {
      view.rerender({ uploads: [uploadRow("a", "pending")] });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
    }
    expect(fixture.listDocuments).toHaveBeenCalledTimes(1);
  });

  it("seen indexed set reconciles so a retry round trip re-fires", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );

    const view = renderHook(
      (props: { uploads: UploadFile[] }) => useDocumentPolling(optionsWith(props.uploads, 1)),
      { initialProps: { uploads: [] } },
    );
    await act(async () => {
      await flush();
    });
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);

    fixture.listDocuments.mockClear();
    // A indexes → one refetch.
    view.rerender({ uploads: [uploadRow("a", "indexed")] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(1);

    // A leaves indexed (retryUpload returns it to pending): the seen set must
    // FORGET it, so the SECOND transition re-fires the refetch (one more call).
    view.rerender({ uploads: [uploadRow("a", "pending")] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    view.rerender({ uploads: [uploadRow("a", "indexed")] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(fixture.listDocuments).toHaveBeenCalledTimes(2);
  });

  it("indexed upload triggers a refetch despite progress churn", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );

    const view = renderHook(
      (props: { uploads: UploadFile[] }) =>
        useDocumentPolling(optionsWith(props.uploads, 1)),
      { initialProps: { uploads: [] } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: initial load settled; the status poll is disarmed (the only
    // listed document is indexed, not processing).
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);

    fixture.listDocuments.mockClear();
    // 5s of monitor churn: a fresh uploads array every 500ms, A indexed the
    // whole time, B uploading with progress incremented each tick.
    for (let tick = 1; tick <= 10; tick += 1) {
      const uploads = [
        uploadRow("a", "indexed"),
        uploadRow("b", "uploading", tick * 10),
      ];
      await act(async () => {
        view.rerender({ uploads });
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
    }

    // Discriminating assertion: the indexed upload must still trigger a
    // refetch. At base every churn rerender restarts the 1s timer: zero calls.
    expect(fixture.listDocuments.mock.calls.length).toBeGreaterThan(0);
  });

  it("a single indexed upload refetches once across repeated monitor ticks", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    fixture.listDocuments.mockImplementation(() =>
      Promise.resolve({ documents: [documentRow("5", "indexed")], total: 1 }),
    );

    const view = renderHook(
      (props: { uploads: UploadFile[] }) =>
        useDocumentPolling(optionsWith(props.uploads, 1)),
      { initialProps: { uploads: [] } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: initial load settled.
    expect(view.result.current.documents.map((row) => row.id)).toEqual(["5"]);

    fixture.listDocuments.mockClear();
    // The transition: uploads goes from [] to [A indexed]... then five more
    // monitor ticks with FRESH arrays holding the same indexed A, 1500ms
    // apart (each fresh array re-runs the uploads effect).
    await act(async () => {
      view.rerender({ uploads: [uploadRow("a", "indexed")] });
    });
    for (let tick = 1; tick <= 5; tick += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      await act(async () => {
        view.rerender({ uploads: [uploadRow("a", "indexed")] });
      });
    }
    // Advance past the last armed timeout so every base timer fires.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    await act(async () => {
      await flush();
    });

    // Discriminating assertion: one indexed transition means exactly ONE
    // refetch. At base every fresh array restarts the timer and the fetch
    // fires on every monitor tick (~6 calls).
    expect(fixture.listDocuments).toHaveBeenCalledTimes(1);
  });

  it("status poll interval resets when progress changes", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    let listCalls = 0;
    fixture.listDocuments.mockImplementation(() => {
      listCalls += 1;
      // metadata.progress CONSTANT; the PRODUCTION top-level surface
      // (progress_percent — what the backend emits) advances; the adaptive
      // poll's signature reads it, so the interval must reset on this alone.
      const row = {
        ...documentRow("p1", "processing"),
        metadata: { status: "processing", progress: 7 },
        progress_percent: listCalls,
      } as unknown as Document;
      return Promise.resolve({ documents: [row], total: 1 });
    });

    const view = renderHook(
      (props: { vault: number }) => useDocumentPolling(optionsWith([], props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: the initial load settled with the processing row (poll armed).
    expect(view.result.current.documents).toHaveLength(1);

    fixture.listDocuments.mockClear();
    for (let step = 0; step < 4; step += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }

    // Discriminating assertion: advancing progress means active work, so the
    // interval resets to base cadence — 4 polls in 8s (2s/4s/6s/8s). At base
    // it only grows (2s→3s→4.5s): 2 polls land inside the 8s window.
    expect(fixture.listDocuments).toHaveBeenCalledTimes(4);
  });

  it("documents and stats failure logs carry no Authorization header", { timeout: 30_000 }, async () => {
    const authError = normalizedBearerError();
    // Sentinel: the fixture error serializes the Authorization header (via
    // the raw AxiosError under originalError) — else this check is vacuous.
    expect(JSON.stringify(authError)).toContain("Authorization");

    fixture.listDocuments.mockRejectedValue(authError);
    fixture.getDocumentStats.mockRejectedValue(authError);
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const warnSpy = vi
      .spyOn(console, "warn")
      .mockImplementation(() => undefined);

    renderHook(
      (props: { vault: number }) => useDocumentPolling(optionsWith([], props.vault)),
      { initialProps: { vault: 1 } },
    );
    await act(async () => {
      await flush();
    });
    // Sentinel: both failure logs actually fired.
    expect(errorSpy.mock.calls.length).toBeGreaterThanOrEqual(2);

    const logged = serializedLoggedArgs([errorSpy, warnSpy]);
    const leaking = logged.filter((serialized) =>
      serialized.includes("Authorization"),
    );

    // Discriminating assertion: no logged argument may serialize the request
    // config's Authorization header. At base both console.error(err) calls
    // serialize the AxiosError with its config: 2 leaking arguments.
    expect(leaking).toHaveLength(0);
    // Positive pin (review round 1): the sanitized log still DELIVERS the
    // status and URL — stripping the header must not strip the diagnosis.
    expect(logged.some((serialized) => serialized.includes("500"))).toBe(true);
    expect(logged.some((serialized) => serialized.includes("/documents"))).toBe(true);
  });
});

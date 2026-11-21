// frontend/src/hooks/useJobStatus.m04.test.ts
// Issue #784 ([Workstream M] PR 4 of 4), re-pinned for the #1157 aggregate
// seam: the Activity tray's data source is useActivityJobs, which now polls
// ONE endpoint per tick — getActivityJobs from "@/lib/api" — instead of the
// old five-family, per-vault fan-out. The frozen component-level contract
// lives in ActivityTray.m04.test.tsx / PageShell.m04.test.tsx; this file
// pins the SEAM semantics against the aggregate: exactly one request per
// tick with zero legacy family dispatches, family-discriminated row mapping
// (wiki error titles, draft active_stage phases, ingest phase labels through
// the REAL documentProgress vocabulary, reindex terminal classification),
// PER-FAMILY terminal retention across a multi-family backlog (one family's
// backlog must not spend another family's budget), live-row non-capping,
// degradation to the last snapshot on a rejected aggregate call, refresh
// coalescing while a tick is in flight, the interval floor, and unmount
// safety of the poll loop.
//
// Lineage (feedback round 853-20261005 discipline preserved): mocks are
// reset in beforeEach (clearMocks only clears call history); the ingest
// phase assertion still exercises the REAL documentProgress vocabulary; the
// retention fixtures stay server-ordered newest-first like the real lists.
// Mock layout mirrors useJobStatus.audit1115.budget.test.tsx: "@/lib/api"
// is mocked with the importActual spread PLUS getActivityJobs, and every
// legacy per-family module stays mocked so the zero-tolerance no-fan-out
// assertions keep their teeth.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import type { ActivityJob } from "@/lib/api";

const {
  mockGetActivityJobs,
  mockListDocuments,
  mockListReindexJobs,
  mockListWikiJobs,
  mockListKMSJobs,
  mockListDrafts,
  mockListDraftJobs,
  mockVaultState,
} = vi.hoisted(() => ({
  mockGetActivityJobs: vi.fn(),
  mockListDocuments: vi.fn(),
  mockListReindexJobs: vi.fn(),
  mockListWikiJobs: vi.fn(),
  mockListKMSJobs: vi.fn(),
  mockListDrafts: vi.fn(),
  mockListDraftJobs: vi.fn(),
  mockVaultState: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getActivityJobs: mockGetActivityJobs,
}));

vi.mock("@/lib/api/core", () => ({
  listDocuments: mockListDocuments,
  listReindexJobs: mockListReindexJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  listWikiJobs: mockListWikiJobs,
}));

vi.mock("@/lib/api/kms", () => ({
  listKMSJobs: mockListKMSJobs,
}));

vi.mock("@/lib/api/draftRoom", () => ({
  listDrafts: mockListDrafts,
  listDraftJobs: mockListDraftJobs,
}));

vi.mock("@/stores/useVaultStore", () => ({
  // A zustand store stand-in: callable component-side, with a poll-time
  // getState the tests drive through mockReturnValue (asserted UNCONSULTED —
  // accessibleVaultIds is gone; the server scopes vaults now).
  useVaultStore: Object.assign(() => ({}), { getState: mockVaultState }),
}));

// The REAL documentProgress runs (no identity mock): ingest phase asserts
// the family's own phase vocabulary through it (PRR-016).
import { useActivityJobs } from "./useJobStatus";

/** A complete ActivityJob row; per-family fields default to their nulls. */
function activityJob(
  overrides: Partial<ActivityJob> & Pick<ActivityJob, "family" | "id">
): ActivityJob {
  return {
    vault_id: null,
    status: "running",
    title: "t",
    created_at: "2026-01-01T00:00:00Z",
    completed_at: null,
    draft_id: null,
    file_id: null,
    active_stage: null,
    phase: null,
    phase_message: null,
    progress_percent: null,
    processed_units: null,
    total_units: null,
    unit_label: null,
    error: null,
    ...overrides,
  };
}

/** The wire shape GET /api/activity/jobs answers with. */
function activityResponse(
  jobs: ActivityJob[] = [],
  changeToken = "t0",
  changed = false,
) {
  return { jobs, change_token: changeToken, changed };
}

beforeEach(() => {
  mockVaultState.mockReturnValue({
    vaults: [{ id: 1 }],
    loading: false,
    fetchVaults: vi.fn().mockResolvedValue(undefined),
  });
  mockGetActivityJobs.mockReset();
  mockGetActivityJobs.mockResolvedValue(activityResponse());
  mockListDocuments.mockReset();
  mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
  mockListReindexJobs.mockReset();
  mockListReindexJobs.mockResolvedValue({ jobs: [] });
  mockListWikiJobs.mockReset();
  mockListWikiJobs.mockResolvedValue({ jobs: [] });
  mockListKMSJobs.mockReset();
  mockListKMSJobs.mockResolvedValue({ jobs: [] });
  mockListDrafts.mockReset();
  mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mockListDraftJobs.mockReset();
  mockListDraftJobs.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useActivityJobs aggregate seam (issues #784 / #1157)", () => {
  it("dispatches exactly one getActivityJobs call for the first tick and consults none of the legacy per-family routes", async () => {
    const { result } = renderHook(() => useActivityJobs({ intervalMs: 8000 }));

    // Wait on the settle (loading clears in the tick's finally), then pin
    // the exactly-once dispatch: one tick, one aggregate request.
    await waitFor(
      () => {
        expect(result.current.loading).toBe(false);
      },
      { timeout: 8_000 }
    );
    expect(mockGetActivityJobs).toHaveBeenCalledTimes(1);

    // Zero fan-out: none of the deleted per-family fetchers, and none of the
    // deleted vault-store consultation, may run (the server scopes families
    // to the caller's vaults now — #1157).
    expect(mockListDocuments).not.toHaveBeenCalled();
    expect(mockListWikiJobs).not.toHaveBeenCalled();
    expect(mockListKMSJobs).not.toHaveBeenCalled();
    expect(mockListDrafts).not.toHaveBeenCalled();
    expect(mockListDraftJobs).not.toHaveBeenCalled();
    expect(mockListReindexJobs).not.toHaveBeenCalled();
    expect(mockVaultState).not.toHaveBeenCalled();
  });

  it("maps every family's rows onto ActivityJobRow (wiki error titles, draft active_stage, ingest phase vocabulary, reindex classification, keys)", async () => {
    mockGetActivityJobs.mockResolvedValue(
      activityResponse(
        [
          // wiki: a failed compile's own (redacted) error text is the title;
          // a running compile keeps the server title and is cancellable.
          activityJob({ family: "wiki", id: 7, vault_id: 3, status: "running", title: "Wiki compile" }),
          activityJob({ family: "wiki", id: 8, vault_id: 3, status: "failed", title: "Wiki compile", error: "compile exploded" }),
          // kms: status rows are never cancellable.
          activityJob({ family: "kms", id: 11, vault_id: 3, status: "running", title: "KMS recompile" }),
          // draft-room: phase prefers active_stage, falling back to status.
          activityJob({ family: "draft-room", id: 9, draft_id: 5, status: "running", active_stage: "compiling", title: "Q3 report" }),
          activityJob({ family: "draft-room", id: 10, draft_id: 6, status: "queued", active_stage: null, title: "Launch spec" }),
          // ingest: phase labels come from the real documentProgress
          // vocabulary — phase_message outranks phase, phase outranks the
          // status-derived fallbacks.
          activityJob({ family: "ingest", id: 1, file_id: "42", vault_id: 1, status: "processing", phase: "embedding", title: "handbook.pdf" }),
          activityJob({ family: "ingest", id: 2, file_id: "43", vault_id: 1, status: "processing", phase_message: "Chunking document", title: "spec.pdf" }),
          activityJob({ family: "ingest", id: 3, file_id: "44", vault_id: 1, status: "processing", title: "notes.txt" }),
          // reindex: terminal classification is status-derived; never
          // cancellable; the title is the server's.
          activityJob({ family: "reindex", id: 21, vault_id: null, status: "running", title: "Embedding reindex (vault 4)" }),
          activityJob({ family: "reindex", id: 22, vault_id: null, status: "failed", title: "Embedding reindex" }),
        ],
        "t1",
        true
      )
    );

    const { result } = renderHook(() => useActivityJobs({ intervalMs: 8000 }));

    await waitFor(
      () => {
        expect(result.current.rows).toHaveLength(10);
      },
      { timeout: 8_000 }
    );

    const wiki = result.current.rows.filter((r) => r.family === "wiki");
    expect(wiki.map((r) => r.key).sort()).toEqual(["wiki-7", "wiki-8"]);
    const running = wiki.find((r) => r.key === "wiki-7");
    expect(running?.title).toBe("Wiki compile");
    expect(running?.cancellable).toBe(true);
    expect(running?.retryable).toBe(false);
    expect(running?.terminal).toBe(false);
    const failed = wiki.find((r) => r.key === "wiki-8");
    expect(failed?.title).toBe("compile exploded");
    expect(failed?.retryable).toBe(true);
    expect(failed?.terminal).toBe(true);

    const kms = result.current.rows.filter((r) => r.family === "kms");
    expect(kms.map((r) => r.key)).toEqual(["kms-11"]);
    expect(kms[0]?.title).toBe("KMS recompile");
    expect(kms[0]?.phase).toBe("running");
    expect(kms[0]?.cancellable).toBe(false);
    expect(kms[0]?.retryable).toBe(false);
    expect(kms[0]?.jobId).toBe(11);

    const drafts = result.current.rows.filter((r) => r.family === "draft-room");
    expect(drafts.map((r) => r.key).sort()).toEqual(["draft-5-job-9", "draft-6-job-10"]);
    const staged = drafts.find((r) => r.key === "draft-5-job-9");
    expect(staged?.title).toBe("Q3 report");
    expect(staged?.phase).toBe("compiling");
    expect(staged?.cancellable).toBe(true);
    expect(staged?.draftId).toBe(5);
    expect(staged?.jobId).toBe(9);
    const stageless = drafts.find((r) => r.key === "draft-6-job-10");
    expect(stageless?.phase).toBe("queued"); // active_stage ?? status

    const ingest = result.current.rows.filter((r) => r.family === "ingest");
    expect(ingest.map((r) => r.key).sort()).toEqual(["ingest-42", "ingest-43", "ingest-44"]);
    const phaseRow = ingest.find((r) => r.fileId === "42");
    expect(phaseRow?.phase).toBe("embedding"); // real documentProgress vocabulary
    expect(phaseRow?.cancellable).toBe(true);
    expect(phaseRow?.retryable).toBe(false);
    expect(ingest.find((r) => r.fileId === "43")?.phase).toBe("Chunking document"); // message outranks phase
    expect(ingest.find((r) => r.fileId === "44")?.phase).toBe("Waiting"); // vocabulary fallback

    const reindex = result.current.rows.filter((r) => r.family === "reindex");
    expect(reindex.map((r) => r.key).sort()).toEqual(["reindex-21", "reindex-22"]);
    const live = reindex.find((r) => r.jobId === 21);
    expect(live?.title).toBe("Embedding reindex (vault 4)");
    expect(live?.phase).toBe("running");
    expect(live?.terminal).toBe(false);
    expect(live?.cancellable).toBe(false);
    const dead = reindex.find((r) => r.jobId === 22);
    expect(dead?.terminal).toBe(true);
  });

  it("retains at most three terminal rows PER FAMILY across a multi-family backlog (one family's backlog cannot spend another's budget)", async () => {
    // Server order is newest-first per family. Both families carry a full
    // terminal backlog; a single global retention pass would keep three rows
    // TOTAL and blank one family — the per-family budget is the guarantee.
    mockGetActivityJobs.mockResolvedValue(
      activityResponse([
        ...[5, 4, 3, 2, 1].map((id) =>
          activityJob({ family: "wiki", id, vault_id: 3, status: "completed", title: "Wiki compile" })
        ),
        ...[5, 4, 3, 2, 1].map((id) =>
          activityJob({ family: "kms", id, vault_id: 3, status: "completed", title: "KMS recompile" })
        ),
      ])
    );

    const { result } = renderHook(() => useActivityJobs({ intervalMs: 8000 }));

    await waitFor(
      () => {
        expect(result.current.rows).toHaveLength(6);
      },
      { timeout: 8_000 }
    );
    // Each family keeps its OWN three most recent terminal rows (family job
    // ids are globally autoincrement, so descending id is global recency).
    const wiki = result.current.rows.filter((r) => r.family === "wiki");
    expect(wiki.map((r) => r.key)).toEqual(["wiki-5", "wiki-4", "wiki-3"]);
    expect(wiki.every((r) => r.terminal)).toBe(true);
    const kms = result.current.rows.filter((r) => r.family === "kms");
    expect(kms.map((r) => r.key)).toEqual(["kms-5", "kms-4", "kms-3"]);
    expect(kms.every((r) => r.terminal)).toBe(true);
  });

  it("keeps live rows uncapped alongside the per-family terminal budget", async () => {
    mockGetActivityJobs.mockResolvedValue(
      activityResponse([
        // Four LIVE wiki rows (beyond the terminal budget of three) plus a
        // five-row terminal backlog, newest-first.
        ...[1, 2, 3, 4].map((id) =>
          activityJob({ family: "wiki", id, vault_id: 3, status: "running", title: "Wiki compile" })
        ),
        ...[15, 14, 13, 12, 11].map((id) =>
          activityJob({ family: "wiki", id, vault_id: 3, status: "completed", title: "Wiki compile" })
        ),
      ])
    );

    const { result } = renderHook(() => useActivityJobs({ intervalMs: 8000 }));

    await waitFor(
      () => {
        expect(result.current.rows).toHaveLength(7);
      },
      { timeout: 8_000 }
    );
    // All live rows survive (never capped), followed by the three most
    // recent terminal rows by descending job id.
    expect(result.current.rows.map((r) => r.key)).toEqual([
      "wiki-1",
      "wiki-2",
      "wiki-3",
      "wiki-4",
      "wiki-15",
      "wiki-14",
      "wiki-13",
    ]);
  });

  it("coalesces a refresh issued while a tick is in flight (does not drop it, never dispatches concurrently)", async () => {
    let releaseAggregate!: () => void;
    mockGetActivityJobs.mockImplementation(
      () =>
        new Promise((resolve) => {
          releaseAggregate = () => resolve(activityResponse());
        })
    );

    const { result } = renderHook(() => useActivityJobs({ intervalMs: 8000 }));

    // First tick starts; the aggregate fetch is parked in flight.
    await waitFor(() => expect(mockGetActivityJobs).toHaveBeenCalledTimes(1), {
      timeout: 8_000,
    });

    // refresh() during the in-flight tick must be coalesced, not dropped AND
    // not dispatched concurrently: the count stays at one until the parked
    // fetch settles, then the chain runs exactly one more pass.
    act(() => {
      result.current.refresh();
    });
    expect(mockGetActivityJobs).toHaveBeenCalledTimes(1);
    act(() => {
      releaseAggregate();
    });
    await waitFor(() => expect(mockGetActivityJobs).toHaveBeenCalledTimes(2), {
      timeout: 8_000,
    });
    // The coalesced pass is exactly one extra dispatch — no third call (the
    // next scheduled tick is intervalMs away).
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockGetActivityJobs).toHaveBeenCalledTimes(2);
  });

  it("keeps the last snapshot when the aggregate call rejects (no row clearing; chain stays alive)", async () => {
    vi.useFakeTimers();
    // First tick lands a live wiki row; every later aggregate call rejects
    // (403-shaped, network-shaped — the lane does not care: no per-family
    // degradation and no client-side backoff exist any more).
    mockGetActivityJobs
      .mockResolvedValueOnce(
        activityResponse(
          [activityJob({ family: "wiki", id: 31, vault_id: 1, status: "running", title: "Wiki compile" })],
          "t1",
          true
        )
      )
      .mockRejectedValue(Object.assign(new Error("503 upstream"), { status: 503 }));

    const { result } = renderHook(() => useActivityJobs({ intervalMs: 1000 }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.rows.map((r) => r.key)).toEqual(["wiki-31"]);

    // Two more ticks dispatch and reject: rows keep the last good snapshot
    // instead of clearing, and the chain never stops.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(mockGetActivityJobs.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(result.current.rows.map((r) => r.key)).toEqual(["wiki-31"]);
    expect(result.current.loading).toBe(false);
  });

  it("floors caller-supplied intervals at 1s (no setTimeout(0) hot loop)", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useActivityJobs({ intervalMs: 0 }));
    // First tick runs synchronously on mount; flush its promises.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const callsAfterFirstTick = mockGetActivityJobs.mock.calls.length;
    expect(callsAfterFirstTick).toBeGreaterThanOrEqual(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    // 3s at a floored 1s cadence = up to 3 more ticks — NOT the hundreds a
    // setTimeout(fn, 0) hot loop would produce.
    expect(mockGetActivityJobs.mock.calls.length).toBeLessThanOrEqual(
      callsAfterFirstTick + 4
    );
    expect(result.current.rows).toEqual([]);
  });

  it("stops polling after unmount (fetch count frozen; no post-unmount work)", async () => {
    const { unmount } = renderHook(() => useActivityJobs({ intervalMs: 1000 }));
    // First tick is immediate; wait for it to settle.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const callsAtUnmount = mockGetActivityJobs.mock.calls.length;
    expect(callsAtUnmount).toBeGreaterThanOrEqual(1);
    unmount();
    // A real (floored) interval would fire within a second if the chain
    // survived; assert the fetch count is frozen well past that point.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1200));
    });
    expect(mockGetActivityJobs.mock.calls.length).toBe(callsAtUnmount);
  });
});

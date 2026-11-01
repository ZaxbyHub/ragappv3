// frontend/src/hooks/useJobStatus.m04.test.ts
// Issue #784 ([Workstream M] PR 4 of 4) — unit coverage for the Activity
// tray's data source: the useActivityJobs aggregation and its five family
// adapters. The frozen component-level contract lives in
// ActivityTray.m04.test.tsx / PageShell.m04.test.tsx; this file pins the
// ADAPTER semantics: vault fan-out flattening, the pending+processing
// ingest union dedupe, 403 tolerance for gated families, the draft
// two-hop fan-out, GLOBAL-recency terminal retention (including the
// two-vault regression for the per-parent backlog bug), refresh
// coalescing, the interval floor, and unmount safety of the poll loop.
//
// Feedback round 853-20261005: mocks are reset in beforeEach (clearMocks
// only clears call history); the ingest phase assertion exercises the REAL
// documentProgress vocabulary; the retention fixture is server-ordered
// newest-first like the real lists.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

const {
  mockListDocuments,
  mockListReindexJobs,
  mockListKMSJobs,
  mockListDrafts,
  mockListDraftJobs,
  mockVaultState,
} = vi.hoisted(() => ({
  mockListDocuments: vi.fn(),
  mockListReindexJobs: vi.fn(),
  mockListKMSJobs: vi.fn(),
  mockListDrafts: vi.fn(),
  mockListDraftJobs: vi.fn(),
  mockVaultState: vi.fn(),
}));

vi.mock("@/lib/api/core", () => ({
  listDocuments: mockListDocuments,
  listReindexJobs: mockListReindexJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  listWikiJobs: vi.fn(),
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
  // getState the tests drive through mockReturnValue.
  useVaultStore: Object.assign(() => ({}), { getState: mockVaultState }),
}));

// The REAL documentProgress runs (no identity mock): ingest phase asserts
// the family's own phase vocabulary through it (PRR-016).
import { useActivityJobs } from "./useJobStatus";
import { listWikiJobs } from "@/lib/api/wiki";

function doc(id: string, status: string, filename = `${id}.txt`) {
  return {
    id,
    filename,
    vault_id: 1,
    metadata: { status, phase: "embedding" },
  };
}

function wikiJob(id: number, status: string, error: string | null = null) {
  return {
    id,
    vault_id: 3,
    trigger_type: "manual",
    trigger_id: null,
    status,
    error,
    result_json: "{}",
    created_at: `2026-01-01T00:00:00Z`,
    started_at: null,
    completed_at: null,
    retry_count: 0,
  };
}

function kmsJob(id: number, status: string) {
  return { ...wikiJob(id, status), vault_id: 3 };
}

function reindexJob(id: number, status: string, vaultId: number | null = 1) {
  return {
    id,
    vault_id: vaultId,
    trigger_type: "api",
    trigger_id: "1",
    status,
    error: null,
    result_json: "{}",
    input_json: "{}",
    retry_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    completed_at: null,
  };
}

function draftJob(id: number, draftId: number, status: string, activeStage: string | null = null) {
  return {
    id,
    draft_id: draftId,
    job_type: "compile",
    status,
    active_stage: activeStage,
  };
}

beforeEach(() => {
  mockVaultState.mockReturnValue({
    vaults: [],
    loading: false,
    fetchVaults: vi.fn().mockResolvedValue(undefined),
  });
  mockListDocuments.mockReset();
  mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
  mockListKMSJobs.mockReset();
  mockListKMSJobs.mockResolvedValue({ jobs: [] });
  mockListDrafts.mockReset();
  mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mockListDraftJobs.mockReset();
  mockListDraftJobs.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
  mockListReindexJobs.mockReset();
  mockListReindexJobs.mockResolvedValue({ jobs: [] });
  const wikiMock = vi.mocked(listWikiJobs);
  wikiMock.mockReset();
  wikiMock.mockResolvedValue({ jobs: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useActivityJobs adapters (issue #784)", () => {
  it("fans wiki jobs out across the accessible vaults and flattens", async () => {
    mockVaultState.mockReturnValue({
      vaults: [{ id: 1 }, { id: 2 }],
      loading: false,
      fetchVaults: vi.fn().mockResolvedValue(undefined),
    });
    const wikiMock = vi.mocked(listWikiJobs);
    wikiMock.mockImplementation(async ({ vault_id }: { vault_id: number }) =>
      vault_id === 1
        ? { jobs: [wikiJob(1, "running")] }
        : { jobs: [wikiJob(2, "failed", "compile exploded")] }
    );

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const wiki = result.current.rows.filter((r) => r.family === "wiki");
        expect(wiki.map((r) => r.key).sort()).toEqual(["wiki-1", "wiki-2"]);
        expect(wikiMock).toHaveBeenCalledWith({ vault_id: 1 });
        expect(wikiMock).toHaveBeenCalledWith({ vault_id: 2 });
      },
      { timeout: 8_000 }
    );
    const failed = result.current.rows.find((r) => r.key === "wiki-2");
    expect(failed?.retryable).toBe(true);
    expect(failed?.title).toBe("compile exploded");
    const running = result.current.rows.find((r) => r.key === "wiki-1");
    expect(running?.cancellable).toBe(true);
    expect(running?.title).toBe("Wiki compile");
  });

  it("keeps a later vault's just-failed job when an earlier vault has a terminal backlog (two-vault retention regression)", async () => {
    // Server lists are newest-first per vault; vault 1 has a full terminal
    // backlog, vault 2 has one FRESH failed job. The pre-fix positional
    // slice kept vault 1's three oldest-terminal rows and dropped wiki-9.
    mockVaultState.mockReturnValue({
      vaults: [{ id: 1 }, { id: 2 }],
      loading: false,
      fetchVaults: vi.fn().mockResolvedValue(undefined),
    });
    const wikiMock = vi.mocked(listWikiJobs);
    wikiMock.mockImplementation(async ({ vault_id }: { vault_id: number }) =>
      vault_id === 1
        ? { jobs: [wikiJob(3, "completed"), wikiJob(2, "completed"), wikiJob(1, "completed")] }
        : { jobs: [wikiJob(9, "failed", "later vault failed")] }
    );

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const wiki = result.current.rows.filter((r) => r.family === "wiki");
        expect(wiki.map((r) => r.key)).toEqual(["wiki-9", "wiki-3", "wiki-2"]);
      },
      { timeout: 8_000 }
    );
    const failed = result.current.rows.find((r) => r.key === "wiki-9");
    expect(failed?.retryable).toBe(true);
    expect(failed?.title).toBe("later vault failed");
  });

  it("dedupes the ingest pending+processing union by file id (real documentProgress vocabulary)", async () => {
    mockListDocuments.mockImplementation(async ({ status }: { status?: string }) =>
      status === "pending"
        ? {
            documents: [doc("42", "pending"), doc("43", "pending")],
            total: 2,
          }
        : { documents: [doc("42", "processing")], total: 1 }
    );

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const ingest = result.current.rows.filter((r) => r.family === "ingest");
        expect(ingest.map((r) => r.fileId).sort()).toEqual(["42", "43"]);
      },
      { timeout: 8_000 }
    );
    const row = result.current.rows.find((r) => r.fileId === "42");
    expect(row?.status).toBe("processing");
    expect(row?.cancellable).toBe(true);
    expect(row?.retryable).toBe(false);
    // The real documentProgress maps metadata.phase through its vocabulary.
    expect(row?.phase).toBe("embedding");
  });

  it("bounds the ingest pagination (stops at the page budget, dedupes across pages)", async () => {
    // total exceeds one page: the adapter pages until the budget, and the
    // union must not double-count a document returned on two pages.
    mockListDocuments.mockImplementation(
      async ({ status, page }: { status?: string; page?: number }) => {
        const p = page ?? 1;
        const document = doc(`${status}-${p}`, status);
        return { documents: [document], total: 1000 };
      }
    );

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        expect(mockListDocuments).toHaveBeenCalledWith({
          status: "pending",
          perPage: 200,
          page: 3,
        });
      },
      { timeout: 8_000 }
    );
    // Page budget reached: exactly 3 pages per status, no 4th call.
    expect(mockListDocuments).not.toHaveBeenCalledWith({
      status: "pending",
      perPage: 200,
      page: 4,
    });
    const ingest = result.current.rows.filter((r) => r.family === "ingest");
    expect(ingest).toHaveLength(6); // 3 pages x 2 statuses
  });

  it("drops the kms family silently when every vault call rejects (403)", async () => {
    mockVaultState.mockReturnValue({
      vaults: [{ id: 9 }],
      loading: false,
      fetchVaults: vi.fn().mockResolvedValue(undefined),
    });
    const wikiMock = vi.mocked(listWikiJobs);
    wikiMock.mockResolvedValue({ jobs: [] });
    mockListKMSJobs.mockRejectedValue(new Error("403 kms disabled"));

    const { result } = renderHook(() => useActivityJobs());

    // Wait on the sibling wiki call (same vault fan-out) rather than on
    // empty rows — rows start empty, so that wait would be vacuous before
    // the first tick lands. The rejecting kms mock leaves zero kms rows,
    // which is the behaviour under test.
    await waitFor(() => expect(wikiMock).toHaveBeenCalledWith({ vault_id: 9 }), {
      timeout: 8_000,
    });
    await waitFor(
      () => {
        expect(result.current.loading).toBe(false);
      },
      { timeout: 8_000 }
    );
    expect(result.current.rows.filter((r) => r.family === "kms")).toHaveLength(0);
  });

  it("maps kms rows as non-cancellable status rows", async () => {
    mockVaultState.mockReturnValue({
      vaults: [{ id: 1 }],
      loading: false,
      fetchVaults: vi.fn().mockResolvedValue(undefined),
    });
    mockListKMSJobs.mockResolvedValue({ jobs: [kmsJob(11, "running")] });

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const kms = result.current.rows.filter((r) => r.family === "kms");
        expect(kms).toHaveLength(1);
      },
      { timeout: 8_000 }
    );
    const row = result.current.rows.find((r) => r.family === "kms");
    expect(row?.title).toBe("KMS recompile");
    expect(row?.cancellable).toBe(false);
    expect(row?.retryable).toBe(false);
    expect(row?.phase).toBe("running");
    expect(row?.jobId).toBe(11);
  });

  it("maps reindex rows (the family backed by the new list route)", async () => {
    mockListReindexJobs.mockResolvedValue({
      jobs: [reindexJob(21, "running", 4), reindexJob(22, "failed", null)],
    });

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const reindex = result.current.rows.filter((r) => r.family === "reindex");
        expect(reindex).toHaveLength(2);
      },
      { timeout: 8_000 }
    );
    const scoped = result.current.rows.find((r) => r.jobId === 21);
    expect(scoped?.title).toBe("Embedding reindex (vault 4)");
    expect(scoped?.phase).toBe("running");
    expect(scoped?.cancellable).toBe(false);
    const unscoped = result.current.rows.find((r) => r.jobId === 22);
    expect(unscoped?.title).toBe("Embedding reindex");
    expect(unscoped?.terminal).toBe(true);
  });

  it("walks the draft two-hop fan-out and keeps sibling drafts whose job call rejects", async () => {
    mockListDrafts.mockResolvedValue({
      items: [
        { id: 5, title: "Q3 report" },
        { id: 6, title: "Broken draft" },
      ],
      total: 2,
      page: 1,
      per_page: 10,
    });
    mockListDraftJobs.mockImplementation(async (draftId: number) => {
      if (draftId === 6) throw new Error("403 draft job read");
      return {
        items: [draftJob(9, 5, "running", "compiling")],
        total: 1,
        page: 1,
        per_page: 20,
      };
    });

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const draft = result.current.rows.find((r) => r.family === "draft-room");
        expect(draft).toBeDefined();
      },
      { timeout: 8_000 }
    );
    const row = result.current.rows.find((r) => r.family === "draft-room");
    expect(row?.draftId).toBe(5);
    expect(row?.jobId).toBe(9);
    expect(row?.title).toBe("Q3 report");
    expect(row?.phase).toBe("compiling");
    expect(row?.cancellable).toBe(true);
    expect(result.current.rows.filter((r) => r.family === "draft-room")).toHaveLength(1);
  });

  it("keeps at most three terminal rows per family by global recency (server order is newest-first)", async () => {
    mockVaultState.mockReturnValue({
      vaults: [{ id: 1 }],
      loading: false,
      fetchVaults: vi.fn().mockResolvedValue(undefined),
    });
    const wikiMock = vi.mocked(listWikiJobs);
    // Real server order: newest-first.
    wikiMock.mockResolvedValue({
      jobs: [
        wikiJob(5, "completed"),
        wikiJob(4, "completed"),
        wikiJob(3, "completed"),
        wikiJob(2, "completed"),
        wikiJob(1, "running"),
      ],
    });

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const wiki = result.current.rows.filter((r) => r.family === "wiki");
        expect(wiki).toHaveLength(4);
      },
      { timeout: 8_000 }
    );
    const wiki = result.current.rows.filter((r) => r.family === "wiki");
    expect(wiki.map((r) => r.key)).toEqual(["wiki-1", "wiki-5", "wiki-4", "wiki-3"]);
  });

  it("coalesces a refresh issued while a tick is in flight (does not drop it)", async () => {
    let releaseReindex!: () => void;
    mockListReindexJobs.mockImplementation(
      () =>
        new Promise((resolve) => {
          releaseReindex = () => resolve({ jobs: [] });
        })
    );

    const { result } = renderHook(() => useActivityJobs());

    // First tick starts; the reindex adapter's fetch is parked in flight.
    await waitFor(() => expect(mockListReindexJobs).toHaveBeenCalledTimes(1), {
      timeout: 8_000,
    });

    // refresh() during the in-flight tick must be coalesced, not dropped:
    // when the parked fetch settles, the chain runs one more full pass.
    act(() => {
      result.current.refresh();
    });
    act(() => {
      releaseReindex();
    });
    await waitFor(() => expect(mockListReindexJobs).toHaveBeenCalledTimes(2), {
      timeout: 8_000,
    });
  });

  it("keeps other families alive when the reindex and ingest adapters reject", async () => {
    // PRR-014 residual legs: per-adapter rejection tolerance for the two
    // "@/lib/api/core" consumers (reindex = admin-gated; ingest = network).
    mockListReindexJobs.mockRejectedValue(new Error("403 admin only"));
    mockListDocuments.mockRejectedValue(new Error("network down"));
    mockVaultState.mockReturnValue({
      vaults: [{ id: 1 }],
      loading: false,
      fetchVaults: vi.fn().mockResolvedValue(undefined),
    });
    const wikiMock = vi.mocked(listWikiJobs);
    wikiMock.mockResolvedValue({ jobs: [wikiJob(31, "running")] });

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        expect(result.current.rows.map((r) => r.family)).toEqual(["wiki"]);
      },
      { timeout: 8_000 }
    );
    expect(result.current.rows[0]?.key).toBe("wiki-31");
    expect(mockListDocuments).toHaveBeenCalled();
  });

  it("floors caller-supplied intervals at 1s (no setTimeout(0) hot loop)", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useActivityJobs({ intervalMs: 0 }));
    // First tick runs synchronously on mount; flush its promises.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const callsAfterFirstTick = mockListReindexJobs.mock.calls.length;
    expect(callsAfterFirstTick).toBeGreaterThanOrEqual(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    // 3s at a floored 1s cadence = up to 3 more ticks — NOT the hundreds a
    // setTimeout(fn, 0) hot loop would produce.
    expect(mockListReindexJobs.mock.calls.length).toBeLessThanOrEqual(
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
    const callsAtUnmount = mockListReindexJobs.mock.calls.length;
    expect(callsAtUnmount).toBeGreaterThanOrEqual(1);
    unmount();
    // A real (floored) interval would fire within a second if the chain
    // survived; assert the fetch count is frozen well past that point.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1200));
    });
    expect(mockListReindexJobs.mock.calls.length).toBe(callsAtUnmount);
  });
});

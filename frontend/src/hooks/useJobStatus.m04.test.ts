// frontend/src/hooks/useJobStatus.m04.test.ts
// Issue #784 ([Workstream M] PR 4 of 4) — unit coverage for the Activity
// tray's data source: the useActivityJobs aggregation and its five family
// adapters. The frozen component-level contract lives in
// ActivityTray.m04.test.tsx / PageShell.m04.test.tsx; this file pins the
// ADAPTER semantics: vault fan-out flattening, the pending+processing
// ingest union dedupe, 403 tolerance for gated families, the draft
// two-hop fan-out, terminal-row retention, and unmount safety of the poll
// loop.

import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

const {
  mockListDocuments,
  mockListReindexJobs,
  mockListWikiJobs,
  mockListKMSJobs,
  mockListDrafts,
  mockListDraftJobs,
  mockVaultState,
} = vi.hoisted(() => ({
  mockListDocuments: vi.fn(),
  mockListReindexJobs: vi.fn(),
  mockListWikiJob: vi.fn(),
  mockListKMSJobs: vi.fn(),
  mockListDrafts: vi.fn(),
  mockListDraftJobs: vi.fn(),
  mockVaultState: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
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

vi.mock("@/components/documents/documentProgress", () => ({
  documentProgress: (doc: { metadata?: { status?: string } }) => ({
    label: doc.metadata?.status ?? "Waiting",
  }),
}));

import { useActivityJobs } from "./useJobStatus";
import { listWikiJobs } from "@/lib/api/wiki";

function doc(id: string, status: string, filename = `${id}.txt`) {
  return {
    id,
    filename,
    vault_id: 1,
    metadata: { status },
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
    created_at: `2026-01-0${id}T00:00:00Z`,
    started_at: null,
    completed_at: null,
    retry_count: 0,
  };
}

describe("useActivityJobs adapters (issue #784)", () => {
  it("fans wiki jobs out across the accessible vaults and flattens", { timeout: 15_000 }, async () => {
    mockVaultState.mockReturnValue({ vaults: [{ id: 1 }, { id: 2 }] });
    const wikiMock = vi.mocked(listWikiJobs);
    wikiMock.mockImplementation(async ({ vault_id }: { vault_id: number }) =>
      vault_id === 1
        ? { jobs: [wikiJob(1, "running")] }
        : { jobs: [wikiJob(2, "failed", "compile exploded")] }
    );
    mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
    mockListKMSJobs.mockResolvedValue({ jobs: [] });
    mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
    mockListReindexJobs.mockResolvedValue({ jobs: [] });

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

  it("dedupes the ingest pending+processing union by file id", { timeout: 15_000 }, async () => {
    mockVaultState.mockReturnValue({ vaults: [] });
    mockListWikiJobSafe();
    mockListDocuments.mockImplementation(async ({ status }: { status?: string }) =>
      status === "pending"
        ? { documents: [doc("42", "pending"), doc("43", "pending")], total: 2 }
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
    expect(row?.phase).toBe("processing");
  });

  it("drops the kms family silently when every vault call rejects (403)", { timeout: 15_000 }, async () => {
    mockVaultState.mockReturnValue({ vaults: [{ id: 9 }] });
    const wikiMock = vi.mocked(listWikiJobs);
    wikiMock.mockResolvedValue({ jobs: [] });
    mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
    mockListKMSJobs.mockRejectedValue(new Error("403 kms disabled"));
    mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
    mockListReindexJobs.mockResolvedValue({ jobs: [] });

    const { result } = renderHook(() => useActivityJobs());

    // Wait on the sibling wiki call (same vault fan-out) rather than on
    // empty rows — rows start empty, so that wait would be vacuous before
    // the first tick lands. The rejecting kms mock leaves zero kms rows,
    // which is the behaviour under test.
    await waitFor(() => expect(wikiMock).toHaveBeenCalledWith({ vault_id: 9 }), {
      timeout: 8_000,
    });
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    }, { timeout: 8_000 });
    expect(result.current.rows.filter((r) => r.family === "kms")).toHaveLength(0);
  });

  it("walks the draft two-hop fan-out and carries draftId+jobId", { timeout: 15_000 }, async () => {
    mockVaultState.mockReturnValue({ vaults: [] });
    mockListWikiJobSafe();
    mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
    mockListDrafts.mockResolvedValue({
      items: [{ id: 5, title: "Q3 report" }],
      total: 1,
      page: 1,
      per_page: 10,
    });
    mockListDraftJobs.mockResolvedValue({
      items: [
        {
          id: 9,
          draft_id: 5,
          job_type: "compile",
          status: "running",
          active_stage: "compiling",
        },
      ],
      total: 1,
      page: 1,
      per_page: 20,
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
  });

  it("keeps at most three terminal rows per family (newest first from the server)", { timeout: 15_000 }, async () => {
    mockVaultState.mockReturnValue({ vaults: [{ id: 1 }] });
    const wikiMock = vi.mocked(listWikiJobs);
    wikiMock.mockResolvedValue({
      jobs: [
        wikiJob(1, "completed"),
        wikiJob(2, "completed"),
        wikiJob(3, "completed"),
        wikiJob(4, "completed"),
        wikiJob(5, "running"),
      ],
    });
    mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
    mockListKMSJobs.mockResolvedValue({ jobs: [] });
    mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
    mockListReindexJobs.mockResolvedValue({ jobs: [] });

    const { result } = renderHook(() => useActivityJobs());

    await waitFor(
      () => {
        const wiki = result.current.rows.filter((r) => r.family === "wiki");
        expect(wiki).toHaveLength(4);
      },
      { timeout: 8_000 }
    );
    const wiki = result.current.rows.filter((r) => r.family === "wiki");
    expect(wiki.map((r) => r.key)).toEqual(["wiki-5", "wiki-1", "wiki-2", "wiki-3"]);
  });

  it("stops polling after unmount without crashing", { timeout: 15_000 }, async () => {
    mockVaultState.mockReturnValue({ vaults: [] });
    mockListWikiJobSafe();
    mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
    mockListReindexJobs.mockResolvedValue({ jobs: [] });

    const { unmount } = renderHook(() => useActivityJobs({ intervalMs: 5 }));
    await act(async () => {
      await Promise.resolve();
    });
    unmount();
    // Let any latent timer fire; a crash or state update would fail the test.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  });
});

function mockListWikiJobSafe() {
  const wikiMock = vi.mocked(listWikiJobs);
  wikiMock.mockReset();
  wikiMock.mockResolvedValue({ jobs: [] });
  mockListKMSJobs.mockReset();
  mockListKMSJobs.mockResolvedValue({ jobs: [] });
  mockListDrafts.mockReset();
  mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mockListDraftJobs.mockReset();
  mockListReindexJobs.mockReset();
  mockListReindexJobs.mockResolvedValue({ jobs: [] });
}

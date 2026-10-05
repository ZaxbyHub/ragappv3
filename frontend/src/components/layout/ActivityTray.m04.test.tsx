// frontend/src/components/layout/ActivityTray.m04.test.tsx
// Issue-trace 784-activity-tray-shell-job-center — acceptance checks
// C2-C6 (AC1/AC3/AC4/AC5).
//
// The Activity tray surfaces every running background job — all five
// families (ingest / wiki / draft-room / kms / reindex) — in one shell-level
// region, sourced from the useJobStatus aggregation export (useActivityJobs),
// with Cancel/Retry actions that call the SERVER clients from the tray
// component itself. Ingest rows come from server-side job state, so they
// must survive an EMPTY client upload store (uploads are not the source of
// truth any more). KMS completions must show in the tray even when the user
// is not on the KMS page (the tray is the only cross-page surface).
//
// Expected pre-fix (base) verdicts (frontend/src/components/layout/
// ActivityTray.tsx does not exist):
//   C2-C6 RED with "Failed to resolve import" mentioning ./ActivityTray.
//
// Mock idioms reused (not invented):
// - vi.hoisted vi.fn() factories + vi.mock module replacement, per the
//   #783 precedent KMSPage.m03.test.tsx (the factory defines the mocked
//   module surface).
// - The action clients are mocked at their SOURCE modules (@/lib/api/wiki,
//   @/lib/api/draftRoom, @/lib/api) because the pinned contract has the
//   TRAY — not the hook — importing and calling them.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const {
  mockUseActivityJobs,
  mockCancelWikiJob,
  mockRetryWikiJob,
  mockCancelDraftJob,
  mockRetryDraftJob,
  mockCancelIngest,
} = vi.hoisted(() => ({
  mockUseActivityJobs: vi.fn(),
  mockCancelWikiJob: vi.fn(),
  mockRetryWikiJob: vi.fn(),
  mockCancelDraftJob: vi.fn(),
  mockRetryDraftJob: vi.fn(),
  mockCancelIngest: vi.fn(),
}));

vi.mock("@/hooks/useJobStatus", () => ({
  useActivityJobs: mockUseActivityJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  cancelWikiJob: mockCancelWikiJob,
  retryWikiJob: mockRetryWikiJob,
}));

vi.mock("@/lib/api/draftRoom", () => ({
  cancelDraftJob: mockCancelDraftJob,
  retryDraftJob: mockRetryDraftJob,
}));

vi.mock("@/lib/api", () => ({
  cancelDocumentIngest: mockCancelIngest,
}));

// Empty-stores zustand stand-in: the REAL initial useUploadStore shape
// (uploads: [], no transfers, no chat attachments). Selector-aware so both
// `useUploadStore()` and `useUploadStore((s) => s.uploads)` call shapes
// work; phaseLabelFor passes the raw phase through (the tray's own
// vocabulary mapping is not this file's concern).
vi.mock("@/stores/useUploadStore", () => {
  const emptyUploadState = {
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],
  };
  return {
    useUploadStore: (selector?: (state: typeof emptyUploadState) => unknown) =>
      selector ? selector(emptyUploadState) : emptyUploadState,
    phaseLabelFor: (phase?: string | null) => phase ?? null,
  };
});

import { ActivityTray } from "./ActivityTray";

// Structural mirror of the pinned contract's ActivityJobRow (exported from
// @/hooks/useJobStatus by the implementation; declared locally here so the
// file carries no import that would add failure noise beyond the missing
// component module).
interface ActivityJobRowLike {
  family: "ingest" | "wiki" | "draft-room" | "kms" | "reindex";
  key: string;
  title: string;
  phase: string;
  status: string;
  terminal: boolean;
  cancellable: boolean;
  retryable: boolean;
  jobId?: number;
  vaultId?: number;
  draftId?: number;
  fileId?: string;
}

function makeRow(overrides: Partial<ActivityJobRowLike> = {}): ActivityJobRowLike {
  return {
    family: "ingest",
    key: "row-1",
    title: "Row 1",
    phase: "running",
    status: "running",
    terminal: false,
    cancellable: false,
    retryable: false,
    ...overrides,
  };
}

// The hook's return value under test. Kept in a live variable read by the
// mockImplementation so rerenders (C6's completion flip) observe updates.
let hookState: { rows: ActivityJobRowLike[]; loading: boolean };

function mockHappyPath(rows: ActivityJobRowLike[], loading = false) {
  hookState = { rows, loading };
  mockUseActivityJobs.mockImplementation(() => hookState);
}

/** The rendered listitem row whose text carries `family`. */
function rowByFamily(family: string): HTMLElement {
  const row = screen
    .getAllByRole("listitem")
    .find((li) => li.textContent?.toLowerCase().includes(family.toLowerCase()));
  expect(row, `no listitem row found for family ${family}`).toBeDefined();
  return row as HTMLElement;
}

beforeEach(() => {
  hookState = { rows: [], loading: false };
  mockUseActivityJobs.mockImplementation(() => hookState);
  mockCancelWikiJob.mockResolvedValue({ job_id: 7, status: "cancelled" });
  mockRetryWikiJob.mockResolvedValue({ job_id: 7, status: "pending" });
  mockCancelDraftJob.mockResolvedValue({});
  mockRetryDraftJob.mockResolvedValue({});
  mockCancelIngest.mockResolvedValue({ ok: true });
});

describe("ActivityTray m04 (issue-trace 784-activity-tray-shell-job-center)", () => {
  it("lists one row per running job across all five families", () => {
    mockHappyPath([
      makeRow({
        family: "ingest",
        key: "ingest-42",
        title: "handbook.pdf",
        phase: "embedding",
        status: "processing",
        cancellable: true,
        fileId: "42",
      }),
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        phase: "running",
        status: "running",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
      makeRow({
        family: "draft-room",
        key: "draft-5",
        title: "Q3 report draft",
        phase: "compiling",
        status: "running",
        cancellable: true,
        draftId: 5,
        jobId: 9,
      }),
      makeRow({
        family: "kms",
        key: "kms-11",
        title: "KMS recompile",
        phase: "running",
        status: "running",
      }),
      makeRow({
        family: "reindex",
        key: "reindex-3",
        title: "Embedding reindex",
        phase: "embedding",
        status: "running",
      }),
    ]);

    render(<ActivityTray />);

    // One row per family, no more, no less.
    expect(screen.getAllByRole("listitem").length).toBe(5);

    // Every family name is visible in the tray.
    for (const family of ["ingest", "wiki", "draft-room", "kms", "reindex"]) {
      expect(
        screen.getAllByText(new RegExp(family, "i")).length,
        `family ${family} should appear in the tray`
      ).toBeGreaterThan(0);
    }

    // The ingest row carries its phase text (documentProgress vocabulary:
    // phase strings like "embedding").
    expect(rowByFamily("ingest").textContent).toMatch(/embedding/i);
  });

  it("ingest row survives an empty client upload store", () => {
    // ONE server-side ingest job; the client upload store is empty (the
    // module mock above is the real initial shape: uploads: []).
    mockHappyPath([
      makeRow({
        family: "ingest",
        key: "ingest-42",
        title: "handbook.pdf",
        phase: "parsing",
        status: "processing",
        cancellable: true,
        fileId: "42",
      }),
    ]);

    render(<ActivityTray />);

    expect(screen.getAllByRole("listitem").length).toBe(1);
    const row = rowByFamily("ingest");
    expect(within(row).getByText(/ingest/i)).toBeInTheDocument();
  });

  it("cancel reaches the server for wiki draft-room and ingest", async () => {
    const user = userEvent.setup();
    mockHappyPath([
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        phase: "running",
        status: "running",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
      makeRow({
        family: "draft-room",
        key: "draft-5",
        title: "Q3 report draft",
        phase: "compiling",
        status: "running",
        cancellable: true,
        draftId: 5,
        jobId: 9,
      }),
      makeRow({
        family: "ingest",
        key: "ingest-42",
        title: "handbook.pdf",
        phase: "parsing",
        status: "processing",
        cancellable: true,
        fileId: "42",
      }),
    ]);

    render(<ActivityTray />);

    await user.click(
      within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i })
    );
    await user.click(
      within(rowByFamily("draft-room")).getByRole("button", { name: /cancel/i })
    );
    await user.click(
      within(rowByFamily("ingest")).getByRole("button", { name: /cancel/i })
    );

    await waitFor(() => {
      expect(mockCancelWikiJob).toHaveBeenCalledTimes(1);
      expect(mockCancelWikiJob).toHaveBeenCalledWith(7, 3);
    });
    await waitFor(() => {
      expect(mockCancelDraftJob).toHaveBeenCalledTimes(1);
      expect(mockCancelDraftJob).toHaveBeenCalledWith(5, 9);
    });
    await waitFor(() => {
      expect(mockCancelIngest).toHaveBeenCalledTimes(1);
      expect(mockCancelIngest).toHaveBeenCalledWith("42");
    });
  });

  it("retry reaches the server for failed jobs", async () => {
    const user = userEvent.setup();
    mockHappyPath([
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        phase: "failed",
        status: "failed",
        terminal: true,
        retryable: true,
        jobId: 7,
        vaultId: 3,
      }),
      makeRow({
        family: "draft-room",
        key: "draft-5",
        title: "Q3 report draft",
        phase: "failed",
        status: "failed",
        terminal: true,
        retryable: true,
        draftId: 5,
        jobId: 9,
      }),
      // A failed but NON-retryable row: Retry must not be offered on it.
      makeRow({
        family: "ingest",
        key: "ingest-42",
        title: "handbook.pdf",
        phase: "failed",
        status: "failed",
        terminal: true,
        retryable: false,
        fileId: "42",
      }),
    ]);

    render(<ActivityTray />);

    // Retry is NOT offered on the non-retryable row.
    expect(
      within(rowByFamily("ingest")).queryByRole("button", { name: /retry/i })
    ).not.toBeInTheDocument();

    await user.click(
      within(rowByFamily("wiki")).getByRole("button", { name: /retry/i })
    );
    await user.click(
      within(rowByFamily("draft-room")).getByRole("button", { name: /retry/i })
    );

    await waitFor(() => {
      expect(mockRetryWikiJob).toHaveBeenCalledTimes(1);
      expect(mockRetryWikiJob).toHaveBeenCalledWith(7, 3);
    });
    await waitFor(() => {
      expect(mockRetryDraftJob).toHaveBeenCalledTimes(1);
      expect(mockRetryDraftJob).toHaveBeenCalledWith(5, 9);
    });
  });

  it("KMS completion shows in the tray off the KMS page", () => {
    // Only the tray is mounted — no KMSPage anywhere in this render tree,
    // which is exactly the point: a KMS job that completes while the user
    // is on another page still surfaces in the tray.
    mockHappyPath([
      makeRow({
        family: "kms",
        key: "kms-11",
        title: "KMS recompile",
        phase: "running",
        status: "running",
      }),
    ]);

    const view = render(<ActivityTray />);
    expect(screen.getAllByRole("listitem").length).toBe(1);
    expect(rowByFamily("kms").textContent).not.toMatch(/completed/i);

    // The poll flips the job to its terminal completion.
    mockHappyPath([
      makeRow({
        family: "kms",
        key: "kms-11",
        title: "KMS recompile",
        phase: "completed",
        status: "completed",
        terminal: true,
      }),
    ]);
    view.rerender(<ActivityTray />);

    const kmsRow = rowByFamily("kms");
    expect(within(kmsRow).getByText(/completed/i)).toBeInTheDocument();
  });
});

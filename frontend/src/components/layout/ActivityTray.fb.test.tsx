// frontend/src/components/layout/ActivityTray.fb.test.tsx
// Feedback round 853-20261005 (PR #853 swarm review): assertions the
// frozen m04 contract does not carry and that must live OUTSIDE the frozen
// files (they are byte-locked by the trace checkpoint). Covers:
// - PRR-041: kms/reindex rows render NO Cancel and failed ingest rows
//   render no Retry (negative-button assertions).
// - PRR-042: family labels are asserted ROW-SCOPED, not document-scoped.
// - PRR-043 / PRR-006 / PRR-005: the Hide control collapses the list;
//   a clicked action's button is disabled while in flight; a Cancel
//   refused with 403 disappears from that row (learned affordance); a
//   non-403 failure surfaces a toast.
// - PRR-019: source tripwire — the tray must never import the client
//   upload store (the "server-sourced" constraint regression guard).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const {
  mockUseActivityJobs,
  mockCancelWikiJob,
  mockRetryWikiJob,
  mockToastError,
} = vi.hoisted(() => ({
  mockUseActivityJobs: vi.fn(),
  mockCancelWikiJob: vi.fn(),
  mockRetryWikiJob: vi.fn(),
  mockToastError: vi.fn(),
}));

vi.mock("@/hooks/useJobStatus", () => ({
  useActivityJobs: mockUseActivityJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  cancelWikiJob: mockCancelWikiJob,
  retryWikiJob: mockRetryWikiJob,
}));

vi.mock("@/lib/api/draftRoom", () => ({
  cancelDraftJob: vi.fn(),
  retryDraftJob: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  cancelDocumentIngest: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { error: mockToastError, success: vi.fn(), info: vi.fn() },
}));

// Empty-stores zustand stand-in (the tray must not read it at all — the
// source tripwire below asserts that; this mock only keeps an accidental
// import from crashing the test instead of silently passing).
vi.mock("@/stores/useUploadStore", () => {
  const emptyUploadState = { uploads: [] };
  return {
    useUploadStore: (selector?: (state: typeof emptyUploadState) => unknown) =>
      selector ? selector(emptyUploadState) : emptyUploadState,
  };
});

import { ActivityTray } from "./ActivityTray";

interface RowLike {
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
  fileId?: string;
}

function makeRow(overrides: Partial<RowLike> = {}): RowLike {
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

let hookRows: RowLike[];

function rowByFamily(family: string): HTMLElement {
  const row = screen
    .getAllByRole("listitem")
    .find((li) => li.textContent?.toLowerCase().includes(family.toLowerCase()));
  expect(row, `no listitem row found for family ${family}`).toBeDefined();
  return row as HTMLElement;
}

beforeEach(() => {
  hookRows = [];
  mockUseActivityJobs.mockImplementation(() => ({
    rows: hookRows,
    loading: false,
    refresh: vi.fn(),
  }));
  mockCancelWikiJob.mockReset();
  mockRetryWikiJob.mockReset();
  mockToastError.mockReset();
});

describe("ActivityTray feedback-round assertions (853-20261005)", () => {
  it("offers no Cancel on kms/reindex rows and no Retry on live ingest rows", () => {
    hookRows = [
      makeRow({ family: "kms", key: "kms-11", title: "KMS recompile", status: "running" }),
      makeRow({
        family: "reindex",
        key: "reindex-21",
        title: "Embedding reindex (vault 4)",
        status: "running",
      }),
      makeRow({
        family: "ingest",
        key: "ingest-42",
        title: "handbook.pdf",
        status: "processing",
        cancellable: true,
        fileId: "42",
      }),
    ];
    render(<ActivityTray />);

    expect(within(rowByFamily("kms")).queryByRole("button", { name: /cancel/i })).toBeNull();
    expect(within(rowByFamily("kms")).queryByRole("button", { name: /retry/i })).toBeNull();
    expect(within(rowByFamily("reindex")).queryByRole("button", { name: /cancel/i })).toBeNull();
    expect(within(rowByFamily("ingest")).queryByRole("button", { name: /retry/i })).toBeNull();
    expect(within(rowByFamily("ingest")).getByRole("button", { name: /cancel/i })).toBeDefined();
  });

  it("asserts family labels row-scoped (each family text inside its own listitem)", () => {
    hookRows = [
      makeRow({ family: "ingest", key: "i-1", title: "a.pdf" }),
      makeRow({ family: "wiki", key: "w-1", title: "Wiki compile" }),
      makeRow({ family: "draft-room", key: "d-1", title: "Draft 1" }),
      makeRow({ family: "kms", key: "k-1", title: "KMS recompile" }),
      makeRow({ family: "reindex", key: "r-1", title: "Embedding reindex" }),
    ];
    render(<ActivityTray />);

    for (const family of ["ingest", "wiki", "draft-room", "kms", "reindex"]) {
      const row = rowByFamily(family);
      expect(within(row).getAllByText(new RegExp(family, "i")).length).toBeGreaterThan(0);
    }
  });

  it("collapses the list via Hide and keeps the count visible while collapsed", async () => {
    const user = userEvent.setup();
    hookRows = [makeRow({ family: "wiki", key: "w-1", title: "Wiki compile" })];
    render(<ActivityTray />);

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: /hide/i }));

    expect(screen.queryByRole("listitem")).toBeNull();
    expect(screen.getByText(/activity/i)).toBeDefined();
    // Re-expand via the expand-only header button.
    await user.click(screen.getByRole("button", { name: /activity/i }));
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("disables the clicked action's button while its request is in flight", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    mockCancelWikiJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ job_id: 7, status: "cancelled" });
        })
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
    ];
    render(<ActivityTray />);

    const cancel = within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i });
    await user.click(cancel);
    expect(cancel).toBeDisabled();

    act(() => {
      release();
    });
    await waitFor(() => expect(cancel).not.toBeDisabled());
  });

  it("stops offering Cancel on a row the server refused with 403 (learned affordance)", async () => {
    const user = userEvent.setup();
    mockCancelWikiJob.mockRejectedValue(
      Object.assign(new Error("Insufficient vault permissions"), {
        response: { status: 403 },
      })
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
    ];
    render(<ActivityTray />);

    const cancel = within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i });
    await user.click(cancel);

    await waitFor(() => {
      expect(within(rowByFamily("wiki")).queryByRole("button", { name: /cancel/i })).toBeNull();
    });
  });

  it("stops offering Retry on a row the server refused with 403 (learned affordance covers retry)", async () => {
    const user = userEvent.setup();
    mockRetryWikiJob.mockRejectedValue(
      Object.assign(new Error("Insufficient vault permissions"), {
        response: { status: 403 },
      })
    );
    hookRows = [
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
    ];
    render(<ActivityTray />);

    await user.click(within(rowByFamily("wiki")).getByRole("button", { name: /retry/i }));

    await waitFor(() => {
      expect(within(rowByFamily("wiki")).queryByRole("button", { name: /retry/i })).toBeNull();
    });
  });

  it("surfaces a non-403 action failure as an error toast", async () => {
    const user = userEvent.setup();
    mockCancelWikiJob.mockRejectedValue(new Error("network down"));
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
    ];
    render(<ActivityTray />);

    await user.click(within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i }));

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalledWith("network down");
    });
    // A non-403 failure does NOT remove the affordance.
    expect(within(rowByFamily("wiki")).queryByRole("button", { name: /cancel/i })).not.toBeNull();
  });

  it("triggers the hook refresh after an action settles (finally-refresh is asserted, not decorative)", async () => {
    const user = userEvent.setup();
    const mockRefresh = vi.fn();
    mockUseActivityJobs.mockImplementation(() => ({
      rows: [
        makeRow({
          family: "wiki",
          key: "wiki-7",
          title: "Wiki compile",
          cancellable: true,
          jobId: 7,
          vaultId: 3,
        }),
      ],
      loading: false,
      refresh: mockRefresh,
    }));
    mockCancelWikiJob.mockResolvedValue({ job_id: 7, status: "cancelled" });
    render(<ActivityTray />);

    await user.click(within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i }));

    await waitFor(() => {
      expect(mockRefresh).toHaveBeenCalledTimes(1);
    });
  });

  it("never imports the client upload store (server-sourced constraint tripwire)", () => {
    const traySource = readFileSync(
      path.join(path.dirname(fileURLToPath(import.meta.url)), "ActivityTray.tsx"),
      "utf8"
    );
    expect(traySource).not.toMatch(/useUploadStore|stores\/useUploadStore/);
  });
});

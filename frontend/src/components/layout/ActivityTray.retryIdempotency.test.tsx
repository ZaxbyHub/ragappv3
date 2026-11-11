// frontend/src/components/layout/ActivityTray.retryIdempotency.test.tsx
//
// Arm's-length acceptance check C5 for the draft-capability-liveness trace
// (902-905-911-913). Frozen specification: RED at base, GREEN after the fix.
//
// An ActivityTray retry action on a draft-room job must send a non-empty
// Idempotency-Key — observed as retryDraftJob's 4th argument (the C4(a)
// spec pins that argument as the Idempotency-Key request header). The key
// is bound to the retry identity (draft id + job id), not to request
// settlement: two clicks on the SAME failed row — the second after the
// first settled but before the tray's rows refresh — must carry the SAME
// key, so an accidental double-retry replays the original job instead of
// creating a duplicate child (issue #905). At base the tray calls
// retryDraftJob(draftId, jobId) with no key at all.
//
// Harness mirrors ActivityTray.m04.test.tsx (vi.hoisted vi.fn() factories +
// module replacement, empty-stores zustand stand-in). The render is wrapped
// in a QueryClientProvider because the fix gives ActivityTray a
// useQueryClient() consumer (the worker_unavailable capabilities
// invalidation pinned by the C19 tray spec); at base the wrapper is inert.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockUseActivityJobs, mockCancelWikiJob, mockRetryWikiJob, mockCancelDraftJob, mockRetryDraftJob, mockCancelIngest } =
  vi.hoisted(() => ({
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

// Spread the real module so the tray's other imports from it (draftRoomKeys,
// isDraftRoomErrorCode) stay defined; only the two network calls are stubbed
// (Copilot review on PR #1103).
vi.mock("@/lib/api/draftRoom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/draftRoom")>()),
  cancelDraftJob: mockCancelDraftJob,
  retryDraftJob: mockRetryDraftJob,
}));

vi.mock("@/lib/api", () => ({
  cancelDocumentIngest: mockCancelIngest,
}));

// Empty-stores zustand stand-in (same idiom as ActivityTray.m04.test.tsx).
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

function mockHappyPath(rows: ActivityJobRowLike[], loading = false) {
  mockUseActivityJobs.mockImplementation(() => ({ rows, loading }));
}

/** The rendered listitem row whose text carries `family`. */
function rowByFamily(family: string): HTMLElement {
  const row = screen
    .getAllByRole("listitem")
    .map((item) => item as HTMLElement)
    .find((item) => item.textContent?.includes(family));
  if (!row) throw new Error(`no tray row found for family ${family}`);
  return row;
}

beforeEach(() => {
  mockUseActivityJobs.mockReset();
  mockCancelWikiJob.mockReset();
  mockRetryWikiJob.mockReset();
  mockCancelDraftJob.mockReset();
  mockRetryDraftJob.mockReset().mockResolvedValue({ id: 10 });
  mockCancelIngest.mockReset();
});

describe("ActivityTray retry idempotency (C5)", () => {
  it("sends a non-empty Idempotency-Key when retrying a draft-room job", async () => {
    const user = userEvent.setup();
    mockHappyPath([
      makeRow({
        family: "draft-room",
        key: "draft-1",
        title: "Draft compile",
        phase: "failed",
        status: "failed",
        terminal: true,
        cancellable: false,
        retryable: true,
        draftId: 42,
        jobId: 9,
      }),
    ]);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ActivityTray />
      </QueryClientProvider>
    );

    const row = rowByFamily("draft-room");
    const retryButton = within(row).getByRole("button", { name: "Retry" });

    // First click on the failed row (kept pending until the test settles it).
    let settleFirst!: () => void;
    mockRetryDraftJob.mockImplementationOnce(
      () =>
        new Promise((_resolve) => {
          settleFirst = _resolve;
        })
    );
    await user.click(retryButton);
    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(1));

    const firstKey = mockRetryDraftJob.mock.calls[0][3];
    expect(
      typeof firstKey === "string" && firstKey.length > 0,
      "ActivityTray retry must send a non-empty Idempotency-Key (retryDraftJob 4th argument)"
    ).toBe(true);

    // Settle the first retry, then click the SAME row again BEFORE the tray
    // refreshes (the mocked rows are static): the retry identity is
    // unchanged, so the key must be IDENTICAL — a settlement must not
    // rotate it (that is what makes an accidental double-retry a
    // server-side replay instead of a duplicate child).
    settleFirst();
    await waitFor(() => expect(retryButton).toBeEnabled());
    await user.click(retryButton);
    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(2));

    const secondKey = mockRetryDraftJob.mock.calls[1][3];
    expect(
      secondKey,
      "two retries of the SAME failed row (second after the first settled, row not yet refreshed) must carry the SAME non-empty Idempotency-Key"
    ).toBe(firstKey);
  });
});

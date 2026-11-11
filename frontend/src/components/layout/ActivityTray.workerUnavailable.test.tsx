// frontend/src/components/layout/ActivityTray.workerUnavailable.test.tsx
//
// Arm's-length acceptance check C19 (ActivityTray retry leg) for the
// draft-capability-liveness trace (902-905-911-913). Frozen specification:
// RED at base, GREEN after the fix.
//
// When the tray's Retry action on a draft-room job rejects with a Draft
// Room error whose code is `worker_unavailable` (HTTP 503, body
// {"detail":"draft room worker is unavailable","code":"worker_unavailable"}),
// the Draft Room capabilities query (draftRoomKeys.capabilities()) must be
// invalidated/refetched. At base the tray's catch only toasts.
//
// Harness mirrors ActivityTray.m04.test.tsx (vi.hoisted vi.fn() factories +
// module replacement, empty-stores zustand stand-in) plus a
// QueryClientProvider with a capabilities-query probe.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";

import { draftRoomKeys, type DraftRoomCapabilities } from "@/lib/api/draftRoom";

const {
  mockUseActivityJobs,
  mockCancelWikiJob,
  mockRetryWikiJob,
  mockCancelDraftJob,
  mockRetryDraftJob,
  mockCancelIngest,
  capsQueryFn,
} = vi.hoisted(() => ({
  mockUseActivityJobs: vi.fn(),
  mockCancelWikiJob: vi.fn(),
  mockRetryWikiJob: vi.fn(),
  mockCancelDraftJob: vi.fn(),
  mockRetryDraftJob: vi.fn(),
  mockCancelIngest: vi.fn(),
  capsQueryFn: vi.fn(),
}));

vi.mock("@/hooks/useJobStatus", () => ({
  useActivityJobs: mockUseActivityJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  cancelWikiJob: mockCancelWikiJob,
  retryWikiJob: mockRetryWikiJob,
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    cancelDraftJob: mockCancelDraftJob,
    retryDraftJob: mockRetryDraftJob,
  };
});

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

function makeCapabilities(): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["compose"],
    tiers: ["standard"],
    piece_types: ["article"],
    transformation_strengths: ["light"],
    limits: { max_inputs: 20 },
    export_formats: ["md"],
    logical_model_modes: ["default"],
    default_logical_mode: "default",
    compile_start_stages: ["research"],
    compile_stage_order: ["research", "assemble"],
    prompt_bundle_version: "v1",
    editorial_gates_installed: true,
    compile_available: true,
    findings_available: true,
    claims_available: true,
    evidence_available: true,
    ready_available: true,
    promote_available: true,
    upload_available: true,
  };
}

/** The apiClient-normalized 503 worker_unavailable error (real shape parseDraftRoomError reads). */
function workerUnavailableError(): Error {
  const error = new Error("Request failed with status code 503");
  (error as unknown as { status: number }).status = 503;
  (error as unknown as { originalError: unknown }).originalError = {
    response: {
      status: 503,
      data: {
        detail: "draft room worker is unavailable",
        code: "worker_unavailable",
      },
    },
  };
  return error;
}

/** Mounted consumer of the capabilities query — observes invalidation refetches. */
function CapabilitiesProbe() {
  useQuery({
    queryKey: draftRoomKeys.capabilities(),
    queryFn: capsQueryFn,
    staleTime: Infinity,
    retry: false,
  });
  return null;
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
  mockRetryDraftJob.mockReset();
  mockCancelIngest.mockReset();
  capsQueryFn.mockReset().mockResolvedValue(makeCapabilities());
});

describe("ActivityTray worker_unavailable invalidation (C19 retry leg)", () => {
  it("refetches the capabilities query when a tray retry rejects with worker_unavailable", async () => {
    const user = userEvent.setup();
    mockUseActivityJobs.mockImplementation(() => ({
      rows: [
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
      ],
      loading: false,
    }));
    mockRetryDraftJob.mockRejectedValue(workerUnavailableError());
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <CapabilitiesProbe />
        <ActivityTray />
      </QueryClientProvider>
    );

    await waitFor(() => expect(capsQueryFn).toHaveBeenCalledTimes(1));
    // Mount-time baseline, captured BEFORE the action is triggered —
    // the implementation invalidates inside onError, which can land
    // before any later side effect is observed.
    const baseline = capsQueryFn.mock.calls.length;
    const row = rowByFamily("draft-room");
    await user.click(within(row).getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(1));
    const refetched = await waitFor(
      () => {
        if (capsQueryFn.mock.calls.length > baseline) return true;
        expect(capsQueryFn.mock.calls.length, "capabilities query must refetch").toBeGreaterThan(baseline);
        return false;
      },
      { timeout: 1500 }
    ).then(
      () => true,
      () => false
    );
    expect(
      refetched,
      "a worker_unavailable rejection from the tray's retry must invalidate/refetch the Draft Room capabilities query"
    ).toBe(true);
  });
});

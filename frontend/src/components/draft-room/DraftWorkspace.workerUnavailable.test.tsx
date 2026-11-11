// frontend/src/components/draft-room/DraftWorkspace.workerUnavailable.test.tsx
//
// Arm's-length acceptance check C19 (compile/retry legs + negative leg) for
// the draft-capability-liveness trace (902-905-911-913). Frozen
// specification: RED at base, GREEN after the fix.
//
// When a compile or retry request from DraftWorkspace rejects with a Draft
// Room error whose code is `worker_unavailable` (HTTP 503, body
// {"detail":"draft room worker is unavailable","code":"worker_unavailable"}),
// the Draft Room capabilities query (draftRoomKeys.capabilities()) must be
// invalidated/refetched — the worker died mid-session and the UI must not
// keep offering compile/upload for the query's full stale window (issue
// #911 claim 3). A 503 carrying a DIFFERENT code must NOT refetch
// capabilities. At base the mutations' onError handlers only toast.
// The upload (DraftSourceUpload) and promote (DraftPromoteDialog) legs of
// C19 live in their own sibling spec files; ActivityTray's retry leg too.
//
// The rejection value is shaped exactly the way the repo's apiClient
// normalization + parseDraftRoomError consume it: a plain Error carrying
// `.status` and `.originalError.response.data.{detail, code}` (see
// parseDraftRoomError in src/lib/api/draftRoom.ts, kept REAL via
// importActual below — a wrongly shaped error would fail the toast
// assertion, not silently pass).
//
// Upload/promote paths are not covered here: those mutations live in
// DraftSourceUpload / DraftPromoteDialog (both mocked in this harness), not
// in DraftWorkspace itself.
//
// Harness mirrors DraftWorkspace.issue516.test.tsx (leaf-component stubs,
// partially-mocked draftRoom API, ui store reset, make* fixtures), plus a
// capabilities-query probe mounted in the same QueryClient.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, waitFor, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";

import { DraftWorkspace } from "./DraftWorkspace";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import { draftRoomKeys } from "@/lib/api/draftRoom";
import type {
  DraftDetail,
  DraftInput,
  DraftJob,
  DraftRoomCapabilities,
  DraftSummary,
} from "@/lib/api/draftRoom";

// jsdom has no ResizeObserver; several unmocked Radix primitives need it.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const render: typeof rtlRender = (ui, options) => rtlRender(ui, { wrapper: MemoryRouter, ...options });

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock("./DraftAssignmentForm", async () => {
  const actual = await vi.importActual<typeof import("./DraftAssignmentForm")>("./DraftAssignmentForm");
  return {
    ...actual,
    DraftAssignmentForm: () => <div data-testid="assignment-form" />,
  };
});
vi.mock("./DraftSourceUpload", () => ({
  DraftSourceUpload: () => <div data-testid="source-upload" />,
}));
vi.mock("./DraftSourceList", () => ({
  DraftSourceList: () => <div data-testid="source-list" />,
}));
vi.mock("./DraftEditor", () => ({
  DraftEditor: () => <div />,
}));
vi.mock("./DraftPreview", () => ({ DraftPreview: () => <div data-testid="preview" /> }));
vi.mock("./DraftRevisionDiff", () => ({ DraftRevisionDiff: () => <div data-testid="revision-diff" /> }));
vi.mock("./DraftStageArtifact", () => ({
  DraftStageArtifact: () => <div data-testid="stage-artifact" />,
}));
vi.mock("./DraftStageRail", () => ({
  DraftStageRail: () => <div data-testid="stage-rail" />,
}));
vi.mock("./DraftInspector", () => ({
  DraftInspector: () => <div data-testid="inspector" />,
}));
vi.mock("./DraftReadyDialog", () => ({
  DraftReadyDialog: () => null,
}));
vi.mock("./DraftExportDialog", () => ({
  DraftExportDialog: () => null,
}));
vi.mock("./DraftPromoteDialog", () => ({
  DraftPromoteDialog: () => null,
}));
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(value: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: { onValueChange: (v: string) => void; children: React.ReactNode }) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children, id }: { children: React.ReactNode; id?: string }) =>
      React.createElement("div", { role: "group", "aria-label": "Retry from", id }, children),
    SelectValue: () => null,
    SelectContent: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement("button", { type: "button", onClick: () => onValueChange(value) }, children);
    },
  };
});

const {
  mockListDraftJobs,
  mockGetDraftStages,
  mockListDraftRevisions,
  mockCompileDraft,
  mockRetryDraftJob,
  capsQueryFn,
} = vi.hoisted(() => ({
  mockListDraftJobs: vi.fn(),
  mockGetDraftStages: vi.fn(),
  mockListDraftRevisions: vi.fn(),
  mockCompileDraft: vi.fn(),
  mockRetryDraftJob: vi.fn(),
  capsQueryFn: vi.fn(),
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    listDraftJobs: mockListDraftJobs,
    getDraftStages: mockGetDraftStages,
    listDraftRevisions: mockListDraftRevisions,
    getDraftRevision: vi.fn(),
    listDraftFindings: vi.fn(),
    updateDraft: vi.fn(),
    compileDraft: mockCompileDraft,
    cancelDraftJob: vi.fn(),
    retryDraftJob: mockRetryDraftJob,
    createDraftRevision: vi.fn(),
    deleteDraft: vi.fn(),
    restoreDraft: vi.fn(),
    archiveDraft: vi.fn(),
  };
});

function makeDraft(overrides: Partial<DraftSummary> = {}): DraftSummary {
  return {
    id: 42,
    vault_id: 7,
    vault_access: "write",
    title: "Q3 press release",
    mode: "compose",
    status: "draft",
    tier: "standard",
    lock_version: 3,
    current_revision_id: 100,
    active_job_id: null,
    input_count: 1,
    open_blocker_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ready_at: null,
    ...overrides,
  };
}

function makeInput(overrides: Partial<DraftInput> = {}): DraftInput {
  return {
    id: 1,
    role: "manuscript",
    authority: "primary",
    as_of_date: null,
    original_name: "manuscript.docx",
    extension: "docx",
    media_type: null,
    size_bytes: 1234,
    content_sha256: "abc",
    parse_status: "ready",
    parse_error: null,
    parsed_char_count: 500,
    active_parse_job_id: null,
    last_parse_job_id: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeDetail(overrides: Partial<DraftDetail> = {}): DraftDetail {
  return {
    summary: makeDraft(),
    brief: {
      piece_type: "article",
      audience: "Local reporters",
      purpose: "Announce the Q3 results",
      tone: "clear and direct",
      target_words: 800,
      transformation_strength: "moderate",
      primary_input_id: null,
      must_include: [],
      must_avoid: [],
      preserve_quotes: true,
      preserve_numbers: true,
      preserve_uncertainty: true,
      drafting_priority: "balanced",
      additional_instructions: "",
    },
    inputs: [makeInput()],
    current_revision_summary: null,
    active_compile_job: null,
    revision_count: 1,
    evidence_count: 0,
    claim_counts_by_status: {},
    finding_counts_by_severity: {},
    ...overrides,
  } as DraftDetail;
}

function makeCapabilities(overrides: Partial<DraftRoomCapabilities> = {}): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["rewrite", "compose"],
    tiers: ["standard", "high_stakes", "sensitive"],
    piece_types: ["article"],
    transformation_strengths: ["light", "moderate", "substantial"],
    limits: { job_max_model_calls: 40, max_inputs: 20 },
    export_formats: ["md"],
    logical_model_modes: ["default"],
    default_logical_mode: "default",
    compile_start_stages: ["research", "outline", "draft", "lint", "copy", "standards", "fact"],
    compile_stage_order: ["intake", "research", "outline", "draft", "lint", "copy", "standards", "fact", "assemble"],
    prompt_bundle_version: "v1",
    editorial_gates_installed: true,
    compile_available: true,
    findings_available: true,
    claims_available: true,
    evidence_available: true,
    ready_available: true,
    promote_available: true,
    upload_available: true,
    ...overrides,
  };
}

function makeJob(overrides: Partial<DraftJob> = {}): DraftJob {
  return {
    id: 9,
    draft_id: 42,
    job_type: "compile",
    status: "completed",
    start_stage: "research",
    active_stage: null,
    progress_percent: 100,
    model_call_count: 5,
    max_model_calls: 40,
    retry_count: 0,
    parent_job_id: null,
    attempt_no: 1,
    compile_input_sha256: null,
    prompt_bundle_version: null,
    timeout_seconds: 600,
    cancel_requested_at: null,
    heartbeat_at: null,
    error_code: null,
    error_message: null,
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:05:00Z",
    ...overrides,
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

const initialUiState = useDraftRoomUiStore.getState();

function renderWorkspace(detail: DraftDetail) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProbe />
      <DraftWorkspace
        draftId={42}
        draft={detail.summary}
        detail={detail}
        capabilities={makeCapabilities()}
        vaultAccess="write"
      />
    </QueryClientProvider>
  );
}

/** True when the capabilities queryFn ran again after the mount-time
 * baseline. The baseline MUST be captured right after the initial mount
 * fetch, BEFORE the mutation is triggered — the implementation
 * invalidates inside onError, which can land before any later side
 * effect (e.g. the toast) is observed, so a later baseline would miss
 * the refetch entirely. */
async function capabilitiesRefetched(baseline: number): Promise<boolean> {
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
  return refetched;
}

beforeEach(() => {
  useDraftRoomUiStore.setState(initialUiState, true);
  mockListDraftJobs.mockReset().mockResolvedValue({ items: [makeJob()], total: 1, page: 1, per_page: 10 });
  mockGetDraftStages.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mockListDraftRevisions.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 });
  mockCompileDraft.mockReset();
  mockRetryDraftJob.mockReset();
  capsQueryFn.mockReset().mockResolvedValue(makeCapabilities());
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
  vi.mocked(toast.info).mockClear();
});

/** The apiClient-normalized 503 with a DIFFERENT code (negative leg). */
function otherCodeError(): Error {
  const error = new Error("Request failed with status code 503");
  (error as unknown as { status: number }).status = 503;
  (error as unknown as { originalError: unknown }).originalError = {
    response: {
      status: 503,
      data: {
        detail: "model unavailable",
        code: "model_unavailable",
      },
    },
  };
  return error;
}

describe("DraftWorkspace worker_unavailable invalidation (C19)", () => {
  it("refetches the capabilities query when a compile rejects with worker_unavailable", async () => {
    const user = userEvent.setup();
    mockCompileDraft.mockRejectedValue(workerUnavailableError());
    renderWorkspace(makeDetail());

    await waitFor(() => expect(capsQueryFn).toHaveBeenCalledTimes(1));
    const baseline = capsQueryFn.mock.calls.length;
    await user.click(await screen.findByRole("button", { name: "Create draft" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Create draft" }));

    await waitFor(() => expect(mockCompileDraft).toHaveBeenCalledTimes(1));
    // The real parseDraftRoomError must surface the server's detail verbatim.
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("draft room worker is unavailable"));
    const refetched = await capabilitiesRefetched(baseline);
    expect(
      refetched,
      "a worker_unavailable rejection from compile must invalidate/refetch the Draft Room capabilities query"
    ).toBe(true);
  });

  it("refetches the capabilities query when a retry rejects with worker_unavailable", async () => {
    mockListDraftJobs.mockResolvedValue({
      items: [makeJob({ status: "failed", error_code: "model_timeout" })],
      total: 1,
      page: 1,
      per_page: 10,
    });
    mockRetryDraftJob.mockRejectedValue(workerUnavailableError());
    renderWorkspace(makeDetail());

    await waitFor(() => expect(capsQueryFn).toHaveBeenCalledTimes(1));
    const baseline = capsQueryFn.mock.calls.length;
    const retryButton = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(screen.getByRole("button", { name: "Outline" }));
    fireEvent.click(retryButton);

    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("draft room worker is unavailable"));
    const refetched = await capabilitiesRefetched(baseline);
    expect(
      refetched,
      "a worker_unavailable rejection from retry must invalidate/refetch the Draft Room capabilities query"
    ).toBe(true);
  });

  it("does NOT refetch the capabilities query when a 503 carries a different code", async () => {
    const user = userEvent.setup();
    mockCompileDraft.mockRejectedValue(otherCodeError());
    renderWorkspace(makeDetail());

    await waitFor(() => expect(capsQueryFn).toHaveBeenCalledTimes(1));
    const baseline = capsQueryFn.mock.calls.length;
    await user.click(await screen.findByRole("button", { name: "Create draft" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Create draft" }));

    await waitFor(() => expect(mockCompileDraft).toHaveBeenCalledTimes(1));
    // onError ran (the server's detail surfaced) — that is the trigger
    // point; a different code must not invalidate anything.
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("model unavailable"));
    const refetched = await capabilitiesRefetched(baseline);
    expect(
      refetched,
      "a 503 carrying a different code (model_unavailable) must NOT refetch the Draft Room capabilities query"
    ).toBe(false);
  });
});

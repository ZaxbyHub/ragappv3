// frontend/src/components/draft-room/DraftWorkspace.retryIdempotency.test.tsx
//
// Arm's-length acceptance check C4(b) for the draft-capability-liveness
// trace (902-905-911-913). Frozen specification: RED at base, GREEN after
// the fix.
//
// The retry Idempotency-Key must be bound to the retry IDENTITY — (draft
// id, parent job id, requested start stage) — NOT to request settlement:
//
//  * two submissions of the SAME identity carry the IDENTICAL non-empty
//    key, whether the second is dispatched while the first is still in
//    flight (same-tick double submit) or issued AFTER the first settled
//    but BEFORE the job list refreshes to a new parent (an accidental
//    double-retry then replays the original job server-side instead of
//    creating a duplicate child — issue #905);
//  * changing the requested start stage, or the parent job changing (the
//    failed child becoming the new parent), yields a DIFFERENT key (same
//    key with a different request fingerprint would 409
//    idempotency_key_conflict server-side).
//
// The key is observed as the 4th retryDraftJob argument, which the C4(a)
// spec pins as the Idempotency-Key request header. At base the retry
// mutation never sends a key at all.
//
// Harness mirrors DraftWorkspace.issue516.test.tsx (leaf-component stubs,
// partially-mocked draftRoom API, ui store reset, make* fixtures).
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";

import { DraftWorkspace } from "./DraftWorkspace";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
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
    DraftAssignmentForm: (props: { inputs?: unknown[] }) => (
      <div data-testid="assignment-form">inputs:{props.inputs?.length ?? "none"}</div>
    ),
  };
});
vi.mock("./DraftSourceUpload", () => ({
  DraftSourceUpload: (props: {
    disabled?: boolean;
    disabledReason?: string;
    maxInputs: number;
    currentInputCount: number;
  }) => (
    <div data-testid="source-upload">
      disabled:{String(props.disabled)}|reason:{props.disabledReason ?? "none"}|max:{props.maxInputs}|count:
      {props.currentInputCount}
    </div>
  ),
}));
vi.mock("./DraftSourceList", () => ({
  DraftSourceList: (props: { locked: boolean; canEdit: boolean; lockedReason?: string; inputs: unknown[] }) => (
    <div data-testid="source-list">
      locked:{String(props.locked)}|canEdit:{String(props.canEdit)}|count:{props.inputs.length}
    </div>
  ),
}));
vi.mock("./DraftEditor", () => ({
  DraftEditor: (props: { value: string; onChange: (next: string) => void; disabled?: boolean; disabledReason?: string }) => (
    <div>
      <label htmlFor="mock-editor">Draft content</label>
      <textarea
        id="mock-editor"
        value={props.value}
        disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.value)}
      />
    </div>
  ),
}));
vi.mock("./DraftPreview", () => ({ DraftPreview: () => <div data-testid="preview" /> }));
vi.mock("./DraftRevisionDiff", () => ({ DraftRevisionDiff: () => <div data-testid="revision-diff" /> }));
vi.mock("./DraftStageArtifact", () => ({
  DraftStageArtifact: () => <div data-testid="stage-artifact" />,
}));
vi.mock("./DraftStageRail", () => ({
  DraftStageRail: (props: { stageOrder: string[]; selectedStage: string | null; onSelectStage: (stage: string) => void }) => (
    <div data-testid="stage-rail">
      {props.stageOrder.map((stage) => (
        <button key={stage} type="button" onClick={() => props.onSelectStage(stage)}>
          select-{stage}
        </button>
      ))}
    </div>
  ),
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

// Radix `Select` can't be driven with userEvent in jsdom; mocked to a plain
// button-per-item list that exercises the real onValueChange wiring (same
// idiom as DraftWorkspace.issue516.test.tsx).
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
  mockGetDraftRevision,
  mockListDraftFindings,
  mockCompileDraft,
  mockCancelDraftJob,
  mockRetryDraftJob,
  mockCreateDraftRevision,
} = vi.hoisted(() => ({
  mockListDraftJobs: vi.fn(),
  mockGetDraftStages: vi.fn(),
  mockListDraftRevisions: vi.fn(),
  mockGetDraftRevision: vi.fn(),
  mockListDraftFindings: vi.fn(),
  mockCompileDraft: vi.fn(),
  mockCancelDraftJob: vi.fn(),
  mockRetryDraftJob: vi.fn(),
  mockCreateDraftRevision: vi.fn(),
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    listDraftJobs: mockListDraftJobs,
    getDraftStages: mockGetDraftStages,
    listDraftRevisions: mockListDraftRevisions,
    getDraftRevision: mockGetDraftRevision,
    listDraftFindings: mockListDraftFindings,
    compileDraft: mockCompileDraft,
    cancelDraftJob: mockCancelDraftJob,
    retryDraftJob: mockRetryDraftJob,
    createDraftRevision: mockCreateDraftRevision,
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
  };
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

const initialUiState = useDraftRoomUiStore.getState();

function renderWorkspace(props: Partial<React.ComponentProps<typeof DraftWorkspace>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const detail = props.detail ?? makeDetail();
  const draft = props.draft ?? detail.summary;
  return render(
    <QueryClientProvider client={queryClient}>
      <DraftWorkspace
        draftId={42}
        draft={draft}
        detail={detail}
        capabilities={makeCapabilities()}
        vaultAccess="write"
        {...props}
      />
    </QueryClientProvider>
  );
}

/** The retryDraftJob 4th argument: the per-attempt Idempotency-Key. */
function keyOf(call: unknown[]): unknown {
  return call[3];
}

function expectNonEmptyKey(value: unknown, message: string): void {
  expect(typeof value === "string" && value.length > 0, message).toBe(true);
}

beforeEach(() => {
  useDraftRoomUiStore.setState(initialUiState, true);
  mockListDraftJobs.mockReset().mockResolvedValue({ items: [makeJob()], total: 1, page: 1, per_page: 10 });
  mockGetDraftStages.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mockListDraftRevisions.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 });
  mockGetDraftRevision.mockReset();
  mockListDraftFindings.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mockCompileDraft.mockReset();
  mockCancelDraftJob.mockReset();
  mockCreateDraftRevision.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
  vi.mocked(toast.info).mockClear();
});

describe("DraftWorkspace retry idempotency (C4b)", () => {
  it("reuses one Idempotency-Key across same-identity submissions, in flight or after settle", async () => {
    // The last compile job failed, which is what makes the retry control
    // appear. The job list is STATIC here: even after the submissions
    // settle, the refetch still reports the SAME failed parent — the retry
    // identity (draft 42, parent job 9, stage outline) has not changed.
    const failedParent = makeJob({ status: "failed", error_code: "model_timeout" });
    const jobItems = [failedParent];
    mockListDraftJobs.mockReset().mockImplementation(() =>
      Promise.resolve({ items: jobItems, total: jobItems.length, page: 1, per_page: 10 })
    );

    // Submissions stay pending until the test settles them.
    const settles: Array<(job: DraftJob) => void> = [];
    mockRetryDraftJob.mockReset().mockImplementation(
      () => new Promise<DraftJob>((resolve) => settles.push(resolve))
    );

    renderWorkspace();
    const retryButton = await screen.findByRole("button", { name: "Retry" });

    // Two submissions of the SAME identity, the second dispatched in the
    // same tick — before the first settles (the disabled attribute only
    // lands on re-render, so both dispatches reach the handler; fireEvent
    // is deliberate for exactly that reason).
    fireEvent.click(screen.getByRole("button", { name: "Outline" }));
    fireEvent.click(retryButton);
    fireEvent.click(retryButton);
    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(2));

    const firstKey = keyOf(mockRetryDraftJob.mock.calls[0]);
    const secondKey = keyOf(mockRetryDraftJob.mock.calls[1]);
    expectNonEmptyKey(
      firstKey,
      "each retry submission must carry a non-empty Idempotency-Key (retryDraftJob 4th argument)"
    );
    expect(
      secondKey,
      "two retry submissions for the SAME identity must carry the IDENTICAL Idempotency-Key"
    ).toBe(firstKey);
    expect(mockRetryDraftJob.mock.calls[0][0]).toBe(42);
    expect(mockRetryDraftJob.mock.calls[0][1]).toBe(9);

    // Settle BOTH submissions; the refetch still reports the same failed
    // parent (no job-list refresh to a new parent has happened).
    for (const settle of settles.splice(0)) {
      settle(failedParent);
    }
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(2));

    // A SECOND submission of the SAME identity, issued AFTER the first
    // settled but BEFORE the job list refreshes: still the SAME key (a
    // settlement must not rotate the key — that is what makes an accidental
    // double-retry a server-side replay instead of a duplicate child).
    fireEvent.click(screen.getByRole("button", { name: "Outline" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(3));

    const thirdKey = keyOf(mockRetryDraftJob.mock.calls[2]);
    expectNonEmptyKey(thirdKey, "every retry submission must carry a non-empty Idempotency-Key");
    expect(
      thirdKey,
      "a retry of the SAME identity issued after the previous submission settled (job list not yet refreshed) must carry the SAME Idempotency-Key"
    ).toBe(firstKey);
  });

  it("rotates the Idempotency-Key when the retry identity changes (stage or parent job)", async () => {
    const failedParent = makeJob({ status: "failed", error_code: "model_timeout" });
    const failedChild = makeJob({
      id: 10,
      parent_job_id: 9,
      status: "failed",
      error_code: "model_timeout",
      created_at: "2026-01-02T00:00:00Z",
    });
    // Mutable job list: flipping it simulates the refresh that makes the
    // failed CHILD the new latest job (the new parent for the next retry).
    let jobItems = [failedParent];
    mockListDraftJobs.mockReset().mockImplementation(() =>
      Promise.resolve({ items: jobItems, total: jobItems.length, page: 1, per_page: 10 })
    );

    const settles: Array<(job: DraftJob) => void> = [];
    mockRetryDraftJob.mockReset().mockImplementation(
      () => new Promise<DraftJob>((resolve) => settles.push(resolve))
    );

    renderWorkspace();
    const retryButton = await screen.findByRole("button", { name: "Retry" });

    // Identity A: parent job 9, stage outline.
    fireEvent.click(screen.getByRole("button", { name: "Outline" }));
    fireEvent.click(retryButton);
    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(1));
    const identityAKey = keyOf(mockRetryDraftJob.mock.calls[0]);
    expectNonEmptyKey(identityAKey, "the first retry submission must carry a non-empty Idempotency-Key");

    // Identity B: SAME parent job, DIFFERENT requested start stage.
    for (const settle of settles.splice(0)) {
      settle(failedParent);
    }
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Research" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(2));
    const identityBKey = keyOf(mockRetryDraftJob.mock.calls[1]);
    expect(
      identityBKey,
      "changing the requested start stage must yield a DIFFERENT Idempotency-Key (same key would 409 idempotency_key_conflict server-side)"
    ).not.toBe(identityAKey);

    // Identity C: the failed CHILD becomes the new parent (flip the list,
    // then settle so the invalidation refetch observes it).
    jobItems = [failedChild, failedParent];
    for (const settle of settles.splice(0)) {
      settle(failedChild);
    }
    // The settle invalidates the jobs query; wait until that refetch has
    // completed (mount fetch + one per settle = at least 3) so the Retry
    // control targets the failed CHILD (job 10).
    await waitFor(() =>
      expect(mockListDraftJobs.mock.calls.length).toBeGreaterThanOrEqual(3)
    );
    fireEvent.click(screen.getByRole("button", { name: "Outline" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledTimes(3));
    expect(mockRetryDraftJob.mock.calls[2][1]).toBe(10);
    const identityCKey = keyOf(mockRetryDraftJob.mock.calls[2]);
    expect(
      identityCKey,
      "a retry whose PARENT JOB changed (the failed child becomes the new parent) must yield a DIFFERENT Idempotency-Key"
    ).not.toBe(identityAKey);
    expect(identityCKey).not.toBe(identityBKey);
  });
});

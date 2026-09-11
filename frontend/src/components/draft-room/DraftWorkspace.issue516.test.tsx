// Regression tests for issue #516 acceptance checks AC12 (UI-019 in-flight
// save) and AC18 (UI-021 failed-job stages). Mirrors DraftWorkspace.test.tsx
// (leaf-component stubs, partially-mocked draftRoom API, ui store reset,
// make* fixtures). These tests assert REQUIRED behavior and are expected to
// FAIL until the fix lands; each prints an AC<n> CHECK sentinel.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, waitFor, within, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";

import { DraftWorkspace } from "./DraftWorkspace";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import { draftRoomKeys } from "@/lib/api/draftRoom";
import type {
  DraftDetail,
  DraftInput,
  DraftJob,
  DraftRevisionDetail,
  DraftRoomCapabilities,
  DraftStage,
  DraftStageName,
  DraftStageStatus,
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

// ---- Heavy leaf components are mocked to keep this file about
// DraftWorkspace's own wiring/logic, per the shell's test guidance. The
// DraftStageArtifact / DraftInspector stubs are extended to surface the
// owning job_id + error_code so AC18 can assert WHICH job's stages render.
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
  DraftEditor: (props: {
    value: string;
    onChange: (next: string) => void;
    disabled?: boolean;
    disabledReason?: string;
  }) => (
    <div>
      <label htmlFor="mock-editor">Draft content</label>
      <textarea
        id="mock-editor"
        value={props.value}
        disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.value)}
      />
      {props.disabledReason && <p>{props.disabledReason}</p>}
    </div>
  ),
}));
vi.mock("./DraftPreview", () => ({ DraftPreview: () => <div data-testid="preview" /> }));
vi.mock("./DraftRevisionDiff", () => ({ DraftRevisionDiff: () => <div data-testid="revision-diff" /> }));
vi.mock("./DraftStageArtifact", () => ({
  DraftStageArtifact: (props: { stage: DraftStage | null }) => (
    <div data-testid="stage-artifact">
      stage:{props.stage?.stage ?? "none"}|job:{props.stage?.job_id ?? "none"}|error:
      {props.stage?.error_code ?? "none"}
    </div>
  ),
}));
vi.mock("./DraftStageRail", () => ({
  DraftStageRail: (props: {
    stageOrder: string[];
    jobStatus: string | null;
    selectedStage: string | null;
    onSelectStage: (stage: string) => void;
  }) => (
    <div data-testid="stage-rail">
      <span>jobStatus:{props.jobStatus ?? "none"}</span>
      <span>selected:{props.selectedStage ?? "none"}</span>
      {props.stageOrder.map((stage) => (
        <button key={stage} type="button" onClick={() => props.onSelectStage(stage)}>
          select-{stage}
        </button>
      ))}
    </div>
  ),
}));
vi.mock("./DraftInspector", () => ({
  DraftInspector: (props: {
    stage: DraftStage | null;
    revisionId: number | null;
    jobId: number | null;
    canDispose: boolean;
    tier: string;
  }) => (
    <div data-testid="inspector">
      stage:{props.stage?.stage ?? "none"}|stageJob:{props.stage?.job_id ?? "none"}|revision:
      {String(props.revisionId)}|job:{String(props.jobId)}|canDispose:{String(props.canDispose)}|tier:
      {props.tier}
    </div>
  ),
}));
vi.mock("./DraftReadyDialog", () => ({
  DraftReadyDialog: (props: { open: boolean; eligibility: { ok: boolean; blockers: string[] } }) =>
    props.open ? (
      <div data-testid="ready-dialog">ok:{String(props.eligibility.ok)}|blockers:{props.eligibility.blockers.join(",")}</div>
    ) : null,
}));
vi.mock("./DraftExportDialog", () => ({
  DraftExportDialog: (props: { open: boolean }) => (props.open ? <div data-testid="export-dialog" /> : null),
}));
vi.mock("./DraftPromoteDialog", () => ({
  DraftPromoteDialog: (props: { open: boolean; canWrite: boolean }) =>
    props.open ? <div data-testid="promote-dialog">canWrite:{String(props.canWrite)}</div> : null,
}));

// The only real `ui/select` consumer left in DraftWorkspace itself is the
// retry-stage picker. Radix `Select` can't be driven with
// `userEvent.selectOptions` in jsdom, so it is mocked to a plain
// button-per-item list that still exercises the real `onValueChange` wiring.
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
  mockUpdateDraft,
  mockCompileDraft,
  mockCancelDraftJob,
  mockRetryDraftJob,
  mockCreateDraftRevision,
  mockDeleteDraft,
  mockRestoreDraft,
  mockArchiveDraft,
} = vi.hoisted(() => ({
  mockListDraftJobs: vi.fn(),
  mockGetDraftStages: vi.fn(),
  mockListDraftRevisions: vi.fn(),
  mockGetDraftRevision: vi.fn(),
  mockListDraftFindings: vi.fn(),
  mockUpdateDraft: vi.fn(),
  mockCompileDraft: vi.fn(),
  mockCancelDraftJob: vi.fn(),
  mockRetryDraftJob: vi.fn(),
  mockCreateDraftRevision: vi.fn(),
  mockDeleteDraft: vi.fn(),
  mockRestoreDraft: vi.fn(),
  mockArchiveDraft: vi.fn(),
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
    updateDraft: mockUpdateDraft,
    compileDraft: mockCompileDraft,
    cancelDraftJob: mockCancelDraftJob,
    retryDraftJob: mockRetryDraftJob,
    createDraftRevision: mockCreateDraftRevision,
    deleteDraft: mockDeleteDraft,
    restoreDraft: mockRestoreDraft,
    archiveDraft: mockArchiveDraft,
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
    current_revision_summary: {
      id: 100,
      revision_no: 1,
      parent_revision_id: null,
      job_id: 9,
      source: "pipeline",
      content_sha256: "sha-current",
      fact_status: "passed",
      is_current: true,
      created_by: 1,
      created_at: "2026-01-01T00:00:00Z",
    },
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
    limits: { max_model_calls: 40, max_inputs: 20 },
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

function makeStage(overrides: Partial<DraftStage> & { stage: DraftStageName; job_id: number; status: DraftStageStatus }): DraftStage {
  return {
    id: 1,
    attempt: 1,
    input_sha256: "a",
    artifact_sha256: "b",
    candidate_sha256: null,
    semantic_changed: false,
    prompt_id: null,
    prompt_version: null,
    prompt_sha256: null,
    model_name: null,
    temperature: null,
    input_tokens: null,
    output_tokens: null,
    error_message: null,
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:01:00Z",
    artifact: {},
    content_md: null,
    ...overrides,
  };
}

function makeRevisionDetail(overrides: Partial<DraftRevisionDetail> = {}): DraftRevisionDetail {
  return {
    summary: {
      id: 101,
      revision_no: 2,
      parent_revision_id: 100,
      job_id: null,
      source: "manual",
      content_sha256: "sha-101",
      fact_status: "not_run",
      is_current: false,
      created_by: 1,
      created_at: "2026-01-02T00:00:00Z",
    },
    content_md: "First saved text.",
    sections: [],
    citations: [],
    qa_summary: {},
    ...overrides,
  };
}

const initialUiState = useDraftRoomUiStore.getState();

function renderWorkspace(props: Partial<React.ComponentProps<typeof DraftWorkspace>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const detail = props.detail ?? makeDetail();
  const draft = props.draft ?? detail.summary;
  const utils = render(
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
  return { ...utils, queryClient };
}

beforeEach(() => {
  useDraftRoomUiStore.setState(initialUiState, true);
  mockListDraftJobs.mockReset().mockResolvedValue({ items: [makeJob()], total: 1, page: 1, per_page: 10 });
  mockGetDraftStages.mockReset().mockResolvedValue({
    items: [
      makeStage({
        id: 1,
        job_id: 9,
        stage: "research",
        status: "completed",
        error_code: null,
      }),
    ],
    total: 1,
    page: 1,
    per_page: 100,
  });
  mockListDraftRevisions.mockReset().mockResolvedValue({
    items: [
      {
        id: 100,
        revision_no: 1,
        parent_revision_id: null,
        job_id: 9,
        source: "pipeline",
        content_sha256: "sha-current",
        fact_status: "passed",
        is_current: true,
        created_by: 1,
        created_at: "2026-01-01T00:00:00Z",
      },
    ],
    total: 1,
    page: 1,
    per_page: 50,
  });
  mockGetDraftRevision.mockReset().mockResolvedValue({
    summary: {
      id: 100,
      revision_no: 1,
      parent_revision_id: null,
      job_id: 9,
      source: "pipeline",
      content_sha256: "sha-current",
      fact_status: "passed",
      is_current: true,
      created_by: 1,
      created_at: "2026-01-01T00:00:00Z",
    },
    content_md: "Original manuscript text.",
    sections: [],
    citations: [],
    qa_summary: {},
  });
  mockListDraftFindings.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mockUpdateDraft.mockReset();
  mockCompileDraft.mockReset();
  mockCancelDraftJob.mockReset();
  mockRetryDraftJob.mockReset();
  mockCreateDraftRevision.mockReset();
  mockDeleteDraft.mockReset();
  mockRestoreDraft.mockReset();
  mockArchiveDraft.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
  vi.mocked(toast.info).mockClear();
});

describe("DraftWorkspace (issue #516)", () => {
  // --------------------------------------------------------------------------
  // AC12 (UI-019): text typed into the editor WHILE a save is in flight must
  // survive the save's success. The saved revision must contain exactly what
  // was submitted; the editor keeps the newer text and stays dirty so the
  // user can save again. Current defect: onSuccess unconditionally calls
  // clearDraftTextStore(draftId), so the editor falls back to the baseline
  // and the in-flight edits are silently lost.
  // --------------------------------------------------------------------------
  it("AC12: keeps text typed during an in-flight save and stays dirty while the saved revision keeps the submitted text", async () => {
    try {
      const user = userEvent.setup();
      useDraftRoomUiStore.setState({ workspaceTab: "draft" });

      let resolveSave!: (detail: DraftRevisionDetail) => void;
      mockCreateDraftRevision.mockImplementation(
        () =>
          new Promise<DraftRevisionDetail>((resolve) => {
            resolveSave = resolve;
          })
      );
      renderWorkspace();

      const textarea = await screen.findByLabelText("Draft content");
      // Wait for the current-revision query to load the saved baseline.
      await waitFor(() => expect(textarea).toHaveValue("Original manuscript text."));

      // Baseline edit that will be submitted.
      await user.clear(textarea);
      await user.type(textarea, "First saved text.");

      await user.click(screen.getByRole("button", { name: "Save new revision" }));
      const dialog = await screen.findByRole("dialog");
      await user.click(within(dialog).getByRole("button", { name: "Save new revision" }));

      // The mutation is now in flight with exactly the submitted text.
      await waitFor(() => expect(mockCreateDraftRevision).toHaveBeenCalledTimes(1));
      const submitted = mockCreateDraftRevision.mock.calls[0][1];
      expect(submitted).toEqual({
        base_revision_id: 100,
        lock_version: 3,
        content_md: "First saved text.",
      });

      // While the request is pending, the user keeps typing. Close the
      // confirmation first (Cancel), then edit the textarea.
      await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
      fireEvent.change(textarea, { target: { value: "First saved text. Late edit." } });
      expect(textarea).toHaveValue("First saved text. Late edit.");

      // The save succeeds.
      resolveSave(makeRevisionDetail());
      await waitFor(() => expect(toast.success).toHaveBeenCalledWith("New revision saved."));

      // REQUIRED: the newer text is still in the editor and still dirty.
      expect(textarea).toHaveValue("First saved text. Late edit.");
      expect(screen.getByRole("button", { name: "Save new revision" })).toBeEnabled();

      console.log("AC12 CHECK: PASS");
    } catch (err) {
      console.log("AC12 CHECK: FAIL");
      throw err;
    }
  });

  // --------------------------------------------------------------------------
  // AC18 (UI-021): with an older SUCCESSFUL compile job that produced the
  // current revision and a NEWER FAILED compile job, and no active job,
  // stage/artifact inspection must fetch and display the FAILED job's
  // stages. Current defect: relevantJobId = activeJob?.id ??
  // current_revision_summary?.job_id ?? latestCompileJob?.id binds
  // inspection to the old (revision-producing) job, so the user can never
  // see why the latest run failed.
  // --------------------------------------------------------------------------
  it("AC18: inspects the newer FAILED job's stages, not the older job that produced the current revision", async () => {
    try {
      useDraftRoomUiStore.setState({ workspaceTab: "research" });

      // Two compile jobs: older successful one (job 9) produced the current
      // revision; a newer one (job 12) failed.
      mockListDraftJobs.mockResolvedValue({
        items: [
          makeJob({ id: 9, status: "completed", created_at: "2026-01-01T00:00:00Z" }),
          makeJob({
            id: 12,
            status: "failed",
            created_at: "2026-02-01T00:00:00Z",
            started_at: "2026-02-01T00:00:00Z",
            completed_at: "2026-02-01T00:01:00Z",
            error_code: "model_timeout",
            error_message: "model call timed out",
          }),
        ],
        total: 2,
        page: 1,
        per_page: 10,
      });

      const oldJobStages = [
        makeStage({ id: 1, job_id: 9, stage: "research", status: "completed", error_code: null }),
      ];
      const failedJobStages = [
        makeStage({
          id: 2,
          job_id: 12,
          stage: "research",
          status: "failed",
          error_code: "model_timeout",
          error_message: "model call timed out",
        }),
      ];
      mockGetDraftStages.mockImplementation(async (_draftId: number, jobId: number) => ({
        items: jobId === 12 ? failedJobStages : oldJobStages,
        total: (jobId === 12 ? failedJobStages : oldJobStages).length,
        page: 1,
        per_page: 100,
      }));

      renderWorkspace();

      await screen.findByTestId("stage-artifact");
      await waitFor(() => expect(mockGetDraftStages).toHaveBeenCalled());

      // REQUIRED: stages are queried for the FAILED job's id (12)…
      expect(mockGetDraftStages).toHaveBeenCalledWith(42, 12, expect.objectContaining({ per_page: 100 }));

      // …and the artifact rendered for the research stage is the FAILED
      // job's stage (job 12 with its error), not the old job's.
      const artifact = screen.getByTestId("stage-artifact");
      expect(artifact).toHaveTextContent("stage:research");
      expect(artifact).toHaveTextContent("job:12");
      expect(artifact).toHaveTextContent("error:model_timeout");

      console.log("AC18 CHECK: PASS");
    } catch (err) {
      console.log("AC18 CHECK: FAIL");
      throw err;
    }
  });
});

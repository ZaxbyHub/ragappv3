import { describe, expect, it, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";

import { DraftWorkspace } from "./DraftWorkspace";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import {
  DRAFT_ROOM_DISABLED_MESSAGE,
  CANCEL_CONSEQUENCE,
  READY_BLOCKER_LABELS,
} from "./labels";
import { draftRoomKeys } from "@/lib/api/draftRoom";
import type {
  DraftDetail,
  DraftInput,
  DraftJob,
  DraftRoomCapabilities,
  DraftStage,
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

// ---- Heavy leaf components are mocked to keep this file about DraftWorkspace's
// own wiring/logic, per the shell's test guidance. Each stub exposes the props
// that matter for assertions.
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
    <div data-testid="stage-artifact">{props.stage?.stage ?? "none"}</div>
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
      stage:{props.stage?.stage ?? "none"}|revision:{String(props.revisionId)}|job:{String(props.jobId)}|
      canDispose:{String(props.canDispose)}|tier:{props.tier}
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

// The only real `ui/select` consumer left in DraftWorkspace itself (every
// other component that renders one is mocked above) is the retry-stage
// picker. Radix `Select` can't be driven with `userEvent.selectOptions` in
// jsdom (see frontend-testing-gotchas.md #2), so it is mocked to a plain
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
      {
        id: 1,
        job_id: 9,
        stage: "research",
        attempt: 1,
        status: "completed",
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
        error_code: null,
        error_message: null,
        started_at: null,
        completed_at: null,
        artifact: {},
        content_md: null,
      },
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

describe("DraftWorkspace", () => {
  it("disables compile with the exact reason when the project has no ready inputs (draft with no inputs)", async () => {
    renderWorkspace({ detail: makeDetail({ inputs: [] }) });
    expect(
      await screen.findByText("Add at least one parsed, ready source file before compiling.")
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create draft" })).toBeDisabled();
  });

  it("shows the compile confirmation with mode/tier/sources/stage-count/model-call-limit and submits the exact payload with an idempotency key", async () => {
    const user = userEvent.setup();
    mockCompileDraft.mockResolvedValue(makeJob({ status: "pending" }));
    renderWorkspace();

    await user.click(await screen.findByRole("button", { name: "Create draft" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Standard")).toBeInTheDocument();
    expect(within(dialog).getByText(/1 \(1 Manuscript\)/)).toBeInTheDocument();
    expect(within(dialog).getByText("9")).toBeInTheDocument(); // 9 stages in compile_stage_order
    expect(within(dialog).getByText("40")).toBeInTheDocument(); // max_model_calls
    expect(within(dialog).getAllByText(/human review/i).length).toBeGreaterThan(0);

    await user.click(within(dialog).getByRole("button", { name: "Create draft" }));

    await waitFor(() => expect(mockCompileDraft).toHaveBeenCalledTimes(1));
    const [draftIdArg, request, idempotencyKey] = mockCompileDraft.mock.calls[0];
    expect(draftIdArg).toBe(42);
    expect(request).toEqual({ base_revision_id: 100, lock_version: 3, start_stage: undefined });
    expect(typeof idempotencyKey).toBe("string");
    expect(idempotencyKey.length).toBeGreaterThan(0);
  });

  it("focuses the compile confirmation's heading on open, not the Cancel button", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await user.click(await screen.findByRole("button", { name: "Create draft" }));
    const dialog = await screen.findByRole("dialog");

    await waitFor(() => {
      expect(document.activeElement).toBe(within(dialog).getByRole("heading", { name: "Create draft" }));
    });
  });

  it("shows the cancel confirmation with CANCEL_CONSEQUENCE and calls cancelDraftJob", async () => {
    const user = userEvent.setup();
    mockCancelDraftJob.mockResolvedValue(makeJob({ status: "cancelled" }));
    renderWorkspace({
      detail: makeDetail({
        active_compile_job: makeJob({ id: 55, status: "running", active_stage: "research" }),
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(CANCEL_CONSEQUENCE)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(mockCancelDraftJob).toHaveBeenCalledWith(42, 55));
  });

  it("focuses the cancel confirmation's heading on open, not the Keep running button", async () => {
    const user = userEvent.setup();
    renderWorkspace({
      detail: makeDetail({
        active_compile_job: makeJob({ id: 55, status: "running", active_stage: "research" }),
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    const dialog = await screen.findByRole("dialog");

    await waitFor(() => {
      expect(document.activeElement).toBe(within(dialog).getByRole("heading", { name: "Cancel this run?" }));
    });
  });

  it("only offers server-advertised retry stages, never assemble or intake, and surfaces backward normalisation", async () => {
    const user = userEvent.setup();
    mockRetryDraftJob.mockResolvedValue(makeJob({ start_stage: "research" }));
    // The last compile job failed, which is what makes the retry control appear.
    mockListDraftJobs.mockResolvedValue({
      items: [makeJob({ status: "failed", error_code: "model_timeout" })],
      total: 1,
      page: 1,
      per_page: 10,
    });
    renderWorkspace({ detail: makeDetail({ current_revision_summary: null }) });

    await screen.findByRole("group", { name: "Retry from" });
    expect(screen.queryByRole("button", { name: "Assemble" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Intake" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Outline" }));
    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(mockRetryDraftJob).toHaveBeenCalledWith(42, 9, { start_stage: "outline" }));
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(expect.stringContaining("Research"))
    );
  });

  it("offers Reload and Compare on a 409 save conflict, and never silently overwrites", async () => {
    const user = userEvent.setup();
    const conflictError = Object.assign(new Error("conflict"), { status: 409 });
    mockCreateDraftRevision.mockRejectedValue(conflictError);
    useDraftRoomUiStore.setState({ workspaceTab: "draft" });
    renderWorkspace();

    const textarea = await screen.findByLabelText("Draft content");
    await user.clear(textarea);
    await user.type(textarea, "Edited manuscript text.");

    await user.click(screen.getByRole("button", { name: "Save new revision" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Save new revision" }));

    expect(await within(dialog).findByText("Conflict")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Compare" })).toBeInTheDocument();
    expect(mockCreateDraftRevision).toHaveBeenCalledTimes(1);
  });

  it("focuses the save-revision confirmation's heading on open, not the Cancel button", async () => {
    const user = userEvent.setup();
    useDraftRoomUiStore.setState({ workspaceTab: "draft" });
    renderWorkspace();

    const textarea = await screen.findByLabelText("Draft content");
    await user.type(textarea, " More.");

    await user.click(screen.getByRole("button", { name: "Save new revision" }));
    const dialog = await screen.findByRole("dialog");

    await waitFor(() => {
      expect(document.activeElement).toBe(
        within(dialog).getByRole("heading", { name: "Save a new revision?" })
      );
    });
  });

  it("focuses the delete confirmation's heading on open, not the Cancel button", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await user.click(await screen.findByRole("button", { name: "Delete project" }));
    const dialog = await screen.findByRole("dialog");

    await waitFor(() => {
      expect(document.activeElement).toBe(
        within(dialog).getByRole("heading", { name: "Delete this project?" })
      );
    });
  });

  it("gates content actions and explains the permission when vault access is revoked, but keeps Cancel and Delete available", async () => {
    useDraftRoomUiStore.setState({ workspaceTab: "sources" });
    renderWorkspace({
      vaultAccess: "revoked",
      detail: makeDetail({ active_compile_job: makeJob({ id: 77, status: "running" }) }),
    });

    expect(await screen.findByText(READY_BLOCKER_LABELS.vault_access_revoked)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Export" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Promote to vault" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete project" })).toBeEnabled();
    expect(screen.getByTestId("source-upload")).toHaveTextContent("disabled:true");
  });

  it("offers compile, retry and Mark Ready with read-only vault access, but not Promote to vault (backend gates only promote on vault write)", async () => {
    mockListDraftJobs.mockReset().mockResolvedValue({
      items: [makeJob({ status: "failed" })],
      total: 1,
      page: 1,
      per_page: 10,
    });
    renderWorkspace({ vaultAccess: "read" });

    expect(await screen.findByRole("button", { name: "Create draft" })).toBeEnabled();
    await screen.findByRole("group", { name: "Retry from" });
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mark Ready" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Promote to vault" })).not.toBeInTheDocument();
  });

  it("offers compile, retry, Mark Ready and Promote to vault with vault write access", async () => {
    mockListDraftJobs.mockReset().mockResolvedValue({
      items: [makeJob({ status: "failed" })],
      total: 1,
      page: 1,
      per_page: 10,
    });
    renderWorkspace({ vaultAccess: "write" });

    expect(await screen.findByRole("button", { name: "Create draft" })).toBeEnabled();
    await screen.findByRole("group", { name: "Retry from" });
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mark Ready" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Promote to vault" })).toBeEnabled();
  });

  it("offers no submittable Retry when the capability is disabled, even with a failed job", async () => {
    mockListDraftJobs.mockReset().mockResolvedValue({
      items: [makeJob({ status: "failed" })],
      total: 1,
      page: 1,
      per_page: 10,
    });
    renderWorkspace({ capabilities: makeCapabilities({ enabled: false, compile_available: false }) });

    await screen.findByRole("group", { name: "Retry from" });
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(mockRetryDraftJob).not.toHaveBeenCalled();
  });

  it("shows Restore for an authorized archived project and blocks compile with the archived reason", async () => {
    renderWorkspace({ draft: makeDraft({ status: "archived" }), detail: makeDetail({ summary: makeDraft({ status: "archived" }) }) });

    expect(await screen.findByText("This project is archived. Restore it before compiling.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore project" })).toBeInTheDocument();
  });

  it("enables Promote when the caller has vault write and the server reports promotion available", async () => {
    renderWorkspace({ capabilities: makeCapabilities({ promote_available: true }) });

    expect(await screen.findByRole("button", { name: "Promote to vault" })).toBeEnabled();
    expect(screen.queryByText("Promotion is currently unavailable.")).not.toBeInTheDocument();
  });

  it("disables Promote with a reason when the server reports promotion unavailable, even with vault write", async () => {
    renderWorkspace({ capabilities: makeCapabilities({ promote_available: false }) });

    expect(await screen.findByRole("button", { name: "Promote to vault" })).toBeDisabled();
    expect(screen.getByText("Promotion is currently unavailable.")).toBeInTheDocument();
  });

  it("offers Archive for an active project", async () => {
    renderWorkspace();
    expect(await screen.findByRole("button", { name: "Archive project" })).toBeEnabled();
  });

  it("does not offer Archive for an already-archived project", async () => {
    renderWorkspace({ draft: makeDraft({ status: "archived" }), detail: makeDetail({ summary: makeDraft({ status: "archived" }) }) });
    await screen.findByRole("button", { name: "Restore project" });
    expect(screen.queryByRole("button", { name: "Archive project" })).not.toBeInTheDocument();
  });

  it("disables Archive with a stated reason while a job is active", async () => {
    renderWorkspace({
      detail: makeDetail({ active_compile_job: makeJob({ id: 77, status: "running" }) }),
    });

    expect(await screen.findByRole("button", { name: "Archive project" })).toBeDisabled();
    expect(
      screen.getByText("Archiving is unavailable while a newsroom run is active.")
    ).toBeInTheDocument();
  });

  it("focuses the archive confirmation's heading on open, not the Cancel button", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await user.click(await screen.findByRole("button", { name: "Archive project" }));
    const dialog = await screen.findByRole("dialog");

    await waitFor(() => {
      expect(document.activeElement).toBe(
        within(dialog).getByRole("heading", { name: "Archive this project?" })
      );
    });
  });

  it("requires confirmation, archives with the draft's lock_version, and invalidates detail and list queries", async () => {
    const user = userEvent.setup();
    mockArchiveDraft.mockResolvedValue(makeDraft({ status: "archived" }));
    const { queryClient } = renderWorkspace();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(await screen.findByRole("button", { name: "Archive project" }));
    expect(mockArchiveDraft).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/read-only until it is restored/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archive project" }));

    await waitFor(() => expect(mockArchiveDraft).toHaveBeenCalledWith(42, 3));
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(42) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.lists() });
  });

  it("surfaces the server detail via toast on a 409 while archiving", async () => {
    const user = userEvent.setup();
    const conflictError = Object.assign(new Error("conflict"), {
      status: 409,
      originalError: {
        response: {
          data: { detail: "A newsroom run is already active for this project.", code: "active_job" },
        },
      },
    });
    mockArchiveDraft.mockRejectedValue(conflictError);
    renderWorkspace();

    await user.click(await screen.findByRole("button", { name: "Archive project" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Archive project" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("A newsroom run is already active for this project.")
    );
  });

  it("selecting a stage in the rail feeds the inspector's stage artifact", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await screen.findByTestId("stage-rail");

    await user.click(screen.getByRole("button", { name: "select-research" }));

    await waitFor(() => {
      const inspectors = screen.getAllByTestId("inspector");
      expect(inspectors[0]).toHaveTextContent("stage:research");
    });
  });

  it("marks the manuscript dirty only after an edit and enables Save revision", async () => {
    const user = userEvent.setup();
    useDraftRoomUiStore.setState({ workspaceTab: "draft" });
    renderWorkspace();

    const textarea = await screen.findByLabelText("Draft content");
    expect(screen.getByRole("button", { name: "Save new revision" })).toBeDisabled();

    await user.type(textarea, " More.");
    await waitFor(() => expect(screen.getByRole("button", { name: "Save new revision" })).toBeEnabled());
  });

  it("keeps Export/Delete available and explains the disabled reason when the capability is off", async () => {
    renderWorkspace({ capabilities: makeCapabilities({ enabled: false, compile_available: false }) });

    expect(await screen.findByText(DRAFT_ROOM_DISABLED_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create draft" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Export" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete project" })).toBeEnabled();
  });

  it("reports source-only, retrieval-partial, and evidence-invalidated flags to the caller for its status banner", async () => {
    mockGetDraftStages.mockResolvedValue({
      items: [
        {
          id: 1,
          job_id: 9,
          stage: "research",
          attempt: 1,
          status: "completed",
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
          error_code: null,
          error_message: null,
          started_at: null,
          completed_at: null,
          artifact: { source_only: true, retrieval_status: "partial" },
          content_md: null,
        },
      ],
      total: 1,
      page: 1,
      per_page: 100,
    });
    const onDerivedStatus = vi.fn();
    renderWorkspace({
      onDerivedStatus,
      detail: makeDetail({
        current_revision_summary: {
          id: 100,
          revision_no: 1,
          parent_revision_id: null,
          job_id: 9,
          source: "pipeline",
          content_sha256: "sha-current",
          fact_status: "invalidated",
          is_current: true,
          created_by: 1,
          created_at: "2026-01-01T00:00:00Z",
        },
      }),
    });

    await waitFor(() =>
      expect(onDerivedStatus).toHaveBeenCalledWith({
        sourceOnly: true,
        retrievalPartial: true,
        evidenceInvalidated: true,
      })
    );
  });

  it("shows the approver's name and time for a Ready draft (name present)", async () => {
    renderWorkspace({
      draft: makeDraft({
        status: "ready",
        ready_at: "2026-02-01T12:00:00Z",
        ready_by: 5,
        ready_by_username: "jordan",
      }),
    });

    const message = await screen.findByTestId("ready-approval");
    expect(message).toHaveTextContent("Approved by jordan on");
    expect(message).not.toHaveTextContent(/\b5\b/);
  });

  it("falls back to an honest message, never a bare id, when the approver's account was deleted", async () => {
    renderWorkspace({
      draft: makeDraft({
        status: "ready",
        ready_at: "2026-02-01T12:00:00Z",
        ready_by: 5,
        ready_by_username: null,
      }),
    });

    const message = await screen.findByTestId("ready-approval");
    expect(message).toHaveTextContent("Approved by a user who no longer has an account on");
    expect(message).not.toHaveTextContent(/\b5\b/);
  });

  it("shows no approval message for a draft that has never been marked Ready", async () => {
    renderWorkspace({ draft: makeDraft({ status: "draft", ready_at: null, ready_by: null, ready_by_username: null }) });
    await screen.findByTestId("stage-rail");
    expect(screen.queryByTestId("ready-approval")).not.toBeInTheDocument();
  });
});

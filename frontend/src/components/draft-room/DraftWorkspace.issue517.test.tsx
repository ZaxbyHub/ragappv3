// Regression test for issue #517 acceptance check AC15: unsaved edits in the
// editor must propagate a stale-checks notice to the findings panel. Expected
// to FAIL until the fix lands; prints an "AC15 CHECK: FAIL" sentinel
// immediately before the first failing assertion. Mirrors
// DraftWorkspace.issue516.test.tsx (leaf-component stubs, partially-mocked
// draftRoom API, ui store reset), except DraftInspector is left REAL so the
// real DraftFindingsPanel renders inside it.
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render as rtlRender, screen, waitFor, fireEvent, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";

import { DraftWorkspace } from "./DraftWorkspace";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import type {
  DraftDetail,
  DraftJob,
  DraftRevisionDetail,
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

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// ---- Leaf-component stubs (DraftInspector deliberately NOT mocked) -------
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
    <div data-testid="stage-artifact">stage:{props.stage?.stage ?? "none"}</div>
  ),
}));
vi.mock("./DraftStageRail", () => ({
  DraftStageRail: () => <div data-testid="stage-rail" />,
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

// The only real `ui/select` consumer left in DraftWorkspace itself is the
// retry-stage picker, which Radix cannot drive in jsdom.
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(value: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: { onValueChange: (v: string) => void; children: React.ReactNode }) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children, id }: { children: React.ReactNode; id?: string }) =>
      React.createElement("div", { role: "group", "aria-label": "Retry from", id }, children),
    SelectValue: () => null,
    SelectContent: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", null, children),
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
  mockListDraftClaims,
  mockListDraftEvidence,
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
  mockListDraftClaims: vi.fn(),
  mockListDraftEvidence: vi.fn(),
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
    listDraftClaims: mockListDraftClaims,
    listDraftEvidence: mockListDraftEvidence,
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
    status: "needs_review",
    tier: "standard",
    lock_version: 3,
    current_revision_id: 100,
    active_job_id: null,
    input_count: 1,
    open_blocker_count: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ready_at: null,
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
    inputs: [],
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
    finding_counts_by_severity: { blocker: 1 },
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
    compile_start_stages: ["research"],
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

function makeRevisionSummary() {
  return {
    id: 100,
    revision_no: 1,
    parent_revision_id: null,
    job_id: 9,
    source: "pipeline" as const,
    content_sha256: "sha-current",
    fact_status: "passed" as const,
    is_current: true,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
  };
}

function makeRevisionDetail(overrides: Partial<DraftRevisionDetail> = {}): DraftRevisionDetail {
  return {
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
    content_md: "Original saved revision text.",
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
  mockGetDraftStages.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mockListDraftRevisions.mockReset().mockResolvedValue({
    items: [makeRevisionSummary()],
    total: 1,
    page: 1,
    per_page: 50,
  });
  mockGetDraftRevision.mockReset().mockResolvedValue(makeRevisionDetail());
  mockListDraftFindings.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
  mockListDraftClaims.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 });
  mockListDraftEvidence.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 });
  mockUpdateDraft.mockReset();
  mockCompileDraft.mockReset();
  mockCancelDraftJob.mockReset();
  mockRetryDraftJob.mockReset();
  mockCreateDraftRevision.mockReset();
  mockDeleteDraft.mockReset();
  mockRestoreDraft.mockReset();
  mockArchiveDraft.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("DraftWorkspace (issue #517)", () => {
  it("AC15: typing in the editor surfaces a stale-checks notice on the findings panel", async () => {
    useDraftRoomUiStore.setState({ workspaceTab: "draft" });
    renderWorkspace();

    const textarea = await screen.findByLabelText("Draft content");
    await waitFor(() => expect(textarea).toHaveValue("Original saved revision text."));

    // Fixture sanity: the real inspector's findings panel is rendered.
    expect(await screen.findByText(/no findings match/i)).toBeInTheDocument();

    console.log("AC15 CHECK: FAIL");
    fireEvent.change(textarea, { target: { value: "Edited text with unsaved changes." } });

    expect(
      await screen.findByText(/reflect the last saved revision/i)
    ).toBeInTheDocument();
  });
});

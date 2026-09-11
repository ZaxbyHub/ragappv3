// Regression tests for issue #533 (PRR-003 + PRR-001(b)): the finding
// activation bar above the editor must return focus to the originating
// finding row, and a finding recorded against a different revision must
// offer compare navigation to that revision (issue #517 AC12).
//
// Mirrors DraftWorkspace.issue517.test.tsx (leaf-component stubs,
// partially-mocked draftRoom API, ui store reset) except DraftInspector AND
// DraftRevisionDiff are left REAL — the real findings panel provides the
// focusable row, and the real diff proves the compare wiring end to end.
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render as rtlRender, screen, waitFor, fireEvent, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { DraftWorkspace } from "./DraftWorkspace";
import { FINDINGS_BACK_LABEL, compareFindingRevisionLabel } from "./labels";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import type {
  DraftDetail,
  DraftFinding,
  DraftJob,
  DraftPaginated,
  DraftRevisionDetail,
  DraftRevisionSummary,
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

// ---- Leaf-component stubs (DraftInspector and DraftRevisionDiff
// deliberately NOT mocked — the real row focus target and diff UI are the
// behaviors under test) -----------------------------------------------
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

// Radix `Select` can't be driven in jsdom — mocked to a plain button-per-item
// list that still exercises the real `onValueChange` wiring. Serves the
// workspace's retry picker and the real DraftRevisionDiff's revision pickers.
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
      revision_no: 2,
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

function makeRevisionSummary(overrides: Partial<DraftRevisionSummary> = {}): DraftRevisionSummary {
  return {
    id: 100,
    revision_no: 2,
    parent_revision_id: null,
    job_id: 9,
    source: "pipeline",
    content_sha256: "sha-current",
    fact_status: "passed",
    is_current: true,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeRevisionDetail(overrides: Partial<DraftRevisionDetail> = {}): DraftRevisionDetail {
  return {
    summary: makeRevisionSummary(),
    content_md: "New text line.\nSecond line.",
    sections: [],
    citations: [],
    qa_summary: {},
    ...overrides,
  };
}

function makeFinding(overrides: Partial<DraftFinding> = {}): DraftFinding {
  return {
    id: 9,
    draft_id: 42,
    revision_id: 100,
    job_id: 9,
    stage: "lint",
    rule_id: "readability_signal.long_sentence",
    rule_version: "1",
    category: "style",
    severity: "warning",
    status: "open",
    waivable: true,
    message: "The sentence is long enough to slow the reader down.",
    original_text: "The review window remains open",
    suggestion: null,
    span_start: 8,
    span_end: 37,
    span_text_sha256: "a".repeat(64),
    resolved_by: null,
    resolved_at: null,
    resolution_note: null,
    waiver_rule_version: null,
    waiver_text_sha256: null,
    created_at: "2026-01-01T00:00:00Z",
    can_apply: false,
    can_dismiss: false,
    can_waive: true,
    ...overrides,
  };
}

function paginated<T>(items: T[]): DraftPaginated<T> {
  return { items, total: items.length, page: 1, per_page: 20 };
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

describe("DraftWorkspace (issue #533)", () => {
  it("Back to findings returns focus to the originating finding row and hides the bar", async () => {
    const finding = makeFinding();
    mockListDraftFindings.mockResolvedValue(paginated([finding]));
    renderWorkspace();

    // Fixture sanity: the real inspector's findings panel rendered the row.
    const showButton = await screen.findByRole("button", { name: /show finding in editor/i });
    fireEvent.click(showButton);

    // Activation reveals the editor with the return bar; the finding is on
    // the current revision, so no compare CTA may appear.
    expect(await screen.findByLabelText("Draft content")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: FINDINGS_BACK_LABEL })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: compareFindingRevisionLabel() })
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: FINDINGS_BACK_LABEL }));

    await waitFor(() => {
      expect(document.activeElement).toHaveAttribute("data-finding-row", String(finding.id));
    });
    expect(screen.queryByRole("button", { name: FINDINGS_BACK_LABEL })).not.toBeInTheDocument();
  });

  it("a finding from another revision offers Compare with current revision and switches to the diff", async () => {
    const finding = makeFinding({ id: 21, revision_id: 99 });
    mockListDraftFindings.mockResolvedValue(paginated([finding]));
    // The diff query needs two distinct revisions: 99 (the finding's) and
    // 100 (the current one).
    mockGetDraftRevision.mockImplementation(async (_draftId: number, revisionId: number) =>
      revisionId === 99
        ? makeRevisionDetail({
            summary: makeRevisionSummary({
              id: 99,
              revision_no: 1,
              parent_revision_id: null,
              content_sha256: "sha-old",
              is_current: false,
            }),
            content_md: "Old text line.\nSecond line.",
          })
        : makeRevisionDetail({ content_md: "New text line.\nSecond line." })
    );
    renderWorkspace();

    fireEvent.click(await screen.findByRole("button", { name: /show finding in editor/i }));

    const compareButton = await screen.findByRole("button", { name: compareFindingRevisionLabel() });
    expect(screen.getByRole("button", { name: FINDINGS_BACK_LABEL })).toBeInTheDocument();
    fireEvent.click(compareButton);

    // The editor switches to the compare tab with the finding's revision as
    // "from" and the current revision as "to".
    await waitFor(() => {
      expect(useDraftRoomUiStore.getState().compareFromRevisionId).toBe(99);
      expect(useDraftRoomUiStore.getState().compareToRevisionId).toBe(100);
    });
    expect(screen.getByRole("tab", { name: "Compare" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByLabelText("Draft content")).not.toBeInTheDocument();

    // The real DraftRevisionDiff renders the two served revisions as a diff.
    const rows = await screen.findAllByTestId("draft-diff-row");
    const removed = rows.filter((row) => row.dataset.diffKind === "removed");
    const added = rows.filter((row) => row.dataset.diffKind === "added");
    expect(removed).toHaveLength(1);
    expect(removed[0]).toHaveTextContent("Old text line.");
    expect(added).toHaveLength(1);
    expect(added[0]).toHaveTextContent("New text line.");
  });
});

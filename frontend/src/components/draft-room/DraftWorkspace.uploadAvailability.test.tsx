// frontend/src/components/draft-room/DraftWorkspace.uploadAvailability.test.tsx
//
// Arm's-length acceptance check C12 for the draft-capability-liveness trace
// (902-905-911-913). Frozen specification: RED at base, GREEN after the fix.
//
// DraftWorkspace must disable the upload CTA (the DraftSourceUpload control
// on the Sources tab) when capabilities report `upload_available: false`
// while `enabled` stays true — a dead parse worker means an upload would
// enqueue a parse job nothing will ever claim. At base
// `uploadCapabilityBlocked` only checks `capabilities == null ||
// enabled === false`, so a worker-down deployment still offers the upload
// CTA. The control leg (`upload_available: true`) pins that the gate is not
// a blanket disable.
//
// Harness mirrors DraftWorkspace.issue516.test.tsx (leaf-component stubs,
// partially-mocked draftRoom API, ui store reset, make* fixtures).
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render as rtlRender, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { DraftWorkspace } from "./DraftWorkspace";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import type { DraftRoomCapabilities } from "@/lib/api/draftRoom";

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
  DraftSourceList: (props: {
    canEdit: boolean;
    editCapabilityBlocked: boolean;
    inputs: unknown[];
  }) => (
    <div data-testid="source-list">
      canEdit:{String(props.canEdit)}|editBlocked:{String(props.editCapabilityBlocked)}|count:
      {props.inputs.length}
    </div>
  ),
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

const { mockListDraftJobs, mockGetDraftStages, mockListDraftRevisions, mockRetryDraftJob } = vi.hoisted(() => ({
  mockListDraftJobs: vi.fn(),
  mockGetDraftStages: vi.fn(),
  mockListDraftRevisions: vi.fn(),
  mockRetryDraftJob: vi.fn(),
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
    compileDraft: vi.fn(),
    cancelDraftJob: vi.fn(),
    retryDraftJob: mockRetryDraftJob,
    createDraftRevision: vi.fn(),
    deleteDraft: vi.fn(),
    restoreDraft: vi.fn(),
    archiveDraft: vi.fn(),
  };
});

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

const DETAIL = {
  summary: {
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
  },
  brief: {},
  inputs: [],
  current_revision_summary: null,
  active_compile_job: null,
  revision_count: 0,
  evidence_count: 0,
  claim_counts_by_status: {},
  finding_counts_by_severity: {},
} as never;

const initialUiState = useDraftRoomUiStore.getState();

function renderWorkspace(capabilities: DraftRoomCapabilities) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DraftWorkspace
        draftId={42}
        draft={DETAIL.summary}
        detail={DETAIL}
        capabilities={capabilities}
        vaultAccess="write"
      />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  useDraftRoomUiStore.setState(initialUiState, true);
  useDraftRoomUiStore.setState({ workspaceTab: "sources" });
  mockListDraftJobs.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mockGetDraftStages.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mockListDraftRevisions.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 });
  mockRetryDraftJob.mockReset();
});

describe("DraftWorkspace upload availability (C12)", () => {
  it("disables the upload CTA when capabilities report upload_available false with enabled true", async () => {
    renderWorkspace({ ...makeCapabilities(), upload_available: false } as DraftRoomCapabilities);
    const upload = await screen.findByTestId("source-upload");
    expect(
      upload,
      "the upload CTA must be disabled when capabilities report upload_available: false (enabled stays true)"
    ).toHaveTextContent("disabled:true");
    expect(
      upload,
      "a worker-off upload disable must explain itself — the disabled reason must be the exact offline-worker message"
    ).toHaveTextContent("reason:Uploads are unavailable while the Draft Room worker is offline.");
    // Only upload is blocked: editing a stored source / input metadata does
    // not need the parse worker, so those actions must stay enabled.
    const list = await screen.findByTestId("source-list");
    expect(
      list,
      "only upload is blocked by upload_available:false — edit-source / input metadata actions must stay enabled"
    ).toHaveTextContent("editBlocked:false");
  });

  it("keeps the upload CTA enabled when upload_available is true", async () => {
    renderWorkspace({ ...makeCapabilities(), upload_available: true } as DraftRoomCapabilities);
    const upload = await screen.findByTestId("source-upload");
    expect(upload).toHaveTextContent("disabled:false");
    expect(upload).toHaveTextContent("reason:none");
  });
});

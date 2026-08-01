import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import { FileText } from "lucide-react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { EmptyState } from "@/components/EmptyState";
import { DraftStageRail } from "@/components/draft-room/DraftStageRail";
import { DraftStatusBanner } from "@/components/draft-room/DraftStatusBanner";
import { DraftRevisionDiff } from "@/components/draft-room/DraftRevisionDiff";
import { DraftAssignmentForm, createDefaultDraftAssignmentFormValue } from "@/components/draft-room/DraftAssignmentForm";
import { DraftSourceUpload } from "@/components/draft-room/DraftSourceUpload";
import { DraftCreateDialog } from "@/components/draft-room/DraftCreateDialog";
import { DraftReadyDialog, type ReadyEligibility } from "@/components/draft-room/DraftReadyDialog";
import { DraftExportDialog } from "@/components/draft-room/DraftExportDialog";
import { DraftPromoteDialog } from "@/components/draft-room/DraftPromoteDialog";
import { draftRoomKeys } from "@/lib/api/draftRoom";
import type { DraftStage, DraftSummary, DraftRevisionSummary, DraftRoomCapabilities } from "@/lib/api/draftRoom";

// jsdom has no ResizeObserver; Radix Checkbox/RadioGroup (rendered unmocked
// by several surfaces below, e.g. DraftAssignmentForm's preserve-* checkboxes
// and DraftPromoteDialog's confirmation checkbox) need it to mount at all.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({ getRootProps: () => ({}), getInputProps: () => ({}), isDragActive: false }),
}));

const { mockListAccessibleVaults, mockGetVault, mockListFolders, mockListTags } = vi.hoisted(() => ({
  mockListAccessibleVaults: vi.fn(),
  mockGetVault: vi.fn(),
  mockListFolders: vi.fn(),
  mockListTags: vi.fn(),
}));

// Only the network-backed lookups DraftAssignmentForm/DraftPromoteDialog make
// on mount — never opened or interacted with here, so Radix Select does not
// need the jsdom pointer-capture workaround the components' own tests use.
vi.mock("@/lib/api", () => ({
  listAccessibleVaults: mockListAccessibleVaults,
  getVault: mockGetVault,
  listFolders: mockListFolders,
  listTags: mockListTags,
}));

beforeEach(() => {
  mockListAccessibleVaults.mockReset().mockResolvedValue({ vaults: [] });
  mockGetVault.mockReset().mockResolvedValue({ id: 7, name: "Research vault" });
  mockListFolders.mockReset().mockResolvedValue([]);
  mockListTags.mockReset().mockResolvedValue([]);
});

function withQueryClient(children: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { queryClient, ui: <QueryClientProvider client={queryClient}>{children}</QueryClientProvider> };
}

function makeStage(overrides: Partial<DraftStage> & Pick<DraftStage, "stage" | "status">): DraftStage {
  return {
    id: 1,
    job_id: 1,
    attempt: 1,
    input_sha256: "sha",
    artifact_sha256: null,
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
    artifact: null,
    content_md: null,
    ...overrides,
  };
}

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
    current_revision_id: 501,
    active_job_id: null,
    input_count: 1,
    open_blocker_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ready_at: null,
    ...overrides,
  };
}

function makeRevision(overrides: Partial<DraftRevisionSummary> = {}): DraftRevisionSummary {
  return {
    id: 501,
    revision_no: 3,
    parent_revision_id: 500,
    job_id: 900,
    source: "pipeline",
    content_sha256: "abcdef0123456789",
    fact_status: "passed",
    is_current: true,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeCapabilities(overrides: Partial<DraftRoomCapabilities> = {}): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["compose", "rewrite"],
    tiers: ["standard"],
    piece_types: ["article"],
    transformation_strengths: ["light"],
    limits: {},
    export_formats: ["md"],
    logical_model_modes: ["default"],
    default_logical_mode: "default",
    compile_start_stages: ["research"],
    compile_stage_order: ["research"],
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

describe("accessibility smoke gate", () => {
  it("has no axe violations for the empty-state surface", async () => {
    const { container } = render(
      <EmptyState
        icon={FileText}
        title="No documents yet"
        description="Upload files or request write access from an administrator."
        action={{ label: "Open Vaults", onClick: () => {} }}
      />
    );

    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftStageRail with pending/running/warning/blocked/failed states", async () => {
    const stageOrder = ["research", "outline", "draft", "lint", "copy"];
    const stages: DraftStage[] = [
      makeStage({ stage: "research", status: "completed" }),
      makeStage({ stage: "outline", status: "running" }),
      makeStage({ stage: "draft", status: "failed", error_code: "model_timeout" }),
    ];
    const { container } = render(
      <DraftStageRail
        stageOrder={stageOrder}
        stages={stages}
        activeStage="outline"
        selectedStage="research"
        onSelectStage={() => {}}
        jobStatus="running"
        blockerCountsByStage={{ lint: 2 }}
        warningCountsByStage={{ copy: 1 }}
      />
    );

    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftStatusBanner with every banner rendered at once", async () => {
    const { container } = render(
      <DraftStatusBanner
        draft={makeDraft({ status: "needs_review" })}
        detail={undefined}
        factStatus="not_run"
        sourceOnly
        retrievalPartial
        evidenceInvalidated
        vaultAccess="revoked"
        onRerunResearch={() => {}}
        onRerunNewsroom={() => {}}
      />
    );

    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftRevisionDiff with added/removed/unchanged rows", async () => {
    const revisions = [makeRevision({ id: 1, revision_no: 1 }), makeRevision({ id: 2, revision_no: 2 })];
    const { container } = render(
      <DraftRevisionDiff
        revisions={revisions}
        fromRevisionId={1}
        toRevisionId={2}
        onSelectFrom={() => {}}
        onSelectTo={() => {}}
        fromContent={"unchanged line\nremoved line\n"}
        toContent={"unchanged line\nadded line\n"}
      />
    );

    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftAssignmentForm (create variant)", async () => {
    const { ui } = withQueryClient(
      <DraftAssignmentForm
        value={createDefaultDraftAssignmentFormValue()}
        onChange={() => {}}
        variant="create"
        idPrefix="a11y-create"
      />
    );
    const { container } = render(ui);
    await screen.findByLabelText("Project title");

    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftSourceUpload with no files queued", async () => {
    const { ui } = withQueryClient(<DraftSourceUpload draftId={1} maxInputs={10} currentInputCount={0} />);
    const { container } = render(ui);

    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftCreateDialog, focused and open", async () => {
    const { ui } = withQueryClient(
      <DraftCreateDialog open onOpenChange={() => {}} onCreated={() => {}} />
    );
    const { baseElement } = render(ui);
    await screen.findByRole("heading", { name: "Create a drafting project" });

    const results = await axe(baseElement);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftReadyDialog, focused and open with a source-only acknowledgment", async () => {
    const eligibility: ReadyEligibility = { ok: false, blockers: ["source_only_acknowledgment_required"] };
    const { baseElement } = render(
      <DraftReadyDialog
        open
        onOpenChange={() => {}}
        draft={makeDraft()}
        revision={makeRevision()}
        eligibility={eligibility}
        requiresSourceOnlyAck
        onReady={() => {}}
      />
    );
    await screen.findByRole("heading", { name: "Mark Ready" });

    const results = await axe(baseElement);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftExportDialog, focused and open", async () => {
    const { queryClient, ui } = withQueryClient(
      <DraftExportDialog open onOpenChange={() => {}} draftId={42} revision={makeRevision()} isReadyRevision={false} />
    );
    queryClient.setQueryData(draftRoomKeys.capabilities(), makeCapabilities());
    const { baseElement } = render(ui);
    await screen.findByRole("heading", { name: "Export" });

    const results = await axe(baseElement);
    expect(results.violations).toHaveLength(0);
  });

  it("has no axe violations for DraftPromoteDialog, focused and open", async () => {
    const { ui } = withQueryClient(
      <MemoryRouter>
        <DraftPromoteDialog
          open
          onOpenChange={() => {}}
          draft={makeDraft({ status: "ready" })}
          inputs={[]}
          revisions={[makeRevision()]}
          currentRevisionId={501}
          canWrite
          onPromoted={() => {}}
        />
      </MemoryRouter>
    );
    const { baseElement } = render(ui);
    await screen.findByRole("heading", { name: "Promote to vault" });

    const results = await axe(baseElement);
    expect(results.violations).toHaveLength(0);
  });
});

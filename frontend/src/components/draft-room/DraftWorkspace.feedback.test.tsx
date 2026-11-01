import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as React from "react";

import { DraftWorkspace } from "./DraftWorkspace";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import type {
  DraftDetail,
  DraftInput,
  DraftRoomCapabilities,
  DraftSummary,
} from "@/lib/api/draftRoom";

const mocks = vi.hoisted(() => ({
  listDraftJobs: vi.fn(),
  getDraftStages: vi.fn(),
  listDraftRevisions: vi.fn(),
  getDraftRevision: vi.fn(),
  listDraftFindings: vi.fn(),
  updateDraft: vi.fn(),
  compileDraft: vi.fn(),
  cancelDraftJob: vi.fn(),
  retryDraftJob: vi.fn(),
  createDraftRevision: vi.fn(),
  deleteDraft: vi.fn(),
  restoreDraft: vi.fn(),
  archiveDraft: vi.fn(),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

vi.mock("./DraftAssignmentForm", async () => {
  const React = await import("react");
  return {
    DRAFT_ASSIGNMENT_FIELD_ORDER: [],
    DraftAssignmentForm: () => React.createElement("div", { "data-testid": "assignment" }),
    focusFirstInvalidDraftAssignmentField: vi.fn(),
    validateDraftAssignmentForm: () => ({}),
  };
});

vi.mock("./DraftSourceUpload", async () => {
  const React = await import("react");
  return {
    DraftSourceUpload: (props: { maxInputs: number }) =>
      React.createElement("div", { "data-testid": "source-upload" }, `max:${props.maxInputs}`),
  };
});
vi.mock("./DraftSourceList", () => ({ DraftSourceList: () => null }));
vi.mock("./DraftEvidencePanel", () => ({ DraftEvidencePanel: () => null }));
vi.mock("./DraftEditor", () => ({ DraftEditor: () => null }));
vi.mock("./DraftPreview", () => ({ DraftPreview: () => null }));
vi.mock("./DraftRevisionDiff", () => ({ DraftRevisionDiff: () => null }));
vi.mock("./DraftStageArtifact", () => ({ DraftStageArtifact: () => null }));
vi.mock("./DraftStageRail", () => ({ DraftStageRail: () => null }));
vi.mock("./DraftInspector", () => ({ DraftInspector: () => null }));
vi.mock("./DraftReadyDialog", () => ({ DraftReadyDialog: () => null }));
vi.mock("./DraftExportDialog", () => ({ DraftExportDialog: () => null }));
vi.mock("./DraftPromoteDialog", () => ({ DraftPromoteDialog: () => null }));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    ...mocks,
  };
});

vi.mock("@/components/ui/button", async () => {
  const React = await import("react");
  return { Button: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) => React.createElement("button", props) };
});
vi.mock("@/components/ui/alert", async () => {
  const React = await import("react");
  const Box = (props: React.HTMLAttributes<HTMLDivElement>) => React.createElement("div", props);
  return { Alert: Box, AlertDescription: Box, AlertTitle: Box };
});
vi.mock("@/components/ui/label", async () => {
  const React = await import("react");
  return { Label: (props: React.LabelHTMLAttributes<HTMLLabelElement>) => React.createElement("label", props) };
});
vi.mock("@/components/ui/dialog", async () => {
  const React = await import("react");
  const Box = (props: React.HTMLAttributes<HTMLDivElement>) => React.createElement("div", props);
  return {
    Dialog: ({ open, children }: { open: boolean; children: React.ReactNode }) => (open ? children : null),
    DialogContent: (props: React.HTMLAttributes<HTMLDivElement>) =>
      React.createElement("div", { ...props, role: "dialog" }),
    DialogDescription: Box,
    DialogFooter: Box,
    DialogHeader: Box,
    DialogTitle: (props: React.HTMLAttributes<HTMLHeadingElement>) => React.createElement("h2", props),
  };
});
vi.mock("@/components/ui/sheet", async () => {
  const React = await import("react");
  const Box = (props: React.HTMLAttributes<HTMLDivElement>) => React.createElement("div", props);
  return {
    Sheet: ({ open, children }: { open: boolean; children: React.ReactNode }) => (open ? children : null),
    SheetContent: Box,
    SheetHeader: Box,
    SheetTitle: (props: React.HTMLAttributes<HTMLHeadingElement>) => React.createElement("h2", props),
  };
});
vi.mock("@/components/ui/tabs", async () => {
  const React = await import("react");
  const TabsContext = React.createContext("assignment");
  return {
    Tabs: ({ value, children }: { value: string; children: React.ReactNode }) =>
      React.createElement(TabsContext.Provider, { value }, children),
    TabsList: (props: React.HTMLAttributes<HTMLDivElement>) => React.createElement("div", props),
    TabsTrigger: ({ value, children }: { value: string; children: React.ReactNode }) =>
      React.createElement("button", { type: "button" }, children),
    TabsContent: ({ value, children }: { value: string; children: React.ReactNode }) =>
      React.useContext(TabsContext) === value ? React.createElement("div", null, children) : null,
  };
});
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  return {
    Select: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectContent: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectItem: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectTrigger: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectValue: () => null,
  };
});

function makeDraft(): DraftSummary {
  return {
    id: 42,
    vault_id: 7,
    vault_access: "write",
    title: "Issue 773 draft",
    mode: "compose",
    status: "draft",
    tier: "standard",
    lock_version: 3,
    current_revision_id: null,
    active_job_id: null,
    input_count: 1,
    open_blocker_count: 0,
    created_at: "2026-09-30T00:00:00Z",
    updated_at: "2026-09-30T00:00:00Z",
    ready_at: null,
    ready_by: null,
    ready_by_username: null,
  } as DraftSummary;
}

function makeInput(parse_status: DraftInput["parse_status"]): DraftInput {
  return {
    id: Math.random(),
    role: "manuscript",
    authority: "primary",
    as_of_date: null,
    original_name: "source.md",
    extension: "md",
    media_type: "text/markdown",
    size_bytes: 10,
    content_sha256: "sha",
    parse_status,
    parse_error: null,
    parsed_char_count: 10,
    active_parse_job_id: null,
    last_parse_job_id: null,
    created_at: "2026-09-30T00:00:00Z",
  } as DraftInput;
}

function makeDetail(inputs: DraftInput[]): DraftDetail {
  return {
    summary: makeDraft(),
    brief: {} as DraftDetail["brief"],
    inputs,
    current_revision_summary: null,
    active_compile_job: null,
    revision_count: 0,
    evidence_count: 0,
    claim_counts_by_status: {},
    finding_counts_by_severity: {},
  } as DraftDetail;
}

function makeCapabilities(limits: Record<string, unknown> = { job_max_model_calls: 40, max_inputs: 10 }): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["compose"],
    tiers: ["standard"],
    piece_types: ["article"],
    transformation_strengths: ["moderate"],
    limits,
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
  } as DraftRoomCapabilities;
}

function renderWorkspace(
  overrides: Partial<React.ComponentProps<typeof DraftWorkspace>> = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const detail = overrides.detail ?? makeDetail([makeInput("ready")]);
  const draft = overrides.draft ?? detail.summary;
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftWorkspace
          draftId={42}
          draft={draft}
          detail={detail}
          capabilities={makeCapabilities()}
          vaultAccess="write"
          {...overrides}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useDraftRoomUiStore.getState().resetForDraft(42);
  mocks.listDraftJobs.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mocks.getDraftStages.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mocks.listDraftRevisions.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 });
  mocks.getDraftRevision.mockReset();
  mocks.listDraftFindings.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
});

afterEach(() => cleanup());

describe("issue #773 Draft Room capability contract", () => {
  it("shows the server-reported job_max_model_calls in the compile confirmation", async () => {
    const user = userEvent.setup();
    renderWorkspace({ capabilities: makeCapabilities({ job_max_model_calls: 40, max_inputs: 10 }) });

    await user.click(screen.getByRole("button", { name: "Create draft" }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).queryAllByText("Not reported by the server").length).toBe(0);
    expect(within(dialog).getByText("40")).toBeInTheDocument();
  });

  it("blocks compile when any input is not ready", async () => {
    renderWorkspace({
      detail: makeDetail([makeInput("ready"), makeInput("failed")]),
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Create draft" }).hasAttribute("disabled")).toBe(true),
    );
  });

  it("fails closed on maxInputs while capabilities are loading", () => {
    useDraftRoomUiStore.setState({ workspaceTab: "sources" });
    renderWorkspace({ capabilities: undefined });

    const text = screen.getByTestId("source-upload").textContent ?? "";
    const max = Number(text.match(/max:(\d+)/)?.[1]);
    expect(max).toBe(10);
  });
});

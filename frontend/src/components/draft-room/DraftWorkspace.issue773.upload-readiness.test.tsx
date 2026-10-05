import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as React from "react";

import { DraftWorkspace } from "./DraftWorkspace";
import { ADD_SOURCE_FILES_CTA } from "./labels";
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
  uploadDraftInput: vi.fn(),
  getDraft: vi.fn(),
  getDraftInputContent: vi.fn(),
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

vi.mock("react-dropzone", () => ({
  useDropzone: ({ onDrop, disabled }: { onDrop: (files: File[]) => void; disabled: boolean }) => ({
    getRootProps: () => ({
      "data-drop-disabled": disabled ? "true" : "false",
      onDrop: (event: React.DragEvent<HTMLElement>) => {
        if (!disabled) onDrop(Array.from(event.dataTransfer?.files ?? []));
      },
    }),
    getInputProps: () => ({}),
    isDragActive: false,
  }),
}));

vi.mock("@/components/ui/input", async () => {
  const React = await import("react");
  return { Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => React.createElement("input", props) };
});

vi.mock("@/components/ui/progress", async () => {
  const React = await import("react");
  return { Progress: (props: React.HTMLAttributes<HTMLDivElement>) => React.createElement("div", props) };
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
  return {
    Button: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) => React.createElement("button", props),
    buttonVariants: () => "",
  };
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
    id: 1,
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
  return {
    queryClient,
    ...render(
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
    ),
  };
}

beforeEach(() => {
  useDraftRoomUiStore.getState().resetForDraft(42);
  mocks.listDraftJobs.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mocks.getDraftStages.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mocks.listDraftRevisions.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 });
  mocks.getDraftRevision.mockReset();
  mocks.listDraftFindings.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 100 });
  mocks.uploadDraftInput.mockReset();
  mocks.getDraft.mockReset();
  mocks.getDraftInputContent.mockReset();
});

afterEach(() => cleanup());

describe("issue #773 Draft source upload readiness", () => {
  it("fails closed through the real file/drop controls until capabilities load, then uploads", async () => {
    const user = userEvent.setup();
    useDraftRoomUiStore.setState({ workspaceTab: "sources" });
    const { rerender, queryClient } = renderWorkspace({ capabilities: undefined });
    const file = new File(["source"], "source.md", { type: "text/markdown" });
    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    const dropTarget = screen.getByText("Drag and drop files here, or").parentElement;

    expect(fileInput).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Loading Draft Room capabilities");
    fireEvent.drop(dropTarget!, { dataTransfer: { files: [file] } });
    await user.upload(fileInput, file);
    expect(mocks.uploadDraftInput).not.toHaveBeenCalled();

    rerender(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <DraftWorkspace
            draftId={42}
            draft={makeDraft()}
            detail={makeDetail([makeInput("ready")])}
            capabilities={{ ...makeCapabilities(), enabled: false }}
            vaultAccess="write"
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(screen.getByLabelText(ADD_SOURCE_FILES_CTA)).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Draft Room is not enabled on this deployment");

    mocks.uploadDraftInput.mockResolvedValue({ input: { id: 1, parse_status: "ready" } });
    rerender(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <DraftWorkspace
            draftId={42}
            draft={makeDraft()}
            detail={makeDetail([makeInput("ready")])}
            capabilities={makeCapabilities()}
            vaultAccess="write"
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    const enabledInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    expect(enabledInput).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await user.upload(enabledInput, file);
    await waitFor(() => expect(mocks.uploadDraftInput).toHaveBeenCalledTimes(1));
  });
});

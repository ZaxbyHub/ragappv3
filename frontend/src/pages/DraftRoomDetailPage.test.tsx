import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import DraftRoomDetailPage from "./DraftRoomDetailPage";
import { DRAFT_ROOM_DISABLED_MESSAGE } from "@/components/draft-room/labels";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import type { DraftDetail, DraftRoomCapabilities } from "@/lib/api/draftRoom";

const {
  mockGetDraft,
  mockGetDraftRevision,
  mockUseDraftRoomCapabilities,
  mockRequestCompile,
} = vi.hoisted(() => ({
  mockGetDraft: vi.fn(),
  mockGetDraftRevision: vi.fn(),
  mockUseDraftRoomCapabilities: vi.fn(),
  mockRequestCompile: vi.fn(),
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return { ...actual, getDraft: mockGetDraft, getDraftRevision: mockGetDraftRevision };
});

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: mockUseDraftRoomCapabilities,
}));

vi.mock("@/hooks/useDraftRoomEvents", () => ({
  useDraftRoomEvents: () => ({ connected: false, pollingFallback: false, lastEvent: null }),
}));

vi.mock("@/components/draft-room/DraftStatusBanner", () => ({
  DraftStatusBanner: (props: {
    sourceOnly: boolean;
    retrievalPartial: boolean;
    evidenceInvalidated: boolean;
    vaultAccess: string;
    factStatus: string | null;
    onRerunResearch?: () => void;
    onRerunNewsroom?: () => void;
  }) => (
    <div data-testid="status-banner">
      <span>
        sourceOnly:{String(props.sourceOnly)}|retrievalPartial:{String(props.retrievalPartial)}|
        evidenceInvalidated:{String(props.evidenceInvalidated)}|vaultAccess:{props.vaultAccess}|factStatus:
        {String(props.factStatus)}
      </span>
      <button type="button" onClick={props.onRerunResearch}>
        rerun-research
      </button>
      <button type="button" onClick={props.onRerunNewsroom}>
        rerun-newsroom
      </button>
    </div>
  ),
}));

vi.mock("@/components/draft-room/DraftWorkspace", async () => {
  const React = await import("react");
  interface StubProps {
    draftId: number;
    vaultAccess: string;
    onDerivedStatus?: (status: { sourceOnly: boolean; retrievalPartial: boolean; evidenceInvalidated: boolean }) => void;
  }
  const DraftWorkspace = React.forwardRef<{ requestCompile: (s?: string) => void }, StubProps>((props, ref) => {
    React.useImperativeHandle(ref, () => ({ requestCompile: mockRequestCompile }), []);
    React.useEffect(() => {
      props.onDerivedStatus?.({ sourceOnly: true, retrievalPartial: false, evidenceInvalidated: false });
      // Fire once per mount to simulate the workspace's own derivation effect.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return React.createElement(
      "div",
      { "data-testid": "workspace" },
      `draftId:${props.draftId}|vaultAccess:${props.vaultAccess}`
    );
  });
  return { DraftWorkspace };
});

function makeDetail(overrides: Partial<DraftDetail> = {}): DraftDetail {
  return {
    summary: {
      id: 42,
      vault_id: 7,
      vault_access: "write",
      title: "Q3 press release",
      mode: "compose",
      status: "needs_review",
      tier: "standard",
      lock_version: 1,
      current_revision_id: 100,
      active_job_id: null,
      input_count: 1,
      open_blocker_count: 0,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      ready_at: null,
    },
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
    finding_counts_by_severity: {},
    ...overrides,
  };
}

function makeCapabilities(overrides: Partial<DraftRoomCapabilities> = {}): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["rewrite", "compose"],
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

const initialUiState = useDraftRoomUiStore.getState();

function renderDetailPage(draftId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/draft-room/${draftId}`]}>
        <Routes>
          <Route path="/draft-room" element={<div>Draft Room List</div>} />
          <Route path="/draft-room/:draftId" element={<DraftRoomDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...utils, queryClient };
}

beforeEach(() => {
  useDraftRoomUiStore.setState(initialUiState, true);
  mockGetDraft.mockReset();
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
  mockUseDraftRoomCapabilities.mockReset().mockReturnValue({
    data: makeCapabilities(),
    isLoading: false,
    isError: false,
  });
  mockRequestCompile.mockReset();
  vi.mocked(window.confirm).mockReset().mockReturnValue(true);
});

describe("DraftRoomDetailPage", () => {
  it("renders an honest not-found state for an invalid draft id, without ever querying the server", () => {
    renderDetailPage("not-a-number");
    expect(screen.getByText("Draft not found")).toBeInTheDocument();
    expect(mockGetDraft).not.toHaveBeenCalled();
  });

  it("shows a layout-matched loading skeleton before the detail resolves", () => {
    mockGetDraft.mockReturnValue(new Promise(() => {}));
    renderDetailPage("42");
    expect(screen.getByTestId("draft-detail-skeleton")).toBeInTheDocument();
  });

  it("renders an honest not-found state for a 404", async () => {
    const error = Object.assign(new Error("not found"), { status: 404 });
    mockGetDraft.mockRejectedValue(error);
    renderDetailPage("42");
    expect(await screen.findByText("Draft not found")).toBeInTheDocument();
  });

  it("renders a permission-denied state for a 403", async () => {
    const error = Object.assign(new Error("forbidden"), { status: 403 });
    mockGetDraft.mockRejectedValue(error);
    renderDetailPage("42");
    expect(await screen.findByText("You don't have access to this project")).toBeInTheDocument();
  });

  it("shows the capability-disabled banner while still rendering the workspace", async () => {
    mockGetDraft.mockResolvedValue(makeDetail());
    mockUseDraftRoomCapabilities.mockReturnValue({
      data: makeCapabilities({ enabled: false }),
      isLoading: false,
      isError: false,
    });
    renderDetailPage("42");

    expect(await screen.findByText(DRAFT_ROOM_DISABLED_MESSAGE)).toBeInTheDocument();
    expect(screen.getByTestId("workspace")).toBeInTheDocument();
  });

  it("feeds the workspace's derived status and current fact status into the status banner", async () => {
    mockGetDraft.mockResolvedValue(makeDetail());
    renderDetailPage("42");

    await waitFor(() =>
      expect(screen.getByTestId("status-banner").textContent).toMatch(
        /sourceOnly:true\|retrievalPartial:false\|\s*evidenceInvalidated:false\|vaultAccess:write\|factStatus:\s*passed/
      )
    );
  });

  it("wires the status banner's rerun CTAs to the workspace's compile trigger via the imperative handle", async () => {
    const user = userEvent.setup();
    mockGetDraft.mockResolvedValue(makeDetail());
    renderDetailPage("42");
    await screen.findByTestId("workspace");

    await user.click(screen.getByRole("button", { name: "rerun-research" }));
    expect(mockRequestCompile).toHaveBeenCalledWith("research");

    await user.click(screen.getByRole("button", { name: "rerun-newsroom" }));
    expect(mockRequestCompile).toHaveBeenCalledWith(undefined);
  });

  it("warns before an in-app navigation with unsaved changes and does not navigate when the warning is declined", async () => {
    const user = userEvent.setup();
    mockGetDraft.mockResolvedValue(makeDetail());
    renderDetailPage("42");
    await screen.findByTestId("workspace");

    useDraftRoomUiStore.setState((state) => ({ draftText: { ...state.draftText, 42: "Edited text." } }));
    vi.mocked(window.confirm).mockReturnValue(false);

    await user.click(screen.getByRole("link", { name: /back to draft room/i }));

    expect(window.confirm).toHaveBeenCalled();
    expect(screen.queryByText("Draft Room List")).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace")).toBeInTheDocument();
  });

  it("navigates away without a prompt once there are no unsaved changes", async () => {
    const user = userEvent.setup();
    mockGetDraft.mockResolvedValue(makeDetail());
    renderDetailPage("42");
    await screen.findByTestId("workspace");

    await user.click(screen.getByRole("link", { name: /back to draft room/i }));

    expect(window.confirm).not.toHaveBeenCalled();
    expect(await screen.findByText("Draft Room List")).toBeInTheDocument();
  });

  it("allows navigation with unsaved changes once the warning is accepted", async () => {
    const user = userEvent.setup();
    mockGetDraft.mockResolvedValue(makeDetail());
    renderDetailPage("42");
    await screen.findByTestId("workspace");

    useDraftRoomUiStore.setState((state) => ({ draftText: { ...state.draftText, 42: "Edited text." } }));
    vi.mocked(window.confirm).mockReturnValue(true);

    await user.click(screen.getByRole("link", { name: /back to draft room/i }));

    expect(window.confirm).toHaveBeenCalled();
    expect(await screen.findByText("Draft Room List")).toBeInTheDocument();
  });

  it("renders exactly one main landmark when nested inside the app shell's main region", async () => {
    mockGetDraft.mockResolvedValue(makeDetail());
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/draft-room/42"]}>
          <main id="main-content">
            <Routes>
              <Route path="/draft-room/:draftId" element={<DraftRoomDetailPage />} />
            </Routes>
          </main>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await screen.findByTestId("workspace");
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });
});

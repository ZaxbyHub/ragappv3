import { describe, expect, it, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import DraftRoomPage from "./DraftRoomPage";
import { DRAFT_ROOM_DISABLED_MESSAGE } from "@/components/draft-room/labels";
import type { DraftSummary } from "@/lib/api/draftRoom";

const render: typeof rtlRender = (ui, options) => rtlRender(ui, { wrapper: MemoryRouter, ...options });

const {
  mockNavigate,
  mockListAccessibleVaults,
  mockListDrafts,
  mockGetDraftRoomCapabilities,
} = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  mockListAccessibleVaults: vi.fn(),
  mockListDrafts: vi.fn(),
  mockGetDraftRoomCapabilities: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock("@/lib/api", () => ({
  listAccessibleVaults: mockListAccessibleVaults,
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    listDrafts: mockListDrafts,
    getDraftRoomCapabilities: mockGetDraftRoomCapabilities,
  };
});

vi.mock("@/components/draft-room/DraftCreateDialog", () => ({
  DraftCreateDialog: (props: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    defaultVaultId: number | null;
    onCreated: (draft: DraftSummary) => void;
  }) =>
    props.open ? (
      <div data-testid="create-dialog">
        defaultVault:{String(props.defaultVaultId)}
        <button type="button" onClick={() => props.onCreated(makeDraft({ id: 99 }))}>
          simulate-created
        </button>
      </div>
    ) : null,
}));

function makeDraft(overrides: Partial<DraftSummary> = {}): DraftSummary {
  return {
    id: 1,
    vault_id: 7,
    vault_access: "write",
    title: "Q3 press release",
    mode: "compose",
    status: "draft",
    tier: "standard",
    lock_version: 1,
    current_revision_id: null,
    active_job_id: null,
    input_count: 2,
    open_blocker_count: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ready_at: null,
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DraftRoomPage />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockNavigate.mockReset();
  mockListAccessibleVaults.mockReset();
  mockListDrafts.mockReset();
  mockGetDraftRoomCapabilities.mockReset().mockResolvedValue({
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
  });
});

describe("DraftRoomPage", () => {
  it("shows a layout-matched skeleton while loading, with no empty-state flash", () => {
    mockListAccessibleVaults.mockReturnValue(new Promise(() => {}));
    mockListDrafts.mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(screen.getByTestId("draft-list-skeleton")).toBeInTheDocument();
    expect(screen.queryByText(/no projects/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/you need vault access/i)).not.toBeInTheDocument();
  });

  it("explains the vault requirement and links to Vaults when the user has no accessible vault", async () => {
    const user = userEvent.setup();
    mockListAccessibleVaults.mockResolvedValue({ vaults: [] });
    mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
    renderPage();

    expect(await screen.findByText("You need vault access to use Draft Room")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Go to Vaults" }));
    expect(mockNavigate).toHaveBeenCalledWith("/vaults");
    expect(screen.getByRole("button", { name: NEW_DRAFT_CTA_TEXT })).toBeDisabled();
  });

  it("shows the purpose statement, privacy note, and New draft CTA when the vault exists but no projects do", async () => {
    mockListAccessibleVaults.mockResolvedValue({ vaults: [{ id: 7, name: "Research vault" }] });
    mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
    renderPage();

    expect(await screen.findByText("Start your first drafting project")).toBeInTheDocument();
    expect(screen.getByText(/stay private to this project/i)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: NEW_DRAFT_CTA_TEXT }).length).toBeGreaterThan(0);
  });

  it("shows the honest unavailable message and disables New draft when the capability is off, while the list still works", async () => {
    mockGetDraftRoomCapabilities.mockResolvedValue({
      enabled: false,
      modes: [],
      tiers: [],
      piece_types: [],
      transformation_strengths: [],
      limits: {},
      export_formats: [],
      logical_model_modes: [],
      default_logical_mode: "default",
      compile_start_stages: [],
      compile_stage_order: [],
      prompt_bundle_version: "v1",
      editorial_gates_installed: false,
      compile_available: false,
      findings_available: false,
      claims_available: false,
      evidence_available: false,
      ready_available: false,
      promote_available: false,
    });
    mockListAccessibleVaults.mockResolvedValue({ vaults: [{ id: 7, name: "Research vault" }] });
    mockListDrafts.mockResolvedValue({ items: [makeDraft()], total: 1, page: 1, per_page: 20 });
    renderPage();

    expect(await screen.findByText(DRAFT_ROOM_DISABLED_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: NEW_DRAFT_CTA_TEXT })).toBeDisabled();
    expect(screen.getByText("Q3 press release")).toBeInTheDocument();
  });

  it("renders the drafts list with vault name, status, updated date, counts, and an Open link, and paginates", async () => {
    mockListAccessibleVaults.mockResolvedValue({ vaults: [{ id: 7, name: "Research vault" }] });
    mockListDrafts.mockResolvedValue({
      items: [makeDraft()],
      total: 45,
      page: 1,
      per_page: 20,
    });
    renderPage();

    expect(await screen.findByText("Q3 press release")).toBeInTheDocument();
    expect(screen.getByText("Research vault")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/draft-room/1");
    expect(screen.getByText((_, element) => element?.textContent === "Showing 1 to 20 of 45 projects")).toBeInTheDocument();
  });

  it("opens the create dialog with the current vault filter and navigates to the new draft on success", async () => {
    const user = userEvent.setup();
    mockListAccessibleVaults.mockResolvedValue({ vaults: [{ id: 7, name: "Research vault" }] });
    mockListDrafts.mockResolvedValue({ items: [makeDraft()], total: 1, page: 1, per_page: 20 });
    renderPage();

    await screen.findByText("Q3 press release");
    await user.click(screen.getAllByRole("button", { name: NEW_DRAFT_CTA_TEXT })[0]);

    const dialog = await screen.findByTestId("create-dialog");
    expect(dialog).toHaveTextContent("defaultVault:null");
    await user.click(screen.getByRole("button", { name: "simulate-created" }));
    expect(mockNavigate).toHaveBeenCalledWith("/draft-room/99");
  });
});

const NEW_DRAFT_CTA_TEXT = "New draft";

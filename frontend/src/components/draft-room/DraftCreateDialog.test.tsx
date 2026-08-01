import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { DraftCreateDialog } from "./DraftCreateDialog";
import { draftRoomKeys, type DraftCreateRequest, type DraftSummary } from "@/lib/api/draftRoom";
import { DRAFT_ROOM_DISABLED_MESSAGE } from "./labels";

// jsdom has no ResizeObserver; Radix's Checkbox (rendered inside the real
// DraftAssignmentForm here, unmocked) needs it.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const { mockCreateDraft, mockListAccessibleVaults } = vi.hoisted(() => ({
  mockCreateDraft: vi.fn(),
  mockListAccessibleVaults: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api", () => ({
  listAccessibleVaults: mockListAccessibleVaults,
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    createDraft: mockCreateDraft,
  };
});

function makeSummary(overrides: Partial<DraftSummary> = {}): DraftSummary {
  return {
    id: 42,
    vault_id: 7,
    vault_access: "write",
    title: "Q3 press release",
    mode: "compose",
    status: "draft",
    tier: "standard",
    lock_version: 1,
    current_revision_id: null,
    active_job_id: null,
    input_count: 0,
    open_blocker_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ready_at: null,
    ...overrides,
  };
}

function renderDialog(props: Partial<React.ComponentProps<typeof DraftCreateDialog>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const onOpenChange = vi.fn();
  const onCreated = vi.fn();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <DraftCreateDialog
        open
        onOpenChange={onOpenChange}
        onCreated={onCreated}
        {...props}
      />
    </QueryClientProvider>
  );
  return { ...utils, onOpenChange, onCreated, invalidateSpy };
}

beforeEach(() => {
  mockCreateDraft.mockReset();
  mockListAccessibleVaults.mockReset();
  mockListAccessibleVaults.mockResolvedValue({ vaults: [] });
  vi.mocked(toast.success).mockClear();
});

describe("DraftCreateDialog", () => {
  it("focuses the dialog heading on open (positive control for the rAF focus pattern)", async () => {
    renderDialog();
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Create a drafting project" }));
    });
  });

  it("shows inline errors and focuses the first invalid field on submit", async () => {
    const user = userEvent.setup();
    mockListAccessibleVaults.mockResolvedValue({
      vaults: [{ id: 7, name: "Research vault" }],
    });
    renderDialog();
    await screen.findByLabelText("Vault");

    await user.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByText("Title must be 1-300 characters.")).toBeInTheDocument();
    expect(screen.getByText("Choose a vault.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Project title")).toHaveFocus();
    });
    expect(mockCreateDraft).not.toHaveBeenCalled();
  });

  it("creates the draft with the exact payload, toasts, invalidates, and calls onCreated", async () => {
    const user = userEvent.setup();
    const created = makeSummary();
    mockCreateDraft.mockResolvedValue(created);

    const { onOpenChange, onCreated, invalidateSpy } = renderDialog({ defaultVaultId: 7 });

    await user.type(screen.getByLabelText("Project title"), "Q3 press release");
    await user.type(screen.getByLabelText("Audience"), "Local reporters");
    await user.type(screen.getByLabelText("Purpose"), "Announce the Q3 results");

    await user.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(mockCreateDraft).toHaveBeenCalledTimes(1));
    const payload = mockCreateDraft.mock.calls[0][0] as DraftCreateRequest;
    expect(payload).toEqual({
      vault_id: 7,
      title: "Q3 press release",
      mode: "compose",
      tier: "standard",
      brief: expect.objectContaining({
        audience: "Local reporters",
        purpose: "Announce the Q3 results",
      }),
    });

    expect(toast.success).toHaveBeenCalled();
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.lists() });
    expect(onCreated).toHaveBeenCalledWith(created);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders the honest unavailable message for a draft_room_disabled 503", async () => {
    const user = userEvent.setup();
    const error = new Error("draft room is disabled") as Error & {
      status?: number;
      originalError?: { response?: { data?: { detail?: string; code?: string } } };
    };
    error.status = 503;
    error.originalError = { response: { data: { detail: "draft room is disabled", code: "draft_room_disabled" } } };
    mockCreateDraft.mockRejectedValue(error);

    renderDialog({ defaultVaultId: 7 });
    await user.type(screen.getByLabelText("Project title"), "Q3 press release");
    await user.type(screen.getByLabelText("Audience"), "Local reporters");
    await user.type(screen.getByLabelText("Purpose"), "Announce the Q3 results");
    await user.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByText(DRAFT_ROOM_DISABLED_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText("draft room is disabled")).not.toBeInTheDocument();
  });

  it("renders the server's detail for an ordinary 422 failure", async () => {
    const user = userEvent.setup();
    const error = new Error("brief.audience: field required") as Error & {
      status?: number;
      originalError?: { response?: { data?: { detail?: string; code?: string } } };
    };
    error.status = 422;
    mockCreateDraft.mockRejectedValue(error);

    renderDialog({ defaultVaultId: 7 });
    await user.type(screen.getByLabelText("Project title"), "Q3 press release");
    await user.type(screen.getByLabelText("Audience"), "Local reporters");
    await user.type(screen.getByLabelText("Purpose"), "Announce the Q3 results");
    await user.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByText("brief.audience: field required")).toBeInTheDocument();
  });
});

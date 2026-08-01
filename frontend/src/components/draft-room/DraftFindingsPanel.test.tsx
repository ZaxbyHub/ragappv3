import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DraftFinding, DraftPaginated, FindingDispositionResponse } from "@/lib/api/draftRoom";

const listDraftFindingsMock = vi.hoisted(() => vi.fn());
const setDraftFindingDispositionMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    listDraftFindings: listDraftFindingsMock,
    setDraftFindingDisposition: setDraftFindingDispositionMock,
  };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { DraftFindingsPanel } from "./DraftFindingsPanel";
import { draftRoomKeys } from "@/lib/api/draftRoom";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";

function makeFinding(overrides: Partial<DraftFinding> = {}): DraftFinding {
  return {
    id: 1,
    draft_id: 42,
    revision_id: 7,
    job_id: 5,
    stage: "lint",
    rule_id: "no-boilerplate",
    rule_version: "1",
    category: "boilerplate",
    severity: "warning",
    status: "open",
    waivable: true,
    message: "Removed a boilerplate phrase.",
    original_text: "In today's fast-paced world",
    suggestion: "",
    span_start: 0,
    span_end: 10,
    span_text_sha256: "a".repeat(64),
    resolved_by: null,
    resolved_at: null,
    resolution_note: null,
    waiver_rule_version: null,
    waiver_text_sha256: null,
    created_at: "2026-01-01T00:00:00Z",
    can_apply: true,
    can_dismiss: true,
    can_waive: true,
    ...overrides,
  };
}

function paginated<T>(items: T[]): DraftPaginated<T> {
  return { items, total: items.length, page: 1, per_page: 20 };
}

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

const defaultProps = {
  draftId: 42,
  revisionId: 7,
  lockVersion: 3,
  baseRevisionId: 6,
  canDispose: true,
};

describe("DraftFindingsPanel", () => {
  afterEach(() => {
    cleanup();
    listDraftFindingsMock.mockReset();
    setDraftFindingDispositionMock.mockReset();
    useDraftRoomUiStore.getState().resetForDraft(0);
  });

  it("renders Apply/Dismiss/Waive if and only if the server flag is true", async () => {
    listDraftFindingsMock.mockResolvedValue(
      paginated([
        makeFinding({
          id: 1,
          severity: "blocker",
          can_apply: false,
          can_dismiss: false,
          can_waive: true,
        }),
        makeFinding({
          id: 2,
          severity: "warning",
          can_apply: true,
          can_dismiss: true,
          can_waive: false,
        }),
      ])
    );
    render(<DraftFindingsPanel {...defaultProps} />, { wrapper });

    await waitFor(() => expect(listDraftFindingsMock).toHaveBeenCalled());

    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(2);

    // Blocker: can_dismiss is false — Dismiss must not render even though it's a blocker.
    expect(within(rows[0]).queryByRole("button", { name: "Apply" })).not.toBeInTheDocument();
    expect(within(rows[0]).queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    expect(within(rows[0]).getByRole("button", { name: "Waive" })).toBeInTheDocument();

    // Second finding: apply + dismiss allowed, waive not.
    expect(within(rows[1]).getByRole("button", { name: "Apply" })).toBeInTheDocument();
    expect(within(rows[1]).getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(within(rows[1]).queryByRole("button", { name: "Waive" })).not.toBeInTheDocument();
  });

  it("blocks Waive confirmation until a reason is entered and shows the consequence", async () => {
    listDraftFindingsMock.mockResolvedValue(paginated([makeFinding({ id: 9, can_waive: true })]));
    render(<DraftFindingsPanel {...defaultProps} />, { wrapper });

    await waitFor(() => expect(listDraftFindingsMock).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "Waive" }));

    const confirmButton = await screen.findByRole("button", { name: /confirm waiver/i });
    expect(confirmButton).toBeDisabled();
    expect(screen.getByText(/waiver no longer matches the text it was granted for/i)).toBeInTheDocument();

    const textarea = screen.getByLabelText("Reason for waiver");
    fireEvent.change(textarea, { target: { value: "  " } });
    expect(confirmButton).toBeDisabled();

    fireEvent.change(textarea, { target: { value: "Sole source is the official record." } });
    expect(confirmButton).not.toBeDisabled();
  });

  it("posts the exact disposition payload and invalidates detail/revisions/findings/claims", async () => {
    const finding = makeFinding({ id: 9, can_apply: true });
    listDraftFindingsMock.mockResolvedValue(paginated([finding]));
    const revision = {
      id: 8,
      revision_no: 2,
      parent_revision_id: 7,
      job_id: 5,
      source: "manual" as const,
      content_sha256: "b".repeat(64),
      fact_status: "not_run" as const,
      is_current: true,
      created_by: 1,
      created_at: "2026-01-02T00:00:00Z",
    };
    const response: FindingDispositionResponse = { finding: { ...finding, status: "applied" }, revision };
    setDraftFindingDispositionMock.mockResolvedValue(response);

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const onRevisionCreated = vi.fn();

    render(
      <QueryClientProvider client={queryClient}>
        <DraftFindingsPanel {...defaultProps} onRevisionCreated={onRevisionCreated} />
      </QueryClientProvider>
    );

    await waitFor(() => expect(listDraftFindingsMock).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "Apply" }));

    await waitFor(() => expect(setDraftFindingDispositionMock).toHaveBeenCalledTimes(1));
    expect(setDraftFindingDispositionMock).toHaveBeenCalledWith(42, 9, {
      action: "apply",
      base_revision_id: 6,
      lock_version: 3,
    });

    await waitFor(() => expect(onRevisionCreated).toHaveBeenCalledWith(revision));

    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey);
    expect(invalidatedKeys).toContainEqual(draftRoomKeys.detail(42));
    expect(invalidatedKeys).toContainEqual(draftRoomKeys.revisions(42));
    expect(invalidatedKeys).toContainEqual(draftRoomKeys.findings(42));
    expect(invalidatedKeys).toContainEqual(draftRoomKeys.claims(42));
  });

  it("renders a conflict state with Reload on a 409 and never retries the mutation automatically", async () => {
    const finding = makeFinding({ id: 9, can_dismiss: true });
    listDraftFindingsMock.mockResolvedValue(paginated([finding]));
    setDraftFindingDispositionMock.mockRejectedValue({
      message: "conflict",
      status: 409,
      originalError: { response: { data: { detail: "stale", code: "conflict" } } },
    });

    render(<DraftFindingsPanel {...defaultProps} />, { wrapper });

    await waitFor(() => expect(listDraftFindingsMock).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(setDraftFindingDispositionMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/changed in another tab/i)).toBeInTheDocument();
    const reloadButton = screen.getByRole("button", { name: /reload/i });

    fireEvent.click(reloadButton);
    // Reload must not blindly resubmit the disposition.
    expect(setDraftFindingDispositionMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/changed in another tab/i)).not.toBeInTheDocument();
  });

  it("disables actions when canDispose is false without hiding them", async () => {
    listDraftFindingsMock.mockResolvedValue(paginated([makeFinding({ id: 3, can_apply: true })]));
    render(<DraftFindingsPanel {...defaultProps} canDispose={false} />, { wrapper });

    await waitFor(() => expect(listDraftFindingsMock).toHaveBeenCalled());
    const applyButton = await screen.findByRole("button", { name: "Apply" });
    expect(applyButton).toBeDisabled();
  });
});

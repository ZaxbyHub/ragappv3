// Regression tests for issue #517 acceptance checks AC12 (finding-row
// navigation to the editor span / evidence) and AC13a (human-readable
// rule/category labels with raw diagnostics available on demand).
//
// These tests assert REQUIRED behavior that does not exist yet and are
// expected to FAIL until the fix lands; each prints an "AC<n> CHECK: FAIL"
// sentinel immediately before its first failing assertion. Mirrors
// DraftFindingsPanel.test.tsx (api-client mock, ui store reset, makeFinding).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DraftFinding, DraftPaginated } from "@/lib/api/draftRoom";

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
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";

function makeFinding(overrides: Partial<DraftFinding> = {}): DraftFinding {
  return {
    id: 9,
    draft_id: 42,
    revision_id: 7,
    job_id: 5,
    stage: "fact",
    rule_id: "fact.claim_unsupported",
    rule_version: "1",
    category: "factuality",
    severity: "blocker",
    status: "open",
    waivable: false,
    message: "An atomic claim is unsupported.",
    original_text: "The review window is 30 days",
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
  tier: "standard" as const,
};

describe("DraftFindingsPanel (issue #517)", () => {
  afterEach(() => {
    cleanup();
    listDraftFindingsMock.mockReset();
    setDraftFindingDispositionMock.mockReset();
    useDraftRoomUiStore.getState().resetForDraft(0);
  });

  it("AC12: clicking a finding row requests the editor select its span and opens the related evidence", async () => {
    const finding = makeFinding();
    listDraftFindingsMock.mockResolvedValue(paginated([finding]));

    // NEW-SURFACE contract: the panel accepts select-span / open-evidence
    // handlers and a keyboard-focusable row. Cast through unknown so this
    // acceptance test compiles before the props exist; once the fix adds
    // them, the cast is a harmless no-op.
    const onSelectSpan = vi.fn();
    const onOpenEvidence = vi.fn();
    const extraProps = { onSelectSpan, onOpenEvidence } as unknown as Partial<
      React.ComponentProps<typeof DraftFindingsPanel>
    >;

    render(<DraftFindingsPanel {...defaultProps} {...extraProps} />, { wrapper });
    await waitFor(() => expect(listDraftFindingsMock).toHaveBeenCalled());
    const row = await screen.findByRole("listitem");

    console.log("AC12 CHECK: FAIL");
    fireEvent.click(row);
    expect(onSelectSpan).toHaveBeenCalledTimes(1);
    expect(onSelectSpan).toHaveBeenCalledWith(
      expect.objectContaining({ id: 9, span_start: 8, span_end: 37 })
    );
    expect(onOpenEvidence).toHaveBeenCalledTimes(1);
    expect(onOpenEvidence).toHaveBeenCalledWith(expect.objectContaining({ id: 9 }));

    // Focus can return to the finding row after inspecting the span: the
    // row (or a control inside it) must be keyboard-focusable.
    const focusable = row.matches("[tabindex],button,a")
      ? row
      : row.querySelector("[tabindex],button,a");
    expect(focusable).not.toBeNull();
  });

  it("AC13a: renders a human-readable explanation with the raw rule code available on demand", async () => {
    listDraftFindingsMock.mockResolvedValue(
      paginated([
        makeFinding({
          category: "boilerplate",
          rule_id: "BP-001",
          rule_version: "2",
          message: "A blocked boilerplate phrase was rewritten.",
        }),
      ])
    );
    render(<DraftFindingsPanel {...defaultProps} />, { wrapper });
    await waitFor(() => expect(listDraftFindingsMock).toHaveBeenCalled());
    const row = await screen.findByRole("listitem");

    console.log("AC13a CHECK: FAIL");

    // (1) An accessible, human-readable explanation is attached to the row.
    const describedBy = row.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    const explanation = describedBy
      ? (within(document.body).queryByText((_, element) => element?.id === describedBy) ?? null)
      : null;
    expect(explanation).not.toBeNull();
    const explanationText = (explanation?.textContent ?? "").trim();
    expect(explanationText.split(/\s+/).length).toBeGreaterThanOrEqual(3);
    expect(explanationText).not.toContain("BP-001");
    expect(explanationText).not.toBe("boilerplate");

    // (2) The raw diagnostic codes stay available on demand — a collapsed
    // details/tooltip-style disclosure that contains them, not the default
    // rendering of the row.
    const diagnostic = row.querySelector("details,[data-diagnostic]");
    expect(diagnostic).not.toBeNull();
    expect(diagnostic?.textContent).toContain("BP-001");
    expect(diagnostic?.textContent).toContain("boilerplate");
    if (diagnostic?.tagName === "DETAILS") {
      expect(diagnostic).not.toHaveAttribute("open");
    }

    // The row's default visible copy is no longer just the raw codes.
    expect(row.textContent).not.toMatch(/boilerplate\s*·\s*BP-001/);
  });
});

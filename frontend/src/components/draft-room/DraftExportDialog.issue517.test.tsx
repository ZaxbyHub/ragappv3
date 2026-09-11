// Regression test for issue #517 acceptance check AC13b (frontend part):
// the export dialog must disclose unresolved blocker findings BEFORE the
// export button downloads anything. Expected to FAIL until the fix lands;
// prints an "AC13b CHECK: FAIL" sentinel immediately before the first
// failing assertion. Mirrors DraftExportDialog.test.tsx.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { DraftExportDialog } from "./DraftExportDialog";
import type { DraftRevisionSummary, DraftRoomCapabilities } from "@/lib/api/draftRoom";

// jsdom has no ResizeObserver; Radix's Checkbox and Select need it.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const { mockExportDraftRevision, mockGetCapabilities } = vi.hoisted(() => ({
  mockExportDraftRevision: vi.fn(),
  mockGetCapabilities: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    exportDraftRevision: mockExportDraftRevision,
    getDraftRoomCapabilities: mockGetCapabilities,
  };
});

function makeCapabilities(overrides: Partial<DraftRoomCapabilities> = {}): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["compose", "rewrite"],
    tiers: ["standard", "high_stakes", "sensitive"],
    piece_types: ["article"],
    transformation_strengths: ["light", "moderate", "substantial"],
    limits: {},
    export_formats: ["md"],
    logical_model_modes: ["instant", "thinking"],
    default_logical_mode: "instant",
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

function makeRevision(overrides: Partial<DraftRevisionSummary> = {}): DraftRevisionSummary {
  return {
    id: 501,
    revision_no: 3,
    parent_revision_id: 500,
    job_id: 900,
    source: "pipeline",
    content_sha256: "abcdef0123456789",
    fact_status: "findings",
    is_current: true,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  mockExportDraftRevision.mockReset();
  mockGetCapabilities.mockReset().mockResolvedValue(makeCapabilities());
  vi.mocked(toast.success).mockClear();
  global.URL.createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
  global.URL.revokeObjectURL = vi.fn();
});

describe("DraftExportDialog (issue #517)", () => {
  it("AC13b: shows the unresolved-blocker disclosure before any export runs", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // NEW-SURFACE contract: the dialog takes the count of OPEN BLOCKER
    // findings for the revision. Cast through unknown so this acceptance
    // test compiles before the prop exists.
    const extraProps = { openBlockers: 1 } as unknown as Partial<
      React.ComponentProps<typeof DraftExportDialog>
    >;
    render(
      <QueryClientProvider client={queryClient}>
        <DraftExportDialog
          open
          onOpenChange={vi.fn()}
          draftId={42}
          revision={makeRevision({ fact_status: "findings" })}
          isReadyRevision={false}
          {...extraProps}
        />
      </QueryClientProvider>
    );

    console.log("AC13b CHECK: FAIL");
    const disclosure = await screen.findByText(/1 unresolved blocker/i);
    expect(disclosure).toBeInTheDocument();

    // The disclosure is visible BEFORE the export button is used.
    expect(mockExportDraftRevision).not.toHaveBeenCalled();
  });
});

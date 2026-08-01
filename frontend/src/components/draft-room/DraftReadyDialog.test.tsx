import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { DraftReadyDialog, type ReadyEligibility } from "./DraftReadyDialog";
import { READY_BLOCKER_LABELS, READY_MEANING } from "./labels";
import type { DraftRevisionSummary, DraftSummary } from "@/lib/api/draftRoom";

// jsdom has no ResizeObserver; Radix's Checkbox (rendered when a source-only
// acknowledgment is required) needs it.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const { mockMarkReady } = vi.hoisted(() => ({ mockMarkReady: vi.fn() }));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    markDraftRevisionReady: mockMarkReady,
  };
});

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
    input_count: 2,
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
    content_sha256: "abcdef0123456789fedcba9876543210",
    fact_status: "passed",
    is_current: true,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeError(code: string, detail = "server rejected the request") {
  const err = new Error(detail) as Error & {
    status?: number;
    originalError?: { response?: { data?: { detail?: string; code?: string } } };
  };
  err.status = 409;
  err.originalError = { response: { data: { detail, code } } };
  return err;
}

function renderDialog(props: Partial<React.ComponentProps<typeof DraftReadyDialog>> = {}) {
  const onOpenChange = vi.fn();
  const onReady = vi.fn();
  const defaultEligibility: ReadyEligibility = { ok: true, blockers: [] };
  const utils = render(
    <DraftReadyDialog
      open
      onOpenChange={onOpenChange}
      draft={makeDraft()}
      revision={makeRevision()}
      eligibility={defaultEligibility}
      requiresSourceOnlyAck={false}
      onReady={onReady}
      {...props}
    />
  );
  return { ...utils, onOpenChange, onReady };
}

const ALL_CHECKLIST_CODES = [
  "active_job",
  "fact_not_current",
  "fact_candidate_mismatch",
  "unresolved_claim_blocker",
  "non_waivable_blocker",
  "unresolved_blocker",
  "invalid_waiver",
  "stale_waiver",
  "evidence_changed",
  "source_deleted",
  "source_only_acknowledgment_required",
];

beforeEach(() => {
  mockMarkReady.mockReset();
  vi.mocked(toast.success).mockClear();
});

describe("DraftReadyDialog", () => {
  it("focuses the dialog heading on open, not the first focusable control", async () => {
    renderDialog();
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Mark Ready" }));
    });
  });

  it("renders one checklist row per eligibility condition with pass/fail as icon + text", () => {
    renderDialog({ eligibility: { ok: false, blockers: ["fact_not_current", "evidence_changed"] } });

    const list = screen.getByRole("list", { name: "Ready eligibility checklist" });
    const rows = Array.from(list.querySelectorAll("li"));
    expect(rows).toHaveLength(ALL_CHECKLIST_CODES.length);
    ALL_CHECKLIST_CODES.forEach((code, i) => {
      expect(rows[i]).toHaveTextContent(READY_BLOCKER_LABELS[code]);
    });

    // evidence_changed and source_deleted intentionally share the same wording
    // (EVIDENCE_INVALIDATED_WARNING) — assert by row position, not by text.
    expect(rows[ALL_CHECKLIST_CODES.indexOf("fact_not_current")]).toHaveTextContent("Blocked:");
    expect(rows[ALL_CHECKLIST_CODES.indexOf("evidence_changed")]).toHaveTextContent("Blocked:");
    expect(rows[ALL_CHECKLIST_CODES.indexOf("source_deleted")]).toHaveTextContent("Clear:");
    expect(rows[ALL_CHECKLIST_CODES.indexOf("active_job")]).toHaveTextContent("Clear:");
  });

  it("disables submit while a blocker exists", () => {
    renderDialog({ eligibility: { ok: false, blockers: ["active_job"] } });
    expect(screen.getByRole("button", { name: "Mark Ready" })).toBeDisabled();
  });

  it("disables submit while a required source-only acknowledgment is unchecked, and enables it once checked", async () => {
    const user = userEvent.setup();
    renderDialog({ requiresSourceOnlyAck: true });

    expect(screen.getByRole("button", { name: "Mark Ready" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "Mark Ready" })).toBeEnabled();
  });

  it("does not render the source-only acknowledgment checkbox when not required", () => {
    renderDialog({ requiresSourceOnlyAck: false });
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("names the exact revision being approved", () => {
    renderDialog({ revision: makeRevision({ revision_no: 7, content_sha256: "deadbeefcafef00d1234" }) });
    expect(screen.getByText(/Revision 7/)).toBeInTheDocument();
    expect(screen.getByTestId("ready-revision-sha")).toHaveTextContent("deadbeefcafe");
  });

  it("posts the exact payload and calls onReady on success", async () => {
    const user = userEvent.setup();
    const summary = makeDraft({ status: "ready" });
    mockMarkReady.mockResolvedValue(summary);
    const { onReady, onOpenChange } = renderDialog({
      draft: makeDraft({ id: 42, lock_version: 5 }),
      revision: makeRevision({ id: 501 }),
    });

    await user.click(screen.getByRole("button", { name: "Mark Ready" }));

    await waitFor(() => expect(mockMarkReady).toHaveBeenCalledTimes(1));
    expect(mockMarkReady).toHaveBeenCalledWith(42, 501, {
      lock_version: 5,
      acknowledge_source_only: false,
    });
    expect(onReady).toHaveBeenCalledWith(summary);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders READY_MEANING and no truth/publication language", () => {
    renderDialog();
    expect(screen.getByText(READY_MEANING)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/verified true|factually true|human-written|\bpublished\b/i);
  });

  for (const code of ALL_CHECKLIST_CODES) {
    it(`renders the specific message for a 409 "${code}" response`, async () => {
      const user = userEvent.setup();
      mockMarkReady.mockRejectedValue(makeError(code));
      renderDialog();

      await user.click(screen.getByRole("button", { name: "Mark Ready" }));

      await waitFor(() => {
        const list = screen.getByRole("list", { name: "Ready eligibility checklist" });
        const rows = Array.from(list.querySelectorAll("li"));
        const row = rows[ALL_CHECKLIST_CODES.indexOf(code)];
        expect(row).toHaveTextContent("Blocked:");
        expect(row).toHaveTextContent(READY_BLOCKER_LABELS[code]);
      });
    });
  }
});

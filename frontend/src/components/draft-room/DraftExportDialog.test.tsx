import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { DraftExportDialog } from "./DraftExportDialog";
import {
  EXPORT_ACK_LABEL,
  EXPORT_READY_EXPLANATION,
  EXPORT_REVIEW_EXPLANATION,
  EXPORT_UNVERIFIED_EXPLANATION,
} from "./labels";
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
    fact_status: "passed",
    is_current: true,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderDialog(props: Partial<React.ComponentProps<typeof DraftExportDialog>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenChange = vi.fn();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <DraftExportDialog
        open
        onOpenChange={onOpenChange}
        draftId={42}
        revision={makeRevision()}
        isReadyRevision={false}
        {...props}
      />
    </QueryClientProvider>
  );
  return { ...utils, onOpenChange };
}

beforeEach(() => {
  mockExportDraftRevision.mockReset();
  mockGetCapabilities.mockReset();
  mockGetCapabilities.mockResolvedValue(makeCapabilities());
  vi.mocked(toast.success).mockClear();

  // jsdom does not implement the Blob URL APIs, and clicking a real anchor
  // makes jsdom log an unimplemented-navigation error — stub both away.
  global.URL.createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
  global.URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

describe("DraftExportDialog", () => {
  it("focuses the dialog heading on open, not the format select", async () => {
    renderDialog();
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Export" }));
    });
  });

  it("focuses the heading FIRST — no other control receives focus on the way there", async () => {
    // A capturing focusin listener records every element focused from mount
    // onward. A rAF-deferred focus() lets Radix's FocusScope focus the Cancel
    // button first (a real focus/focusin event a screen reader would announce)
    // before the rAF steals focus a frame later; the terminal activeElement
    // check above alone does not catch that.
    const focusedTags: string[] = [];
    const onFocusIn = (event: FocusEvent) => {
      const target = event.target as HTMLElement;
      focusedTags.push(`${target.tagName}:${target.textContent?.trim() ?? ""}`);
    };
    document.addEventListener("focusin", onFocusIn, true);

    try {
      renderDialog();
      await waitFor(() => {
        expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Export" }));
      });
      expect(focusedTags[0]).toBe("H2:Export");
      expect(focusedTags).toHaveLength(1);
    } finally {
      document.removeEventListener("focusin", onFocusIn, true);
    }
  });

  it("shows fact status and approval status before download", async () => {
    renderDialog({
      revision: makeRevision({ fact_status: "findings" }),
      isReadyRevision: false,
    });
    expect(await screen.findByText("Fact-checked with findings")).toBeInTheDocument();
    expect(screen.getByText("Not ready")).toBeInTheDocument();
    expect(mockExportDraftRevision).not.toHaveBeenCalled();
  });

  it.each(["not_run", "running", "invalidated"] as const)(
    "requires the acknowledgment checkbox and shows the UNVERIFIED explanation for fact_status=%s",
    async (factStatus) => {
      renderDialog({ revision: makeRevision({ fact_status: factStatus }) });
      await screen.findByText(EXPORT_UNVERIFIED_EXPLANATION);
      expect(screen.getByText(EXPORT_ACK_LABEL)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
    }
  );

  it.each(["passed", "findings"] as const)(
    "does not show the acknowledgment checkbox for fact_status=%s",
    async (factStatus) => {
      renderDialog({ revision: makeRevision({ fact_status: factStatus }) });
      await waitFor(() => expect(mockGetCapabilities).toHaveBeenCalled());
      expect(screen.queryByText(EXPORT_ACK_LABEL)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Export" })).toBeEnabled();
    }
  );

  it("shows the REVIEW explanation for a fact-current, not-yet-Ready revision", async () => {
    renderDialog({ revision: makeRevision({ fact_status: "passed" }), isReadyRevision: false });
    expect(await screen.findByText(EXPORT_REVIEW_EXPLANATION)).toBeInTheDocument();
  });

  it("shows the ordinary-filename explanation for the current Ready revision", async () => {
    renderDialog({ revision: makeRevision({ fact_status: "passed" }), isReadyRevision: true });
    expect(await screen.findByText(EXPORT_READY_EXPLANATION)).toBeInTheDocument();
  });

  it("downloads using the server-provided filename and the exact blob, then revokes the object URL", async () => {
    const user = userEvent.setup();
    const blob = new Blob(["# Title\n\nBody text."], { type: "text/markdown" });
    mockExportDraftRevision.mockResolvedValue({
      blob,
      filename: "q3-release-rev3-REVIEW.md",
      factStatus: "passed",
      approvalStatus: "not_ready",
      contentSha256: "abc123",
    });

    let capturedAnchor: HTMLAnchorElement | null = null;
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tagName: string) => {
      const el = originalCreateElement(tagName);
      if (tagName.toLowerCase() === "a") capturedAnchor = el as HTMLAnchorElement;
      return el;
    });

    renderDialog({ revision: makeRevision({ fact_status: "passed" }), isReadyRevision: false });
    await screen.findByText(EXPORT_REVIEW_EXPLANATION);

    await user.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() => expect(mockExportDraftRevision).toHaveBeenCalledTimes(1));
    expect(mockExportDraftRevision).toHaveBeenCalledWith(42, 501, {
      format: "md",
      acknowledge_not_fact_checked: false,
    });

    await waitFor(() => expect(global.URL.createObjectURL).toHaveBeenCalledWith(blob));
    expect(capturedAnchor?.download).toBe("q3-release-rev3-REVIEW.md");
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
  });

  it("never injects a warning into the downloaded content", async () => {
    const user = userEvent.setup();
    const blob = new Blob(["exact server bytes"], { type: "text/markdown" });
    mockExportDraftRevision.mockResolvedValue({
      blob,
      filename: "draft-UNVERIFIED.md",
      factStatus: "not_run",
      approvalStatus: "not_ready",
      contentSha256: "abc123",
    });

    renderDialog({ revision: makeRevision({ fact_status: "not_run" }) });
    await screen.findByText(EXPORT_ACK_LABEL);
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() => expect(global.URL.createObjectURL).toHaveBeenCalledTimes(1));
    // The exact same Blob reference reaches the download call — nothing wraps or rewrites it.
    expect(global.URL.createObjectURL).toHaveBeenCalledWith(blob);
    const passedBlob = (global.URL.createObjectURL as ReturnType<typeof vi.fn>).mock.calls[0][0] as Blob;
    expect(passedBlob.size).toBe(blob.size);
  });

  it("sends acknowledge_not_fact_checked=true once the checkbox is checked", async () => {
    const user = userEvent.setup();
    mockExportDraftRevision.mockResolvedValue({
      blob: new Blob(["x"]),
      filename: "draft-UNVERIFIED.md",
      factStatus: "not_run",
      approvalStatus: "not_ready",
      contentSha256: "abc123",
    });

    renderDialog({ revision: makeRevision({ fact_status: "not_run" }) });
    await screen.findByText(EXPORT_ACK_LABEL);
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() =>
      expect(mockExportDraftRevision).toHaveBeenCalledWith(42, 501, {
        format: "md",
        acknowledge_not_fact_checked: true,
      })
    );
  });
});

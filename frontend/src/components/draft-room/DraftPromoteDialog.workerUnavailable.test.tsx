// frontend/src/components/draft-room/DraftPromoteDialog.workerUnavailable.test.tsx
//
// Arm's-length acceptance check C19 (promote leg) for the
// draft-capability-liveness trace (902-905-911-913). Frozen specification:
// RED at base, GREEN after the fix.
//
// When a promote from DraftPromoteDialog rejects with a Draft Room error
// whose code is `worker_unavailable` (HTTP 503, body
// {"detail":"draft room worker is unavailable","code":"worker_unavailable"}),
// the Draft Room capabilities query (draftRoomKeys.capabilities()) must be
// invalidated/refetched — the ingestion worker died and promote must stop
// being offered for the query's stale window. At base the dialog's error
// path only toasts.
//
// Harness mirrors DraftPromoteDialog.test.tsx (mocked promoteDraftSource /
// vault API, Radix select reshaped to plain buttons).
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { toast } from "sonner";

import { DraftPromoteDialog } from "./DraftPromoteDialog";
import { draftRoomKeys, type DraftRoomCapabilities } from "@/lib/api/draftRoom";
import type { Folder, Tag, Vault } from "@/lib/api";

// jsdom has no ResizeObserver; Radix's Checkbox needs it.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const { mockPromoteDraftSource, mockGetVault, mockListFolders, mockListTags, capsQueryFn } =
  vi.hoisted(() => ({
    mockPromoteDraftSource: vi.fn(),
    mockGetVault: vi.fn(),
    mockListFolders: vi.fn(),
    mockListTags: vi.fn(),
    capsQueryFn: vi.fn(),
  }));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api", () => ({
  getVault: mockGetVault,
  listFolders: mockListFolders,
  listTags: mockListTags,
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    promoteDraftSource: mockPromoteDraftSource,
  };
});

// Radix Select cannot be opened in jsdom; reshaped to plain buttons that
// still fire the real onValueChange path (same idiom as the existing spec).
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: { onValueChange: (v: string) => void; children: React.ReactNode }) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectValue: ({ placeholder }: { placeholder?: string }) => React.createElement("span", null, placeholder),
    SelectContent: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement("button", { type: "button", onClick: () => onValueChange(value) }, children);
    },
  };
});

function makeDraft() {
  return {
    id: 42,
    vault_id: 7,
    vault_access: "write",
    title: "Q3 press release",
    mode: "compose",
    status: "ready",
    tier: "standard",
    lock_version: 3,
    current_revision_id: 501,
    active_job_id: null,
    input_count: 1,
    open_blocker_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ready_at: "2026-01-02T00:00:00Z",
  } as never;
}

function makeInput() {
  return {
    id: 900,
    role: "manuscript",
    authority: "primary",
    as_of_date: null,
    original_name: "source.docx",
    extension: ".docx",
    media_type: null,
    size_bytes: 1024,
    content_sha256: "deadbeef",
    parse_status: "ready",
    parse_error: null,
    parsed_char_count: 500,
    active_parse_job_id: null,
    last_parse_job_id: null,
    created_at: "2026-01-01T00:00:00Z",
  } as never;
}

function makeRevision() {
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
  } as never;
}

function makeVault(overrides: Partial<Vault> = {}): Vault {
  return {
    id: 7,
    name: "Research vault",
    description: "",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    file_count: 5,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    current_user_permission: "write",
    effective_enrichment_enabled: false,
    ...overrides,
  };
}

function makeCapabilities(): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["compose"],
    tiers: ["standard"],
    piece_types: ["article"],
    transformation_strengths: ["light"],
    limits: { max_inputs: 20 },
    export_formats: ["md"],
    logical_model_modes: ["default"],
    default_logical_mode: "default",
    compile_start_stages: ["research"],
    compile_stage_order: ["research", "assemble"],
    prompt_bundle_version: "v1",
    editorial_gates_installed: true,
    compile_available: true,
    findings_available: true,
    claims_available: true,
    evidence_available: true,
    ready_available: true,
    promote_available: true,
    upload_available: true,
  };
}

/** The apiClient-normalized 503 worker_unavailable error (real shape parseDraftRoomError reads). */
function workerUnavailableError(): Error {
  const error = new Error("Request failed with status code 503");
  (error as unknown as { status: number }).status = 503;
  (error as unknown as { originalError: unknown }).originalError = {
    response: {
      status: 503,
      data: {
        detail: "draft room worker is unavailable",
        code: "worker_unavailable",
      },
    },
  };
  return error;
}

/** Mounted consumer of the capabilities query — observes invalidation refetches. */
function CapabilitiesProbe() {
  useQuery({
    queryKey: draftRoomKeys.capabilities(),
    queryFn: capsQueryFn,
    staleTime: Infinity,
    retry: false,
  });
  return null;
}

function renderDialog() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CapabilitiesProbe />
        <DraftPromoteDialog
          open
          onOpenChange={vi.fn()}
          draft={makeDraft()}
          inputs={[makeInput()]}
          revisions={[makeRevision()]}
          currentRevisionId={501}
          canWrite
          onPromoted={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockPromoteDraftSource.mockReset();
  mockGetVault.mockReset().mockResolvedValue(makeVault());
  mockListFolders.mockReset().mockResolvedValue([] as Folder[]);
  mockListTags.mockReset().mockResolvedValue([] as Tag[]);
  capsQueryFn.mockReset().mockResolvedValue(makeCapabilities());
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

describe("DraftPromoteDialog worker_unavailable invalidation (C19 promote leg)", () => {
  it("refetches the capabilities query when a promote rejects with worker_unavailable", async () => {
    const user = userEvent.setup();
    mockPromoteDraftSource.mockRejectedValue(workerUnavailableError());
    renderDialog();

    await waitFor(() => expect(capsQueryFn).toHaveBeenCalledTimes(1));
    // Mount-time baseline, captured BEFORE the action is triggered —
    // the implementation invalidates inside onError, which can land
    // before any later side effect is observed.
    const baseline = capsQueryFn.mock.calls.length;
    await user.click(
      screen.getByRole("checkbox", { name: /understand and want to promote/i })
    );
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    await waitFor(() => expect(mockPromoteDraftSource).toHaveBeenCalledTimes(1));
    const refetched = await waitFor(
      () => {
        if (capsQueryFn.mock.calls.length > baseline) return true;
        expect(capsQueryFn.mock.calls.length, "capabilities query must refetch").toBeGreaterThan(baseline);
        return false;
      },
      { timeout: 1500 }
    ).then(
      () => true,
      () => false
    );
    expect(
      refetched,
      "a worker_unavailable rejection from promote must invalidate/refetch the Draft Room capabilities query"
    ).toBe(true);
  });
});

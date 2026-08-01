import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { toast } from "sonner";
import { DraftPromoteDialog } from "./DraftPromoteDialog";
import { PROMOTE_CONSEQUENCE } from "./labels";
import {
  draftRoomKeys,
  type DraftInput,
  type DraftRevisionSummary,
  type DraftSummary,
  type PromoteResponse,
} from "@/lib/api/draftRoom";
import type { Folder, Tag, Vault } from "@/lib/api";

// jsdom has no ResizeObserver; Radix's Checkbox needs it.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const { mockPromoteDraftSource, mockGetVault, mockListFolders, mockListTags } = vi.hoisted(() => ({
  mockPromoteDraftSource: vi.fn(),
  mockGetVault: vi.fn(),
  mockListFolders: vi.fn(),
  mockListTags: vi.fn(),
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

// Radix Select cannot be opened via fireEvent/userEvent in jsdom (pointer-capture
// APIs are missing). DraftPromoteDialog is the sole ui/select consumer in this
// file, so reshaping it to plain elements that still fire the real
// onValueChange path is safe (see frontend-testing-gotchas.md #2).
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: { onValueChange: (v: string) => void; children: React.ReactNode }) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectValue: ({ placeholder }: { placeholder?: string }) =>
      React.createElement("span", null, placeholder),
    SelectContent: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement("button", { type: "button", onClick: () => onValueChange(value) }, children);
    },
  };
});

function makeDraft(overrides: Partial<DraftSummary> = {}): DraftSummary {
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
    ...overrides,
  };
}

function makeInput(overrides: Partial<DraftInput> = {}): DraftInput {
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

function makeFolder(overrides: Partial<Folder> = {}): Folder {
  return {
    id: 30,
    vault_id: 7,
    parent_folder_id: null,
    name: "Press releases",
    description: "",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    document_count: 0,
    ...overrides,
  };
}

function makeTag(overrides: Partial<Tag> = {}): Tag {
  return {
    id: 40,
    vault_id: 7,
    name: "external",
    color: "#ffffff",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    document_count: 0,
    ...overrides,
  };
}

function makePromoteResponse(overrides: Partial<PromoteResponse> = {}): PromoteResponse {
  return {
    promotion_id: 1,
    draft_id: 42,
    vault_id: 7,
    source_type: "input",
    source_id: 900,
    source_sha256: "deadbeef",
    file_id: 5001,
    filename: "q3-press-release.docx",
    created_at: "2026-01-03T00:00:00Z",
    ...overrides,
  };
}

function makeError(status: number, code: string, detail: string) {
  const err = new Error(detail) as Error & {
    status?: number;
    originalError?: { response?: { data?: { detail?: string; code?: string } } };
  };
  err.status = status;
  err.originalError = { response: { data: { detail, code } } };
  return err;
}

function renderDialog(props: Partial<React.ComponentProps<typeof DraftPromoteDialog>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const onOpenChange = vi.fn();
  const onPromoted = vi.fn();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DraftPromoteDialog
          open
          onOpenChange={onOpenChange}
          draft={makeDraft()}
          inputs={[makeInput()]}
          revisions={[makeRevision()]}
          currentRevisionId={501}
          canWrite
          onPromoted={onPromoted}
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...utils, onOpenChange, onPromoted, invalidateSpy };
}

async function checkConfirmation(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("checkbox", { name: /understand and want to promote/i }));
}

beforeEach(() => {
  mockPromoteDraftSource.mockReset();
  mockGetVault.mockReset();
  mockGetVault.mockResolvedValue(makeVault());
  mockListFolders.mockReset();
  mockListFolders.mockResolvedValue([]);
  mockListTags.mockReset();
  mockListTags.mockResolvedValue([]);
  vi.mocked(toast.success).mockClear();
});

describe("DraftPromoteDialog", () => {
  it("disables the action and explains why when canWrite is false", async () => {
    renderDialog({ canWrite: false });
    expect(await screen.findByText(/read-only access/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Promote to vault" })).toBeDisabled();
  });

  it("defaults to the first input and posts source_type=input", async () => {
    const user = userEvent.setup();
    mockPromoteDraftSource.mockResolvedValue(makePromoteResponse());
    renderDialog({ inputs: [makeInput({ id: 900, original_name: "manuscript.docx" })] });

    await checkConfirmation(user);
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    await waitFor(() => expect(mockPromoteDraftSource).toHaveBeenCalledTimes(1));
    expect(mockPromoteDraftSource).toHaveBeenCalledWith(42, {
      source_type: "input",
      source_id: 900,
      title: "Q3 press release",
      folder_id: null,
      tag_ids: [],
    });
  });

  it("switching to a revision posts source_type=revision with the current revision id", async () => {
    const user = userEvent.setup();
    mockPromoteDraftSource.mockResolvedValue(makePromoteResponse({ source_type: "revision", source_id: 501 }));
    renderDialog({ currentRevisionId: 501, revisions: [makeRevision({ id: 501 })] });

    await user.click(screen.getByRole("radio", { name: "A draft revision" }));
    await checkConfirmation(user);
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    await waitFor(() => expect(mockPromoteDraftSource).toHaveBeenCalledTimes(1));
    expect(mockPromoteDraftSource).toHaveBeenCalledWith(42, {
      source_type: "revision",
      source_id: 501,
      title: "Q3 press release",
      folder_id: null,
      tag_ids: [],
    });
  });

  it("selecting a specific source item updates the payload", async () => {
    const user = userEvent.setup();
    mockPromoteDraftSource.mockResolvedValue(makePromoteResponse({ source_id: 902 }));
    renderDialog({
      inputs: [
        makeInput({ id: 900, original_name: "manuscript.docx" }),
        makeInput({ id: 902, original_name: "background.pdf" }),
      ],
    });

    await user.click(screen.getByRole("button", { name: "background.pdf" }));
    await checkConfirmation(user);
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    await waitFor(() =>
      expect(mockPromoteDraftSource).toHaveBeenCalledWith(
        42,
        expect.objectContaining({ source_id: 902 })
      )
    );
  });

  it("sends the selected folder and tags in the payload", async () => {
    const user = userEvent.setup();
    mockListFolders.mockResolvedValue([makeFolder({ id: 30, name: "Press releases" })]);
    mockListTags.mockResolvedValue([makeTag({ id: 40, name: "external" })]);
    mockPromoteDraftSource.mockResolvedValue(makePromoteResponse());
    renderDialog();

    await user.click(await screen.findByRole("button", { name: "Press releases" }));
    await user.click(await screen.findByRole("checkbox", { name: "external" }));
    await checkConfirmation(user);
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    await waitFor(() =>
      expect(mockPromoteDraftSource).toHaveBeenCalledWith(
        42,
        expect.objectContaining({ folder_id: 30, tag_ids: [40] })
      )
    );
  });

  it("shows the destination vault read-only, equal to draft.vault_id", async () => {
    renderDialog({ draft: makeDraft({ vault_id: 7 }) });
    const vaultField = await screen.findByLabelText("Destination vault");
    await waitFor(() => expect(vaultField).toHaveValue("Research vault"));
    expect(vaultField).toBeDisabled();
    expect(mockGetVault).toHaveBeenCalledWith(7);
  });

  it("rejects an empty title and a title over 300 characters", async () => {
    const user = userEvent.setup();
    renderDialog();
    const titleField = screen.getByLabelText("Document title");

    await user.clear(titleField);
    await checkConfirmation(user);
    expect(screen.getByRole("button", { name: "Promote to vault" })).toBeDisabled();

    fireEvent.change(titleField, { target: { value: "a".repeat(305) } });
    expect(screen.getByRole("button", { name: "Promote to vault" })).toBeDisabled();

    fireEvent.change(titleField, { target: { value: "A valid title" } });
    expect(screen.getByRole("button", { name: "Promote to vault" })).toBeEnabled();
  });

  it("renders PROMOTE_CONSEQUENCE and requires confirmation before submitting", async () => {
    const user = userEvent.setup();
    renderDialog();
    expect(screen.getByText(PROMOTE_CONSEQUENCE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Promote to vault" })).toBeDisabled();

    await checkConfirmation(user);
    expect(screen.getByRole("button", { name: "Promote to vault" })).toBeEnabled();
  });

  it("shows the new document identity and invalidates the right queries on success", async () => {
    const user = userEvent.setup();
    const response = makePromoteResponse({ file_id: 5001, filename: "q3-press-release.docx" });
    mockPromoteDraftSource.mockResolvedValue(response);
    const { onPromoted, invalidateSpy } = renderDialog({ draft: makeDraft({ id: 42 }) });

    await checkConfirmation(user);
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    expect(await screen.findByText(/q3-press-release\.docx/)).toBeInTheDocument();
    expect(screen.getByText(/document #5001/)).toBeInTheDocument();
    expect(screen.getByText(/queued for indexing/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "q3-press-release.docx" })).toHaveAttribute(
      "href",
      "/documents/5001"
    );

    expect(onPromoted).toHaveBeenCalledWith(response);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(42) });
    // DocumentsPage does not use React Query, so invalidating a ["documents"] key
    // would be a no-op that only looks like cache maintenance. The new document is
    // surfaced by link instead; assert we do not pretend otherwise.
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: ["documents"] });
  });

  it("renders the permission explanation for a 403", async () => {
    const user = userEvent.setup();
    mockPromoteDraftSource.mockRejectedValue(makeError(403, "vault_access_revoked", "no write access"));
    renderDialog();

    await checkConfirmation(user);
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    expect(await screen.findByText(/no longer have write access/i)).toBeInTheDocument();
  });

  it("renders the duplicate_document message for a 409", async () => {
    const user = userEvent.setup();
    mockPromoteDraftSource.mockRejectedValue(makeError(409, "duplicate_document", "already exists"));
    renderDialog();

    await checkConfirmation(user);
    await user.click(screen.getByRole("button", { name: "Promote to vault" }));

    expect(await screen.findByText(/already been promoted/i)).toBeInTheDocument();
  });
});

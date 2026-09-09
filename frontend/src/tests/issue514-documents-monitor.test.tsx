/**
 * DocumentsPage upload monitoring (issue #514 — Phase 4.6 critic fixup).
 *
 * DocumentsPage mounts `useUploadMonitoring`, so uploads initiated from the
 * documents page keep being monitored while the user is NOT in chat: the
 * shared batched poller ticks `getDocumentStatuses` for every pending
 * document id while the page is mounted, and disarms (zero further requests)
 * once the page unmounts and no other monitor consumer remains.
 *
 * Wiring mirrors two proven suites:
 * - `src/components/chat/Composer.queued.test.tsx` — the real `useUploadStore`
 *   + shared monitor driven by fake timers, with `uploadDocument` /
 *   `getDocumentStatuses` / `getDocumentStatus` replaced by hoisted spies on
 *   an otherwise-real `@/lib/api` barrel (the monitor resolves the batched
 *   client through the module namespace).
 * - `src/pages/DocumentsPage.test.tsx` — the jsdom mock set DocumentsPage
 *   needs to render (virtualizer, UI primitives, dropzone, vault store, ...).
 *   The upload store is intentionally NOT mocked here — the page must talk to
 *   the real store the monitor reads.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

const { uploadDocumentMock, getDocumentStatusesMock, getDocumentStatusMock } = vi.hoisted(() => ({
  uploadDocumentMock: vi.fn(),
  getDocumentStatusesMock: vi.fn(),
  getDocumentStatusMock: vi.fn(),
}));

// Real api barrel with DocumentsPage's list/stats/wiki deps faked (as in
// DocumentsPage.test.tsx) and the upload/monitor trio spied (as in
// Composer.queued.test.tsx).
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listDocuments: vi.fn().mockResolvedValue({
      documents: [
        { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        { id: "2", filename: "test2.pdf", size: 2048, created_at: "2024-01-02", metadata: { status: "processed", chunk_count: 10 } },
      ],
      total: 2,
    }),
    scanDocuments: vi.fn().mockResolvedValue({ added: 0, scanned: 0 }),
    deleteDocument: vi.fn().mockResolvedValue({}),
    deleteDocuments: vi.fn().mockResolvedValue({ deleted_count: 0, failed_ids: [] }),
    deleteAllDocumentsInVault: vi.fn().mockResolvedValue({ deleted_count: 0 }),
    getDocumentWikiStatus: vi.fn().mockResolvedValue({
      wiki_status: "not_compiled",
      pages_count: 0,
      claims_count: 0,
      lint_count: 0,
    }),
    compileDocumentWiki: vi.fn().mockResolvedValue({ job_id: 1, status: "queued" }),
    getDocumentStats: vi.fn().mockResolvedValue({
      total_documents: 2,
      total_chunks: 15,
      total_size_bytes: 3072,
      documents_by_status: { processed: 2 },
    }),
    listTags: vi.fn().mockResolvedValue([]),
    listFolders: vi.fn().mockResolvedValue([]),
    createFolder: vi.fn().mockResolvedValue({ id: 1, name: "mock", vault_id: 1, parent_folder_id: null }),
    updateFolder: vi.fn().mockResolvedValue({ id: 1, name: "updated", vault_id: 1, parent_folder_id: null }),
    deleteFolder: vi.fn().mockResolvedValue(undefined),
    downloadDocument: vi.fn().mockResolvedValue(undefined),
    uploadDocument: uploadDocumentMock,
    getDocumentStatuses: getDocumentStatusesMock,
    getDocumentStatus: getDocumentStatusMock,
  };
});

// Virtualized table: render every row (established jsdom pattern).
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count, estimateSize }) => {
    const size = estimateSize?.() ?? 72;
    return {
      getVirtualItems: () =>
        Array.from({ length: count }, (_, i) => ({
          index: i,
          start: i * size,
          size,
          key: `doc-${i}`,
        })),
      getTotalSize: () => count * size,
      measureElement: vi.fn(),
      scrollToIndex: vi.fn(),
      measure: vi.fn(),
    };
  }),
}));

vi.mock("react-dropzone", () => ({
  useDropzone: vi.fn(() => ({
    getRootProps: () => ({ role: "button" }),
    getInputProps: () => ({ type: "file" }),
    isDragActive: false,
  })),
}));

vi.mock("sonner", () => {
  const toast = Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    dismiss: vi.fn(),
  });
  return { toast };
});

vi.mock("@/hooks/useDebounce", () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: 2,
    vaults: [{ id: 2, name: "Docs Vault", current_user_permission: "admin" }],
  })),
}));

// NOTE: @/stores/useUploadStore is intentionally NOT mocked — the page's
// uploads flow and the shared monitor must share the real store.

vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div data-testid="card-content">{children}</div>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p data-testid="card-description">{children}</p>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3 data-testid="card-title">{children}</h3>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, disabled, ...props }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("@/components/ui/progress", () => ({
  Progress: () => <div role="progressbar" />,
}));

vi.mock("@/components/ui/skeleton", () => ({
  Skeleton: () => <div data-testid="skeleton" />,
}));

vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({ onCheckedChange, checked, ...props }: { onCheckedChange?: (checked: boolean) => void; checked?: boolean }) => (
    <input type="checkbox" onChange={(e) => onCheckedChange?.(e.target.checked)} checked={checked} {...props} />
  ),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("@/components/shared/StatusBadge", () => ({
  StatusBadge: ({ status }: { status: string }) => <span data-testid="status-badge">{status}</span>,
}));

vi.mock("@/components/shared/DocumentCard", () => ({
  DocumentCard: ({ document }: { document: { id: string; filename: string } }) => (
    <div data-testid="document-card">{document.filename}</div>
  ),
}));

vi.mock("@/components/EmptyState", () => ({
  EmptyState: ({ title }: { title: string }) => <div data-testid="empty-state">{title}</div>,
}));

vi.mock("@/lib/formatters", () => ({
  formatFileSize: (bytes: number) => `${bytes} bytes`,
  formatDate: (date: string) => date,
}));

vi.mock("@/components/documents/UploadDropzone", () => ({
  UploadDropzone: () => <div data-testid="upload-dropzone-stub" />,
}));

vi.mock("@/components/documents/RejectedFilesBanner", () => ({
  RejectedFilesBanner: () => null,
}));

import DocumentsPage from "@/pages/DocumentsPage";
import { useUploadStore, uploadNeedsMonitoring } from "@/stores/useUploadStore";

/** Advance the virtual clock by whole monitor ticks (1s cadence). */
async function tick(cycles = 1) {
  for (let i = 0; i < cycles; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  uploadDocumentMock.mockReset();
  getDocumentStatusesMock.mockReset();
  getDocumentStatusMock.mockReset();
  useUploadStore.setState({
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("DocumentsPage upload monitoring (issue #514 — monitored while off chat)", () => {
  it("polls batched getDocumentStatuses for uploads seeded while mounted, then stops after unmount", async () => {
    // Bytes accepted immediately; the document stays non-terminal ("processing")
    // so the only thing that can stop the polling is the monitor disarming.
    uploadDocumentMock.mockResolvedValue({ id: 77, status: "pending" });
    getDocumentStatusesMock.mockResolvedValue({
      results: [{ id: 77, status: "processing", chunk_count: 0, phase: "queued" }],
    });

    let utils!: ReturnType<typeof render>;
    await act(async () => {
      utils = render(
        <MemoryRouter>
          <DocumentsPage />
        </MemoryRouter>
      );
    });

    // Page mount alone must not produce status polling — nothing is in flight.
    expect(getDocumentStatusesMock).not.toHaveBeenCalled();

    // Seed a pending upload while the page is mounted (what its own dropzone
    // does): addUploads -> transfer pool -> uploadDocument resolves -> the
    // store row moves to "processing" with documentId 77.
    await act(async () => {
      useUploadStore.getState().addUploads(
        [new File([new Uint8Array(8)], "notes.txt", { type: "text/plain" })],
        1
      );
    });
    const upload = useUploadStore.getState().uploads[0];
    expect(upload.documentId).toBe("77");
    expect(uploadNeedsMonitoring(upload)).toBe(true);

    // ~2s of monitor ticks with the user NOT in chat: the batched client is
    // called for the pending document id — documents-page uploads are
    // monitored by the page's own useUploadMonitoring() mount.
    await tick(2);
    expect(getDocumentStatusesMock).toHaveBeenCalled();
    expect(getDocumentStatusesMock.mock.calls[0][0]).toEqual(["77"]);
    // No per-file status requests while the batched client exists.
    expect(getDocumentStatusMock).not.toHaveBeenCalled();

    // Unmount the only monitor consumer. Advancing the clock afterwards must
    // produce ZERO additional batched calls — the monitor disarms.
    const callsAtUnmount = getDocumentStatusesMock.mock.calls.length;
    expect(callsAtUnmount).toBeGreaterThan(0);
    await act(async () => {
      utils.unmount();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getDocumentStatusesMock.mock.calls.length).toBe(callsAtUnmount);
    expect(getDocumentStatusMock).not.toHaveBeenCalled();
  });
});

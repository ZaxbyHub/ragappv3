// Issue 258 / AC25 part (a) (legacy-14) — retry affordance on failed
// DocumentsPage list fetch.
//
// Phase 2.5 acceptance check. At base, when `listDocuments` rejects,
// useDocumentPolling catches the error, toasts, and clears the list
// (src/components/documents/useDocumentPolling.ts:82-88) — DocumentsPage
// then renders the plain "No documents yet" EmptyState. There is NO error
// state and NO retry affordance: a user whose fetch failed (network blip,
// backend restart) sees a page indistinguishable from an empty vault.
//
// This check requires: when the initial list fetch fails, the page renders
// an error affordance with a visible RETRY control, and activating it
// refetches the list (the fetch count increases and the recovered list
// renders). At base there is no retry control → the findByRole below
// fails → RED. Phase 4 adds the retry affordance; the recovery half then
// proves it actually refetches.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import DocumentsPage from "@/pages/DocumentsPage";
import type { Document } from "@/lib/api";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({ getRootProps: () => ({}), getInputProps: () => ({}), isDragActive: false }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock("@/hooks/useDebounce", () => ({
  useDebounce: (value: string) => [value, false],
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: 1,
    vaults: [{ id: 1, name: "Research vault", current_user_permission: "admin" }],
  })),
}));

vi.mock("@/stores/useUploadStore", () => ({
  uploadNeedsMonitoring: () => false,
  useUploadStore: Object.assign(
    vi.fn(() => ({
      uploads: [],
      addUploads: vi.fn(),
      cancelUpload: vi.fn(),
      removeUpload: vi.fn(),
      clearCompleted: vi.fn(),
      retryUpload: vi.fn(),
    })),
    { getState: () => ({ uploads: [] }) }
  ),
}));

const recoveredDocuments: Document[] = [
  {
    id: "1",
    filename: "recovered.pdf",
    size: 1024,
    created_at: "2024-01-01T00:00:00Z",
    metadata: { status: "processed", chunk_count: 5 },
  },
] as unknown as Document[];

vi.mock("@/lib/api", () => ({
  // Initial fetch FAILS; the test flips this mock to a resolved value when
  // it activates the retry control.
  listDocuments: vi.fn(() => Promise.reject(new Error("Network down"))),
  getDocumentStats: vi.fn(() =>
    Promise.resolve({
      total_documents: 1,
      total_chunks: 5,
      total_size_bytes: 1024,
      documents_by_status: { processed: 1 },
    })
  ),
  getDocumentWikiStatus: vi.fn(() => Promise.resolve(undefined)),
  compileDocumentWiki: vi.fn(() => Promise.resolve({})),
  listTags: vi.fn(() => Promise.resolve([])),
  listFolders: vi.fn(() => Promise.resolve([])),
  createFolder: vi.fn(() => Promise.resolve({})),
  updateFolder: vi.fn(() => Promise.resolve({})),
  deleteFolder: vi.fn(() => Promise.resolve({})),
  scanDocuments: vi.fn(() => Promise.resolve({ added: 0, scanned: 0 })),
  deleteDocument: vi.fn(() => Promise.resolve({})),
  deleteDocuments: vi.fn(() => Promise.resolve({ deleted_count: 0, failed_ids: [] })),
  deleteAllDocumentsInVault: vi.fn(() => Promise.resolve({ deleted_count: 0 })),
  downloadDocument: vi.fn(() => Promise.resolve({})),
}));

describe("AC25 — DocumentsPage list-fetch retry affordance (legacy-14)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
  });

  it("AC25: failed list fetch renders a retry affordance that refetches and recovers", { timeout: 30_000 }, async () => {
    const { listDocuments } = await import("@/lib/api");
    // The module mock is runtime-only; the imported symbol still carries
    // the real signature, so go through vi.mocked for mock APIs.
    const mockedListDocuments = vi.mocked(listDocuments);

    let container: HTMLElement;
    await act(async () => {
      const result = render(
        <MemoryRouter>
          <DocumentsPage />
        </MemoryRouter>
      );
      container = result.container;
    });

    // The failed fetch has landed (error path taken, page settled).
    await waitFor(() => expect(mockedListDocuments).toHaveBeenCalledTimes(1));

    // Sentinel immediately before the discriminating assertion.
    console.log(
      'AC25 CHECK: FAIL — expecting a visible retry affordance (button /retry|try again/i) after the document list fetch fails'
    );
    // At base this throws: no retry control exists anywhere on the page.
    const retry = await screen.findByRole("button", { name: /retry|try again/i }, { timeout: 3_000 });

    // Recovery half (exercises the affordance once Phase 4 adds it):
    // flip the fetch to succeed, activate retry, expect a refetch and the
    // recovered document row to render.
    mockedListDocuments.mockImplementation(() =>
      Promise.resolve({ documents: recoveredDocuments, total: recoveredDocuments.length })
    );
    await act(async () => {
      fireEvent.click(retry);
    });

    console.log("AC25 CHECK: FAIL — expecting retry to trigger a refetch");
    await waitFor(() => expect(mockedListDocuments.mock.calls.length).toBeGreaterThan(1), { timeout: 5_000 });
    console.log("AC25 CHECK: FAIL — expecting the recovered document list to render after retry");
    await waitFor(() =>
      expect(container!.querySelectorAll("table tbody tr[role='row']").length).toBe(1)
    , { timeout: 5_000 });
    expect(screen.getByText("recovered.pdf")).toBeTruthy();
  });
});

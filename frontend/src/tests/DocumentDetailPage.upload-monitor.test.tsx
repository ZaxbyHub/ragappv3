/**
 * DocumentDetailPage mounts the shared upload monitor (issue #514 feedback
 * round, PRR-013): a user who navigates to a document detail page mid-upload
 * must still see progress converge. The monitor is refcounted — this suite
 * proves the detail page counts as a consumer (polling runs while it is
 * mounted with a non-terminal upload in the store) and that it disarms
 * (zero further requests) once it unmounts, when no other consumer remains.
 *
 * Mock/wiring mirrors DocumentDetailPage.preview-race.test.tsx (same api
 * fakes) plus the monitor spies from issue514-documents-monitor.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter, Routes, Route } from "react-router-dom";

const apiMock = vi.hoisted(() => ({
  getDocument: vi.fn(),
  getDocumentRawBlob: vi.fn(),
  setDocumentTags: vi.fn(),
  downloadDocument: vi.fn(),
  listTags: vi.fn(),
  getDocumentStatuses: vi.fn(),
  getDocumentStatus: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMock);

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

import DocumentDetailPage from "@/pages/DocumentDetailPage";
import { useUploadStore } from "@/stores/useUploadStore";

const DOC1 = {
  id: "1",
  filename: "one.txt",
  vault_id: 1,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "indexed", chunk_count: 1 },
  tags: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  apiMock.listTags.mockResolvedValue([]);
  apiMock.getDocument.mockResolvedValue(DOC1);
  useUploadStore.setState({
    uploads: [
      {
        id: "u1",
        file: new File([new Uint8Array(8)], "one.txt", { type: "text/plain" }),
        status: "processing" as const,
        uploadProgress: 100,
        progress: 100,
        documentId: "1",
        startedAt: Date.now(),
      },
    ],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],
  });
});

afterEach(() => {
  useUploadStore.setState({ uploads: [] });
  vi.useRealTimers();
});

describe("DocumentDetailPage upload monitor (issue #514 — PRR-013)", () => {
  it("keeps polling while the detail page is mounted and disarms after it unmounts", async () => {
    apiMock.getDocumentStatuses.mockResolvedValue({
      results: [{ id: 1, status: "processing", chunk_count: 0, phase: "queued" }],
    });

    const utils = render(
      <MemoryRouter initialEntries={["/documents/1"]}>
        <Routes>
          <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
        </Routes>
      </MemoryRouter>
    );

    // The page renders the document itself...
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiMock.getDocument).toHaveBeenCalledWith(1);

    // ...and the monitor ticks for the in-flight upload while it is open.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(apiMock.getDocumentStatuses).toHaveBeenCalledTimes(1);
    expect(apiMock.getDocumentStatuses.mock.calls[0][0]).toEqual(["1"]);

    // A second tick keeps arriving while the page stays mounted.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(apiMock.getDocumentStatuses).toHaveBeenCalledTimes(2);

    // Unmounting the detail page (with no other monitor consumer) disarms:
    // zero further requests.
    utils.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(apiMock.getDocumentStatuses).toHaveBeenCalledTimes(2);
  });
});

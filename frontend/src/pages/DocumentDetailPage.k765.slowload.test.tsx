/**
 * DocumentDetailPage K765 slow-load regression pin (issue #765 AC7; NOT part
 * of the frozen checkpoint set — added at the implementation reviewer's
 * direction after it caught the refresh-disarm race this file pins).
 *
 * The background status refresh must SURVIVE its first tick firing before the
 * foreground load commits: a tick with no loaded document (or the previous
 * document still in the mirror) re-arms instead of returning, so a load that
 * resolves AFTER the first 2 s tick still converges a processing document to
 * indexed without a remount. At the pre-race-fix head the loop died on that
 * first tick and the page stayed "Processing" — the exact symptom #765 AC7
 * forbids. RED at the race head, GREEN after the fix.
 *
 * Harness mirrors DocumentDetailPage.k765.test.tsx (proven in this repo).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter, Routes, Route } from "react-router-dom";

const apiMock = vi.hoisted(() => ({
  getDocument: vi.fn(),
  getDocumentRawBlob: vi.fn(),
  setDocumentTags: vi.fn(),
  downloadDocument: vi.fn(),
  listTags: vi.fn(),
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

function fakeBlob(text: string) {
  return { text: async () => text };
}

const DOC = {
  id: "1",
  filename: "slow.txt",
  vault_id: 1,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "processing", chunk_count: 0 },
  tags: [],
};

function Harness() {
  return (
    <MemoryRouter initialEntries={["/documents/1"]}>
      <Routes>
        <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

describe("DocumentDetailPage K765 slow-load regression", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useUploadStore.setState({ uploads: [] });
    apiMock.listTags.mockResolvedValue([]);
    apiMock.setDocumentTags.mockResolvedValue([]);
    apiMock.downloadDocument.mockResolvedValue(undefined);
    apiMock.getDocumentRawBlob.mockImplementation(() =>
      Promise.resolve(fakeBlob("PREVIEW-CONTENT")),
    );
  });

  afterEach(async () => {
    if (vi.isFakeTimers()) vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    await act(async () => {
      await flushMicrotasks();
    });
  });

  it("converges a processing document whose first load resolves after the first refresh tick", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // The foreground load is HELD: it resolves processing only after the
    // first 2 s background tick has already fired into an empty doc mirror.
    let releaseLoad!: (value: unknown) => void;
    const heldLoad = new Promise((resolve) => {
      releaseLoad = resolve;
    });
    apiMock.getDocument.mockImplementationOnce(() => heldLoad as never);
    // Every later read (the background refreshes) resolves indexed.
    apiMock.getDocument.mockImplementation(() =>
      Promise.resolve({
        ...DOC,
        metadata: { status: "indexed", chunk_count: 3 },
      }),
    );

    render(<Harness />);
    // Fire the first tick BEFORE the load commits (2 s), then release the
    // load (3 s) — at the race head the loop is already dead by now.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await act(async () => {
      releaseLoad({ ...DOC });
      await flushMicrotasks();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    // Sanity: the foreground load committed the processing document.
    expect(screen.getByRole("button", { name: /ask about this document/i })).toBeDisabled();

    // The page must converge without any remount: the re-armed background
    // refresh observes indexed and enables the Ask affordance.
    for (let index = 0; index < 10; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }
    expect(
      screen.getByRole("button", { name: /ask about this document/i }),
      "the status refresh must re-arm when its first tick precedes the load",
    ).toBeEnabled();
  });
});

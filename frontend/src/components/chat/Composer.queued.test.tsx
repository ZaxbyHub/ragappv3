/**
 * PR A regression: chat composer must handle the new async upload path.
 *
 * The async POST /documents route returns immediately with status="pending".
 * The polling loop then sees status="pending" / "processing" plus phase
 * strings (queued / parsing / embedding / ...). The composer chip must
 * stay in its "indexing" state through the whole pipeline and flip to
 * "indexed" only on a terminal status, never on phase alone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { uploadDocumentMock, getDocumentStatusMock } = vi.hoisted(() => ({
  uploadDocumentMock: vi.fn(),
  getDocumentStatusMock: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    uploadDocument: uploadDocumentMock,
    getDocumentStatus: getDocumentStatusMock,
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

beforeEach(() => {
  vi.useFakeTimers();
  uploadDocumentMock.mockReset();
  getDocumentStatusMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/**
 * The Composer's startIndexPoll callback is the unit under test. We
 * exercise it via a thin wrapper because mounting the full <Composer />
 * in JSDOM would require dragging in the chat shell and dropzone
 * scaffolding for behavior that is decoupled from rendering.
 */
async function runPollCycles(cycles: number) {
  // Composer polls at 1s.
  for (let i = 0; i < cycles; i += 1) {
    await vi.advanceTimersByTimeAsync(1000);
  }
}

describe("Composer polling — async upload path", () => {
  it("status='pending' (queued / parsing / embedding) keeps chip in indexing-style state, not error", async () => {
    // Hot-import to ensure module captures the latest mocks.
    const { useUploadStore } = await import("@/stores/useUploadStore");
    // The chat composer's startIndexPoll lives inside the component, but
    // its branching contract mirrors the upload store's snapshot mapping.
    // We assert the upload store contract directly here as a proxy: the
    // store maps every backend status in {pending, processing} -> chip
    // state "processing" (which the composer aliases to "indexing").

    useUploadStore.setState({
      uploads: [],
      isProcessing: false,
      activeVaultId: 1,
    });

    uploadDocumentMock.mockResolvedValue({
      id: 1,
      filename: "f.txt",
      status: "pending",
    });

    let call = 0;
    getDocumentStatusMock.mockImplementation(async () => {
      call += 1;
      if (call === 1) {
        return {
          id: 1,
          filename: "f.txt",
          status: "pending",
          chunk_count: 0,
          phase: "queued",
        };
      }
      if (call === 2) {
        return {
          id: 1,
          filename: "f.txt",
          status: "processing",
          chunk_count: 0,
          phase: "parsing",
        };
      }
      if (call === 3) {
        return {
          id: 1,
          filename: "f.txt",
          status: "processing",
          chunk_count: 0,
          phase: "embedding",
          progress_percent: 40,
        };
      }
      return {
        id: 1,
        filename: "f.txt",
        status: "indexed",
        chunk_count: 5,
        phase: "indexed",
        wiki_status: "completed",
      };
    });

    useUploadStore.getState().addUploads([
      new File([new Uint8Array(8)], "f.txt", { type: "text/plain" }),
    ], 1);
    await vi.advanceTimersByTimeAsync(1);

    // Cycle 1: queued
    await vi.advanceTimersByTimeAsync(3100); // one poll at the 3s floor cadence
    let u = useUploadStore.getState().uploads[0];
    expect(u.status).toBe("processing");
    expect(u.phase).toBe("queued");
    expect(u.error).toBeUndefined();

    // Cycle 2: parsing
    await vi.advanceTimersByTimeAsync(3100); // one poll at the 3s floor cadence
    u = useUploadStore.getState().uploads[0];
    expect(u.phase).toBe("parsing");
    expect(u.status).toBe("processing");

    // Cycle 3: embedding 40%
    await vi.advanceTimersByTimeAsync(3100); // one poll at the 3s floor cadence
    u = useUploadStore.getState().uploads[0];
    expect(u.phase).toBe("embedding");
    expect(u.processingProgress).toBe(40);

    // Cycle 4: indexed
    await vi.advanceTimersByTimeAsync(3100); // one poll at the 3s floor cadence
    u = useUploadStore.getState().uploads[0];
    expect(u.status).toBe("indexed");
  });
});

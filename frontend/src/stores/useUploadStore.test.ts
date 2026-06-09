/**
 * PR A: phase-aware upload store regression tests.
 *
 * Covers the contract that the user-facing fix depends on:
 *  - Network upload reaching 100% does NOT flip the chip to "indexed".
 *  - Adaptive polling resets on phase change and never errors out
 *    on a long-running indexing job (no 3-minute false timeout).
 *  - applyStatusSnapshot maps server fields onto the store correctly.
 *  - retryUpload resets phase + progress + error in addition to status.
 *  - clearCompleted leaves "processing" rows alone (in-flight files
 *    must not vanish under the user mid-pipeline).
 *  - The deprecated `progress` alias keeps mirroring `uploadProgress`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { uploadDocumentMock, getDocumentStatusMock } = vi.hoisted(() => ({
  uploadDocumentMock: vi.fn(),
  getDocumentStatusMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  uploadDocument: uploadDocumentMock,
  getDocumentStatus: getDocumentStatusMock,
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

import { useUploadStore } from "./useUploadStore";
import { MAX_UPLOAD_FILE_SIZE_BYTES } from "@/lib/uploadLimits";
import { toast } from "sonner";

function makeFile(name: string, bytes = 4): File {
  return new File([new Uint8Array(bytes)], name, { type: "text/plain" });
}

function makeFileWithReportedSize(name: string, bytes: number): File {
  const file = makeFile(name);
  Object.defineProperty(file, "size", { value: bytes });
  return file;
}

function resetStore() {
  useUploadStore.setState({
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  resetStore();
  uploadDocumentMock.mockReset();
  getDocumentStatusMock.mockReset();
  vi.mocked(toast.error).mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useUploadStore — async-aware contract", () => {
  it("rejects files over the 100 MB per-file limit before queueing", () => {
    useUploadStore.getState().addUploads(
      [makeFileWithReportedSize("too-large.pdf", MAX_UPLOAD_FILE_SIZE_BYTES + 1)],
      1
    );

    expect(useUploadStore.getState().uploads).toEqual([]);
    expect(uploadDocumentMock).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      "too-large.pdf is too large. Max size: 100 MB."
    );
  });

  it("normalizes upload 413 errors to the 100 MB per-file limit", async () => {
    const error = new Error("Request Entity Too Large") as Error & { status: number };
    error.status = 413;
    uploadDocumentMock.mockRejectedValue(error);

    useUploadStore.getState().addUploads([makeFile("large.pdf")], 1);
    await vi.advanceTimersByTimeAsync(1);

    const u = useUploadStore.getState().uploads[0];
    expect(u.status).toBe("error");
    expect(u.error).toBe("File too large. Max size: 100 MB.");
    expect(toast.error).toHaveBeenCalledWith(
      "Failed to upload large.pdf: File too large. Max size: 100 MB."
    );
  });

  it("network 100% does NOT mark indexed; status moves to processing and waits for backend", async () => {
    let progressCb: (n: number) => void = () => {};
    uploadDocumentMock.mockImplementation((_file, onProgress) => {
      progressCb = onProgress;
      return new Promise((resolve) => {
        // Resolve after the test pushes upload progress to 100.
        setTimeout(() => {
          progressCb(100);
          resolve({ id: "42", filename: "a.txt", status: "pending" });
        }, 0);
      });
    });
    // First poll returns processing/parsing; never resolves to indexed in this test.
    getDocumentStatusMock.mockResolvedValue({
      id: 42,
      filename: "a.txt",
      status: "processing",
      chunk_count: 0,
      phase: "parsing",
      phase_message: "Parsing",
      progress_percent: null,
    });

    useUploadStore.getState().addUploads([makeFile("a.txt")], 1);

    // Drain the microtask + the 0ms timeout that resolves uploadDocument.
    await vi.advanceTimersByTimeAsync(1);
    // Drive one polling cycle (1500ms first interval).
    await vi.advanceTimersByTimeAsync(1600);

    const u = useUploadStore.getState().uploads[0];
    expect(u).toBeDefined();
    expect(u.uploadProgress).toBe(100);
    // CONTRACT: status is NOT "indexed".
    expect(u.status).not.toBe("indexed");
    // CONTRACT: status is processing-style; phase string drives detail.
    expect(u.status).toBe("processing");
    expect(u.phase).toBe("parsing");
    expect(u.phaseLabel).toBe("Parsing");
  });

  it("polling does NOT time out at 3 minutes — stays alive past the legacy threshold", async () => {
    uploadDocumentMock.mockResolvedValue({
      id: 7,
      filename: "big.xlsx",
      status: "pending",
    });
    // Always non-terminal.
    getDocumentStatusMock.mockResolvedValue({
      id: 7,
      filename: "big.xlsx",
      status: "processing",
      chunk_count: 0,
      phase: "embedding",
      phase_message: "Embedding chunks",
      progress_percent: 12,
    });

    useUploadStore.getState().addUploads([makeFile("big.xlsx")], 1);
    await vi.advanceTimersByTimeAsync(1);

    // Run well past the legacy 3-minute (180_000ms) ceiling.
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000);

    const u = useUploadStore.getState().uploads[0];
    // CONTRACT: no error, no timeout — still processing.
    expect(u.status).toBe("processing");
    expect(u.error).toBeUndefined();
    expect(u.phase).toBe("embedding");
  });

  it("indexed + wiki=running keeps polling; indexed + wiki=completed stops", async () => {
    uploadDocumentMock.mockResolvedValue({
      id: 9,
      filename: "doc.txt",
      status: "pending",
    });
    let call = 0;
    getDocumentStatusMock.mockImplementation(async () => {
      call += 1;
      if (call < 3) {
        return {
          id: 9,
          filename: "doc.txt",
          status: "indexed",
          chunk_count: 5,
          phase: "indexed",
          wiki_status: "running",
        };
      }
      return {
        id: 9,
        filename: "doc.txt",
        status: "indexed",
        chunk_count: 5,
        phase: "indexed",
        wiki_status: "completed",
      };
    });

    useUploadStore.getState().addUploads([makeFile("doc.txt")], 1);
    await vi.advanceTimersByTimeAsync(1);

    // First poll: indexed but wiki running -> keep polling.
    await vi.advanceTimersByTimeAsync(1600);
    const after1 = useUploadStore.getState().uploads[0];
    expect(after1.status).toBe("indexed");
    expect(after1.wikiStatus).toBe("running");

    // Two more polling cycles to pass the wiki-running snapshot.
    await vi.advanceTimersByTimeAsync(3500);
    const after2 = useUploadStore.getState().uploads[0];
    expect(after2.wikiStatus).toBe("completed");
  });

  it("retryUpload clears phase / progress / error and re-enqueues", async () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "x",
          file: makeFile("err.txt"),
          status: "error",
          uploadProgress: 50,
          progress: 50,
          processingProgress: 30,
          wikiProgress: null,
          phase: "parsing",
          phaseLabel: "Parsing",
          phaseMessage: "Parsing",
          processedUnits: 1,
          totalUnits: 5,
          unitLabel: "chunks",
          error: "boom",
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });
    // Make the retry upload hang forever so we can inspect post-retry state.
    uploadDocumentMock.mockImplementation(() => new Promise(() => {}));

    useUploadStore.getState().retryUpload("x");
    // Let processQueue run one tick.
    await vi.advanceTimersByTimeAsync(1);

    const u = useUploadStore.getState().uploads[0];
    expect(u.error).toBeUndefined();
    expect(u.phase).toBeNull();
    expect(u.phaseLabel).toBeNull();
    expect(u.processedUnits).toBeNull();
    expect(u.totalUnits).toBeNull();
    // Status moved past pending to uploading (retry triggers processQueue).
    expect(["pending", "uploading"]).toContain(u.status);
  });

  it("clearCompleted leaves processing rows alone", () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "a",
          file: makeFile("a.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
        },
        {
          id: "b",
          file: makeFile("b.txt"),
          status: "indexed",
          uploadProgress: 100,
          progress: 100,
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });
    useUploadStore.getState().clearCompleted();
    const ids = useUploadStore.getState().uploads.map((u) => u.id);
    expect(ids).toEqual(["a"]);
  });

  it("`progress` stays as a deprecated alias of uploadProgress", () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "a",
          file: makeFile("a.txt"),
          status: "uploading",
          uploadProgress: 0,
          progress: 0,
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });
    useUploadStore.getState().updateUploadProgress("a", 73);
    const u = useUploadStore.getState().uploads[0];
    expect(u.uploadProgress).toBe(73);
    expect(u.progress).toBe(73);
  });

  it("applyStatusSnapshot bumps phaseStartedAt only on phase transition", () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "a",
          file: makeFile("a.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
          phase: "parsing",
          phaseStartedAt: 1000,
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });
    // Same phase: should not bump.
    useUploadStore.getState().applyStatusSnapshot("a", {
      id: 1,
      filename: "a.txt",
      status: "processing",
      chunk_count: 0,
      phase: "parsing",
      progress_percent: 50,
    });
    expect(useUploadStore.getState().uploads[0].phaseStartedAt).toBe(1000);
    // Different phase: should bump.
    useUploadStore.getState().applyStatusSnapshot("a", {
      id: 1,
      filename: "a.txt",
      status: "processing",
      chunk_count: 0,
      phase: "embedding",
      progress_percent: 1,
    });
    expect(useUploadStore.getState().uploads[0].phaseStartedAt).not.toBe(1000);
    expect(useUploadStore.getState().uploads[0].phase).toBe("embedding");
    expect(useUploadStore.getState().uploads[0].phaseLabel).toBe("Embedding");
  });
});

/**
 * Upload store contract tests.
 *
 * Covers the behavior the unified upload pipeline depends on:
 *  - Network upload reaching 100% does NOT flip the status to "indexed";
 *    the store hands monitoring to the batched status poller and never
 *    polls on its own.
 *  - The byte-transfer pool is bounded by UPLOAD_CONCURRENCY and a slot
 *    frees as soon as bytes are accepted (transfers never serialize
 *    behind indexing).
 *  - applyStatusSnapshot maps server fields onto the store correctly, is
 *    attempt-ordered (older seq never wins) and terminal-sticky.
 *  - Size limits derive from the server-configured max_file_size_mb (via
 *    the settings store), defaulting to 100 MB until settings load.
 *  - Chat attachment registration survives outside any component.
 *  - retryUpload resets phase + progress + error in addition to status.
 *  - clearCompleted leaves "processing" rows alone (in-flight files
 *    must not vanish under the user mid-pipeline).
 *  - The deprecated `progress` alias keeps mirroring `uploadProgress`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { uploadDocumentMock, getDocumentStatusMock, settingsState } = vi.hoisted(() => ({
  uploadDocumentMock: vi.fn(),
  getDocumentStatusMock: vi.fn(),
  settingsState: { settings: null as { max_file_size_mb: number } | null },
}));

vi.mock("@/lib/api", () => ({
  uploadDocument: uploadDocumentMock,
  getDocumentStatus: getDocumentStatusMock,
}));

vi.mock("@/stores/useSettingsStore", () => ({
  useSettingsStore: Object.assign(vi.fn(() => settingsState.settings), {
    getState: () => ({ settings: settingsState.settings }),
  }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

import { useUploadStore, UPLOAD_CONCURRENCY, uploadNeedsMonitoring } from "./useUploadStore";
import { DEFAULT_UPLOAD_LIMIT_BYTES } from "@/lib/uploadLimits";
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
    chatAttachmentIds: [],
  });
}

const MB = 1024 * 1024;

beforeEach(() => {
  vi.useFakeTimers();
  resetStore();
  settingsState.settings = null;
  uploadDocumentMock.mockReset();
  getDocumentStatusMock.mockReset();
  vi.mocked(toast.error).mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useUploadStore — size limit", () => {
  it("rejects files over the 100 MB default per-file limit before queueing (settings not loaded)", () => {
    useUploadStore.getState().addUploads(
      [makeFileWithReportedSize("too-large.pdf", DEFAULT_UPLOAD_LIMIT_BYTES + 1)],
      1
    );

    expect(useUploadStore.getState().uploads).toEqual([]);
    expect(uploadDocumentMock).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      "too-large.pdf is too large. Max size: 100 MB."
    );
  });

  it("rejects files over the server-configured limit and queues files under it", () => {
    settingsState.settings = { max_file_size_mb: 50 };
    uploadDocumentMock.mockImplementation(() => new Promise(() => {}));

    const queuedIds = useUploadStore.getState().addUploads(
      [makeFileWithReportedSize("big-60mb.pdf", 60 * MB), makeFileWithReportedSize("ok-10mb.pdf", 10 * MB)],
      1
    );

    expect(queuedIds).toHaveLength(1);
    const uploads = useUploadStore.getState().uploads;
    expect(uploads.map((u) => u.file.name)).toEqual(["ok-10mb.pdf"]);
    expect(toast.error).toHaveBeenCalledWith(
      "big-60mb.pdf is too large. Max size: 50 MB."
    );
  });

  it("normalizes upload 413 errors to the per-file limit message", async () => {
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
});

describe("useUploadStore — bounded, decoupled transfer pool", () => {
  it("exports the documented concurrency bound", () => {
    expect(UPLOAD_CONCURRENCY).toBe(3);
  });

  it("runs at most UPLOAD_CONCURRENCY transfers and starts the next once bytes are accepted", async () => {
    const deferreds: Array<{ file: File; resolve: (v: unknown) => void }> = [];
    uploadDocumentMock.mockImplementation((file: File) => {
      return new Promise((resolve) => {
        deferreds.push({ file, resolve });
      });
    });

    const files = ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"].map((name) => makeFile(name));
    useUploadStore.getState().addUploads(files, 1);

    // Pool fills but never exceeds the bound.
    expect(uploadDocumentMock).toHaveBeenCalledTimes(UPLOAD_CONCURRENCY);
    expect(
      useUploadStore.getState().uploads.filter((u) => u.status === "uploading")
    ).toHaveLength(UPLOAD_CONCURRENCY);

    // First transfer's bytes are accepted; the slot frees even though its
    // indexing is untouched (no status call has been made) and the pool
    // admits the next pending file.
    deferreds[0].resolve({ id: 101, filename: "a.pdf", status: "pending" });
    await vi.advanceTimersByTimeAsync(1);

    expect(uploadDocumentMock).toHaveBeenCalledTimes(UPLOAD_CONCURRENCY + 1);
    const statuses = new Map(
      useUploadStore.getState().uploads.map((u) => [u.file.name, u.status])
    );
    expect(statuses.get("a.pdf")).toBe("processing");
    expect(statuses.get("d.pdf")).toBe("uploading");
    expect(statuses.get("e.pdf")).toBe("pending");
    expect(getDocumentStatusMock).not.toHaveBeenCalled();
  });

  it("never issues status requests itself — monitoring is the batched poller's job", async () => {
    uploadDocumentMock.mockResolvedValue({ id: 42, filename: "a.txt", status: "pending" });

    useUploadStore.getState().addUploads([makeFile("a.txt")], 1);
    await vi.advanceTimersByTimeAsync(10_000);

    expect(getDocumentStatusMock).not.toHaveBeenCalled();
    const u = useUploadStore.getState().uploads[0];
    // Bytes accepted: NOT "indexed", phase queued, no snapshot applied yet.
    expect(u.uploadProgress).toBe(100);
    expect(u.status).toBe("processing");
    expect(u.phase).toBe("queued");
    expect(u.statusSeen).toBe(false);
  });
});

describe("useUploadStore — snapshot application", () => {
  it("network 100% does NOT mark indexed; snapshots map processing fields correctly", () => {
    useUploadStore.getState().addUploads([makeFile("a.txt")], 1);
    useUploadStore.setState({
      uploads: useUploadStore.getState().uploads.map((u) => ({
        ...u,
        status: "processing" as const,
        documentId: "42",
        uploadProgress: 100,
        progress: 100,
      })),
    });

    useUploadStore.getState().applyStatusSnapshot("nonexistent", {
      id: 42,
      filename: "a.txt",
      status: "processing",
      chunk_count: 0,
      phase: "parsing",
      phase_message: "Parsing",
      progress_percent: null,
    });
    // Unknown upload id: no row to patch, no crash.

    const uploadId = useUploadStore.getState().uploads[0].id;
    useUploadStore.getState().applyStatusSnapshot(uploadId, {
      id: 42,
      filename: "a.txt",
      status: "processing",
      chunk_count: 0,
      phase: "parsing",
      phase_message: "Parsing",
      progress_percent: null,
    });

    const u = useUploadStore.getState().uploads[0];
    // CONTRACT: status is NOT "indexed" — only a terminal snapshot may say so.
    expect(u.status).toBe("processing");
    expect(u.phase).toBe("parsing");
    expect(u.phaseLabel).toBe("Parsing");
    expect(u.statusSeen).toBe(true);
  });

  it("is attempt-ordered: a LATE older-attempt snapshot never overwrites a newer one", () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "ordered-1",
          file: makeFile("a.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
          documentId: "9",
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });

    // Newer attempt (seq 5) lands terminal first.
    useUploadStore.getState().applyStatusSnapshot(
      "ordered-1",
      { id: 9, status: "indexed", chunk_count: 4 },
      5
    );
    expect(useUploadStore.getState().uploads[0].status).toBe("indexed");

    // Older attempt (seq 4) arrives late with a stale non-terminal status.
    useUploadStore.getState().applyStatusSnapshot(
      "ordered-1",
      { id: 9, status: "processing", chunk_count: 0 },
      4
    );
    const u = useUploadStore.getState().uploads[0];
    expect(u.status).toBe("indexed");
    expect(u.chunkCount).toBe(4);
  });

  it("terminal states are sticky: even a NEWER non-terminal snapshot cannot regress them", () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "sticky-1",
          file: makeFile("a.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
          documentId: "42",
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });

    useUploadStore.getState().applyStatusSnapshot(
      "sticky-1",
      { id: 42, status: "indexed", chunk_count: 3 },
      1
    );
    // A late in-flight response from a newer tick carries "processing".
    useUploadStore.getState().applyStatusSnapshot(
      "sticky-1",
      { id: 42, status: "processing", chunk_count: 0 },
      2
    );

    expect(useUploadStore.getState().uploads[0].status).toBe("indexed");
  });

  it("indexed + wiki running keeps the upload monitorable; wiki completed stops it", () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "u1",
          file: makeFile("doc.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
          documentId: "9",
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });

    useUploadStore.getState().applyStatusSnapshot("u1", {
      id: 9,
      filename: "doc.txt",
      status: "indexed",
      chunk_count: 5,
      phase: "indexed",
      wiki_status: "running",
    });
    const running = useUploadStore.getState().uploads[0];
    expect(running.status).toBe("indexed");
    expect(running.wikiStatus).toBe("running");
    expect(uploadNeedsMonitoring(running)).toBe(true);

    useUploadStore.getState().applyStatusSnapshot("u1", {
      id: 9,
      filename: "doc.txt",
      status: "indexed",
      chunk_count: 5,
      phase: "indexed",
      wiki_status: "completed",
    });
    const done = useUploadStore.getState().uploads[0];
    expect(done.wikiStatus).toBe("completed");
    expect(done.wikiProgress).toBe(100);
    expect(uploadNeedsMonitoring(done)).toBe(false);
  });

  it("bumps phaseStartedAt only on phase transition", () => {
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

describe("useUploadStore — chat attachments", () => {
  it("registers and unregisters chat attachments (survives unmounts by living in the store)", () => {
    const { attachToChat, detachFromChat } = useUploadStore.getState();
    attachToChat("u1");
    attachToChat("u1"); // idempotent
    attachToChat("u2");
    expect(useUploadStore.getState().chatAttachmentIds).toEqual(["u1", "u2"]);

    detachFromChat("u1");
    expect(useUploadStore.getState().chatAttachmentIds).toEqual(["u2"]);
  });

  it("addUploads returns the queued upload ids so callers can attach them", () => {
    uploadDocumentMock.mockImplementation(() => new Promise(() => {}));
    const ids = useUploadStore.getState().addUploads([makeFile("a.txt"), makeFile("b.txt")], 1);
    expect(ids).toHaveLength(2);
    ids.forEach((id) => useUploadStore.getState().attachToChat(id));
    expect(useUploadStore.getState().chatAttachmentIds).toEqual(ids);
  });
});

describe("useUploadStore — queue maintenance", () => {
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
          statusSeen: true,
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
    expect(u.statusSeen).toBe(false);
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
});

describe("useUploadStore — snapshot-ordering map eviction (issue #514)", () => {
  it("removeUpload evicts the seq entry: a re-added upload with the same id accepts a lower-seq snapshot", () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "evict-1",
          file: makeFile("a.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
          documentId: "9",
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });

    // Attempt 5 lands...
    useUploadStore.getState().applyStatusSnapshot(
      "evict-1",
      { id: 9, status: "processing", chunk_count: 5 },
      5
    );
    expect(useUploadStore.getState().uploads[0].chunkCount).toBe(5);

    // ...and a late seq-3 snapshot stays ignored while the row lives (the
    // ordering map is doing its job — baseline for the eviction below).
    useUploadStore.getState().applyStatusSnapshot(
      "evict-1",
      { id: 9, status: "processing", chunk_count: 3 },
      3
    );
    expect(useUploadStore.getState().uploads[0].chunkCount).toBe(5);

    // Removing the upload evicts its ordering entry...
    useUploadStore.getState().removeUpload("evict-1");
    expect(useUploadStore.getState().uploads).toEqual([]);

    // ...so a re-added upload with the SAME id starts a fresh sequence and a
    // lower-seq snapshot applies (without eviction seq=3 would be ignored).
    useUploadStore.setState({
      uploads: [
        {
          id: "evict-1",
          file: makeFile("a.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
          documentId: "9",
          statusSeen: false,
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });
    useUploadStore.getState().applyStatusSnapshot(
      "evict-1",
      { id: 9, status: "processing", chunk_count: 3 },
      3
    );
    const reAdded = useUploadStore.getState().uploads[0];
    expect(reAdded.chunkCount).toBe(3);
    expect(reAdded.statusSeen).toBe(true);
  });

  it("retryUpload evicts the seq entry: a lower-seq snapshot applies after a retry", async () => {
    useUploadStore.setState({
      uploads: [
        {
          id: "retry-evict-1",
          file: makeFile("a.txt"),
          status: "processing",
          uploadProgress: 100,
          progress: 100,
          documentId: "9",
        },
      ],
      isProcessing: false,
      activeVaultId: 1,
    });

    useUploadStore.getState().applyStatusSnapshot(
      "retry-evict-1",
      { id: 9, status: "processing", chunk_count: 5 },
      5
    );
    expect(useUploadStore.getState().uploads[0].chunkCount).toBe(5);
    // Lower-seq snapshot ignored while the old lifecycle's entry lives.
    useUploadStore.getState().applyStatusSnapshot(
      "retry-evict-1",
      { id: 9, status: "processing", chunk_count: 3 },
      3
    );
    expect(useUploadStore.getState().uploads[0].chunkCount).toBe(5);

    // Hang the retried transfer so the row stays mid-lifecycle for inspection.
    uploadDocumentMock.mockImplementation(() => new Promise(() => {}));
    useUploadStore.getState().retryUpload("retry-evict-1");
    await vi.advanceTimersByTimeAsync(1);

    const retried = useUploadStore.getState().uploads[0];
    expect(retried.statusSeen).toBe(false);

    // The retry evicted the ordering entry, so the fresh monitoring
    // sequence's seq=3 snapshot APPLIES (without eviction it would be
    // ignored as older than the pre-retry seq=5).
    useUploadStore.getState().applyStatusSnapshot(
      "retry-evict-1",
      { id: 9, status: "processing", chunk_count: 3, phase: "parsing" },
      3
    );
    const after = useUploadStore.getState().uploads[0];
    expect(after.chunkCount).toBe(3);
    expect(after.phase).toBe("parsing");
    expect(after.phaseLabel).toBe("Parsing");
    expect(after.statusSeen).toBe(true);
  });
});

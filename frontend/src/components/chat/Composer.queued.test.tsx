/**
 * Composer upload monitoring — async upload path (issue #514).
 *
 * The async POST /documents route returns immediately with status="pending".
 * The shared batched status monitor then sees status="pending" /
 * "processing" plus phase strings (queued / parsing / embedding / ...) and
 * must drive the store through the whole pipeline, flipping to "indexed"
 * only on a terminal status, never on phase alone.
 *
 * The Composer mounts `useUploadMonitoring`; these tests drive its 1s tick
 * with fake timers and assert the store contract end to end: one batched
 * request per tick covering every pending id, per-id convergence for
 * out-of-order entries, wiki-transient follow-up polling, and zero
 * requests after the Composer unmounts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";

const { uploadDocumentMock, getDocumentStatusesMock, getDocumentStatusMock } = vi.hoisted(() => ({
  uploadDocumentMock: vi.fn(),
  getDocumentStatusesMock: vi.fn(),
  getDocumentStatusMock: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    uploadDocument: uploadDocumentMock,
    getDocumentStatuses: getDocumentStatusesMock,
    getDocumentStatus: getDocumentStatusMock,
  };
});

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: vi.fn(() => ({
    input: "hello world",
    setInput: vi.fn(),
    inputError: null,
    activeChatId: null,
  })),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: Object.assign(
    vi.fn((selector?: (s: unknown) => unknown) => {
      const state = {
        activeVaultId: 1,
        getActiveVault: () => ({ id: 1, name: "v", file_count: 1 }),
      };
      return typeof selector === "function" ? selector(state) : state;
    }),
    { getState: () => ({ activeVaultId: 1 }) }
  ),
}));

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
    open: vi.fn(),
  }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

import { Composer } from "./Composer";
import { useUploadStore, uploadNeedsMonitoring } from "@/stores/useUploadStore";

function pasteFile(textarea: Element, file: File) {
  const clipboardData = {
    files: [file],
    items: [],
    types: [],
    getData: () => "",
  };
  fireEvent.paste(textarea, { clipboardData });
}

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

describe("Composer polling — async upload path", () => {
  it("drives queued → parsing → embedding → indexed via ONE batched request per tick", async () => {
    uploadDocumentMock.mockResolvedValue({
      id: 1,
      filename: "f.txt",
      status: "pending",
    });
    let call = 0;
    getDocumentStatusesMock.mockImplementation(async () => {
      call += 1;
      if (call === 1) {
        return { results: [{ id: 1, status: "pending", chunk_count: 0, phase: "queued" }] };
      }
      if (call === 2) {
        return { results: [{ id: 1, status: "processing", chunk_count: 0, phase: "parsing" }] };
      }
      if (call === 3) {
        return {
          results: [
            { id: 1, status: "processing", chunk_count: 0, phase: "embedding", progress_percent: 40 },
          ],
        };
      }
      return {
        results: [
          { id: 1, status: "indexed", chunk_count: 5, phase: "indexed", wiki_status: "completed" },
        ],
      };
    });

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    await act(async () => {
      pasteFile(textarea, new File([new Uint8Array(8)], "f.txt", { type: "text/plain" }));
    });

    // Cycle 1: queued — non-terminal statuses keep the chip in an
    // indexing-style state, never error.
    await tick();
    let u = useUploadStore.getState().uploads[0];
    expect(u.status).toBe("processing");
    expect(u.phase).toBe("queued");
    expect(u.error).toBeUndefined();
    expect(getDocumentStatusesMock).toHaveBeenCalledTimes(1);
    // One batched round-trip covers the single pending document id.
    expect(getDocumentStatusesMock.mock.calls[0][0]).toEqual(["1"]);
    // No per-file status requests while the batched client exists.
    expect(getDocumentStatusMock).not.toHaveBeenCalled();

    // Cycle 2: parsing
    await tick();
    u = useUploadStore.getState().uploads[0];
    expect(u.phase).toBe("parsing");
    expect(u.status).toBe("processing");

    // Cycle 3: embedding 40%
    await tick();
    u = useUploadStore.getState().uploads[0];
    expect(u.phase).toBe("embedding");
    expect(u.processingProgress).toBe(40);

    // Cycle 4: indexed
    await tick();
    u = useUploadStore.getState().uploads[0];
    expect(u.status).toBe("indexed");
    expect(u.chunkCount).toBe(5);
    expect(uploadNeedsMonitoring(u)).toBe(false);

    // Terminal: no further ticks fire.
    const callsAfterTerminal = getDocumentStatusesMock.mock.calls.length;
    await tick(5);
    expect(getDocumentStatusesMock.mock.calls.length).toBe(callsAfterTerminal);
  });

  it("converges per id when the batched response arrives in reversed order", async () => {
    uploadDocumentMock.mockImplementation(async (file: File) => ({
      id: file.name === "a.txt" ? 41 : 42,
      filename: file.name,
      status: "pending",
    }));
    getDocumentStatusesMock.mockResolvedValue({
      // Reversed id order: entries must be keyed by their own id. (The
      // raw-envelope normalization from `documents`/`results` lives inside
      // the api client itself and is exercised through the real client by
      // the issue's frozen acceptance check.)
      results: [
        { id: 42, status: "processing", chunk_count: 0 },
        { id: 41, status: "indexed", chunk_count: 7 },
      ],
    });

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    await act(async () => {
      pasteFile(textarea, new File([new Uint8Array(4)], "a.txt", { type: "text/plain" }));
    });
    await act(async () => {
      pasteFile(textarea, new File([new Uint8Array(4)], "b.txt", { type: "text/plain" }));
    });
    await tick();

    const byDoc = new Map(
      useUploadStore.getState().uploads.map((u) => [u.documentId, u])
    );
    expect(byDoc.get("41")?.status).toBe("indexed");
    expect(byDoc.get("41")?.chunkCount).toBe(7);
    expect(byDoc.get("42")?.status).toBe("processing");
    // Chips converge the same way.
    expect(screen.getAllByTestId("attachment-indexed")).toHaveLength(1);
    expect(screen.getAllByTestId("attachment-indexing")).toHaveLength(1);
  });

  it("keeps polling while wiki compilation runs, then stops at completed", async () => {
    uploadDocumentMock.mockResolvedValue({
      id: 9,
      filename: "doc.txt",
      status: "pending",
    });
    let call = 0;
    getDocumentStatusesMock.mockImplementation(async () => {
      call += 1;
      if (call < 3) {
        return {
          results: [
            { id: 9, status: "indexed", chunk_count: 5, phase: "indexed", wiki_status: "running" },
          ],
        };
      }
      return {
        results: [
          { id: 9, status: "indexed", chunk_count: 5, phase: "indexed", wiki_status: "completed" },
        ],
      };
    });

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    await act(async () => {
      pasteFile(textarea, new File([new Uint8Array(4)], "doc.txt", { type: "text/plain" }));
    });

    // Tick 1: indexed but wiki running -> keep monitoring.
    await tick();
    const running = useUploadStore.getState().uploads[0];
    expect(running.status).toBe("indexed");
    expect(running.wikiStatus).toBe("running");
    expect(uploadNeedsMonitoring(running)).toBe(true);

    // Tick 2: still wiki running.
    await tick();
    expect(useUploadStore.getState().uploads[0].wikiStatus).toBe("running");

    // Tick 3: wiki completed -> monitoring ends, no further requests.
    await tick();
    const done = useUploadStore.getState().uploads[0];
    expect(done.wikiStatus).toBe("completed");
    expect(done.wikiProgress).toBe(100);
    expect(uploadNeedsMonitoring(done)).toBe(false);
    const callsAtCompletion = getDocumentStatusesMock.mock.calls.length;
    await tick(4);
    expect(getDocumentStatusesMock.mock.calls.length).toBe(callsAtCompletion);
  });

  it("never issues status requests after the Composer unmounts", async () => {
    let resolveUpload!: (v: unknown) => void;
    uploadDocumentMock.mockImplementation(
      () => new Promise((resolve) => { resolveUpload = resolve; })
    );

    const utils = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    await act(async () => {
      pasteFile(textarea, new File([new Uint8Array(4)], "late.txt", { type: "text/plain" }));
    });
    expect(getDocumentStatusesMock).not.toHaveBeenCalled();

    await act(async () => {
      utils.unmount();
    });

    // The upload resolves AFTER unmount; monitoring must not resume.
    await act(async () => {
      resolveUpload({ id: 7, filename: "late.txt", status: "pending" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(getDocumentStatusesMock).not.toHaveBeenCalled();
    expect(getDocumentStatusMock).not.toHaveBeenCalled();
    // Store state itself survived the unmount (bytes accepted).
    const u = useUploadStore.getState().uploads[0];
    expect(u.status).toBe("processing");
    expect(u.documentId).toBe("7");
  });
});

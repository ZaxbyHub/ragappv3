/**
 * Tests for the composer attachment upload + indexing UX (P1.4).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { Composer } from "./Composer";

const apiMock = vi.hoisted(() => ({
  uploadDocument: vi.fn(),
  getDocumentStatus: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMock);

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
    vi.fn((selector?: (s: any) => unknown) => {
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
    warning: vi.fn(),
  },
}));

beforeEach(() => {
  apiMock.uploadDocument.mockReset();
  apiMock.getDocumentStatus.mockReset();
});

function pasteFile(textarea: Element, file: File) {
  const clipboardData = {
    files: [file],
    items: [],
    types: [],
    getData: () => "",
  };
  fireEvent.paste(textarea, { clipboardData });
}

describe("Composer attachment indexing UX", () => {
  it("transitions uploading → uploaded → indexing → indexed via polling", async () => {
    apiMock.uploadDocument.mockResolvedValue({
      id: 42,
      filename: "doc.pdf",
      status: "pending",
    });
    apiMock.getDocumentStatus
      .mockResolvedValueOnce({
        id: 42,
        filename: "doc.pdf",
        status: "processing",
        chunk_count: 0,
      })
      .mockResolvedValueOnce({
        id: 42,
        filename: "doc.pdf",
        status: "indexed",
        chunk_count: 5,
      });

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    const file = new File(["x"], "doc.pdf", { type: "application/pdf" });
    await act(async () => {
      pasteFile(textarea, file);
    });

    // After upload resolves we should land on "uploaded".
    await waitFor(() => {
      expect(screen.queryByTestId("attachment-uploaded")).toBeInTheDocument();
    });

    // First poll → processing → "indexing" chip.
    await waitFor(
      () => {
        expect(screen.queryByTestId("attachment-indexing")).toBeInTheDocument();
      },
      { timeout: 3000 }
    );

    // Second poll → indexed.
    await waitFor(
      () => {
        expect(screen.queryByTestId("attachment-indexed")).toBeInTheDocument();
      },
      { timeout: 3000 }
    );
    expect(screen.getByText(/Indexed/)).toBeInTheDocument();
  });

  it("surfaces upload failure with status=error and a removable chip", async () => {
    apiMock.uploadDocument.mockRejectedValue(new Error("disk full"));

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    const file = new File(["x"], "bad.pdf", { type: "application/pdf" });
    await act(async () => {
      pasteFile(textarea, file);
    });

    await waitFor(() => {
      expect(screen.queryByTestId("attachment-error")).toBeInTheDocument();
    });
    expect(screen.getByText(/disk full/)).toBeInTheDocument();
    expect(screen.getByLabelText("Remove bad.pdf")).toBeInTheDocument();
  });

  it("surfaces indexing failure from the status endpoint", async () => {
    apiMock.uploadDocument.mockResolvedValue({
      id: 99,
      filename: "broken.pdf",
      status: "pending",
    });
    apiMock.getDocumentStatus.mockResolvedValueOnce({
      id: 99,
      filename: "broken.pdf",
      status: "error",
      chunk_count: 0,
      error_message: "OCR failed",
    });

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    const file = new File(["x"], "broken.pdf", { type: "application/pdf" });
    await act(async () => {
      pasteFile(textarea, file);
    });

    await waitFor(() =>
      expect(screen.queryByTestId("attachment-uploaded")).toBeInTheDocument()
    );

    await waitFor(
      () => {
        expect(screen.queryByTestId("attachment-error")).toBeInTheDocument();
      },
      { timeout: 3000 }
    );
    expect(screen.getByText(/OCR failed/)).toBeInTheDocument();
  });

  it("disables Send while uploads are still transferring", async () => {
    let resolveUpload: ((v: unknown) => void) | null = null;
    apiMock.uploadDocument.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        })
    );

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");
    const file = new File(["x"], "slow.pdf", { type: "application/pdf" });
    await act(async () => {
      pasteFile(textarea, file);
    });

    const sendButton = screen.getByLabelText("Send message");
    expect(sendButton).toBeDisabled();

    // Resolve the upload — Send re-enables once chip is "uploaded".
    await act(async () => {
      resolveUpload?.({ id: 1, filename: "slow.pdf", status: "pending" });
    });
    await waitFor(() => {
      expect(screen.queryByTestId("attachment-uploaded")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Send message")).not.toBeDisabled();
    });
  });
});

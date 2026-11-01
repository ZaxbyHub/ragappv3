import { StrictMode, type ReactElement } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";
import {
  getCommandPaletteActionSnapshot,
} from "@/lib/commandPaletteActions";
import { reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";
import { useChatStore } from "@/stores/useChatStore";
import { useUploadStore } from "@/stores/useUploadStore";
import { useVaultStore } from "@/stores/useVaultStore";

const seams = vi.hoisted(() => ({
  uploadDocument: vi.fn(),
  getDocumentStatuses: vi.fn(),
  getLlmModeHealth: vi.fn(),
  getFilesFromEvent: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    uploadDocument: seams.uploadDocument,
    getDocumentStatuses: seams.getDocumentStatuses,
    getLlmModeHealth: seams.getLlmModeHealth,
  };
});

vi.mock("react-dropzone", async () => {
  const actual = await vi.importActual<typeof import("react-dropzone")>("react-dropzone");
  return {
    ...actual,
    useDropzone: (options: Parameters<typeof actual.useDropzone>[0]) =>
      actual.useDropzone({
        ...options,
        getFilesFromEvent: seams.getFilesFromEvent,
      }),
  };
});

function renderComposer(inRouter = true) {
  const element = <Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />;
  return inRouter
    ? render(<MemoryRouter initialEntries={["/chat"]}>{element}</MemoryRouter>)
    : render(element);
}

function fileInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector('input[type="file"]');
  if (!(input instanceof HTMLInputElement)) {
    throw new Error("Composer file input is missing");
  }
  return input;
}

function openPicker(container: HTMLElement): HTMLInputElement {
  fireEvent.click(screen.getByRole("button", { name: "Attach file" }));
  return fileInput(container);
}

function renderResultElement(): ReactElement {
  return <Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />;
}

beforeEach(() => {
  seams.uploadDocument.mockReset().mockResolvedValue({ id: "document-1" });
  seams.getDocumentStatuses.mockReset().mockResolvedValue({ documents: [] });
  seams.getLlmModeHealth.mockReset().mockResolvedValue({ thinking: true, instant: true });
  seams.getFilesFromEvent.mockReset().mockResolvedValue([]);

  useVaultStore.setState({ activeVaultId: 1 });
  useChatStore.setState({ activeChatId: "chat-a", input: "", inputError: null, isStreaming: false });
  useUploadStore.setState({
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],
  });
});

afterEach(() => {
  cleanup();
  useVaultStore.setState({ activeVaultId: null });
  useChatStore.setState({ activeChatId: null, input: "", inputError: null, isStreaming: false });
  useUploadStore.setState({
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],
  });
  vi.restoreAllMocks();
});

describe("Composer native picker ownership", () => {
  it("opens the real hidden input and attaches a current-owner selection once", async () => {
    const { container } = renderComposer();
    const click = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => undefined);
    const input = openPicker(container);
    const file = new File(["current"], "current.txt", { type: "text/plain" });

    expect(click).toHaveBeenCalledTimes(1);
    seams.getFilesFromEvent.mockResolvedValueOnce([file]);
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(useUploadStore.getState().chatAttachmentIds).toHaveLength(1);
    });
    expect(useUploadStore.getState().uploads).toHaveLength(1);
    expect(useUploadStore.getState().chatAttachmentIds).toEqual([
      useUploadStore.getState().uploads[0]?.id,
    ]);
    expect(seams.uploadDocument).toHaveBeenCalledTimes(1);
  });

  it("rejects an unopened router input while preserving the standalone control", async () => {
    const routerRender = renderComposer();
    const file = new File(["unopened"], "unopened.txt", { type: "text/plain" });
    seams.getFilesFromEvent.mockResolvedValueOnce([file]);
    fireEvent.change(fileInput(routerRender.container), { target: { files: [file] } });

    await waitFor(() => expect(seams.getFilesFromEvent).toHaveBeenCalledTimes(1));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(useUploadStore.getState().uploads).toHaveLength(0);
    expect(useUploadStore.getState().chatAttachmentIds).toHaveLength(0);

    routerRender.unmount();
    const standalone = renderComposer(false);
    const click = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => undefined);
    const standaloneInput = openPicker(standalone.container);
    expect(click).toHaveBeenCalledTimes(1);
    const standaloneFile = new File(["standalone"], "standalone.txt", { type: "text/plain" });
    seams.getFilesFromEvent.mockResolvedValueOnce([standaloneFile]);
    fireEvent.change(standaloneInput, { target: { files: [standaloneFile] } });
    await waitFor(() => {
      expect(useUploadStore.getState().chatAttachmentIds).toHaveLength(1);
    });
  });

  it("does not let an old opening attach after the picker is reopened", async () => {
    let resolveA!: (files: File[]) => void;
    const extractionA = new Promise<File[]>((resolve) => {
      resolveA = resolve;
    });
    seams.getFilesFromEvent
      .mockReturnValueOnce(extractionA)
      .mockResolvedValueOnce([]);
    const { container } = renderComposer();
    vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => undefined);

    const inputA = openPicker(container);
    const fileA = new File(["old"], "old.txt", { type: "text/plain" });
    fireEvent.change(inputA, { target: { files: [fileA] } });
    expect(seams.getFilesFromEvent).toHaveBeenCalledTimes(1);

    const inputB = openPicker(container);
    expect(inputB).not.toBe(inputA);
    resolveA([fileA]);
    await act(async () => {
      await extractionA;
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(useUploadStore.getState().uploads).toHaveLength(0);
    expect(useUploadStore.getState().chatAttachmentIds).toHaveLength(0);
  });

  it.each([
    "chat ABA",
    "vault ABA",
    "authentication replacement",
    "unmount",
  ])("invalidates a delayed selection after %s", async (invalidation) => {
    let resolve!: (files: File[]) => void;
    const extraction = new Promise<File[]>((next) => {
      resolve = next;
    });
    seams.getFilesFromEvent.mockReturnValueOnce(extraction);
    const result = renderComposer();
    vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => undefined);
    const input = openPicker(result.container);
    const file = new File(["stale"], "stale.txt", { type: "text/plain" });
    fireEvent.change(input, { target: { files: [file] } });

    act(() => {
      if (invalidation === "chat ABA") {
        useChatStore.getState().loadChat("chat-b", []);
        useChatStore.getState().loadChat("chat-a", []);
      } else if (invalidation === "vault ABA") {
        useVaultStore.getState().setActiveVault(2);
        useVaultStore.getState().setActiveVault(1);
      } else if (invalidation === "authentication replacement") {
        reserveReplacementAuthOwner();
      }
    });
    if (invalidation === "unmount") result.unmount();

    resolve([file]);
    await act(async () => {
      await extraction;
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(useUploadStore.getState().uploads).toHaveLength(0);
    expect(useUploadStore.getState().chatAttachmentIds).toHaveLength(0);
    if (invalidation !== "unmount") result.unmount();
  });

  it("registers exactly one real Attach file receiver under StrictMode", () => {
    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/chat"]}>{renderResultElement()}</MemoryRouter>
      </StrictMode>,
    );

    expect(
      getCommandPaletteActionSnapshot().filter((entry) => entry.id === "attach-file"),
    ).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Attach file" })).toBeInTheDocument();
  });
});

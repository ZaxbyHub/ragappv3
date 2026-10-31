/**
 * Issue #1052 regression tests — Draft Room attach-path upload policy
 * (AC2/AC3, plus the AC1 message-shape contract for this surface).
 *
 * DraftSourceUpload.addFiles previously had NEITHER a size guard NOR an
 * extension filter (the issue's verification comment: no size constant, no
 * accept, no maxSize anywhere in the file) — every file was queued and the
 * server rejected oversize/disallowed bytes only after a wasted upload. Now
 * addFiles reuses the shared uploadLimits helpers against the
 * server-configured policy: max_file_size_mb (K-08) and the allowlist minus
 * raster images (backend DRAFT_ROOM_ALLOWED_EXTENSIONS, issue #460).
 *
 * Harness follows DraftSourceUpload.test.tsx: the real component with the
 * draft-room API mocked and the REAL settings store driven via setState.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DraftSourceUpload } from "./DraftSourceUpload";
import { ADD_SOURCE_FILES_CTA } from "./labels";
import { useSettingsStore } from "@/stores/useSettingsStore";
import type { SettingsResponse } from "@/lib/api/settings";

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
  }),
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    uploadDraftInput: vi.fn(),
    getDraft: vi.fn(),
  };
});

const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: toastError,
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

import { uploadDraftInput, getDraft } from "@/lib/api/draftRoom";

const mockUploadDraftInput = vi.mocked(uploadDraftInput);
const mockGetDraft = vi.mocked(getDraft);

const MB = 1024 * 1024;

function setServerPolicy(maxFileSizeMb: number, allowedExtensions: string[]) {
  useSettingsStore.setState({
    settings: {
      max_file_size_mb: maxFileSizeMb,
      allowed_extensions: allowedExtensions,
    } as SettingsResponse,
  });
}

function renderComponent() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DraftSourceUpload draftId={1} maxInputs={10} currentInputCount={0} />
    </QueryClientProvider>
  );
}

function sizedFile(name: string, bytes: number) {
  return new File([new Uint8Array(bytes)], name, { type: "application/octet-stream" });
}

beforeEach(() => {
  vi.clearAllMocks();
  // Isolate per-test policy state (zustand store is module-global).
  useSettingsStore.setState({ settings: null });
  mockGetDraft.mockResolvedValue({} as never);
  mockUploadDraftInput.mockImplementation(() => new Promise(() => {}) as never);
});

describe("DraftSourceUpload server-configured upload policy (issue #1052)", () => {
  it("size-guards uploads against the server-configured limit instead of the hardcoded fallback", async () => {
    const user = userEvent.setup();
    // 2 MB server limit — a 3 MB file sails under the old behavior (no guard)
    // and under any 100 MB hardcoded fallback.
    setServerPolicy(2, [".md", ".txt", ".pdf"]);
    renderComponent();

    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    await user.upload(fileInput, sizedFile("big.md", 3 * MB));

    // Rejected at attach time with the configured number in the message
    // (the same uploadSizeExceededMessage helper the upload store uses).
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(toastError).toHaveBeenCalledWith("big.md is too large. Max size: 2 MB.");
    await waitFor(() => {
      expect(mockUploadDraftInput).not.toHaveBeenCalled();
    });
    // Never queued: the item list stays empty.
    expect(screen.queryByText("big.md")).not.toBeInTheDocument();
  });

  it("rejects disallowed extension files at attach time with the dropzone message shape", async () => {
    const user = userEvent.setup();
    setServerPolicy(100, [".md", ".txt"]);
    renderComponent();

    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    await user.upload(fileInput, sizedFile("archive.exe", 8));

    expect(toastError).toHaveBeenCalledTimes(1);
    expect(toastError).toHaveBeenCalledWith("archive.exe (file type not allowed)");
    await waitFor(() => {
      expect(mockUploadDraftInput).not.toHaveBeenCalled();
    });
    expect(screen.queryByText("archive.exe")).not.toBeInTheDocument();
  });

  it("excludes raster images on the Draft Room surface even when the server allowlist includes them", async () => {
    const user = userEvent.setup();
    // .png is allowlisted for documents, but the Draft Room server path
    // stages with DRAFT_ROOM_ALLOWED_EXTENSIONS (allowlist minus raster
    // images, issue #460) — the client mirror must reject it too.
    setServerPolicy(100, [".md", ".txt", ".png"]);
    renderComponent();

    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    await user.upload(fileInput, sizedFile("photo.png", 8));

    expect(toastError).toHaveBeenCalledTimes(1);
    expect(toastError).toHaveBeenCalledWith("photo.png (file type not allowed)");
    await waitFor(() => {
      expect(mockUploadDraftInput).not.toHaveBeenCalled();
    });
  });

  it("accepts a file exactly at the configured limit (boundary is inclusive)", async () => {
    const user = userEvent.setup();
    setServerPolicy(2, [".md", ".txt"]);
    renderComponent();

    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    await user.upload(fileInput, sizedFile("edge.md", 2 * MB));

    await waitFor(() => {
      expect(mockUploadDraftInput).toHaveBeenCalledTimes(1);
    });
    expect(mockUploadDraftInput.mock.calls[0][1].file.name).toBe("edge.md");
    expect(toastError).not.toHaveBeenCalled();
  });

  it("queues an allowed in-limit file while reporting its rejected batch-mates", async () => {
    const user = userEvent.setup();
    setServerPolicy(2, [".md", ".txt"]);
    renderComponent();

    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    await user.upload(fileInput, [
      sizedFile("ok.md", 1024),
      sizedFile("bad.exe", 8),
      sizedFile("huge.md", 3 * MB),
    ]);

    // Only the acceptable file is queued and uploaded; each reject is
    // reported individually with its own message shape.
    await waitFor(() => {
      expect(mockUploadDraftInput).toHaveBeenCalledTimes(1);
    });
    expect(mockUploadDraftInput.mock.calls[0][1].file.name).toBe("ok.md");
    expect(toastError).toHaveBeenCalledTimes(2);
    expect(toastError).toHaveBeenCalledWith("bad.exe (file type not allowed)");
    expect(toastError).toHaveBeenCalledWith("huge.md is too large. Max size: 2 MB.");
  });

  it("keeps the historical pre-load behavior: with settings unloaded there is no attach-time size rejection", async () => {
    const user = userEvent.setup();
    useSettingsStore.setState({ settings: null });
    renderComponent();

    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    await user.upload(fileInput, sizedFile("early.md", 3 * MB));

    // 3 MB is under the 100 MB fallback, so the upload proceeds exactly as
    // before settings load; the server still validates the real policy.
    await waitFor(() => {
      expect(mockUploadDraftInput).toHaveBeenCalledTimes(1);
    });
    expect(toastError).not.toHaveBeenCalled();
  });
});

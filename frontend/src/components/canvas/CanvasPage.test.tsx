import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Shiki's dynamic import is mocked to reject so the preview fallback path is
// deterministic — the plain-text branch must show the content unchanged.
vi.mock("shiki", () => ({
  createHighlighter: vi.fn(async () => {
    throw new Error("shiki unavailable in canvas tests");
  }),
}));

const {
  getCanvasArtifactMock,
  listCanvasVersionsMock,
  getCanvasVersionMock,
  saveCanvasVersionMock,
  restoreCanvasVersionMock,
  editCanvasRangeMock,
  downloadCanvasVersionMock,
  exportCanvasManifestMock,
} = vi.hoisted(() => ({
  getCanvasArtifactMock: vi.fn(),
  listCanvasVersionsMock: vi.fn(),
  getCanvasVersionMock: vi.fn(),
  saveCanvasVersionMock: vi.fn(),
  restoreCanvasVersionMock: vi.fn(),
  editCanvasRangeMock: vi.fn(),
  downloadCanvasVersionMock: vi.fn(),
  exportCanvasManifestMock: vi.fn(),
}));

const { useCanvasCapabilitiesMock } = vi.hoisted(() => ({
  useCanvasCapabilitiesMock: vi.fn(),
}));

vi.mock("@/hooks/useCanvasCapabilities", () => ({
  useCanvasCapabilities: useCanvasCapabilitiesMock,
}));

vi.mock("@/lib/api/canvas", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/canvas")>("@/lib/api/canvas");
  return {
    ...actual,
    getCanvasArtifact: getCanvasArtifactMock,
    listCanvasVersions: listCanvasVersionsMock,
    getCanvasVersion: getCanvasVersionMock,
    saveCanvasVersion: saveCanvasVersionMock,
    restoreCanvasVersion: restoreCanvasVersionMock,
    editCanvasRange: editCanvasRangeMock,
    downloadCanvasVersion: downloadCanvasVersionMock,
    exportCanvasManifest: exportCanvasManifestMock,
  };
});

// Radix Select cannot open in jsdom (no pointer capture) — module-mock it so
// SelectItem clicks call onValueChange (established pattern, see
// DraftRevisionDiff.test.tsx). CanvasCompare is the only ui/select consumer
// in this tree.
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  type OnValueChange = (value: string) => void;
  const SelectCtx = React.createContext<OnValueChange>(() => {});

  function Select({
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: OnValueChange;
    children?: React.ReactNode;
  }) {
    return React.createElement(SelectCtx.Provider, { value: onValueChange ?? (() => {}) }, children);
  }
  function SelectTrigger({ id, children }: { id?: string; children?: React.ReactNode }) {
    return React.createElement("div", { "data-testid": id }, children);
  }
  function SelectValue({ placeholder }: { placeholder?: string }) {
    return React.createElement("span", null, placeholder);
  }
  function SelectContent({ children }: { children?: React.ReactNode }) {
    return React.createElement("div", null, children);
  }
  function SelectItem({ value, children }: { value: string; children?: React.ReactNode }) {
    const onValueChange = React.useContext(SelectCtx);
    return React.createElement(
      "button",
      { type: "button", onClick: () => onValueChange(value) },
      children
    );
  }
  return { Select, SelectTrigger, SelectValue, SelectContent, SelectItem };
});

import CanvasPage from "./CanvasPage";
import type { CanvasArtifact, CanvasVersion, CanvasVersionSummary } from "@/lib/api/canvas";

// =============================================================================
// Fixtures
// =============================================================================

const V1_CONTENT = "def hello():\n    print('v1')\n";
const V2_CONTENT = "def hello():\n    print('v2')\n";

const ARTIFACT_V2: CanvasArtifact = {
  artifact_uid: "cav_test1",
  session_id: 7,
  message_id: 42,
  turn_id: "turn-1",
  kind: "code",
  name: "demo.py",
  language: "python",
  current_version_no: 2,
  source_refs: [{ source_id: "s1", title: "a.pdf" }],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

function makeVersion(no: number, content: string, origin: CanvasVersion["origin"], name: string | null = null): CanvasVersion {
  return {
    version_no: no,
    name,
    origin,
    model_edit: null,
    content_sha256: `sha-${no}`,
    created_at: `2026-01-0${no}T00:00:00Z`,
    content,
  };
}

function makeSummary(no: number, origin: CanvasVersionSummary["origin"], name: string | null = null): CanvasVersionSummary {
  const { content: _content, ...summary } = makeVersion(no, `content-${no}`, origin, name);
  return summary;
}

const VERSION_SUMMARIES = [makeSummary(1, "created"), makeSummary(2, "user_edit")];

function mockHappyPath(options: { artifact?: CanvasArtifact; versions?: CanvasVersionSummary[] } = {}) {
  const artifact = options.artifact ?? ARTIFACT_V2;
  const currentContent = artifact === ARTIFACT_V2 ? V2_CONTENT : `content-v${artifact.current_version_no}`;
  getCanvasArtifactMock.mockResolvedValue({
    artifact,
    version: makeVersion(artifact.current_version_no, currentContent, "user_edit"),
  });
  listCanvasVersionsMock.mockResolvedValue({ versions: options.versions ?? VERSION_SUMMARIES });
}

function makeStatusError(status: number, detail: string): Error {
  const error = new Error(detail);
  (error as unknown as { status?: number }).status = status;
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status, data: { detail } },
  };
  return error;
}

const storage = new Map<string, string>();

function renderCanvasPage(initialEntry = "/chat/7/canvas/cav_test1") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/chat/:sessionId/canvas/:artifactUid" element={<CanvasPage />} />
          <Route path="/chat/:sessionId" element={<div data-testid="chat-home" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  useCanvasCapabilitiesMock.mockReset().mockReturnValue({
    data: { enabled: true },
    isLoading: false,
    isError: false,
  });
  storage.clear();
  vi.mocked(localStorage.getItem).mockImplementation((key: string) => storage.get(key) ?? null);
  vi.mocked(localStorage.setItem).mockImplementation((key: string, value: string) => {
    storage.set(key, value);
  });
  vi.mocked(localStorage.removeItem).mockImplementation((key: string) => {
    storage.delete(key);
  });
  getCanvasArtifactMock.mockReset();
  listCanvasVersionsMock.mockReset();
  getCanvasVersionMock.mockReset();
  saveCanvasVersionMock.mockReset();
  restoreCanvasVersionMock.mockReset();
  editCanvasRangeMock.mockReset();
  downloadCanvasVersionMock.mockReset();
  exportCanvasManifestMock.mockReset();
});

afterEach(() => {
  cleanup();
});

// =============================================================================
// Rendering, rail, badges, keyboard
// =============================================================================

describe("CanvasPage rendering", () => {
  it("renders the header, editor content, and version rail with origin badge labels", async () => {
    mockHappyPath();
    renderCanvasPage();

    expect(await screen.findByRole("heading", { name: "demo.py" })).toBeInTheDocument();
    const editor = await screen.findByLabelText("Canvas content editor");
    expect(editor).toHaveValue(V2_CONTENT);

    const railButtons = screen.getAllByTestId("canvas-version-button");
    expect(railButtons).toHaveLength(2);

    const badges = screen.getAllByTestId("canvas-origin-badge");
    expect(badges.map((badge) => badge.textContent)).toEqual(["Created", "Edited"]);

    expect(screen.getByText("Version 1")).toBeInTheDocument();
    expect(screen.getByText("Version 2")).toBeInTheDocument();
    expect(screen.getByText("Current")).toBeInTheDocument();
  });

  it("shows a loading skeleton while the artifact loads", () => {
    getCanvasArtifactMock.mockReturnValue(new Promise(() => {}));
    renderCanvasPage();
    expect(screen.getByTestId("canvas-skeleton")).toBeInTheDocument();
  });

  it("uses responsive stacking classes (rail above editor on narrow widths)", async () => {
    mockHappyPath();
    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");

    const layout = screen.getByTestId("canvas-layout");
    expect(layout.className).toContain("flex-col");
    expect(layout.className).toContain("lg:flex-row");
    expect(screen.getByTestId("canvas-version-rail").className).toContain("lg:w-72");
  });

  it("keyboard: version buttons are focusable and Enter activates selection", async () => {
    mockHappyPath();
    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");

    const user = userEvent.setup();
    const firstVersion = screen.getAllByTestId("canvas-version-button")[0];
    firstVersion.focus();
    expect(firstVersion).toHaveFocus();
    await user.keyboard("{Enter}");

    await waitFor(() =>
      expect(screen.getByTestId("canvas-readonly-notice")).toHaveTextContent(
        "Viewing version 1 (read-only)"
      )
    );
  });

  it("renders the not-found first-use page with a link back to chat on 404", async () => {
    getCanvasArtifactMock.mockRejectedValue(makeStatusError(404, "canvas_not_found"));
    renderCanvasPage();

    expect(await screen.findByText("This canvas could not be found")).toBeInTheDocument();
    const backLink = screen.getByRole("link", { name: "Back to chat" });
    expect(backLink).toHaveAttribute("href", "/chat/7");
  });

  it("renders an error retry state for other failures", async () => {
    getCanvasArtifactMock.mockRejectedValue(makeStatusError(500, "boom"));
    renderCanvasPage();

    expect(await screen.findByText("Could not load this canvas")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(getCanvasArtifactMock).toHaveBeenCalledTimes(2));
  });

  it("shows the honest disabled banner when the capability is off (content still loads)", async () => {
    mockHappyPath();
    useCanvasCapabilitiesMock.mockReturnValue({
      data: { enabled: false },
      isLoading: false,
      isError: false,
    });
    renderCanvasPage();

    expect(await screen.findByTestId("canvas-disabled-banner")).toHaveTextContent(
      "Canvas is disabled on this server"
    );
    // The page stays honest, not a redirect: content still renders.
    expect(await screen.findByLabelText("Canvas content editor")).toHaveValue(V2_CONTENT);
  });

  it("shows the disabled banner when capabilities fail with a 503 canvas_disabled", async () => {
    mockHappyPath();
    const error = new Error("canvas_disabled");
    (error as unknown as { status?: number }).status = 503;
    useCanvasCapabilitiesMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error,
    });
    renderCanvasPage();

    expect(await screen.findByTestId("canvas-disabled-banner")).toBeInTheDocument();
  });
});

// =============================================================================
// Save flow (new version appears; named version)
// =============================================================================

describe("CanvasPage save flow", () => {
  it("saving appends a new version that appears in the rail", async () => {
    const newContent = "def hello():\n    print('v3')\n";
    const artifactV3 = { ...ARTIFACT_V2, current_version_no: 3, updated_at: "2026-01-03T00:00:00Z" };

    getCanvasArtifactMock
      .mockResolvedValueOnce({ artifact: ARTIFACT_V2, version: makeVersion(2, V2_CONTENT, "user_edit") })
      .mockResolvedValueOnce({ artifact: artifactV3, version: makeVersion(3, newContent, "user_edit") });
    listCanvasVersionsMock
      .mockResolvedValueOnce({ versions: VERSION_SUMMARIES })
      .mockResolvedValueOnce({
        versions: [...VERSION_SUMMARIES, makeSummary(3, "user_edit")],
      });
    saveCanvasVersionMock.mockResolvedValue(makeVersion(3, newContent, "user_edit"));

    renderCanvasPage();
    const editor = await screen.findByLabelText("Canvas content editor");
    fireEvent.change(editor, { target: { value: newContent } });

    const saveButton = screen.getByTestId("canvas-save-button");
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);

    await waitFor(() =>
      expect(saveCanvasVersionMock).toHaveBeenCalledWith("cav_test1", {
        content: newContent,
        base_version_no: 2,
        force: false,
      })
    );
    await waitFor(() => expect(screen.getByText("Version 3")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByLabelText("Canvas content editor")).toHaveValue(newContent));
  });

  it("sends the optional name for a named version", async () => {
    const newContent = "def hello():\n    print('named')\n";
    getCanvasArtifactMock.mockResolvedValue({
      artifact: ARTIFACT_V2,
      version: makeVersion(2, V2_CONTENT, "user_edit"),
    });
    listCanvasVersionsMock.mockResolvedValue({ versions: VERSION_SUMMARIES });
    saveCanvasVersionMock.mockResolvedValue(makeVersion(3, newContent, "user_edit", "Post fix"));

    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    fireEvent.change(screen.getByLabelText("Canvas content editor"), { target: { value: newContent } });
    fireEvent.change(screen.getByLabelText("Version name"), { target: { value: "Post fix" } });
    fireEvent.click(screen.getByTestId("canvas-save-button"));

    await waitFor(() =>
      expect(saveCanvasVersionMock).toHaveBeenCalledWith("cav_test1", {
        content: newContent,
        name: "Post fix",
        base_version_no: 2,
        force: false,
      })
    );
  });

  it("disables Save while the editor is clean", async () => {
    mockHappyPath();
    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    expect(screen.getByTestId("canvas-save-button")).toBeDisabled();
  });
});

// =============================================================================
// Restore
// =============================================================================

describe("CanvasPage restore", () => {
  it("restoring an old version appends a restored version via the confirm dialog", async () => {
    const artifactV3 = { ...ARTIFACT_V2, current_version_no: 3 };
    getCanvasArtifactMock
      .mockResolvedValueOnce({ artifact: ARTIFACT_V2, version: makeVersion(2, V2_CONTENT, "user_edit") })
      .mockResolvedValueOnce({ artifact: artifactV3, version: makeVersion(3, V1_CONTENT, "restore") });
    getCanvasVersionMock.mockResolvedValue({
      artifact: ARTIFACT_V2,
      version: makeVersion(1, V1_CONTENT, "created"),
    });
    listCanvasVersionsMock
      .mockResolvedValue({ versions: [makeSummary(1, "created"), makeSummary(2, "user_edit")] })
      .mockResolvedValue({
        versions: [makeSummary(1, "created"), makeSummary(2, "user_edit"), makeSummary(3, "restore")],
      });
    restoreCanvasVersionMock.mockResolvedValue(makeVersion(3, V1_CONTENT, "restore"));

    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");

    fireEvent.click(screen.getAllByTestId("canvas-version-button")[0]);
    await waitFor(() =>
      expect(screen.getByTestId("canvas-readonly-notice")).toHaveTextContent("Viewing version 1")
    );

    fireEvent.click(screen.getByTestId("canvas-restore-button"));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() =>
      expect(restoreCanvasVersionMock).toHaveBeenCalledWith("cav_test1", {
        version_no: 1,
        base_version_no: 2,
      })
    );
    await waitFor(() => expect(screen.getByText("Version 3")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByLabelText("Canvas content editor")).toHaveValue(V1_CONTENT)
    );
    // Origin badge for the appended restore version uses the human label.
    const badges = screen.getAllByTestId("canvas-origin-badge");
    expect(badges.map((badge) => badge.textContent)).toEqual(["Created", "Edited", "Restored"]);
  });
});

// =============================================================================
// Compare (real diffLines output)
// =============================================================================

describe("CanvasPage compare tab", () => {
  it("renders real diffLines rows with expected removed and added lines", async () => {
    mockHappyPath();
    getCanvasVersionMock.mockImplementation(async (_uid: string, versionNo: number) => ({
      artifact: ARTIFACT_V2,
      version: makeVersion(versionNo, versionNo === 1 ? V1_CONTENT : V2_CONTENT, "user_edit"),
    }));

    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    // Radix tabs activate on pointer-down (not click) — userEvent fires the
    // full pointer sequence.
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Compare tab" }));

    const rows = await screen.findAllByTestId("canvas-diff-row");
    const removed = rows.filter((row) => row.dataset.diffKind === "removed");
    const added = rows.filter((row) => row.dataset.diffKind === "added");
    // Rows carry the gutter marker + sr-only legend prefix; assert the real
    // diffLines line text is present in the removed/added rows respectively.
    expect(removed.map((row) => row.textContent)).toEqual([
      expect.stringContaining("print('v1')"),
    ]);
    expect(added.map((row) => row.textContent)).toEqual([
      expect.stringContaining("print('v2')"),
    ]);
    expect(removed[0].textContent).not.toContain("print('v2')");
    expect(added[0].textContent).not.toContain("print('v1')");
  });
});

// =============================================================================
// Downloads (exact blob bytes) + export manifest
// =============================================================================

describe("CanvasPage downloads", () => {
  it("downloads the selected version with blob bytes equal to the version content", async () => {
    mockHappyPath();
    const versionBlob = new Blob([V2_CONTENT]);
    downloadCanvasVersionMock.mockResolvedValue({ blob: versionBlob, filename: "demo.py" });

    const createObjectURL = vi.fn(() => "blob:mock-url");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, writable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, writable: true });
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    fireEvent.click(screen.getByTestId("canvas-download-button"));

    await waitFor(() => expect(downloadCanvasVersionMock).toHaveBeenCalledWith("cav_test1", 2));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));

    const downloadedBlob = createObjectURL.mock.calls[0][0] as Blob;
    expect(await downloadedBlob.text()).toBe(V2_CONTENT);

    const anchor = anchorClick.mock.instances[0] as HTMLAnchorElement;
    expect(anchor.download).toBe("demo.py");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");

    anchorClick.mockRestore();
  });

  it("exports a manifest JSON containing all required fields", async () => {
    mockHappyPath();
    exportCanvasManifestMock.mockResolvedValue({
      artifact_uid: "cav_test1",
      kind: "code",
      name: "demo.py",
      language: "python",
      version_no: 2,
      version_name: null,
      content: V2_CONTENT,
      content_sha256: "sha-2",
      source_refs: [{ source_id: "s1", title: "a.pdf" }],
      session_id: 7,
      turn_id: "turn-1",
      message_id: 42,
      exported_at: "2026-09-06T00:00:00Z",
    });

    const capturedBlobs: Blob[] = [];
    const createObjectURL = vi.fn((blob: Blob) => {
      capturedBlobs.push(blob);
      return "blob:manifest-url";
    });
    Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, writable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), writable: true });
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    fireEvent.click(screen.getByTestId("canvas-export-manifest-button"));

    await waitFor(() => expect(exportCanvasManifestMock).toHaveBeenCalledWith("cav_test1", 2));
    await waitFor(() => expect(capturedBlobs).toHaveLength(1));

    const manifest = JSON.parse(await capturedBlobs[0].text());
    expect(manifest).toMatchObject({
      artifact_uid: "cav_test1",
      kind: "code",
      name: "demo.py",
      language: "python",
      version_no: 2,
      version_name: null,
      content: V2_CONTENT,
      content_sha256: "sha-2",
      source_refs: [{ source_id: "s1", title: "a.pdf" }],
      session_id: 7,
      turn_id: "turn-1",
      message_id: 42,
      exported_at: "2026-09-06T00:00:00Z",
    });
    const anchor = anchorClick.mock.instances[0] as HTMLAnchorElement;
    expect(anchor.download).toBe("demo-py-v2-manifest.json");

    anchorClick.mockRestore();
  });
});

// =============================================================================
// Preview (bounded support)
// =============================================================================

describe("CanvasPage preview tab", () => {
  it("falls back to plain text and keeps the content unchanged when the renderer fails", async () => {
    mockHappyPath();
    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Preview tab" }));

    // Shiki's loader is mocked to reject — the fallback branch must render the
    // version content verbatim (source of truth is the content string).
    const preview = await waitFor(() => {
      const plain = screen.queryByTestId("canvas-preview-plain");
      const highlighted = screen.queryByTestId("canvas-preview-highlighted");
      const target = plain ?? highlighted;
      expect(target).not.toBeNull();
      return target as HTMLElement;
    });
    expect(preview.textContent).toContain("def hello():");
    expect(preview.textContent).toContain("print('v2')");
  });

  it("shows the explicit unsupported-format label for unknown kinds", async () => {
    mockHappyPath({
      artifact: { ...ARTIFACT_V2, kind: "spreadsheet" as unknown as CanvasArtifact["kind"] },
    });
    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Preview tab" }));

    expect(
      await screen.findByText("Preview not supported for this format")
    ).toBeInTheDocument();
  });
});

// =============================================================================
// Targeted model edit from textarea selection
// =============================================================================

describe("CanvasPage edit-range cancel (issue #509)", () => {
  it("stop aborts an in-flight model edit, keeps the dialog open, adds no version", async () => {
    mockHappyPath();
    let capturedSignal: AbortSignal | undefined;
    let rejectPending!: (err: unknown) => void;
    editCanvasRangeMock.mockImplementationOnce(
      (_uid: string, _payload: unknown, signal?: AbortSignal) => {
        capturedSignal = signal;
        return new Promise((_resolve, reject) => {
          rejectPending = reject;
        });
      }
    );

    renderCanvasPage();
    const editor = (await screen.findByLabelText("Canvas content editor")) as HTMLTextAreaElement;
    editor.setSelectionRange(13, 20);
    fireEvent.select(editor);
    fireEvent.click(screen.getByTestId("canvas-edit-selection-button"));
    fireEvent.change(await screen.findByLabelText("Model edit instruction"), {
      target: { value: "Change the print" },
    });
    const versionCallsBefore = listCanvasVersionsMock.mock.calls.length;
    fireEvent.click(screen.getByTestId("canvas-edit-range-apply"));

    expect(await screen.findByText("Applying…")).toBeInTheDocument();
    expect(capturedSignal).toBeInstanceOf(AbortSignal);

    fireEvent.click(screen.getByTestId("canvas-edit-range-stop"));
    rejectPending(Object.assign(new Error("canceled"), { code: "ERR_CANCELED", name: "CanceledError" }));

    await waitFor(() => expect(screen.queryByText("Applying…")).not.toBeInTheDocument());
    // Cancellation is not a failure: the dialog stays open with the
    // instruction intact, no conflict banner, no error alert, and the
    // version history is not refetched (nothing was appended).
    expect(screen.getByTestId("canvas-edit-range-apply")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByTestId("canvas-conflict-banner")).not.toBeInTheDocument();
    expect(listCanvasVersionsMock).toHaveBeenCalledTimes(versionCallsBefore);
  });
});

describe("CanvasPage edit selection with model", () => {
  it("sends the selection's 1-based line range and instruction to edit-range", async () => {
    const editedContent = "def hello():\n    print('edited')\n";
    const artifactV3 = { ...ARTIFACT_V2, current_version_no: 3 };
    editCanvasRangeMock.mockResolvedValue({
      ...makeVersion(3, editedContent, "model_edit"),
      model_edit: { start_line: 2, end_line: 2, instruction: "Change the print", base_version_no: 2 },
    });
    getCanvasArtifactMock
      .mockResolvedValueOnce({ artifact: ARTIFACT_V2, version: makeVersion(2, V2_CONTENT, "user_edit") })
      .mockResolvedValueOnce({
        artifact: artifactV3,
        version: { ...makeVersion(3, editedContent, "model_edit"), model_edit: { start_line: 2, end_line: 2, instruction: "Change the print", base_version_no: 2 } },
      });
    listCanvasVersionsMock
      .mockResolvedValueOnce({ versions: VERSION_SUMMARIES })
      .mockResolvedValueOnce({
        versions: [...VERSION_SUMMARIES, makeSummary(3, "model_edit")],
      });

    renderCanvasPage();
    const editor = (await screen.findByLabelText("Canvas content editor")) as HTMLTextAreaElement;

    // Select part of line 2 ("    print('v2')") without dirtying the editor.
    editor.setSelectionRange(13, 20);
    fireEvent.select(editor);

    const editButton = screen.getByTestId("canvas-edit-selection-button");
    expect(editButton).toBeEnabled();
    fireEvent.click(editButton);

    fireEvent.change(await screen.findByLabelText("Model edit instruction"), {
      target: { value: "Change the print" },
    });
    fireEvent.click(screen.getByTestId("canvas-edit-range-apply"));

    await waitFor(() =>
      expect(editCanvasRangeMock).toHaveBeenCalledWith(
        "cav_test1",
        {
          start_line: 2,
          end_line: 2,
          instruction: "Change the print",
          base_version_no: 2,
        },
        expect.any(AbortSignal)
      )
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Canvas content editor")).toHaveValue(editedContent)
    );
  });

  it("stays disabled without a selection", async () => {
    mockHappyPath();
    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    expect(screen.getByTestId("canvas-edit-selection-button")).toBeDisabled();
  });
});

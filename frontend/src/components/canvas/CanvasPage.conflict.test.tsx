import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
  getCanvasArtifactMock,
  listCanvasVersionsMock,
  getCanvasVersionMock,
  saveCanvasVersionMock,
  restoreCanvasVersionMock,
} = vi.hoisted(() => ({
  getCanvasArtifactMock: vi.fn(),
  listCanvasVersionsMock: vi.fn(),
  getCanvasVersionMock: vi.fn(),
  saveCanvasVersionMock: vi.fn(),
  restoreCanvasVersionMock: vi.fn(),
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
  };
});

const { useCanvasCapabilitiesMock } = vi.hoisted(() => ({
  useCanvasCapabilitiesMock: vi.fn(),
}));

vi.mock("@/hooks/useCanvasCapabilities", () => ({
  useCanvasCapabilities: useCanvasCapabilitiesMock,
}));

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
  language: "py",
  current_version_no: 2,
  source_refs: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

function makeVersion(
  no: number,
  content: string,
  origin: CanvasVersion["origin"],
  name: string | null = null
): CanvasVersion {
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

function makeSummary(no: number, origin: CanvasVersionSummary["origin"]): CanvasVersionSummary {
  const { content: _content, ...summary } = makeVersion(no, `content-${no}`, origin);
  return summary;
}

const VERSION_SUMMARIES = [makeSummary(1, "created"), makeSummary(2, "user_edit")];

/** Mirrors the normalized 409 the core interceptor produces. */
function conflictError(): Error {
  const error = new Error("canvas_version_conflict");
  (error as unknown as { status?: number }).status = 409;
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status: 409, data: { detail: "canvas_version_conflict" } },
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
  getCanvasArtifactMock.mockReset().mockResolvedValue({
    artifact: ARTIFACT_V2,
    version: makeVersion(2, V2_CONTENT, "user_edit"),
  });
  listCanvasVersionsMock.mockReset().mockResolvedValue({ versions: VERSION_SUMMARIES });
  getCanvasVersionMock.mockReset();
  saveCanvasVersionMock.mockReset();
  restoreCanvasVersionMock.mockReset();
});

afterEach(() => {
  cleanup();
});

// =============================================================================
// 409 conflict banner + force save
// =============================================================================

describe("CanvasPage version conflict", () => {
  it("shows the conflict banner on a 409 save and force-saves via Save anyway", async () => {
    const draft = "def hello():\n    print('conflict')\n";
    const artifactV3 = { ...ARTIFACT_V2, current_version_no: 3 };
    getCanvasArtifactMock
      .mockResolvedValueOnce({ artifact: ARTIFACT_V2, version: makeVersion(2, V2_CONTENT, "user_edit") })
      .mockResolvedValueOnce({ artifact: artifactV3, version: makeVersion(3, draft, "user_edit") });
    listCanvasVersionsMock
      .mockResolvedValueOnce({ versions: VERSION_SUMMARIES })
      .mockResolvedValueOnce({ versions: [...VERSION_SUMMARIES, makeSummary(3, "user_edit")] });
    saveCanvasVersionMock
      .mockRejectedValueOnce(conflictError())
      .mockResolvedValueOnce(makeVersion(3, draft, "user_edit"));

    renderCanvasPage();
    fireEvent.change(await screen.findByLabelText("Canvas content editor"), {
      target: { value: draft },
    });
    fireEvent.click(screen.getByTestId("canvas-save-button"));

    const banner = await screen.findByTestId("canvas-conflict-banner");
    expect(banner).toHaveTextContent("Version conflict");
    expect(screen.getByTestId("canvas-conflict-save-anyway")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("canvas-conflict-save-anyway"));

    await waitFor(() => expect(saveCanvasVersionMock).toHaveBeenCalledTimes(2));
    expect(saveCanvasVersionMock).toHaveBeenLastCalledWith("cav_test1", {
      content: draft,
      base_version_no: 2,
      force: true,
    });
    await waitFor(() => expect(screen.getByText("Version 3")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.queryByTestId("canvas-conflict-banner")).not.toBeInTheDocument()
    );
  });

  it("restore conflicts show a reload-only banner (no force escape hatch)", async () => {
    getCanvasVersionMock.mockResolvedValue({
      artifact: ARTIFACT_V2,
      version: makeVersion(1, V1_CONTENT, "created"),
    });
    restoreCanvasVersionMock.mockRejectedValueOnce(conflictError());

    renderCanvasPage();
    await screen.findByLabelText("Canvas content editor");
    fireEvent.click(screen.getAllByTestId("canvas-version-button")[0]);
    await waitFor(() =>
      expect(screen.getByTestId("canvas-readonly-notice")).toHaveTextContent("Viewing version 1")
    );
    fireEvent.click(screen.getByTestId("canvas-restore-button"));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    const banner = await screen.findByTestId("canvas-conflict-banner");
    expect(banner).toHaveTextContent("changed while you were working");
    expect(screen.queryByTestId("canvas-conflict-save-anyway")).not.toBeInTheDocument();
  });

  it("Reload latest discards the draft and shows the server content", async () => {
    const draft = "def hello():\n    print('draft')\n";
    const serverV3 = "def hello():\n    print('server-v3')\n";
    const artifactV3 = { ...ARTIFACT_V2, current_version_no: 3 };
    saveCanvasVersionMock.mockRejectedValueOnce(conflictError());
    // The refetch after Reload latest reveals the server moved ahead to v3.
    getCanvasArtifactMock
      .mockResolvedValueOnce({ artifact: ARTIFACT_V2, version: makeVersion(2, V2_CONTENT, "user_edit") })
      .mockResolvedValueOnce({ artifact: artifactV3, version: makeVersion(3, serverV3, "user_edit") });

    renderCanvasPage();
    fireEvent.change(await screen.findByLabelText("Canvas content editor"), {
      target: { value: draft },
    });
    fireEvent.click(screen.getByTestId("canvas-save-button"));
    await screen.findByTestId("canvas-conflict-banner");

    fireEvent.click(screen.getByRole("button", { name: "Reload latest version" }));

    await waitFor(() =>
      expect(screen.getByLabelText("Canvas content editor")).toHaveValue(serverV3)
    );
    await waitFor(() =>
      expect(screen.queryByTestId("canvas-conflict-banner")).not.toBeInTheDocument()
    );
    await waitFor(() =>
      expect(screen.queryByTestId("canvas-dirty-indicator")).not.toBeInTheDocument()
    );
  });
});

// =============================================================================
// localStorage draft persistence
// =============================================================================

describe("CanvasPage localStorage draft durability", () => {
  it("rehydrates an unsaved draft across unmount/remount with the restored notice", async () => {
    const draft = "def hello():\n    print('draft survives')\n";
    const first = renderCanvasPage();
    fireEvent.change(await screen.findByLabelText("Canvas content editor"), {
      target: { value: draft },
    });

    // The debounced write lands in localStorage (500ms debounce, real timers).
    await waitFor(
      () => expect(storage.get("canvas-draft:cav_test1")).toBe(draft),
      { timeout: 2000 }
    );
    first.unmount();

    renderCanvasPage();
    await waitFor(() =>
      expect(screen.getByLabelText("Canvas content editor")).toHaveValue(draft)
    );
    expect(screen.getByTestId("canvas-draft-restored-notice")).toHaveTextContent(
      "Unsaved edits restored"
    );
  });

  it("clears the persisted draft after a successful save", async () => {
    const draft = "def hello():\n    print('saved now')\n";
    const artifactV3 = { ...ARTIFACT_V2, current_version_no: 3 };
    getCanvasArtifactMock
      .mockResolvedValueOnce({ artifact: ARTIFACT_V2, version: makeVersion(2, V2_CONTENT, "user_edit") })
      .mockResolvedValueOnce({ artifact: artifactV3, version: makeVersion(3, draft, "user_edit") });
    saveCanvasVersionMock.mockResolvedValueOnce(makeVersion(3, draft, "user_edit"));

    renderCanvasPage();
    fireEvent.change(await screen.findByLabelText("Canvas content editor"), {
      target: { value: draft },
    });
    await waitFor(() => expect(storage.get("canvas-draft:cav_test1")).toBe(draft), {
      timeout: 2000,
    });

    fireEvent.click(screen.getByTestId("canvas-save-button"));
    await waitFor(() => expect(saveCanvasVersionMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(storage.has("canvas-draft:cav_test1")).toBe(false));
  });

  it("does not crash or restore anything when localStorage is unavailable", async () => {
    // Best-effort persistence: every access is wrapped in try/catch.
    vi.mocked(localStorage.getItem).mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.mocked(localStorage.setItem).mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.mocked(localStorage.removeItem).mockImplementation(() => {
      throw new Error("quota exceeded");
    });

    renderCanvasPage();
    const editor = await screen.findByLabelText("Canvas content editor");
    expect(editor).toHaveValue(V2_CONTENT);
    fireEvent.change(editor, { target: { value: "still editable" } });
    expect(screen.getByLabelText("Canvas content editor")).toHaveValue("still editable");
    expect(screen.getByTestId("canvas-dirty-indicator")).toBeInTheDocument();
  });
});

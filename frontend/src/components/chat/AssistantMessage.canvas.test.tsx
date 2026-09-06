import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Keep CodeBlock renders cheap and deterministic — the fallback path is fine,
// these tests target the canvas button, not highlighting.
vi.mock("shiki", () => ({
  createHighlighter: vi.fn(async () => {
    throw new Error("shiki unavailable in canvas entry tests");
  }),
}));

const { createCanvasArtifactMock, useCanvasCapabilitiesMock } = vi.hoisted(() => ({
  createCanvasArtifactMock: vi.fn(),
  useCanvasCapabilitiesMock: vi.fn(),
}));

vi.mock("@/lib/api/canvas", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/canvas")>("@/lib/api/canvas");
  return {
    ...actual,
    createCanvasArtifact: createCanvasArtifactMock,
  };
});

vi.mock("@/hooks/useCanvasCapabilities", () => ({
  useCanvasCapabilities: useCanvasCapabilitiesMock,
}));

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

import { useChatShellStore } from "@/stores/useChatShellStore";
import { AssistantMessage } from "./AssistantMessage";
import type { Message } from "@/stores/useChatStore";
import type { Source } from "@/lib/api";

const SOURCES: Source[] = [{ id: "s1", filename: "a.pdf", source_label: "S1" }];

const MESSAGE: Message = {
  id: "42",
  role: "assistant",
  content: "Here is code [S1]:\n\n```python\nx = 1  # [S1] marker\n```\n",
  sources: SOURCES,
};

const CREATED_ARTIFACT = {
  artifact: {
    artifact_uid: "cav_new1",
    session_id: 7,
    message_id: 42,
    turn_id: "turn-1",
    kind: "code",
    name: "demo",
    language: "py",
    current_version_no: 1,
    source_refs: [],
    created_at: "2026-09-06T00:00:00Z",
    updated_at: "2026-09-06T00:00:00Z",
  },
  version: {
    version_no: 1,
    name: null,
    origin: "created",
    model_edit: null,
    content_sha256: "sha-1",
    created_at: "2026-09-06T00:00:00Z",
    content: MESSAGE.content,
  },
};

function renderAssistantMessage(options: { sessionId?: string } = {}) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/chat/7"]}>
        <Routes>
          <Route
            path="/chat/:sessionId"
            element={<AssistantMessage message={MESSAGE} sessionId={options.sessionId} />}
          />
          <Route
            path="/chat/:sessionId/canvas/:artifactUid"
            element={<div data-testid="canvas-route" />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  createCanvasArtifactMock.mockReset().mockResolvedValue(CREATED_ARTIFACT);
  (useChatShellStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    openRightPane: vi.fn(),
    setSelectedEvidenceSource: vi.fn(),
    setSelectedEvidenceMessageId: vi.fn(),
    setEvidenceReturnFocusId: vi.fn(),
    setActiveRightTab: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  useCanvasCapabilitiesMock.mockReset();
});

describe("AssistantMessage canvas entry points — capability gating (fail-closed)", () => {
  it("hides both entry points when the capability query reports disabled", () => {
    useCanvasCapabilitiesMock.mockReturnValue({
      data: { enabled: false },
      isLoading: false,
      isError: false,
    });
    renderAssistantMessage({ sessionId: "7" });

    expect(screen.queryByTestId("codeblock-open-in-canvas")).not.toBeInTheDocument();
    expect(screen.queryByTestId("open-as-document-button")).not.toBeInTheDocument();
  });

  it("hides both entry points while the capability query is loading or failed", () => {
    useCanvasCapabilitiesMock.mockReturnValueOnce({
      data: undefined,
      isLoading: true,
      isError: false,
    });
    renderAssistantMessage({ sessionId: "7" });
    expect(screen.queryByTestId("codeblock-open-in-canvas")).not.toBeInTheDocument();
    expect(screen.queryByTestId("open-as-document-button")).not.toBeInTheDocument();

    cleanup();

    // Backend signals disabled with 503 canvas_disabled -> error state.
    useCanvasCapabilitiesMock.mockReturnValueOnce({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    renderAssistantMessage({ sessionId: "7" });
    expect(screen.queryByTestId("codeblock-open-in-canvas")).not.toBeInTheDocument();
    expect(screen.queryByTestId("open-as-document-button")).not.toBeInTheDocument();
  });

  it("hides both entry points when no session id is available", () => {
    useCanvasCapabilitiesMock.mockReturnValue({
      data: { enabled: true },
      isLoading: false,
      isError: false,
    });
    renderAssistantMessage({ sessionId: undefined });

    expect(screen.queryByTestId("codeblock-open-in-canvas")).not.toBeInTheDocument();
    expect(screen.queryByTestId("open-as-document-button")).not.toBeInTheDocument();
  });
});

describe("AssistantMessage canvas entry points — enabled behavior", () => {
  beforeEach(() => {
    useCanvasCapabilitiesMock.mockReturnValue({
      data: { enabled: true },
      isLoading: false,
      isError: false,
    });
  });

  it("shows the code-block button and creates a code artifact with [S1] markers preserved verbatim", async () => {
    renderAssistantMessage({ sessionId: "7" });

    const openButton = await screen.findByTestId("codeblock-open-in-canvas");
    expect(screen.getByTestId("open-as-document-button")).toBeInTheDocument();
    fireEvent.click(openButton);

    await waitFor(() => expect(createCanvasArtifactMock).toHaveBeenCalledTimes(1));
    const [sessionId, payload] = createCanvasArtifactMock.mock.calls[0];
    expect(sessionId).toBe(7);
    // Fence language maps to the backend extension key (python -> py).
    expect(payload).toEqual({
      kind: "code",
      name: "python: x = 1  # [S1] marker",
      language: "py",
      content: "x = 1  # [S1] marker\n",
      message_id: 42,
      source_refs: [{ source_id: "s1", title: "a.pdf" }],
    });
    // [S1] markers survive verbatim in the artifact payload.
    // Byte-exact: the trailing newline of the fenced block is preserved.
    expect(payload.content).toBe("x = 1  # [S1] marker\n");

    // Navigates to the canvas route for the created artifact.
    await waitFor(() => expect(screen.getByTestId("canvas-route")).toBeInTheDocument());
  });

  it("Open as document creates a document artifact with citations stripped and language md", async () => {
    renderAssistantMessage({ sessionId: "7" });

    fireEvent.click(await screen.findByTestId("open-as-document-button"));

    await waitFor(() => expect(createCanvasArtifactMock).toHaveBeenCalledTimes(1));
    const [sessionId, payload] = createCanvasArtifactMock.mock.calls[0];
    expect(sessionId).toBe(7);
    expect(payload.kind).toBe("document");
    expect(payload.language).toBe("md");
    expect(payload.message_id).toBe(42);
    expect(payload.content).not.toContain("[S1]");
    expect(payload.content).toContain("Here is code");
    expect(payload.source_refs).toEqual([{ source_id: "s1", title: "a.pdf" }]);

    await waitFor(() => expect(screen.getByTestId("canvas-route")).toBeInTheDocument());
  });

  it("shows an error toast and stays on the chat route when creation fails", async () => {
    const { toast } = await import("sonner");
    const toastError = vi.spyOn(toast, "error").mockImplementation(() => "id");
    createCanvasArtifactMock.mockRejectedValueOnce(new Error("boom"));

    renderAssistantMessage({ sessionId: "7" });
    fireEvent.click(await screen.findByTestId("open-as-document-button"));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Couldn't open canvas"));
    expect(screen.queryByTestId("canvas-route")).not.toBeInTheDocument();
    toastError.mockRestore();
  });
});

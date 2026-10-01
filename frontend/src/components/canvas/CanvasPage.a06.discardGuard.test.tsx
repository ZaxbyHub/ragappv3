import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

// Same deterministic mocks as CanvasPage.a06.draftFlush.test.tsx (verbatim
// from CanvasPage.test.tsx) — this file guards the DISCARD side of the
// issue #688 draft lifecycle: no pending write may resurrect a cleared draft.

vi.mock("@/lib/highlighter", () => ({
  loadHighlighter: vi.fn(async () => {
    throw new Error("shiki unavailable in canvas tests");
  }),
}));

const { getCanvasArtifactMock, listCanvasVersionsMock, saveCanvasVersionMock } = vi.hoisted(() => ({
  getCanvasArtifactMock: vi.fn(),
  listCanvasVersionsMock: vi.fn(),
  saveCanvasVersionMock: vi.fn(),
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
    saveCanvasVersion: saveCanvasVersionMock,
  };
});

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
  function SelectItem({ value, children }: { value?: string; children?: React.ReactNode }) {
    const onValueChange = React.useContext(SelectCtx);
    return React.createElement(
      "button",
      { type: "button", onClick: () => onValueChange(value ?? "") },
      children,
    );
  }
  return { Select, SelectTrigger, SelectValue, SelectContent, SelectItem };
});

import CanvasPage from "./CanvasPage";
import type { CanvasArtifact, CanvasVersion, CanvasVersionSummary } from "@/lib/api/canvas";

const SAVED_CONTENT = "def hello():\n    print('v1')\n";

const ARTIFACT_TEST: CanvasArtifact = {
  artifact_uid: "cav_discard",
  session_id: 7,
  message_id: 42,
  turn_id: "turn-1",
  kind: "code",
  name: "demo.py",
  language: "python",
  current_version_no: 1,
  source_refs: [{ source_id: "s1", title: "a.pdf" }],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

function makeVersion(no: number, content: string): CanvasVersion {
  return {
    version_no: no,
    name: null,
    origin: no === 1 ? "created" : "user_edit",
    model_edit: null,
    content_sha256: `sha-${no}`,
    created_at: `2026-01-0${no}T00:00:00Z`,
    content,
  };
}

function makeSummary(no: number): CanvasVersionSummary {
  const { content: _content, ...summary } = makeVersion(no, `content-${no}`);
  return summary;
}

const storage = new Map<string, string>();

function renderCanvasPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/chat/7/canvas/cav_discard"]}>
        <Routes>
          <Route path="/chat/:sessionId/canvas/:artifactUid" element={<CanvasPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
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
  listCanvasVersionsMock.mockReset().mockResolvedValue({ versions: [makeSummary(1)] });
  saveCanvasVersionMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

test("unmount after a save discarded the draft must not resurrect it", async () => {
  const savedV2 = makeVersion(2, "def hello():\n    print('v2 saved')\n");
  // The server state advances with the save (otherwise the post-save editor
  // text legitimately stays a pending draft against a stale current content).
  let serverVersion = makeVersion(1, SAVED_CONTENT);
  getCanvasArtifactMock.mockImplementation(async () => ({
    artifact: { ...ARTIFACT_TEST, current_version_no: serverVersion.version_no },
    version: serverVersion,
  }));
  saveCanvasVersionMock.mockImplementation(async () => {
    serverVersion = savedV2;
    return savedV2;
  });

  renderCanvasPage();
  const editor = await screen.findByLabelText("Canvas content editor");
  expect(editor).toHaveValue(SAVED_CONTENT);

  vi.useFakeTimers();
  fireEvent.change(editor, { target: { value: "edited draft" } });
  act(() => {
    vi.advanceTimersByTime(100);
  });

  // Save discards the draft synchronously (applyNewVersion: cancel + clear).
  const saveButton = screen.getByRole("button", { name: /save/i });
  await act(async () => {
    fireEvent.click(saveButton);
  });
  await act(async () => {});
  expect(localStorage.getItem("canvas-draft:cav_discard")).toBeNull();

  // Unmount inside the original debounce window: the discarded pending write
  // must NOT re-persist the draft (issue #688 constraint).
  act(() => {
    vi.advanceTimersByTime(100);
  });
  cleanup();
  expect(localStorage.getItem("canvas-draft:cav_discard")).toBeNull();
});

test("unmount after an equality revert must not resurrect the cleared draft", async () => {
  getCanvasArtifactMock.mockResolvedValue({ artifact: ARTIFACT_TEST, version: makeVersion(1, SAVED_CONTENT) });

  renderCanvasPage();
  const editor = await screen.findByLabelText("Canvas content editor");
  expect(editor).toHaveValue(SAVED_CONTENT);

  vi.useFakeTimers();
  fireEvent.change(editor, { target: { value: "edited draft" } });
  act(() => {
    vi.advanceTimersByTime(100);
  });
  // Re-type the original text: the equality branch clears the draft.
  fireEvent.change(editor, { target: { value: SAVED_CONTENT } });
  await act(async () => {});
  expect(localStorage.getItem("canvas-draft:cav_discard")).toBeNull();

  act(() => {
    vi.advanceTimersByTime(100);
  });
  cleanup();
  expect(localStorage.getItem("canvas-draft:cav_discard")).toBeNull();
});

test("applied save content is not re-persisted as a draft before the refetch lands", async () => {
  // External F-003: after applyNewVersion the editor shows the saved content
  // while currentContent is still the stale cached version. Unmounting
  // inside that window (refetch never resolves) must NOT flush the applied
  // content as a "draft" that could later resurface over newer server data.
  const savedV2 = makeVersion(2, "def hello():\n    print('v2 saved')\n");
  let serverVersion = makeVersion(1, SAVED_CONTENT);
  getCanvasArtifactMock.mockImplementation(async () => ({
    artifact: { ...ARTIFACT_TEST, current_version_no: serverVersion.version_no },
    version: serverVersion,
  }));
  saveCanvasVersionMock.mockImplementation(async () => {
    serverVersion = savedV2;
    return savedV2;
  });

  renderCanvasPage();
  const editor = await screen.findByLabelText("Canvas content editor");
  expect(editor).toHaveValue(SAVED_CONTENT);

  vi.useFakeTimers();
  fireEvent.change(editor, { target: { value: "edited draft" } });
  act(() => {
    vi.advanceTimersByTime(100);
  });

  // Save applies v2, but the artifact refetch is suspended: currentContent
  // stays at v1 for the whole window.
  const saveButton = screen.getByRole("button", { name: /save/i });
  await act(async () => {
    fireEvent.click(saveButton);
  });
  await act(async () => {});

  act(() => {
    vi.advanceTimersByTime(400);
  });
  cleanup();

  // Neither the pre-save draft nor the applied v2 content may be persisted.
  expect(localStorage.getItem("canvas-draft:cav_discard")).toBeNull();
});

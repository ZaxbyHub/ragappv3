import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

// The shared highlighter's loader is mocked to reject so the preview fallback
// path is deterministic (verbatim from CanvasPage.test.tsx).
vi.mock("@/lib/highlighter", () => ({
  loadHighlighter: vi.fn(async () => {
    throw new Error("shiki unavailable in canvas tests");
  }),
}));

const { getCanvasArtifactMock, listCanvasVersionsMock } = vi.hoisted(() => ({
  getCanvasArtifactMock: vi.fn(),
  listCanvasVersionsMock: vi.fn(),
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
  };
});

// Radix Select cannot open in jsdom (no pointer capture) — module-mock it so
// SelectItem clicks call onValueChange (verbatim from CanvasPage.test.tsx;
// CanvasCompare is the only ui/select consumer in this tree).
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
// Fixtures (CanvasPage.test.tsx shapes)
// =============================================================================

const SAVED_CONTENT = "def hello():\n    print('v2')\n";
const A_CONTENT = "def a():\n    print('a')\n";
const B_CONTENT = "def b():\n    print('b')\n";

const ARTIFACT_TEST: CanvasArtifact = {
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

const ARTIFACT_A: CanvasArtifact = { ...ARTIFACT_TEST, artifact_uid: "cav_a", name: "alpha.py" };
const ARTIFACT_B: CanvasArtifact = { ...ARTIFACT_TEST, artifact_uid: "cav_b", name: "beta.py" };

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

// Exposes the router's navigate() so a test can switch the :artifactUid param
// while keeping the SAME CanvasPage instance mounted (the artifact-switch
// path, not a remount).
const navigateRef: { current: ((to: string) => void) | null } = { current: null };
function NavProbe() {
  const navigate = useNavigate();
  useEffect(() => {
    navigateRef.current = navigate;
  }, [navigate]);
  return null;
}

const storage = new Map<string, string>();

function renderCanvasPage(initialEntry = "/chat/7/canvas/cav_test1") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <NavProbe />
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
  listCanvasVersionsMock.mockReset().mockResolvedValue({ versions: VERSION_SUMMARIES });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

// =============================================================================
// Issue #688 (defect 6): the debounce's pending draft must be flushed when the
// component unmounts or switches artifacts inside the 500ms window. The
// correct sibling pattern is Composer.tsx's flushPendingDraft (unmount flush
// at L303). At base both checks are RED: the pending write is simply dropped.
// =============================================================================

test("unmount inside the debounce window flushes the draft", async () => {
  getCanvasArtifactMock.mockResolvedValue({
    artifact: ARTIFACT_TEST,
    version: makeVersion(2, SAVED_CONTENT, "user_edit"),
  });
  const { unmount } = renderCanvasPage();

  const editor = await screen.findByLabelText("Canvas content editor");
  expect(editor).toHaveValue(SAVED_CONTENT);

  vi.useFakeTimers();
  fireEvent.change(editor, { target: { value: "edited draft" } });
  // 100ms is inside the 500ms DRAFT_PERSIST_DEBOUNCE_MS window — the write is
  // still pending when the component goes away.
  act(() => {
    vi.advanceTimersByTime(100);
  });
  unmount();

  expect(localStorage.getItem("canvas-draft:cav_test1")).toBe("edited draft");
});

test("artifact switch inside the debounce window flushes the previous draft", async () => {
  getCanvasArtifactMock.mockImplementation(async (uid: string) =>
    uid === "cav_b"
      ? { artifact: ARTIFACT_B, version: makeVersion(2, B_CONTENT, "user_edit") }
      : { artifact: ARTIFACT_A, version: makeVersion(2, A_CONTENT, "user_edit") }
  );
  renderCanvasPage("/chat/7/canvas/cav_a");

  const editor = await screen.findByLabelText("Canvas content editor");
  expect(editor).toHaveValue(A_CONTENT);

  vi.useFakeTimers();
  fireEvent.change(editor, { target: { value: "edited draft" } });
  act(() => {
    vi.advanceTimersByTime(100);
  });

  // Switch artifacts without unmounting CanvasPage — the pending draft for A
  // must be flushed under A's key before the editor resets to B.
  act(() => {
    navigateRef.current?.("/chat/7/canvas/cav_b");
  });
  await act(async () => {});

  expect(localStorage.getItem("canvas-draft:cav_a")).toBe("edited draft");
});

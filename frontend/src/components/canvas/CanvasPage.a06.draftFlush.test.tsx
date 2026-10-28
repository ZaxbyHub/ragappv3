import { useEffect } from "react";
import {
  canvasDraftKey,
  invalidatePendingCanvasDraftWrites,
} from "@/lib/canvasDrafts";
import { useAuthStore } from "@/stores/useAuthStore";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  expect(localStorage.getItem(canvasDraftKey("cav_test1", null))).toBe("edited draft");
});

test("a flush scheduled before an identity-switch generation bump writes nothing (issue #1032)", async () => {
  getCanvasArtifactMock.mockResolvedValue({
    artifact: ARTIFACT_TEST,
    version: makeVersion(2, SAVED_CONTENT, "user_edit"),
  });
  const { unmount } = renderCanvasPage();

  const editor = await screen.findByLabelText("Canvas content editor");
  expect(editor).toHaveValue(SAVED_CONTENT);

  vi.useFakeTimers();
  fireEvent.change(editor, { target: { value: "user A's unsent canvas text" } });
  act(() => {
    vi.advanceTimersByTime(100); // inside the debounce window: scheduled at gen G
  });
  // The identity boundary kills delayed writers: the generation bumps to
  // G+1. The pending write captured G at SCHEDULE time — the eventual flush
  // (timer, unmount, or artifact switch) must drop it, not re-capture the
  // current generation at flush time.
  invalidatePendingCanvasDraftWrites();
  act(() => {
    vi.advanceTimersByTime(500 + 50); // DRAFT_PERSIST_DEBOUNCE_MS window
  });
  unmount(); // unmount flush must also drop the stale pending

  expect(localStorage.getItem(canvasDraftKey("cav_test1", null))).toBeNull();
});

test("artifact switch after a generation bump drops the stale pending draft", async () => {
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
  invalidatePendingCanvasDraftWrites();
  act(() => {
    navigateRef.current?.("/chat/7/canvas/cav_b");
  });
  await act(async () => {});

  expect(localStorage.getItem(canvasDraftKey("cav_a", null))).toBeNull();
});

// Behavioral pin (multi-stage review HIGH — disclosed non-discriminating):
// after the principal switch the editor shows the SAVED content, never the
// previous principal's unsent text. The invariant holds through the
// artifact-reset effect's editorBaseVersionNo reset (flushPendingDraft dep
// chain); CanvasPage's explicit principalChanged bypass removes the reliance
// on that implicit coupling, so this leg stays green with OR without it.
test("same-artifact principal switch resets stale editor text (multi-stage review HIGH)", async () => {
  getCanvasArtifactMock.mockResolvedValue({
    artifact: ARTIFACT_TEST,
    version: makeVersion(2, SAVED_CONTENT, "user_edit"),
  });
  useAuthStore.setState({ user: { id: 11, username: "usera", full_name: "A", role: "admin", is_active: true } });
  renderCanvasPage();

  const editor = await screen.findByLabelText("Canvas content editor");
  expect(editor).toHaveValue(SAVED_CONTENT);

  // User A types an unsent edit (in-memory only — nothing flushed).
  fireEvent.change(editor, { target: { value: "user A's private unsent text" } });
  expect(editor).toHaveValue("user A's private unsent text");

  // A different principal logs in on the SAME artifact. The rehydrate
  // effect must take its full path despite the version guard — otherwise
  // A's unsent text survives in component state for B.
  useAuthStore.setState({ user: { id: 22, username: "userb", full_name: "B", role: "admin", is_active: true } });

  await waitFor(() => {
    expect(screen.getByLabelText("Canvas content editor")).toHaveValue(SAVED_CONTENT);
  });

  // Reset the principal so the leak does not poison later suites in this
  // file (draftFlush has no auth-store afterEach).
  useAuthStore.setState({ user: null });
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

  expect(localStorage.getItem(canvasDraftKey("cav_a", null))).toBe("edited draft");
});

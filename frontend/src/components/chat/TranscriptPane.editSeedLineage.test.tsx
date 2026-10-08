// frontend/src/components/chat/TranscriptPane.editSeedLineage.test.tsx
// Issue #685 — the edit-seed half of the display-only stepper: when an older
// version is DISPLAYED (pointer set), clicking Edit seeds the composer with
// the DISPLAYED text (pre-#685 lineage semantics), while the store's content
// keeps the live text that a follow-up send serializes as history.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { TranscriptPane } from "./TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const sendDirectMock = vi.hoisted(() => vi.fn());
const handleSendMock = vi.hoisted(() => vi.fn());
const refreshHistoryMock = vi.hoisted(() => vi.fn(async () => {}));
const truncateMock = vi.hoisted(() => vi.fn(async () => ({})));

vi.mock("@/lib/api", () => ({
  forkChatSession: vi.fn(async () => ({ id: 99, messages: [] })),
  truncateChatSession: truncateMock,
  getChatSession: vi.fn(),
}));
vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: () => ({ refreshHistory: refreshHistoryMock }),
}));
vi.mock("@/hooks/useSendMessage", () => ({
  useSendMessage: () => ({
    handleSend: handleSendMock,
    handleStop: vi.fn(),
    sendDirect: sendDirectMock,
    currentStage: null,
  }),
  MAX_INPUT_LENGTH: 100000,
}));
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn((selector?: (s: unknown) => unknown) =>
    typeof selector === "function"
      ? selector({ getActiveVault: () => null, activeVaultId: null, vaults: [] })
      : { getActiveVault: () => null, activeVaultId: null, vaults: [] }
  ),
}));
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector?: (s: unknown) => unknown) =>
    typeof selector === "function"
      ? selector({ user: { username: "tester", full_name: "Test User" } })
      : { user: { username: "tester", full_name: "Test User" } }
  ),
}));
vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn((selector?: (s: unknown) => unknown) =>
    typeof selector === "function"
      ? selector({ activeSessionId: "7", activeSessionTitle: "Edit seed lineage" })
      : { activeSessionId: "7", activeSessionTitle: "Edit seed lineage" }
  ),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useInRouterContext: () => false,
}));

let nextId = 200;
function seedStore() {
  useChatStore.setState({
    activeChatId: "7",
    messageIds: ["u1", "a1"],
    messagesById: {
      u1: { id: "u1", role: "user", content: "original question" },
      a1: { id: "a1", role: "assistant", content: "original answer", status: "complete" },
    },
    isStreaming: false,
    streamingMessageId: null,
    messageEditVersions: {},
    activeEditVersion: {},
  });
}

describe("TranscriptPane edit seed from displayed version (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    seedStore();
    handleSendMock.mockImplementation(async () => {
      const { input, isStreaming } = useChatStore.getState();
      if (!input.trim() || isStreaming) return;
      const id = `u${nextId++}`;
      useChatStore.getState().addMessage({ id, role: "user", content: input.trim() });
    });
  });

  afterEach(() => {
    useChatStore.getState().newChat();
  });

  it("editing from a displayed old version seeds the displayed text and keeps live content", async () => {
    render(<TranscriptPane />);

    // Edit → re-send so two sibling versions exist.
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    await waitFor(() => expect(truncateMock).toHaveBeenCalledTimes(1));
    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "edited question" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("2 / 2")).toBeInTheDocument());

    // Step back to the older version.
    fireEvent.click(screen.getByRole("button", { name: "Show previous version" }));
    await waitFor(() => expect(screen.getByText("1 / 2")).toBeInTheDocument());

    // Display shows the old snapshot; the store's live content is untouched.
    // (The stepper's sr-only announcement repeats the displayed text, so
    // count matches instead of requiring a single element.)
    expect(screen.getAllByText("original question").length).toBeGreaterThan(0);
    const state = useChatStore.getState();
    const liveUser = state.messageIds
      .map((id) => state.messagesById[id])
      .find((m) => m?.role === "user");
    expect(liveUser?.content).toBe("edited question");

    // Editing from the displayed old version seeds the composer with the
    // DISPLAYED text (the pre-#685 lineage behavior), not the live text.
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    await waitFor(() => expect(truncateMock).toHaveBeenCalledTimes(2));
    const editComposer = screen.getByLabelText("Message input") as HTMLTextAreaElement;
    expect(editComposer.value).toBe("original question");
  });
});

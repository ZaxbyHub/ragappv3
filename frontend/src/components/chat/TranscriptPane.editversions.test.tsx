// frontend/src/components/chat/TranscriptPane.editversions.test.tsx
// Issue #573 (AC3) — final-critic revision: integration test for the REAL
// edit → re-send → version-step cycle through the mounted TranscriptPane and
// the REAL useChatStore (no store mock). Pins the invariant the isolated
// VersionStepper spec cannot: stepping between sibling versions of an edited
// turn never appends the displayed content as a duplicate sibling — the
// position stays "1 / 2" while the content swaps.

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
      ? selector({ activeVaultId: null, vaults: [] })
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
      ? selector({ activeSessionId: "7", activeSessionTitle: "Edit versions" })
      : { activeSessionId: "7", activeSessionTitle: "Edit versions" }
  ),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

let nextId = 100;
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

describe("TranscriptPane edit-version cycle (issue #573 AC3, final-critic revision)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    seedStore();
    // The mocked send path appends the composer's current input as the new
    // user turn — the same store effect sendCore performs in production.
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

  it("edit → re-send → step cycle keeps exactly two sibling versions and swaps content", async () => {
    render(<TranscriptPane />);

    // 1. The original exchange renders.
    expect(screen.getByText("original question")).toBeInTheDocument();

    // 2. Edit the user turn (real MessageBubble edit action → handleEdit:
    //    snapshot + server truncate + local trim + composer restore).
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    await waitFor(() => expect(truncateMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText("original answer")).not.toBeInTheDocument());

    // 3. Edit in the composer and send (real Composer Enter path → the
    //    mocked send adds the resent user message to the real store).
    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "edited question" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    // (The message bubble and the stepper's sr-only active-version span both
    // carry the text — assert presence, not uniqueness.)
    await waitFor(() => expect(screen.getAllByText("edited question").length).toBeGreaterThan(0));

    // 4. The edited message renders a 2-version stepper at position 2 / 2.
    await waitFor(() => expect(screen.getByText("2 / 2")).toBeInTheDocument());

    // 5. Step to the previous version: content swaps to the original…
    fireEvent.click(screen.getByRole("button", { name: "Show previous version" }));
    await waitFor(() => expect(screen.getAllByText("original question").length).toBeGreaterThan(0));
    // …and the position is STILL "1 / 2" — the displayed content must not be
    // re-appended as a duplicate sibling (the final-critic bug).
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.queryByText("1 / 3")).not.toBeInTheDocument();

    // 6. Step back to the newest version: content swaps to the edited text.
    fireEvent.click(screen.getByRole("button", { name: "Show next version" }));
    await waitFor(() => expect(screen.getAllByText("edited question").length).toBeGreaterThan(0));
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    expect(screen.queryByText("3 / 3")).not.toBeInTheDocument();
  });

  it("re-editing from a displayed old version renders the new text live (no stale pointer, no duplicate snapshot)", async () => {
    render(<TranscriptPane />);
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    await waitFor(() => expect(truncateMock).toHaveBeenCalledTimes(1));

    let composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "edited question" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("2 / 2")).toBeInTheDocument());

    // Step back to the original, then edit FROM that displayed old version.
    fireEvent.click(screen.getByRole("button", { name: "Show previous version" }));
    await waitFor(() => expect(screen.getByText("1 / 2")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    await waitFor(() => expect(truncateMock).toHaveBeenCalledTimes(2));
    composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "edited from old" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    // The re-sent message renders ITS text as the live content (the stale
    // pointer is cleared) and the version list holds three distinct texts.
    await waitFor(() => expect(screen.getAllByText("edited from old").length).toBeGreaterThan(0));
    expect(screen.getByText("3 / 3")).toBeInTheDocument();
    expect(screen.queryByText("1 / 3")).not.toBeInTheDocument();
  });

  it("repeated stepping never grows the version list", async () => {
    render(<TranscriptPane />);
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    await waitFor(() => expect(truncateMock).toHaveBeenCalledTimes(1));

    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "edited question" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("2 / 2")).toBeInTheDocument());

    for (let round = 0; round < 3; round++) {
      fireEvent.click(screen.getByRole("button", { name: "Show previous version" }));
      await waitFor(() => expect(screen.getByText("1 / 2")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: "Show next version" }));
      await waitFor(() => expect(screen.getByText("2 / 2")).toBeInTheDocument());
    }
    expect(screen.queryByText(/3 \/ 3/)).not.toBeInTheDocument();
  });
});

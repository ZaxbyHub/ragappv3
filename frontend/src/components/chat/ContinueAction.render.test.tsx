// frontend/src/components/chat/ContinueAction.render.test.tsx
// Issue #573 AC2 — PR-review PRR-002: the RENDER GATE for ContinueAction in
// the real transcript (TranscriptPane.tsx: finishReason === "length" on the
// last, non-streaming assistant message) is exercised by no other suite —
// the frozen C2 checks the component in isolation plus a wiring source-scan,
// so a flipped gate would pass everything. This file pins the gate.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
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
      ? selector({ activeSessionId: "5", activeSessionTitle: "Continue gate" })
      : { activeSessionId: "5", activeSessionTitle: "Continue gate" }
  ),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

function seedTurn(finishReason: string | undefined, status: "complete" | "interrupted") {
  useChatStore.setState({
    activeChatId: "5",
    messageIds: ["u1", "a1"],
    messagesById: {
      u1: { id: "u1", role: "user", content: "tell me everything" },
      a1: {
        id: "a1",
        role: "assistant",
        content: "a truncated answer",
        status,
        ...(finishReason ? { finishReason } : {}),
      },
    },
    isStreaming: false,
    streamingMessageId: null,
    messageEditVersions: {},
    activeEditVersion: {},
  });
}

describe("ContinueAction render gate in the real transcript (PRR-002)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
    useChatStore.getState().newChat();
  });

  it("renders Continue for the last complete assistant message with finishReason 'length'", () => {
    seedTurn("length", "complete");
    render(<TranscriptPane />);
    expect(screen.getByRole("button", { name: /continue/i })).toBeInTheDocument();
  });

  it("does NOT render Continue for finishReason 'stop'", () => {
    seedTurn("stop", "complete");
    render(<TranscriptPane />);
    expect(screen.queryByRole("button", { name: /continue/i })).not.toBeInTheDocument();
  });

  it("does NOT render Continue when finishReason is absent (restored/persisted rows)", () => {
    seedTurn(undefined, "complete");
    render(<TranscriptPane />);
    expect(screen.queryByRole("button", { name: /continue/i })).not.toBeInTheDocument();
  });

  it("does NOT render Continue while the turn is streaming", () => {
    seedTurn("length", "complete");
    useChatStore.setState({ isStreaming: true, streamingMessageId: "a1" });
    render(<TranscriptPane />);
    expect(screen.queryByRole("button", { name: /continue/i })).not.toBeInTheDocument();
  });

  it("clicking Continue resends with the truncated turn as prior context", async () => {
    seedTurn("length", "complete");
    render(<TranscriptPane />);
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() => expect(sendDirectMock).toHaveBeenCalledTimes(1));
    const [content, history] = sendDirectMock.mock.calls[0];
    expect(String(content)).toMatch(/continue/i);
    expect(history[history.length - 1]).toMatchObject({
      role: "assistant",
      content: "a truncated answer",
    });
  });
});

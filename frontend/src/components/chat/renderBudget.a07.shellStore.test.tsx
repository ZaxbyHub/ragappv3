// frontend/src/components/chat/renderBudget.a07.shellStore.test.tsx
// A07 (AC12): the transcript's expensive subtrees must not re-render for
// shell/chat-store fields they do not consume. AssistantMessage takes the
// whole useChatShellStore() and Composer the whole useChatStore(), so an
// unrelated shell-store write (sessionRailWidth) or an unrelated chat-store
// write (expandedSources) re-renders the wrapped subtree today. Both must
// produce a zero Profiler commit-count delta.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { Profiler, type ProfilerOnRenderCallback } from "react";
import { render, act } from "@testing-library/react";
import { AssistantMessage } from "./AssistantMessage";
import { Composer } from "./Composer";
import { useChatStore, type Message } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import type { Vault } from "@/lib/api";

// Mock ResizeObserver for Radix UI primitives used by Composer.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const chatStreamMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    chatStream: (...args: unknown[]) => chatStreamMock(...args),
    getLlmModeHealth: vi.fn(async () => ({ thinking: true, instant: true })),
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

// Heavy children are stubbed (keeping real exports like parseCitationSegments
// and stripCitations alive) so the Profiler measures the components under
// test, not the markdown pipeline.
vi.mock("./MarkdownMessage", async () => {
  const actual = await vi.importActual<typeof import("./MarkdownMessage")>(
    "./MarkdownMessage"
  );
  return {
    ...actual,
    MarkdownMessage: () => <div data-testid="markdown-stub" />,
  };
});
vi.mock("./SourceCards", () => ({
  SourceCards: () => <div data-testid="source-cards-stub" />,
}));
vi.mock("./MemoryCards", () => ({ MemoryCards: () => null }));
vi.mock("./WikiCards", () => ({ WikiCards: () => null }));
vi.mock("./KMSCards", () => ({ KMSCards: () => null }));
vi.mock("./AssistantMessageActions", async () => {
  const actual = await vi.importActual<typeof import("./AssistantMessageActions")>(
    "./AssistantMessageActions"
  );
  return {
    ...actual,
    AssistantMessageActions: () => <div data-testid="actions-stub" />,
  };
});
// The Composer's shared upload monitor owns intervals; it is not under test.
vi.mock("@/hooks/useUploadMonitoring", () => ({
  useUploadMonitoring: () => undefined,
}));

const TEST_VAULT: Vault = { id: 1, name: "Test Vault", file_count: 5 } as Vault;

const message: Message = {
  // Non-numeric id: the canvas bridge (network) never mounts.
  id: "local-msg-1",
  role: "assistant",
  content: "An answer that cites a source [S1].",
  status: "complete",
  sources: [{ id: "chunk-1", filename: "handbook.pdf" }],
};

describe("transcript render budget (A07 AC12)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    chatStreamMock.mockReturnValue(vi.fn());
    useChatStore.setState({
      messageIds: [],
      messagesById: {},
      streamingMessageId: null,
      input: "",
      isStreaming: false,
      abortFn: null,
      inputError: null,
      expandedSources: new Set(),
      activeChatId: null,
      pendingTurnPersist: null,
    });
    useChatShellStore.setState({
      sessionRailOpen: true,
      rightPaneOpen: false,
      sessionRailWidth: 320,
      activeSessionId: null,
      activeSessionTitle: null,
      selectedEvidenceSource: null,
      selectedEvidenceMessageId: null,
      evidenceReturnFocusId: null,
      activeRightTab: "evidence",
    });
    useVaultStore.setState({
      vaults: [TEST_VAULT],
      activeVaultId: 1,
      loading: false,
      error: null,
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
  });

  it("unrelated shell-store updates do not re-render messages or composer", () => {
    let commits = 0;
    const onRender: ProfilerOnRenderCallback = () => {
      commits += 1;
    };

    render(
      <Profiler id="a07-render-budget" onRender={onRender}>
        <AssistantMessage message={message} />
        <Composer onSend={() => {}} onStop={() => {}} isStreaming={false} />
      </Profiler>
    );
    const baseline = commits;

    // An unrelated shell-store layout field: neither the assistant message
    // nor the composer reads it.
    act(() => {
      useChatShellStore.setState({ sessionRailWidth: 333 });
    });
    expect(commits - baseline).toBe(0);

    // An unrelated chat-store field (not input/inputError/activeChatId).
    act(() => {
      useChatStore.setState({ expandedSources: new Set(["chunk-unrelated"]) });
    });
    expect(commits - baseline).toBe(0);
  });
});

// frontend/src/components/chat/renderBudget.fb.positive.test.tsx
// PR #835 feedback PRR-012f: the a07 render-budget check only asserts the
// NEGATIVE direction (unrelated store writes cause zero commits) — a subtree
// that never re-rendered at all would also pass. This pins the POSITIVE
// direction: the Composer DOES re-render when a field it consumes (input)
// changes, and the baseline mount commits are nonzero.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { Profiler, type ProfilerOnRenderCallback } from "react";
import { render, act } from "@testing-library/react";
import { Composer } from "./Composer";
import { useChatStore } from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import type { Vault } from "@/lib/api";

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

vi.mock("@/hooks/useUploadMonitoring", () => ({
  useUploadMonitoring: () => undefined,
}));

const TEST_VAULT: Vault = { id: 1, name: "Test Vault", file_count: 5 } as Vault;

describe("composer render-budget positive direction (fb PRR-012f)", () => {
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
    useVaultStore.setState({
      vaults: [TEST_VAULT],
      activeVaultId: 1,
      loading: false,
      error: null,
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
  });

  it("the composer re-renders when its consumed `input` field changes", () => {
    let commits = 0;
    const onRender: ProfilerOnRenderCallback = () => {
      commits += 1;
    };

    render(
      <Profiler id="fb-render-positive" onRender={onRender}>
        <Composer onSend={() => {}} onStop={() => {}} isStreaming={false} />
      </Profiler>
    );
    // Baseline sanity: the mount committed at least once (a subtree that
    // never renders would make every zero-delta assertion vacuous).
    const baseline = commits;
    expect(baseline).toBeGreaterThan(0);

    act(() => {
      useChatStore.setState({ input: "typed text" });
    });
    expect(commits - baseline).toBeGreaterThanOrEqual(1);

    act(() => {
      useChatStore.setState({ inputError: "too long" });
    });
    expect(commits - baseline).toBeGreaterThanOrEqual(2);
  });
});

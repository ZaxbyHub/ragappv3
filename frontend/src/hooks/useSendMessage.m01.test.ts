// frontend/src/hooks/useSendMessage.m01.test.ts
// Issue-trace 781-vaultgate-first-run-baseline — acceptance check C4 (AC1).
//
// First-run scenario: exactly ONE vault is accessible but none is selected
// ("All Vaults" — activeVaultId null, kv_active_vault_id unset). Sending the
// FIRST message of a NEW chat (no activeChatId) must not dead-end on
// "Please select a vault before starting a chat." — with a single accessible
// vault there is nothing for the user to choose between; the gate contradicts
// the All Vaults affordance the composer itself shows.
//
// Mirrors the @/lib/api mock pattern of useSendMessage.test.ts (same hoisted
// mocks and store reset so a send that proceeds completes without throwing).

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useVaultStore } from "@/stores/useVaultStore";
import type { Vault } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessage: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  chatStream: vi.fn(),
  getLlmModeHealth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessage: (...args: unknown[]) => apiMocks.addChatMessage(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  getLlmModeHealth: (...args: unknown[]) => apiMocks.getLlmModeHealth(...args),
}));

// Same full Vault shape as useVaultStore.selection-guardrails.test.ts's
// makeVault — the store is real here (not mocked), so the seed must satisfy
// the Vault type.
function makeVault(id: number, name: string): Vault {
  return {
    id,
    name,
    description: "",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    effective_enrichment_enabled: true,
    effective_multimodal_enabled: true,
  };
}

describe("useSendMessage m01 (issue-trace 781-vaultgate-first-run-baseline)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });
    // First-run seeding: exactly one accessible vault, none selected.
    localStorage.removeItem("kv_active_vault_id");
    useVaultStore.setState({
      vaults: [makeVault(1, "Only Vault")],
      activeVaultId: null,
      loading: false,
      error: null,
    });
    apiMocks.createChatSession.mockResolvedValue({ id: 42 });
    apiMocks.addChatMessagesBatch.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);
    // Default completing stream (useSendMessage.test.ts beforeEach pattern):
    // a send that passes the vault gate settles without throwing.
    apiMocks.chatStream.mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
          onComplete: () => Promise<void>;
        }
      ) => {
        handlers.onMessage("hello");
        void handlers.onComplete();
        return vi.fn();
      }
    );
  });

  it("All Vaults first send does not hit the vault-agnostic error", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ input: "First question from the All Vaults composer" });

    // The hook receives the store's activeVaultId — null in the first-run
    // state seeded above (one accessible vault, none picked).
    const { result } = renderHook(() => useSendMessage(null, refresh));

    await act(async () => {
      await result.current.handleSend();
    });

    // The exact vault-agnostic dead-end copy must NOT be stamped into the
    // composer. At base the null-vault guard fires and this expression is
    // true, failing the assertion ("expected true to be false").
    expect(
      useChatStore.getState().inputError ===
        "Please select a vault before starting a chat."
    ).toBe(false);
  });
});

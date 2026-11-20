// frontend/src/tests/transcriptEmptyState.audit1115.test.tsx
// Issue #1153 AC4 (arm's-length check C4, round-1 critic revision): the
// transcript empty state must ASK THE USER TO CHOOSE A VAULT when no single
// vault is selected (the global "All Vaults" scope) while accessible vaults
// with INDEXED documents exist.
//
// Vault signal (plan F4b + critic round 1): the discriminator is the
// per-vault indexed-document count on the accessible-vaults payload, mocked
// here as `indexed_file_count` on the vault store rows (the fix wires the
// real field; the API and frontend ship together, no file_count fallback).
// file_count is only a proxy -- it counts pending/failed uploads too, which
// must NOT trigger the vault-choice state.
//
// Legs:
// - POSITIVE (RED at base): activeVaultId null + at least one accessible
//   readable vault with indexed_file_count > 0 -> the pane asks for a vault
//   choice. Fixed copy pinned: "Choose a vault to start chatting" (asserted
//   case-insensitively as /choose a vault/). The copy does not exist at
//   base, so this leg is RED at base and turns GREEN when the empty state
//   asks for the choice.
// - NEGATIVE (GREEN at base): every accessible vault has
//   indexed_file_count === 0 EVEN THOUGH file_count > 0 (pending/failed
//   uploads only) -> the choose-a-vault copy is ABSENT. The exact legacy
//   upload copy is deliberately not pinned (it may legitimately change).
//
// Harness: the REAL TranscriptPane into the REAL chat/vault stores with the
// api barrel mocked at the module boundary (the TranscriptPane suite
// pattern); the vault store is seeded directly so no vault fetch runs.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import type { ReactNode } from "react";
import { TranscriptPane } from "@/components/chat/TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useVaultStore } from "@/stores/useVaultStore";
import type { Vault } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  chatStream: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSettings: async () => ({ chat_message_max_chars: 20000 }),
  getChatSession: vi.fn(async () => ({ id: 1, vault_id: 1, messages: [] })),
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...(args as [])),
  addChatMessagesBatch: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatch(...(args as [])),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...(args as [])),
  truncateChatSession: vi.fn(),
  forkChatSession: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(() => ({ user: null })),
}));

vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: () => ({
    refreshHistory: vi.fn(),
    chatHistory: [],
    isChatLoading: false,
    chatHistoryError: null,
  }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useInRouterContext: () => false,
  useLocation: () => ({ pathname: "/chat" }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

vi.mock("framer-motion", () => ({
  motion: {
    div: (props: { children?: ReactNode }) =>
      createElement("div", { "data-testid": "motion-div" }, props.children),
  },
  AnimatePresence: ({ children }: { children?: ReactNode }) =>
    createElement("div", null, children),
  useReducedMotion: () => false,
}));

vi.mock("@/components/chat/MessageBubble", () => ({
  MessageBubble: ({ message }: { message: { id: string; content: string } }) =>
    createElement("div", { "data-testid": "message-bubble", "data-message-id": message.id }, message.content),
}));

vi.mock("@/components/chat/AssistantMessage", () => ({
  AssistantMessage: ({ message }: { message: { id: string; content: string } }) =>
    createElement("div", { "data-testid": "assistant-message", "data-message-id": message.id }, message.content),
}));

/**
 * Vault row as the check's fixed contract sees it: the accessible-vaults
 * payload entry plus the per-vault INDEXED-document count (plan F4b; mocked
 * field name indexed_file_count -- the fix wires the real field onto the
 * store rows).
 */
interface AuditVault extends Vault {
  indexed_file_count: number;
}

function makeVault(
  id: number,
  name: string,
  file_count: number,
  indexed_file_count: number,
): AuditVault {
  return {
    id,
    name,
    description: "",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    file_count,
    indexed_file_count,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    current_user_permission: "read",
    effective_enrichment_enabled: false,
    effective_multimodal_enabled: false,
  };
}

function emptyTranscriptRegion(): HTMLElement {
  return screen.getByRole("region", { name: "Empty transcript" });
}

async function renderPane(): Promise<void> {
  await act(async () => {
    render(createElement(TranscriptPane));
    // Let the Composer's cap subscription settle inside act.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("transcript empty state vault choice (issue #1153 AC4)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    act(() => {
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
        messageEditVersions: {},
        activeEditVersion: {},
      });
      // No single vault selected: the global "All Vaults" scope.
      useVaultStore.setState({
        vaults: [],
        activeVaultId: null,
        loading: false,
        error: null,
      });
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });
  });

  afterEach(() => {
    cleanup();
  });

  it("asks the user to choose a vault when no single vault is selected and a vault has indexed documents", async () => {
    act(() => {
      useVaultStore.setState({
        vaults: [
          makeVault(1, "Workshop Manuals", 3, 2),
          makeVault(2, "Service Bulletins", 5, 4),
        ],
        activeVaultId: null,
      });
    });

    await renderPane();

    // RED at base: the choose-a-vault copy does not exist yet. The fixed
    // contract renders "Choose a vault to start chatting" here instead of
    // the generic upload copy (indexed documents exist and the user can
    // read the vaults -- a choice is what is missing).
    expect(emptyTranscriptRegion()).toHaveTextContent(/choose a vault/i);
  });

  it("does not ask for a vault choice when pending/failed uploads leave every vault with zero indexed documents", async () => {
    act(() => {
      // file_count > 0 on BOTH vaults, but nothing is indexed: the raw
      // file proxy must not trigger the vault-choice state.
      useVaultStore.setState({
        vaults: [
          makeVault(1, "Pending Uploads Vault", 4, 0),
          makeVault(2, "Failed Uploads Vault", 2, 0),
        ],
        activeVaultId: null,
      });
    });

    await renderPane();

    // The upload/empty guidance stays for the nothing-indexed case; only
    // the choose-a-vault copy is asserted absent (the exact legacy upload
    // copy is deliberately not pinned -- it may legitimately change).
    const region = emptyTranscriptRegion();
    expect(region).not.toHaveTextContent(/choose a vault/i);
    expect((region.textContent ?? "").trim().length).toBeGreaterThan(0);
  });
});

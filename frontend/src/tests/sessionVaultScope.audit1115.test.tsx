// frontend/src/tests/sessionVaultScope.audit1115.test.tsx
// Issue #1153 AC5 (arm's-length check C5, round-1 critic revision): a
// follow-up sent inside a session the app has OPEN must scope the chat
// transport to the SESSION's stored vault, not to the global vault-selector
// state. The session's stored scope is authoritative however the session
// was opened.
//
// CHOSEN SEAM (documented per the check charter): the public behavior is
// asserted end to end through the production load + send path --
//
//   real ChatShell session-load effect (MemoryRouter routes /chat and
//   /chat/:id) -> api-boundary mocks (getChatSession / createChatSession /
//   forkChatSession return details carrying vault_id) -> real
//   useChatStore.loadChat
//   real TranscriptPane + real Composer send path -> real useSendMessage
//   -> mocked chatStream observed at the transport boundary
//      (chatStream's 3rd positional argument is the vault scope)
//
// THREE legs (critic round 1):
//  1. REOPEN: open /chat/2 (stored vault V2) with the global selector on
//     V1; a follow-up must scope the transport to V2. At base the transport
//     receives the global V1 because ChatShell's load discards
//     detail.vault_id. (If the backend clamps retrieval to the session
//     vault per plan F5b, the frontend assertion stays exactly this:
//     chatStream receives the stored vault.)
//  2. SECOND-SEND-AFTER-CREATE: start at /chat with the global selector on
//     V1; the first send creates session 9 (createChatSession returns
//     {id: 9, vault_id: V1}); the global selector is then switched to V2
//     and a second send goes out on the SAME session -- the transport must
//     STILL receive V1 (the created session's stored vault is
//     authoritative; a global switch must not re-scope an open session).
//     At base the second send carries V2 -- the defect.
//  3. FORK: opened session 2 (stored vault V2), fork it through the pane's
//     real Fork action (forkChatSession returns the new session detail
//     carrying vault_id V2); a follow-up in the forked session must scope
//     the transport to V2. The fork path is driven cheaply through the
//     existing harness: the real TranscriptPane handleFork -> mocked
//     forkChatSession -> real loadChat, then the same Composer send path.
//     At base the send carries the global V1.
//
// Rendering ChatShell's heavy siblings (SessionRail, RightPane, ShareAction,
// VaultSelector, PageTitleHeader) as stubs keeps the tree jsdom-light; every
// module on the discriminating path (ChatShell, TranscriptPane, Composer,
// useSendMessage, the stores) is REAL, so a fix landing in any of them
// (bindSession/loadChat recording the vault, send-scope resolution,
// fork-path binding) turns the legs GREEN.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ChatShell from "@/pages/ChatShell";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { __resetChatInputCapForTests } from "@/hooks/useSendMessage";
import type { Vault } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  getChatSession: vi.fn(),
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  forkChatSession: vi.fn(),
  chatStream: vi.fn(),
}));

const V1 = 1;
const V2 = 2;

vi.mock("@/lib/api", () => ({
  getSettings: async () => ({ chat_message_max_chars: 20000 }),
  getChatSession: (...args: unknown[]) => apiMocks.getChatSession(...(args as [])),
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...(args as [])),
  addChatMessagesBatch: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatch(...(args as [])),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...(args as [])),
  truncateChatSession: vi.fn(),
  forkChatSession: (...args: unknown[]) => apiMocks.forkChatSession(...(args as [])),
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

// The AssistantMessage stub exposes the Fork action (the fork leg drives the
// pane's REAL handleFork through it, mirroring the cancelRestore stub).
vi.mock("@/components/chat/AssistantMessage", () => ({
  AssistantMessage: ({
    message,
    onFork,
  }: {
    message: { id: string; content: string };
    onFork?: () => void;
  }) =>
    createElement(
      "div",
      { "data-testid": "assistant-message", "data-message-id": message.id },
      message.content,
      onFork
        ? createElement(
            "button",
            { type: "button", "aria-label": `Fork ${message.id}`, onClick: onFork },
            "Fork",
          )
        : null,
    ),
}));

// ChatShell's heavy siblings: stubbed so the load + transcript path stays
// the only live surface (see the seam comment in the header).
vi.mock("@/components/chat/SessionRail", () => ({
  SessionRail: () => createElement("div", { "data-testid": "session-rail-stub" }),
}));
vi.mock("@/components/chat/RightPane", () => ({
  RightPane: () => createElement("div", { "data-testid": "right-pane-stub" }),
}));
vi.mock("@/components/chat/ShareAction", () => ({
  ShareAction: () => createElement("div", { "data-testid": "share-action-stub" }),
}));
vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => createElement("div", { "data-testid": "vault-selector-stub" }),
}));
vi.mock("@/components/layout/PageTitleHeader", () => ({
  PageTitleHeader: () => createElement("div", { "data-testid": "page-title-stub" }),
}));

interface StreamHandlers {
  onMessage: (chunk: string) => void;
  onComplete: () => void;
  onRetired?: () => void;
}

let streamHandlers: StreamHandlers | null = null;

interface SessionRow {
  id: number;
  role: "user" | "assistant";
  content: string;
  sources: null;
  created_at: string;
  seq: number;
  status?: string;
}

function sessionRow(
  id: number,
  role: "user" | "assistant",
  content: string,
  seq: number,
): SessionRow {
  return {
    id,
    role,
    content,
    sources: null,
    created_at: `2026-11-01T00:00:0${seq}Z`,
    seq,
    ...(role === "assistant" ? { status: "complete" as const } : {}),
  };
}

function makeVault(id: number, name: string): Vault {
  return {
    id,
    name,
    description: "",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    file_count: 4,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    current_user_permission: "read",
    effective_enrichment_enabled: false,
    effective_multimodal_enabled: false,
  };
}

async function renderChatShellAt(path: string): Promise<void> {
  await act(async () => {
    render(
      createElement(
        MemoryRouter,
        { initialEntries: [path] },
        createElement(
          Routes,
          null,
          createElement(Route, { path: "/chat", element: createElement(ChatShell) }),
          createElement(Route, {
            path: "/chat/:sessionId",
            element: createElement(ChatShell),
          }),
        ),
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function typeAndSend(text: string): Promise<void> {
  await act(async () => {
    await userEvent.type(screen.getByLabelText("Message input"), text);
  });
  await act(async () => {
    await userEvent.click(screen.getByLabelText("Send message"));
  });
  await waitFor(
    () => {
      expect(apiMocks.chatStream).toHaveBeenCalled();
    },
    { timeout: 5000 },
  );
}

async function settleStream(): Promise<void> {
  await act(async () => {
    streamHandlers!.onMessage("scoped answer");
  });
  await act(async () => {
    await streamHandlers!.onComplete();
  });
  await waitFor(() => {
    expect(useChatStore.getState().isStreaming).toBe(false);
  });
}

/** The vault scope of the MOST RECENT chatStream call. */
function lastStreamVaultArg(): unknown {
  const calls = apiMocks.chatStream.mock.calls;
  return calls[calls.length - 1][2];
}

describe("session vault scope for follow-up sends (issue #1153 AC5)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    streamHandlers = null;
    __resetChatInputCapForTests();
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
        activeSessionVaultId: null,
        pendingTurnPersist: null,
        messageEditVersions: {},
        activeEditVersion: {},
      });
      useChatShellStore.setState({ activeSessionId: null, activeSessionTitle: null });
      // Global selector parked on V1; both vaults accessible so nothing
      // auto-resolves. Session 2 (the session under test) is stored in V2.
      useVaultStore.setState({
        vaults: [makeVault(V1, "Workshop Manuals"), makeVault(V2, "Service Bulletins")],
        activeVaultId: V1,
        loading: false,
        error: null,
      });
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatModeStore.setState({ scopeDocumentIds: null });

    // Default loaded session: session 2 belongs to vault 2 (V2).
    apiMocks.getChatSession.mockImplementation(async (sessionId: number) => ({
      id: sessionId,
      vault_id: V2,
      title: "Turbo service history",
      created_at: "2026-11-01T00:00:00Z",
      updated_at: "2026-11-01T00:00:00Z",
      messages: [sessionRow(1, "user", "first question", 1), sessionRow(2, "assistant", "first answer", 2)],
    }));
    apiMocks.addChatMessagesBatch.mockImplementation(
      async (_sessionId: number, messages: Array<{ role: string }>) =>
        messages.map((_, index) => ({
          id: 100 + index,
          created_at: "2026-11-01T00:00:05Z",
          seq: 10 + index,
        })),
    );
    apiMocks.chatStream.mockImplementation(
      (_messages: unknown, handlers: StreamHandlers) => {
        streamHandlers = handlers;
        return vi.fn();
      },
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("REOPEN leg: scopes the follow-up transport to the loaded session's stored vault, not the global selector", async () => {
    await renderChatShellAt("/chat/2");
    await waitFor(
      () => {
        expect(useChatStore.getState().activeChatId).toBe("2");
      },
      { timeout: 5000 },
    );
    // The loaded transcript is in place: the follow-up goes through the
    // production send path on an existing session.
    expect(useChatStore.getState().messageIds).toHaveLength(2);

    await typeAndSend("follow-up question");
    expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);

    // Settle the stream first so no in-flight state leaks out of the test;
    // the transport call args are immutable once made.
    await settleStream();

    // chatStream(messages, callbacks, vaultId, ...): the transport's vault
    // scope must be the SESSION's stored vault (V2). At base it receives
    // the global selector's V1 because ChatShell discards detail.vault_id.
    const vaultArg = apiMocks.chatStream.mock.calls[0][2];
    expect(vaultArg).toBe(V2);
  });

  it("SECOND-SEND-AFTER-CREATE leg: a global vault switch must not re-scope the session the first send created", async () => {
    // Fresh chat with the global selector on V1; the first send creates
    // session 9 (stored vault V1).
    apiMocks.createChatSession.mockResolvedValue({ id: 9, vault_id: V1 });
    apiMocks.getChatSession.mockImplementation(async (sessionId: number) => ({
      id: sessionId,
      vault_id: V1,
      title: "Created session",
      created_at: "2026-11-01T00:00:00Z",
      updated_at: "2026-11-01T00:00:00Z",
      messages: [],
    }));

    await renderChatShellAt("/chat");
    await waitFor(
      () => {
        expect(useChatStore.getState().activeChatId).toBeNull();
      },
      { timeout: 5000 },
    );

    await typeAndSend("first question");
    expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
    // Sanity: the creating send itself was scoped to V1.
    expect(apiMocks.chatStream.mock.calls[0][2]).toBe(V1);
    await settleStream();
    // The created session is now the active session.
    expect(useChatStore.getState().activeChatId).toBe("9");

    // The user switches the GLOBAL selector to V2 mid-conversation.
    act(() => {
      useVaultStore.setState({ activeVaultId: V2 });
    });

    // Second send on the SAME session: the stored scope (V1) is
    // authoritative. At base the send re-reads the global selector and
    // ships V2 -- the defect this leg pins.
    await typeAndSend("second question");
    expect(apiMocks.chatStream).toHaveBeenCalledTimes(2);
    await settleStream();
    expect(lastStreamVaultArg()).toBe(V1);
  });

  it("FORK leg: scopes the follow-up transport to the forked session's stored vault", async () => {
    await renderChatShellAt("/chat/2");
    await waitFor(
      () => {
        expect(useChatStore.getState().activeChatId).toBe("2");
      },
      { timeout: 5000 },
    );

    // The fork API returns the new session's detail carrying its vault
    // (V2, same as the source session).
    apiMocks.forkChatSession.mockResolvedValue({
      id: 30,
      vault_id: V2,
      title: "Fork of Turbo service history",
      created_at: "2026-11-01T00:00:10Z",
      updated_at: "2026-11-01T00:00:10Z",
      messages: [
        sessionRow(31, "user", "first question", 1),
        sessionRow(32, "assistant", "first answer", 2),
      ],
      forked_from_session_id: 2,
      fork_message_index: 2,
    });

    // Drive the pane's REAL fork action on the assistant row.
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Fork 2"));
    });
    await waitFor(
      () => {
        expect(useChatStore.getState().activeChatId).toBe("30");
      },
      { timeout: 5000 },
    );
    expect(apiMocks.forkChatSession).toHaveBeenCalledWith(2, { through_seq: 2 });

    // A follow-up in the forked session must scope to the FORK's stored
    // vault (V2). At base the send carries the global selector's V1
    // because the fork path never records the forked session's vault.
    await typeAndSend("follow-up in the fork");
    expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
    await settleStream();
    expect(lastStreamVaultArg()).toBe(V2);
  });
});

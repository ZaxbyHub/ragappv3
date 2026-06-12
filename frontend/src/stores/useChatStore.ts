import { create } from "zustand";
import type { Source, UsedMemory, WikiReference, KMSReference } from "@/lib/api";

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  /** Memories the assistant used while generating this message (structured, with [M#] labels). */
  memoriesUsed?: UsedMemory[];
  /** Wiki evidence cited as [W#] in this assistant message. */
  wikiRefs?: WikiReference[];
  /** KMS evidence cited as [K#] in this assistant message. */
  kmsRefs?: KMSReference[];
  /** Chat mode used to generate this assistant message. */
  mode?: "instant" | "thinking";
  stopped?: boolean;
  error?: string;
  created_at?: string;
  feedback?: "up" | "down" | null;
}

export interface ChatState {
  // Normalized message storage — O(1) streaming updates on the active message
  messageIds: string[];
  messagesById: Record<string, Message>;
  streamingMessageId: string | null;

  input: string;
  isStreaming: boolean;
  abortFn: (() => void) | null;
  inputError: string | null;
  expandedSources: Set<string>;
  activeChatId: string | null;

  // Actions
  addMessage: (message: Message) => void;
  /** Fast streaming path: appends a chunk to content without replacing the full array. */
  appendToMessage: (id: string, chunk: string) => void;
  updateMessage: (id: string, updates: Partial<Message>) => void;
  /**
   * Atomically replace a message ID. Updates messageIds (preserving order),
   * messagesById (moving the entry), and streamingMessageId if it points at
   * oldId. Optionally merges additional updates into the renamed message.
   * No-op if oldId is missing or oldId === newId (with optional merge).
   * Used after persistence to migrate temp client IDs to DB-assigned IDs.
   */
  replaceMessageId: (oldId: string, newId: string, updates?: Partial<Message>) => void;
  /** Trim messages array at the given index (exclusive). */
  removeMessagesFrom: (index: number) => void;
  clearMessages: () => void;
  setInput: (input: string) => void;
  setIsStreaming: (isStreaming: boolean) => void;
  setStreamingMessageId: (id: string | null) => void;
  setAbortFn: (abortFn: (() => void) | null) => void;
  setInputError: (error: string | null) => void;
  toggleSource: (sourceId: string) => void;
  clearExpandedSources: () => void;
  stopStreaming: () => void;
  loadChat: (chatId: string, messages: Message[]) => void;
  newChat: () => void;

  // Legacy compat — converts array to/from normalized shape
  setMessages: (messages: Message[] | ((prev: Message[]) => Message[])) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  messageIds: [],
  messagesById: {},
  streamingMessageId: null,
  input: "",
  isStreaming: false,
  abortFn: null,
  inputError: null,
  expandedSources: new Set(),
  activeChatId: null,

  addMessage: (message) => {
    set((state) => ({
      messageIds: [...state.messageIds, message.id],
      messagesById: { ...state.messagesById, [message.id]: message },
    }));
  },

  appendToMessage: (id, chunk) => {
    set((state) => {
      const msg = state.messagesById[id];
      if (!msg) return state;
      return {
        messagesById: {
          ...state.messagesById,
          [id]: { ...msg, content: msg.content + chunk },
        },
      };
    });
  },

  updateMessage: (id, updates) => {
    set((state) => {
      const msg = state.messagesById[id];
      if (!msg) return state;
      return {
        messagesById: {
          ...state.messagesById,
          [id]: { ...msg, ...updates },
        },
      };
    });
  },

  replaceMessageId: (oldId, newId, updates) => {
    set((state) => {
      const msg = state.messagesById[oldId];
      if (!msg) return state;
      // Same-ID merge — apply updates only.
      if (oldId === newId) {
        return updates
          ? {
              messagesById: {
                ...state.messagesById,
                [oldId]: { ...msg, ...updates },
              },
            }
          : state;
      }
      // Reject if the target ID is already in use (would corrupt ordering).
      if (state.messagesById[newId]) return state;
      const newIds = state.messageIds.map((id) => (id === oldId ? newId : id));
      const newById: Record<string, Message> = {};
      for (const k of Object.keys(state.messagesById)) {
        if (k === oldId) continue;
        newById[k] = state.messagesById[k];
      }
      newById[newId] = { ...msg, id: newId, ...(updates ?? {}) };
      return {
        messageIds: newIds,
        messagesById: newById,
        streamingMessageId:
          state.streamingMessageId === oldId ? newId : state.streamingMessageId,
      };
    });
  },

  removeMessagesFrom: (index) => {
    set((state) => {
      const newIds = state.messageIds.slice(0, index);
      const newById: Record<string, Message> = {};
      for (const id of newIds) {
        if (state.messagesById[id]) newById[id] = state.messagesById[id];
      }
      return { messageIds: newIds, messagesById: newById };
    });
  },

  clearMessages: () => {
    set({ messageIds: [], messagesById: {}, expandedSources: new Set(), activeChatId: null, streamingMessageId: null });
  },

  setInput: (input) => set({ input }),
  setIsStreaming: (isStreaming) => set({ isStreaming }),
  setStreamingMessageId: (streamingMessageId) => set({ streamingMessageId }),
  setAbortFn: (abortFn) => set({ abortFn }),
  setInputError: (inputError) => set({ inputError }),

  toggleSource: (sourceId) => {
    set((state) => {
      const newSet = new Set(state.expandedSources);
      if (newSet.has(sourceId)) {
        newSet.delete(sourceId);
      } else {
        newSet.add(sourceId);
      }
      return { expandedSources: newSet };
    });
  },

  clearExpandedSources: () => {
    set({ expandedSources: new Set() });
  },

  stopStreaming: () => {
    const { abortFn, messageIds, messagesById, streamingMessageId } = get();
    if (abortFn) abortFn();

    const updates: Partial<ChatState> = {
      abortFn: null,
      isStreaming: false,
      streamingMessageId: null,
    };

    const targetId = streamingMessageId ?? messageIds[messageIds.length - 1];
    if (targetId) {
      const lastMsg = messagesById[targetId];
      if (lastMsg && lastMsg.role === "assistant") {
        updates.messagesById = {
          ...messagesById,
          [targetId]: { ...lastMsg, stopped: true },
        };
      }
    }
    set(updates);
  },

  loadChat: (chatId, messages) => {
    // Abort any in-flight stream before switching sessions. Otherwise the orphan
    // stream keeps running: its onComplete fires later and the assistant reply is
    // silently dropped (its message id no longer exists), and the new session's
    // composer stays locked in "Generating". The wrapped abortFn (see
    // useSendMessage) also clears the send guard. No-op when nothing is streaming.
    const { abortFn } = get();
    if (abortFn) abortFn();
    const messageIds = messages.map((m) => m.id);
    const messagesById: Record<string, Message> = {};
    for (const m of messages) messagesById[m.id] = m;
    set({ activeChatId: chatId, messageIds, messagesById, expandedSources: new Set(), streamingMessageId: null, isStreaming: false, abortFn: null });
  },

  newChat: () => {
    // Abort any in-flight stream before starting a new chat (see loadChat).
    const { abortFn } = get();
    if (abortFn) abortFn();
    set({ activeChatId: null, messageIds: [], messagesById: {}, expandedSources: new Set(), streamingMessageId: null, isStreaming: false, abortFn: null });
  },

  setMessages: (messages) => {
    const current = get();
    const resolved =
      typeof messages === "function"
        ? messages(current.messageIds.map((id) => current.messagesById[id]))
        : messages;
    const newIds = resolved.map((m) => m.id);
    const newById: Record<string, Message> = {};
    for (const m of resolved) newById[m.id] = m;
    set({ messageIds: newIds, messagesById: newById });
  },
}));

// Selectors — use granular selectors to limit re-renders
export const useMessageIds = () => useChatStore((s) => s.messageIds);
/** Subscribe to a single message by ID — streaming rows only re-render for their own message. */
export const useMessage = (id: string) => useChatStore((s) => s.messagesById[id]);
/** Legacy: full messages array. Triggers re-render when any message changes. */
export const useChatMessages = () =>
  useChatStore((s) => s.messageIds.map((id) => s.messagesById[id]));
export const useChatInput = () => useChatStore((s) => s.input);
export const useChatIsStreaming = () => useChatStore((s) => s.isStreaming);
export const useChatInputError = () => useChatStore((s) => s.inputError);
export const useChatActiveChatId = () => useChatStore((s) => s.activeChatId);
export const useChatStreamingId = () => useChatStore((s) => s.streamingMessageId);

/**
 * Granular selector for the byte length of the currently-streaming assistant
 * message's content. Triggers a re-render every time tokens grow, while
 * intentionally not exposing the full content string. Components use this to
 * drive auto-scroll on token growth without subscribing to the message body.
 * Returns 0 when no message is streaming.
 */
export const useStreamingMessageContentLength = (): number =>
  useChatStore((s) => {
    const id = s.streamingMessageId;
    if (!id) return 0;
    return s.messagesById[id]?.content.length ?? 0;
  });

/**
 * Selector returning the sources of the most recent *completed* assistant
 * message. "Completed" means: not the actively streaming message. The
 * RightPane uses this so its "Sources" tab only re-renders when streaming
 * ends or sources change — never on every token chunk.
 */
export const useLastCompletedAssistantSources = (): Source[] | undefined =>
  useChatStore((s) => {
    const streamingId = s.streamingMessageId;
    for (let i = s.messageIds.length - 1; i >= 0; i--) {
      const id = s.messageIds[i];
      if (id === streamingId) continue;
      const msg = s.messagesById[id];
      if (msg?.role === "assistant" && msg.sources) return msg.sources;
    }
    return undefined;
  });

/**
 * Selector returning the wiki refs of the most recent *completed* assistant
 * message. Mirrors useLastCompletedAssistantSources pattern.
 */
export const useLastCompletedAssistantWikiRefs = (): WikiReference[] | undefined =>
  useChatStore((s) => {
    const streamingId = s.streamingMessageId;
    for (let i = s.messageIds.length - 1; i >= 0; i--) {
      const id = s.messageIds[i];
      if (id === streamingId) continue;
      const msg = s.messagesById[id];
      if (msg?.role === "assistant" && msg.wikiRefs && msg.wikiRefs.length > 0) return msg.wikiRefs;
    }
    return undefined;
  });

/**
 * Selector returning the KMS refs of the most recent *completed* assistant
 * message. Mirrors useLastCompletedAssistantWikiRefs.
 */
export const useLastCompletedAssistantKmsRefs = (): KMSReference[] | undefined =>
  useChatStore((s) => {
    const streamingId = s.streamingMessageId;
    for (let i = s.messageIds.length - 1; i >= 0; i--) {
      const id = s.messageIds[i];
      if (id === streamingId) continue;
      const msg = s.messagesById[id];
      if (msg?.role === "assistant" && msg.kmsRefs && msg.kmsRefs.length > 0) return msg.kmsRefs;
    }
    return undefined;
  });

/** Last user message's content (the active query). */
export const useLastUserContent = (): string =>
  useChatStore((s) => {
    for (let i = s.messageIds.length - 1; i >= 0; i--) {
      const msg = s.messagesById[s.messageIds[i]];
      if (msg?.role === "user") return msg.content;
    }
    return "";
  });

/**
 * Returns the assistant-message sources containing the source whose id
 * matches ``sourceId``. RightPane uses this when an inline citation is
 * clicked: the displayed source list should be the parent message's
 * sources, not the latest message's.
 */
export const useSourcesForSourceId = (sourceId?: string): Source[] | undefined =>
  useChatStore((s) => {
    if (!sourceId) return undefined;
    for (let i = 0; i < s.messageIds.length; i++) {
      const msg = s.messagesById[s.messageIds[i]];
      const found = msg?.sources?.some((src) => src.id === sourceId);
      if (found) return msg!.sources;
    }
    return undefined;
  });

/**
 * Returns a JSON-encoded array of every completed (non-streaming)
 * assistant message id. Consumers parse this back to an id array via
 * ``parseCompletedAssistantIds``. Returning a string keeps zustand's
 * default ``Object.is`` equality cheap — re-renders only fire when the
 * set of completed ids actually changes.
 *
 * JSON encoding is used so the separator is unambiguous: message ids are
 * arbitrary strings that may contain any character.
 */
export const useCompletedAssistantMessageIdsKey = (): string =>
  useChatStore((s) => {
    const parts: string[] = [];
    for (const id of s.messageIds) {
      if (id === s.streamingMessageId) continue;
      const msg = s.messagesById[id];
      if (msg?.role === "assistant") parts.push(id);
    }
    return JSON.stringify(parts);
  });

export function parseCompletedAssistantIds(key: string): string[] {
  if (!key) return [];
  try {
    const parsed = JSON.parse(key);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

import { useCallback, useEffect, useRef, useState } from "react";
import {
  chatStream,
  createChatSession,
  addChatMessagesBatch,
  type ChatMessage,
  type ChatSessionMessage,
  type WikiReference,
  type KMSReference,
} from "@/lib/api";
import { useChatStore, type Message } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { computeEffectiveChatMode } from "@/lib/chatMode";
import type { UsedMemory } from "@/lib/api";
import useCoalescedAppend from "./useCoalescedAppend";

export const MAX_INPUT_LENGTH = 2000;

export interface UseSendMessageReturn {
  handleSend: () => Promise<void>;
  handleStop: () => void;
  handleKeyDown: (e: React.KeyboardEvent) => void;
  handleInputChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  /** Send with explicit content + history — does not read or modify composer input state. */
  sendDirect: (content: string, historyMessages: Message[]) => Promise<void>;
  /** Current pipeline stage (Searching/Reading/Drafting) before content streams, or null. */
  currentStage: string | null;
}

export function useSendMessage(
  activeVaultId: number | null,
  refreshHistory: (force?: boolean) => Promise<void>
): UseSendMessageReturn {
  const {
    setInput,
    setIsStreaming,
    setAbortFn,
    setInputError,
    addMessage,
    updateMessage,
    replaceMessageId,
    setStreamingMessageId,
  } = useChatStore();

  // Current pipeline stage — set when backend emits a stage SSE event
  const [currentStage, setCurrentStage] = useState<string | null>(null);

  // Atomic guard — prevents double-send from rapid clicks / Enter
  const sendingRef = useRef(false);

  // Generation token (issue #507 / UI-003): captured per send, bumped on Stop.
  // Checked after every await — when it no longer matches, the send was
  // cancelled and must not start the stream or append any message.
  const sendGenRef = useRef(0);

  // Coalescing hook for streaming content chunks — reduces store write frequency
  const { append, flush, reset, content } = useCoalescedAppend();

  // Ref to track the current streaming assistant message ID so the useEffect
  // can update it even after the closure that created it has returned.
  const assistantMessageIdRef = useRef<string | null>(null);

  /**
   * Core send primitive. Accepts content and a history snapshot directly so
   * it doesn't depend on the Zustand input field at all. Both the normal
   * "send from composer" path and the "retry/sendDirect" path go through here.
   */
  const sendCore = useCallback(
    async (content: string, historyMessages: Message[], clearInput: boolean) => {
      if (sendingRef.current) return;
      sendingRef.current = true;
      setIsStreaming(true);
      const gen = ++sendGenRef.current;

      // UI-003: install the abort handle BEFORE the first await. A Stop
      // pressed during session creation (while no stream exists to abort)
      // invalidates this send's generation so generation can never start
      // afterward with no visible Stop control.
      setAbortFn(() => {
        sendGenRef.current += 1;
        setIsStreaming(false);
        setAbortFn(null);
        sendingRef.current = false;
      });

      const currentState = useChatStore.getState();
      let sessionId: number;

      if (currentState.activeChatId) {
        sessionId = parseInt(currentState.activeChatId);
      } else {
        if (!activeVaultId) {
          setInputError("Please select a vault before starting a chat.");
          setIsStreaming(false);
          setAbortFn(null);
          sendingRef.current = false;
          return;
        }
        try {
          const newSession = await createChatSession({ vault_id: activeVaultId });
          sessionId = newSession.id;
          useChatStore.setState({ activeChatId: newSession.id.toString() });
        } catch (err) {
          console.error("Failed to create chat session:", err);
          const status = (err as { response?: { status?: number } })?.response?.status;
          setInputError(
            status === 403
              ? "You don't have permission to chat in this vault."
              : "Failed to start chat session. Please check your connection."
          );
          setIsStreaming(false);
          setAbortFn(null);
          sendingRef.current = false;
          return;
        }
      }

      if (sendGenRef.current !== gen) {
        // Cancelled while the session was being created — do not stream,
        // do not append any message (no dangling assistant bubble).
        return;
      }

      const turnId =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `turn-${Date.now()}-${Math.random().toString(36).slice(2)}`;

      const userMessage: Message = {
        id: Date.now().toString(),
        role: "user",
        content,
        turnId,
      };
      const assistantMessageId = (Date.now() + 1).toString();
      // Pre-populate mode from the requested effective mode so the badge shows
      // immediately as the response streams. The backend's "mode" SSE event
      // (handled below) overrides this if a fallback was applied server-side.
      const assistantMessage: Message = {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        turnId,
      };

      const chatMessages: ChatMessage[] = [
        ...historyMessages.map((m) => ({ role: m.role, content: m.content })),
        { role: "user", content },
      ];

      addMessage(userMessage);
      addMessage(assistantMessage);
      setStreamingMessageId(assistantMessageId);

      if (clearInput) {
        setInput("");
        setInputError(null);
      }

      // Accumulate wiki refs from the SSE stream so they can be persisted with the message.
      let streamedWikiRefs: WikiReference[] = [];
      // Accumulate KMS refs from the SSE stream so they can be persisted with the message.
      let streamedKmsRefs: KMSReference[] = [];
      // Mirror of the coalesced hook's accumulated content, updated
      // synchronously alongside append(). useCoalescedAppend's flush()
      // writes to React state, and the store sync happens in a useEffect —
      // both deferred to the next render. onComplete/onError need the FULL
      // content immediately (to persist to the backend or update the store
      // without waiting a render), so they read this synchronous mirror
      // instead of relying on the effect having already run.
      let streamedContent = "";

      // Resolve effective chat mode using the same logic as the Composer
      // toggle so the highlighted mode and the sent payload never diverge.
      // Read .getState() (not hook subscriptions) to capture values at send
      // time and avoid stale closures.
      const health = useLlmHealthStore.getState();
      const effectiveMode = computeEffectiveChatMode({
        stored: useChatModeStore.getState().chatMode,
        defaultMode: useSettingsStore.getState().formData.default_chat_mode,
        thinkingHealthy: health.thinking,
        instantHealthy: health.instant,
      });

      // Optimistically attribute the in-flight assistant message to the
      // requested mode so the badge shows immediately. The "mode" SSE event
      // below overwrites this if the server applied a fallback.
      updateMessage(assistantMessageId, { mode: effectiveMode });

      // Reset coalescing buffer and track the assistant message ID for the stream.
      // The useEffect syncs coalesced content → store via updateMessage.
      reset();
      flush();
      assistantMessageIdRef.current = assistantMessageId;

      // Persist a turn durably (issue #507): one ordered, all-or-nothing batch
      // per turn. status "complete" on success, "interrupted" for a partially
      // streamed turn — an interrupted answer is never saved as a successful
      // one, and empty content is never persisted at all (LIVE-01). A failed
      // batch commits nothing server-side, so the visible retry below can
      // never duplicate a successful sibling write (UI-002).
      const migrateId = (oldId: string, saveResult: ChatSessionMessage) => {
        const dbId = String(saveResult.id);
        if (dbId === oldId) {
          updateMessage(oldId, { saveState: "saved" });
          return;
        }
        const feedbackKey = `chat_feedback_${oldId}`;
        const feedbackValue = localStorage.getItem(feedbackKey);
        if (feedbackValue !== null) {
          localStorage.setItem(`chat_feedback_${dbId}`, feedbackValue);
          localStorage.removeItem(feedbackKey);
        }
        replaceMessageId(oldId, dbId, { created_at: saveResult.created_at, saveState: "saved" });
      };

      const persistTurn = async (assistantStatus: "complete" | "interrupted") => {
        const storeState = useChatStore.getState();
        const assistantMsg = storeState.messagesById[assistantMessageId];
        const userMsg = storeState.messagesById[userMessage.id];
        // Abandoned stream (loadChat/newChat cleared the store): skip both
        // saves so no dangling rows land in the old session (issue #235).
        if (!assistantMsg || !userMsg) return;
        if (!assistantMsg.content.trim()) {
          updateMessage(assistantMessageId, {
            error: "The model returned an empty response. Try again.",
          });
          return;
        }
        updateMessage(assistantMessageId, { saveState: "saving" });
        updateMessage(userMessage.id, { saveState: "saving" });
        try {
          const saved = await addChatMessagesBatch(sessionId, [
            { role: "user", content, turn_id: turnId },
            {
              role: "assistant",
              content: assistantMsg.content,
              sources: assistantMsg.sources ?? undefined,
              memories: assistantMsg.memoriesUsed ?? undefined,
              wiki_refs: streamedWikiRefs.length > 0 ? streamedWikiRefs : undefined,
              kms_refs: streamedKmsRefs.length > 0 ? streamedKmsRefs : undefined,
              mode: assistantMsg.mode,
              turn_id: turnId,
              status: assistantStatus,
              citation_confidence: assistantMsg.citationConfidence,
              unverifiable_claims: assistantMsg.unverifiableClaims,
            },
          ]);
          const [userSaveResult, assistantSaveResult] = saved;
          migrateId(userMessage.id, userSaveResult);
          migrateId(assistantMessageId, assistantSaveResult);
          await refreshHistory(true);
          useChatShellStore.getState().requestSessionListRefresh();
        } catch (err) {
          console.error("Failed to save chat messages:", err);
          // UI-002: a failed save must be visible and retryable with the
          // answer and original input intact — never a silent loss.
          updateMessage(assistantMessageId, {
            saveState: "failed",
            error: "Couldn't save this exchange. Retry to avoid losing it.",
          });
          updateMessage(userMessage.id, { saveState: "failed" });
        }
      };

      const abort = chatStream(
        chatMessages,
        {
          onMessage: (chunk) => {
            setCurrentStage(null);
            // Coalesce SSE appends behind requestAnimationFrame (UI-PERF-2):
            // without this, every token chunk updates the store, re-renders
            // MarkdownMessage, and re-runs the full remark/rehype parse of
            // the entire accumulated content (O(n²) in message length).
            // useCoalescedAppend batches appends to once per frame (with a
            // timer fallback), bounding reparse frequency independent of
            // token rate while preserving live citation rendering.
            streamedContent += chunk;
            append(chunk);
          },
          onSources: (sources) => {
            updateMessage(assistantMessageId, { sources });
          },
          onMemories: (memories: UsedMemory[]) => {
            updateMessage(assistantMessageId, { memoriesUsed: memories });
          },
          onWiki: (wikiRefs: WikiReference[]) => {
            streamedWikiRefs = wikiRefs;
            updateMessage(assistantMessageId, { wikiRefs });
          },
          onKMS: (kmsRefs: KMSReference[]) => {
            streamedKmsRefs = kmsRefs;
            updateMessage(assistantMessageId, { kmsRefs });
          },
          onMode: (mode) => {
            updateMessage(assistantMessageId, { mode });
          },
          onStage: (stage) => {
            setCurrentStage(stage);
          },
          onFinalContent: (content) => {
            // Backend stripped invalid citations: adopt the cleaned content so
            // the hallucinated [S#] chip is removed from the rendered message
            // and from what onComplete persists. The replace SUPERSEDES all
            // buffered streaming tokens, so cancel any pending coalesced flush
            // and clear its buffer — otherwise the pending flush would later
            // fire (its `content` state still holding the stale, citation-dirty
            // accumulated text) and the sync-effect below would overwrite this
            // cleaned content with it, re-injecting the stripped citations and
            // duplicating text. Nulling the ref also stops that same effect
            // from firing again for this message once reset() clears content
            // back to "".
            reset();
            assistantMessageIdRef.current = null;
            streamedContent = content;
            updateMessage(assistantMessageId, { content });
          },
          // FR-004: capture citation confidence and unverifiable claims from done event.
          onCitationConfidence: (confidence) => {
            updateMessage(assistantMessageId, { citationConfidence: confidence });
          },
          onUnverifiableClaims: (claims) => {
            updateMessage(assistantMessageId, { unverifiableClaims: claims });
          },
          onError: (error) => {
            // Flush any buffered streaming content before reading store state
            // (UI-PERF-2): rAF-batched appends may not have fired yet, so
            // synchronously drain the buffer to avoid losing the partial tail.
            // flush() only updates the hook's own React state — the sync to
            // the store happens in a useEffect on the next render, which is
            // too late for the synchronous updateMessage below. Write the
            // synchronous streamedContent mirror directly instead (skipped
            // if onFinalContent already finalized this message).
            flush();
            if (assistantMessageIdRef.current !== null) {
              updateMessage(assistantMessageId, { content: streamedContent });
            }
            console.error("Chat stream error:", error);
            const isAbort =
              error.name === "AbortError" || /aborted|abort/i.test(error.message);
            if (isAbort) {
              // User-cancelled turn: mark stopped, never silently retried,
              // and not persisted (the partial answer stays visible locally).
              setIsStreaming(false);
              setAbortFn(null);
              setStreamingMessageId(null);
              sendingRef.current = false;
              return;
            }
            if (error.name === "ChatInterruptedError") {
              // CHAT-004: EOF before the completion marker. Mark the turn
              // retryable, keep the partial answer visible, and persist it as
              // "interrupted" — never as a successful answer.
              updateMessage(assistantMessageId, {
                status: "interrupted",
                error: "Response interrupted. You can retry.",
              });
              setCurrentStage(null);
              setIsStreaming(false);
              setAbortFn(null);
              setStreamingMessageId(null);
              sendingRef.current = false;
              void persistTurn("interrupted");
              return;
            }
            const isNetworkError =
              /failed to fetch|networkerror|network request failed|load failed/i.test(
                error.message
              );
            const friendlyMessage = isNetworkError
              ? "Connection lost. Check your network and try again."
              : error.message;
            updateMessage(assistantMessageId, { error: friendlyMessage });
            setCurrentStage(null);
            setIsStreaming(false);
            setAbortFn(null);
            setStreamingMessageId(null);
            sendingRef.current = false;
          },
          onComplete: async () => {
            // Flush any buffered streaming content before reading store state
            // (UI-PERF-2): rAF-batched appends may not have fired yet when the
            // stream completes, so synchronously drain the buffer to avoid
            // persisting truncated content (the tail would be lost otherwise).
            flush();
            if (assistantMessageIdRef.current !== null) {
              updateMessage(assistantMessageId, { content: streamedContent });
            }
            setCurrentStage(null);
            setIsStreaming(false);
            setAbortFn(null);
            setStreamingMessageId(null);
            sendingRef.current = false;
            await persistTurn("complete");
          },
        },
        activeVaultId ?? undefined,
        effectiveMode,
        useChatModeStore.getState().temperature,
        useChatModeStore.getState().retrievalMode,
        useChatModeStore.getState().citationMode,
      );

      // Wrap the raw abort so any caller that aborts the stream — the Stop
      // button OR a session switch routed through the store (loadChat/newChat) —
      // also clears the hook-local in-flight guard. Without this, aborting via
      // navigation would leave sendingRef stuck true and block the next send.
      setAbortFn(() => {
        abort();
        sendingRef.current = false;
      });
    },
    [
      setInput,
      setIsStreaming,
      setAbortFn,
      setInputError,
      addMessage,
      updateMessage,
      replaceMessageId,
      setStreamingMessageId,
      setCurrentStage,
      append,
      flush,
      reset,
      activeVaultId,
      refreshHistory,
    ]
  );

  /** Normal send — reads content from the Zustand input field. */
  const handleSend = useCallback(async () => {
    const { input: currentInput, isStreaming: currentIsStreaming } =
      useChatStore.getState();
    if (!currentInput.trim() || currentIsStreaming || sendingRef.current) return;
    if (currentInput.length > MAX_INPUT_LENGTH) {
      setInputError(`Input exceeds maximum length of ${MAX_INPUT_LENGTH} characters`);
      return;
    }
    const content = currentInput.trim();
    const { messageIds, messagesById } = useChatStore.getState();
    const history = messageIds.map((id) => messagesById[id]);
    await sendCore(content, history, true);
  }, [setInputError, sendCore]);

  /**
   * Direct send — accepts content and history explicitly.
   * Used for retry / regenerate so it doesn't touch the composer input.
   */
  const sendDirect = useCallback(
    async (content: string, historyMessages: Message[]) => {
      const { isStreaming: currentIsStreaming } = useChatStore.getState();
      if (currentIsStreaming || sendingRef.current) return;
      await sendCore(content, historyMessages, false);
    },
    [sendCore]
  );

  const handleStop = useCallback(() => {
    // Invalidate the in-flight generation BEFORE touching the store: a Stop
    // pressed during session creation has no stream to abort, and without
    // this bump the send would start generating once creation resolved.
    sendGenRef.current += 1;
    useChatStore.getState().stopStreaming();
    sendingRef.current = false;
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // IME guard: don't send while composing CJK or other multi-key input
      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const value = e.target.value;
      setInput(value);
      if (value.length > MAX_INPUT_LENGTH) {
        setInputError(`Input exceeds maximum length of ${MAX_INPUT_LENGTH} characters`);
      } else {
        setInputError(null);
      }
    },
    [setInput, setInputError]
  );

  // Sync coalesced streaming content to the chat store.
  // The coalescing hook batches rapid SSE chunks; this effect writes the
  // accumulated content to the store via updateMessage (not appendToMessage)
  // to avoid double-appending the accumulated content.
  useEffect(() => {
    const messageId = assistantMessageIdRef.current;
    if (messageId === null) return;
    updateMessage(messageId, { content });
  }, [content, updateMessage]);

  return { handleSend, handleStop, handleKeyDown, handleInputChange, sendDirect, currentStage };
}

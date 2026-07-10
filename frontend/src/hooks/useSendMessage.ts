import { useCallback, useEffect, useRef, useState } from "react";
import {
  chatStream,
  createChatSession,
  addChatMessage,
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
    appendToMessage,
    updateMessage,
    replaceMessageId,
    setStreamingMessageId,
  } = useChatStore();

  // Current pipeline stage — set when backend emits a stage SSE event
  const [currentStage, setCurrentStage] = useState<string | null>(null);

  // Atomic guard — prevents double-send from rapid clicks / Enter
  const sendingRef = useRef(false);
  // UI-PERF-2: rAF batching for streaming appends.
  const streamingBufferRef = useRef("");
  const streamingRafRef = useRef<number | null>(null);

  // Cancel any pending rAF flush on unmount so we don't update state after teardown.
  useEffect(() => {
    return () => {
      if (streamingRafRef.current !== null) {
        cancelAnimationFrame(streamingRafRef.current);
        streamingRafRef.current = null;
      }
      streamingBufferRef.current = "";
    };
  }, []);

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

      const currentState = useChatStore.getState();
      let sessionId: number;

      if (currentState.activeChatId) {
        sessionId = parseInt(currentState.activeChatId);
      } else {
        if (!activeVaultId) {
          setInputError("Please select a vault before starting a chat.");
          setIsStreaming(false);
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
          sendingRef.current = false;
          return;
        }
      }

      const userMessage: Message = {
        id: Date.now().toString(),
        role: "user",
        content,
      };
      const assistantMessageId = (Date.now() + 1).toString();
      // Pre-populate mode from the requested effective mode so the badge shows
      // immediately as the response streams. The backend's "mode" SSE event
      // (handled below) overrides this if a fallback was applied server-side.
      const assistantMessage: Message = {
        id: assistantMessageId,
        role: "assistant",
        content: "",
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

      const abort = chatStream(
        chatMessages,
        {
          onMessage: (chunk) => {
            setCurrentStage(null);
            // Coalesce SSE appends behind requestAnimationFrame (UI-PERF-2):
            // without this, every token chunk updates the store, re-renders
            // MarkdownMessage, and re-runs the full remark/rehype parse of
            // the entire accumulated content (O(n²) in message length). The
            // rAF batches appends to once per frame, bounding reparse
            // frequency independent of token rate while preserving live
            // citation rendering (the full ReactMarkdown pipeline still runs).
            streamingBufferRef.current += chunk;
            if (streamingRafRef.current === null) {
              streamingRafRef.current = requestAnimationFrame(() => {
                streamingRafRef.current = null;
                const buffered = streamingBufferRef.current;
                if (buffered) {
                  streamingBufferRef.current = "";
                  appendToMessage(assistantMessageId, buffered);
                }
              });
            }
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
            // buffered streaming tokens, so cancel any pending rAF and clear
            // the buffer — otherwise onComplete's flush would append the stale
            // (citation-dirty) buffered tokens back on top of the cleaned
            // content, re-injecting the stripped citations and duplicating text.
            if (streamingRafRef.current !== null) {
              cancelAnimationFrame(streamingRafRef.current);
              streamingRafRef.current = null;
            }
            streamingBufferRef.current = "";
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
            if (streamingRafRef.current !== null) {
              cancelAnimationFrame(streamingRafRef.current);
              streamingRafRef.current = null;
            }
            const buffered = streamingBufferRef.current;
            if (buffered) {
              streamingBufferRef.current = "";
              appendToMessage(assistantMessageId, buffered);
            }
            console.error("Chat stream error:", error);
            const isAbort =
              error.name === "AbortError" || /aborted|abort/i.test(error.message);
            if (isAbort) {
              setIsStreaming(false);
              setAbortFn(null);
              setStreamingMessageId(null);
              sendingRef.current = false;
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
            if (streamingRafRef.current !== null) {
              cancelAnimationFrame(streamingRafRef.current);
              streamingRafRef.current = null;
            }
            const buffered = streamingBufferRef.current;
            if (buffered) {
              streamingBufferRef.current = "";
              appendToMessage(assistantMessageId, buffered);
            }
            setCurrentStage(null);
            setIsStreaming(false);
            setAbortFn(null);
            setStreamingMessageId(null);
            sendingRef.current = false;
            try {
              const storeState = useChatStore.getState();
              const assistantMsg = storeState.messagesById[assistantMessageId];
              // If the assistant message is gone from the store, the stream was
              // abandoned — typically because loadChat/newChat aborted it and
              // cleared messagesById. The assistant save is already skipped via
              // the guard below; without this same guard on the user save, the
              // closure-captured sessionId would persist a dangling user
              // message to the old session. See issue #235.
              if (!assistantMsg) return;
              const saves: Promise<ChatSessionMessage>[] = [
                addChatMessage(sessionId, { role: "user", content }),
                addChatMessage(sessionId, {
                  role: "assistant",
                  content: assistantMsg.content,
                  sources: assistantMsg.sources ?? undefined,
                  memories: assistantMsg.memoriesUsed ?? undefined,
                  wiki_refs: streamedWikiRefs.length > 0 ? streamedWikiRefs : undefined,
                  kms_refs: streamedKmsRefs.length > 0 ? streamedKmsRefs : undefined,
                  mode: assistantMsg.mode,
                }),
              ];
              const [userSaveResult, assistantSaveResult] = await Promise.all(saves);

              // Atomically migrate temp client IDs to DB-assigned IDs.
              // Uses replaceMessageId so messageIds, messagesById, and
              // streamingMessageId remain consistent. Migrates the local
              // feedback storage key alongside the ID swap.
              const migrateId = (oldId: string, saveResult: ChatSessionMessage) => {
                const dbId = String(saveResult.id);
                if (dbId === oldId) return;
                const feedbackKey = `chat_feedback_${oldId}`;
                const feedbackValue = localStorage.getItem(feedbackKey);
                if (feedbackValue !== null) {
                  localStorage.setItem(`chat_feedback_${dbId}`, feedbackValue);
                  localStorage.removeItem(feedbackKey);
                }
                replaceMessageId(oldId, dbId, { created_at: saveResult.created_at });
              };

              migrateId(userMessage.id, userSaveResult);
              migrateId(assistantMessageId, assistantSaveResult);

              await refreshHistory(true);
              useChatShellStore.getState().requestSessionListRefresh();
            } catch (err) {
              console.error("Failed to save chat messages:", err);
            }
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
      appendToMessage,
      updateMessage,
      replaceMessageId,
      setStreamingMessageId,
      setCurrentStage,
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

  return { handleSend, handleStop, handleKeyDown, handleInputChange, sendDirect, currentStage };
}

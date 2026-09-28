import { useCallback, useEffect, useRef, useState } from "react";
import {
  chatStream,
  createChatSession,
  addChatMessagesBatch,
  addChatMessagesBatchKeepalive,
  type ChatMessage,
  type AddMessageRequest,
  type ChatMetadataFilter,
  type ChatSessionMessage,
  type WikiReference,
  type KMSReference,
} from "@/lib/api";
import { toast } from "sonner";
import { useChatStore, type Message } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { readFeedbackVote, writeFeedbackVote } from "@/lib/chatFeedbackStorage";
import { computeEffectiveChatMode } from "@/lib/chatMode";
import type { UsedMemory } from "@/lib/api";
import useCoalescedAppend from "./useCoalescedAppend";

// Inline composer cap. The backend imposes no message-length contract
// (ChatMessage.content is an unbounded str), so this is a frontend guard
// only: 100k keeps a bound on the SSE payload while covering long-form
// RAG prompts; larger paste-sized input is routed to attachments by the
// Composer instead (LARGE_PASTE_THRESHOLD).
export const MAX_INPUT_LENGTH = 100_000;

// Issue #685 (PRR-001): the in-flight turn's persistence handle lives at
// MODULE scope, not only on the hook instance. The first send from /chat
// navigates to /chat/:id, and PageShell keys the page content by pathname —
// ChatShell (and this hook) remount mid-turn, so an instance ref would be
// null in the remounted handleStop and Stop would silently skip persistStop.
// The module slot survives the remount; it is single-slot because sendingRef
// makes turns strictly sequential.
let activeTurnPersistence: CurrentTurnPersistence | null = null;

export interface UseSendMessageReturn {
  handleSend: () => Promise<void>;
  handleStop: () => void;
  handleKeyDown: (e: React.KeyboardEvent) => void;
  handleInputChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  /**
   * Send with explicit content + history — does not read or modify composer
   * input state. `opts.restoreRows` (issue #684 / T1-13-S-08) carries the
   * original turn rows a revision already truncated, restored server-side if
   * the replacement is admission-rejected before any content.
   */
  sendDirect: (
    content: string,
    historyMessages: Message[],
    opts?: { restoreRows?: Message[] }
  ) => Promise<void>;
  /** Current pipeline stage (Searching/Reading/Drafting) before content streams, or null. */
  currentStage: string | null;
}

interface CurrentTurnPersistence {
  persistStop: () => void;
  persistPagehide: () => void;
}

export function useSendMessage(
  activeVaultId: number | null,
  refreshHistory: (force?: boolean) => Promise<void>,
  options?: { onSessionCreated?: (sessionId: string) => void }
): UseSendMessageReturn {
  // Actions only, resolved once: zustand action references are stable for
  // the store's lifetime, and taking them via getState() avoids subscribing
  // every consumer of this hook (TranscriptPane) to ALL store changes —
  // a whole-store destructure here re-rendered the transcript tree on every
  // composer keystroke (issue #616).
  const {
    setInput,
    setIsStreaming,
    setAbortFn,
    setInputError,
    addMessage,
    updateMessage,
    replaceMessageId,
    setStreamingMessageId,
  } = useChatStore.getState();

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

  // Lifecycle persistence is owned by this hook so navigation aborts can
  // continue discarding old-session work through the shared store path.
  const currentTurnPersistenceRef = useRef<CurrentTurnPersistence | null>(null);

  // Issue #685: optional binding callback for a session created by the first
  // send. Extracted so sendCore depends on the function identity, not the
  // options object.
  const onSessionCreated = options?.onSessionCreated;

  /**
   * Core send primitive. Accepts content and a history snapshot directly so
   * it doesn't depend on the Zustand input field at all. Both the normal
   * "send from composer" path and the "retry/sendDirect" path go through here.
   */
  const sendCore = useCallback(
    async (
      content: string,
      historyMessages: Message[],
      clearInput: boolean,
      revisionOpts?: { restoreRows?: Message[] }
    ) => {
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
      // Issue #685 / external review F-004: only a send that actually CREATED
      // a session may fire the binding callback — follow-up sends on an
      // existing session must not re-navigate (a session switch racing the
      // create would otherwise bounce the user back to the created id).
      let createdSession = false;

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
          createdSession = true;
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

      // Issue #685: a newly created session must be bound across ALL identity
      // mirrors (chat store, shell store, URL) before any continuation runs.
      // Invoked after the generation check so a send stopped during session
      // creation does not navigate, and only for created sessions (F-004).
      if (createdSession) {
        onSessionCreated?.(sessionId.toString());
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
      // Issue #554: accumulate provider reasoning deltas for this turn.
      // Reasoning is transient display state — it becomes a typed
      // "reasoning" part at completion and is never persisted to the
      // session rows (no backend schema change).
      let streamedReasoning = "";
      let reasoningStartedAt: number | null = null;
      let reasoningLastAt: number | null = null;
      let reasoningMetrics: { durationMs?: number; tokensEstimate?: number } | null =
        null;

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
      // streamed turn, "failed" after a mid-stream server error with partial
      // content — none of these are ever saved as a successful answer, and
      // no empty assistant row is persisted (LIVE-01); explicit Stop/pagehide
      // may persist the user row by itself. A failed batch
      // commits nothing server-side, so the visible retry below can never
      // duplicate a successful sibling write (UI-002).
      // Carry the server-issued seq onto the migrated rows: durableKeepSeq
      // (the Retry/Edit truncate anchor) treats a missing seq as "not
      // durable", so dropping it deletes earlier saved turns (#683).
      const migrateId = (oldId: string, saveResult: ChatSessionMessage) => {
        const dbId = String(saveResult.id);
        if (dbId === oldId) {
          updateMessage(oldId, { saveState: "saved", seq: saveResult.seq });
          return;
        }
        // Issue #685 (external review F-009): route the mirror rename through
        // the shared guarded helpers so the key format has one source of
        // truth; a storage exception here must never fail the durable save
        // that already succeeded server-side (best-effort mirror).
        try {
          const vote = readFeedbackVote(oldId);
          if (vote !== null) {
            writeFeedbackVote(dbId, vote);
            writeFeedbackVote(oldId, null);
          }
        } catch {
          // Helpers already guard; this is belt-and-braces for any unexpected
          // storage surface — the vote simply stays under the old key.
        }
        replaceMessageId(oldId, dbId, {
          created_at: saveResult.created_at,
          saveState: "saved",
          seq: saveResult.seq,
        });
      };

      type PersistOptions = {
        allowEmptyAssistant?: boolean;
        keepalive?: boolean;
      };

      const buildTurnPayload = (
        assistantStatus: "complete" | "interrupted" | "failed",
        allowEmptyAssistant: boolean,
      ) => {
        const storeState = useChatStore.getState();
        const assistantMsg = storeState.messagesById[assistantMessageId];
        const userMsg = storeState.messagesById[userMessage.id];
        // Abandoned stream (loadChat/newChat cleared the store): skip both
        // saves so no dangling rows land in the old session (issue #235).
        if (!assistantMsg || !userMsg) return null;

        // Stop/pagehide can run before coalesced content reaches the store.
        const assistantContent = streamedContent || assistantMsg.content;
        if (!assistantContent.trim() && !allowEmptyAssistant) {
          // LIVE-01/PRR-001: pre-content server failures are never persisted.
          updateMessage(assistantMessageId, {
            error: "The model returned an empty response. Try again.",
          });
          return null;
        }

        const messages: AddMessageRequest[] = [
          { role: "user", content, turn_id: turnId },
        ];
        if (assistantContent.trim()) {
          messages.push({
            role: "assistant",
            content: assistantContent,
            sources: assistantMsg.sources ?? undefined,
            memories: assistantMsg.memoriesUsed ?? undefined,
            wiki_refs: streamedWikiRefs.length > 0 ? streamedWikiRefs : undefined,
            kms_refs: streamedKmsRefs.length > 0 ? streamedKmsRefs : undefined,
            mode: assistantMsg.mode,
            turn_id: turnId,
            status: assistantStatus,
            citation_confidence: assistantMsg.citationConfidence,
            unverifiable_claims: assistantMsg.unverifiableClaims,
            currency_warnings: assistantMsg.currencyWarnings,
            citation_enforcement: assistantMsg.citationEnforcement,
          });
        }
        return { assistantMsg, messages };
      };

      let turnPersistenceClaimed = false;
      const claimTurnPersistence = (): boolean => {
        if (turnPersistenceClaimed) return false;
        turnPersistenceClaimed = true;
        return true;
      };

      const persistTurn = (
        assistantStatus: "complete" | "interrupted" | "failed",
        options: PersistOptions = {},
      ): Promise<void> => {
        const prepared = buildTurnPayload(
          assistantStatus,
          options.allowEmptyAssistant ?? false,
        );
        if (!prepared) return Promise.resolve();

        if (options.keepalive) {
          const persistPromise = addChatMessagesBatchKeepalive(sessionId, prepared.messages);
          const { setPendingTurnPersist, registerPendingTurnPersist } = useChatStore.getState();
          setPendingTurnPersist(persistPromise);
          registerPendingTurnPersist?.(persistPromise);
          void persistPromise.finally(() => {
            if (useChatStore.getState().pendingTurnPersist === persistPromise) {
              useChatStore.getState().setPendingTurnPersist(null);
            }
            useChatStore.getState().unregisterPendingTurnPersist?.(persistPromise);
          });
          return persistPromise;
        }

        const persistPromise = (async () => {
          const { assistantMsg, messages } = prepared;
          if (messages.length > 1) {
            updateMessage(assistantMessageId, { saveState: "saving" });
          }
          updateMessage(userMessage.id, { saveState: "saving" });
          try {
            const saved = await addChatMessagesBatch(sessionId, messages);
            const [userSaveResult, assistantSaveResult] = saved;
            if (userSaveResult) migrateId(userMessage.id, userSaveResult);
            if (assistantMsg && assistantSaveResult) {
              migrateId(assistantMessageId, assistantSaveResult);
            }
            await refreshHistory(true);
            useChatShellStore.getState().requestSessionListRefresh();
          } catch (err) {
            console.error("Failed to save chat messages:", err);
            if (messages.length > 1) {
              updateMessage(assistantMessageId, { saveState: "failed" });
            }
            updateMessage(assistantMessageId, {
              error: "Couldn't save this exchange. Retry to avoid losing it.",
            });
            updateMessage(userMessage.id, { saveState: "failed" });
          }
        })();
        // PRR-003: expose the in-flight save so revision operations can await
        // it. Issue #684 (T1-13-S2-06): also register it in the all-in-flight
        // registry — the single slot is overwritten by a newer save, but a
        // revision must wait for EVERY unsettled save.
        const { setPendingTurnPersist, registerPendingTurnPersist } = useChatStore.getState();
        setPendingTurnPersist(persistPromise);
        registerPendingTurnPersist?.(persistPromise);
        void persistPromise.finally(() => {
          if (useChatStore.getState().pendingTurnPersist === persistPromise) {
            useChatStore.getState().setPendingTurnPersist(null);
          }
          useChatStore.getState().unregisterPendingTurnPersist?.(persistPromise);
        });
        return persistPromise;
      };

      // Issue #684 (T1-13-S-08): restore the original turn a revision already
      // truncated when the replacement was admission-rejected before any
      // content — the one terminal outcome where the server provably wrote
      // nothing for the replacement (the stream route returns at the
      // admission gate, before the durable user pre-write). Re-save the rows
      // (server-side append), then mirror locally with the new ids/seqs.
      const restoreRevisedTurn = async (rows: Message[]) => {
        try {
          const payload: AddMessageRequest[] = rows.map((m) => ({
            role: m.role === "assistant" ? "assistant" : "user",
            content: typeof m.content === "string" ? m.content : "",
          }));
          const saved = await addChatMessagesBatch(sessionId, payload);
          const storeState = useChatStore.getState();
          const failedIdx = storeState.messageIds.indexOf(userMessage.id);
          if (failedIdx >= 0) storeState.removeMessagesFrom(failedIdx);
          for (const row of rows) {
            useChatStore.getState().addMessage({ ...row, saveState: "saving" });
          }
          const tailIds = useChatStore.getState().messageIds.slice(-rows.length);
          tailIds.forEach((localId, i) => {
            if (saved[i]) migrateId(localId, saved[i]);
          });
          // Mirror the ordinary persist path's sidebar bookkeeping (review
          // F-004): the restore is a real server write, so the session list
          // must reflect the new updated_at instead of going stale.
          await refreshHistory(true);
          useChatShellStore.getState().requestSessionListRefresh();
          toast.error("Couldn't regenerate — kept your original answer.");
        } catch (err) {
          console.error("Failed to restore the revised turn:", err);
          toast.error("Couldn't update conversation history");
        }
      };

      const turnPersistence: CurrentTurnPersistence = {
        persistStop: () => {
          if (!claimTurnPersistence()) return;
          updateMessage(assistantMessageId, {
            content: streamedContent,
            status: "interrupted",
            candidateSources: undefined,
          });
          currentTurnPersistenceRef.current = null;
          if (activeTurnPersistence === turnPersistence) activeTurnPersistence = null;
          void persistTurn("interrupted", { allowEmptyAssistant: true });
        },
        persistPagehide: () => {
          if (!claimTurnPersistence()) return;
          currentTurnPersistenceRef.current = null;
          if (activeTurnPersistence === turnPersistence) activeTurnPersistence = null;
          void persistTurn("interrupted", {
            allowEmptyAssistant: true,
            keepalive: true,
          });
        },
      };
      currentTurnPersistenceRef.current = turnPersistence;
      activeTurnPersistence = turnPersistence;

      const clearTurnPersistence = () => {
        if (currentTurnPersistenceRef.current === turnPersistence) {
          currentTurnPersistenceRef.current = null;
        }
        if (activeTurnPersistence === turnPersistence) {
          activeTurnPersistence = null;
        }
      };

      // Metadata filter (issue #510 AC-16): snapshot at send time from the
      // same store the Composer edits. Only non-empty fields are forwarded;
      // the whole object is omitted (undefined) when nothing is set so the
      // request body never carries an empty metadata_filter.
      const modeStoreState = useChatModeStore.getState();
      const filterTags = modeStoreState.metadataFilterTags
        .split(",")
        .map((tag) => tag.trim())
        .filter((tag) => tag.length > 0);
      const metadataFilter: ChatMetadataFilter = {};
      if (modeStoreState.metadataFilterDateFrom) {
        metadataFilter.date_from = modeStoreState.metadataFilterDateFrom;
      }
      if (modeStoreState.metadataFilterDateTo) {
        metadataFilter.date_to = modeStoreState.metadataFilterDateTo;
      }
      if (filterTags.length > 0) {
        metadataFilter.tags = filterTags;
      }
      if (modeStoreState.metadataFilterAuthor) {
        metadataFilter.author = modeStoreState.metadataFilterAuthor;
      }

      // Document scope (issue #514 AC-23): snapshot at send time — it scopes
      // exactly THIS question.
      const scopeDocumentIds = modeStoreState.scopeDocumentIds;

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
          onEvidenceCandidates: (candidates) => {
            // Late candidates from a cancelled send must never attach to a
            // newer message: only the message this stream is actively
            // streaming may receive them.
            if (sendGenRef.current !== gen) return;
            if (useChatStore.getState().streamingMessageId !== assistantMessageId) return;
            updateMessage(assistantMessageId, { candidateSources: candidates });
          },
          onReasoning: (chunk) => {
            // Issue #554: provider reasoning deltas accumulate locally and
            // become a typed part at completion — they never enter
            // message.content or the coalesced answer append.
            if (sendGenRef.current !== gen) return;
            const now = Date.now();
            if (reasoningStartedAt === null) reasoningStartedAt = now;
            reasoningLastAt = now;
            streamedReasoning += chunk;
          },
          onReasoningMetrics: (metrics) => {
            // Provider-measured reasoning accounting from the done event,
            // preferred over the locally measured fallback span below.
            if (sendGenRef.current !== gen) return;
            reasoningMetrics = metrics;
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
          // Issue #510 (AC-17 / UI-004): currency warnings and citation
          // enforcement outcome from the done event. These ARE persisted —
          // persistTurn (addChatMessagesBatch) writes them onto the
          // assistant message row via the currency_warnings /
          // citation_enforcement columns, so they re-render on session
          // reload.
          onCurrencyWarnings: (warnings) => {
            updateMessage(assistantMessageId, { currencyWarnings: warnings });
          },
          onCitationEnforcement: (enforcement) => {
            updateMessage(assistantMessageId, { citationEnforcement: enforcement });
          },
          // Issue #573 (AC2): why the provider stopped generating. "length"
          // (max_tokens truncation) is surfaced on the message so the
          // transcript can offer a Continue action. Transient display state
          // — persistTurn does not write it (no backend schema change).
          onFinishReason: (reason) => {
            updateMessage(assistantMessageId, { finishReason: reason });
          },
          onError: (error) => {
            // An orphan stream must not clean up a newer send. A pagehide save
            // leaves this generation alive so its later terminal callback can
            // still clear the UI, while a session switch/Stop bumps the token
            // and makes this callback a no-op.
            if (sendGenRef.current !== gen) return;
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
              // Shared transport-abort branch: mark stopped, never silently
              // retried, and do not persist here. Explicit Stop claims its
              // save before invoking this abort path.
              updateMessage(assistantMessageId, { candidateSources: undefined });
              clearTurnPersistence();
              setIsStreaming(false);
              setAbortFn(null);
              setStreamingMessageId(null);
              sendingRef.current = false;
              return;
            }
            const ownsPersistence = claimTurnPersistence();
            clearTurnPersistence();
            if (error.name === "ChatInterruptedError") {
              // CHAT-004: EOF before the completion marker. Mark the turn
              // retryable, keep the partial answer visible, and persist it as
              // "interrupted" — never as a successful answer.
              updateMessage(assistantMessageId, {
                status: "interrupted",
                error: "Response interrupted. You can retry.",
                candidateSources: undefined,
              });
              setCurrentStage(null);
              setIsStreaming(false);
              setAbortFn(null);
              setStreamingMessageId(null);
              sendingRef.current = false;
              if (ownsPersistence) void persistTurn("interrupted");
              return;
            }
            const isNetworkError =
              /failed to fetch|networkerror|network request failed|load failed/i.test(
                error.message
              );
            const friendlyMessage = isNetworkError
              ? "Connection lost. Check your network and try again."
              : error.message;
            // PRR-001: a server-side failure (backend error frame or generic
            // stream exception) must not lose the turn. Stamp the terminal
            // status and persist it — persistTurn's empty-content guard keeps
            // pre-content failures unpersisted (LIVE-01), while a partial
            // answer lands durably with status "failed" instead of vanishing
            // on reload.
            updateMessage(assistantMessageId, {
              status: "failed",
              error: friendlyMessage,
              candidateSources: undefined,
            });
            setCurrentStage(null);
            setIsStreaming(false);
            setAbortFn(null);
            setStreamingMessageId(null);
            sendingRef.current = false;
            if (ownsPersistence) void persistTurn("failed");
            // Issue #684 (T1-13-S-08): ADMISSION_REJECTED is emitted by the
            // route-level CHAT gate strictly BEFORE the durable user-row
            // pre-write, so this code provably means nothing was written for
            // the replacement — a revision that already truncated the
            // original turn restores it instead of losing the Q&A. Engine-side
            // admission rejections arrive AFTER the pre-write and are relabeled
            // ADMISSION_REJECTED_PREWRITTEN by the route when the pre-write
            // landed (restoring would
            // append the original after the pre-written replacement user row
            // and duplicate the question); other pre-content failures and any
            // partial-content turn keep today's behavior (the latter is
            // already durable via the save above).
            if (
              revisionOpts?.restoreRows &&
              !streamedContent.trim() &&
              (error as { code?: string }).code === "ADMISSION_REJECTED"
            ) {
              void restoreRevisedTurn(revisionOpts.restoreRows);
            }
          },
          onComplete: async () => {
            // A pagehide callback may have already claimed the one-shot save.
            // Terminal cleanup still belongs to this live generation; only the
            // durable persistence call is skipped when the claim is consumed.
            if (sendGenRef.current !== gen) return;
            const ownsPersistence = claimTurnPersistence();
            clearTurnPersistence();
            // Flush any buffered streaming content before reading store state
            // (UI-PERF-2): rAF-batched appends may not have fired yet when the
            // stream completes, so synchronously drain the buffer to avoid
            // persisting truncated content (the tail would be lost otherwise).
            flush();
            if (assistantMessageIdRef.current !== null) {
              updateMessage(assistantMessageId, { content: streamedContent });
            }
            // The done event delivered the final sources — the streaming-only
            // candidate preview is obsolete.
            updateMessage(assistantMessageId, { candidateSources: undefined });
            // Issue #554: if the model reasoned, attach the typed reasoning
            // part so the collapsible "Thinking for Ns" block renders.
            // Duration/tokens prefer the provider-measured values from the
            // done event; the locally measured span and a chars//4 estimate
            // are the fallback when the backend did not report them. Parts
            // are transient display state — persistTurn never writes them.
            if (streamedReasoning.length > 0) {
              const durationMs =
                reasoningMetrics?.durationMs ??
                (reasoningStartedAt !== null && reasoningLastAt !== null
                  ? reasoningLastAt - reasoningStartedAt
                  : 0);
              const tokensEstimate =
                reasoningMetrics?.tokensEstimate ??
                Math.max(1, Math.floor(streamedReasoning.length / 4));
              updateMessage(assistantMessageId, {
                parts: [
                  {
                    kind: "reasoning",
                    text: streamedReasoning,
                    durationMs,
                    tokensEstimate,
                  },
                ],
              });
            }
            setCurrentStage(null);
            setIsStreaming(false);
            setAbortFn(null);
            setStreamingMessageId(null);
            sendingRef.current = false;
            if (ownsPersistence) await persistTurn("complete");
          },
        },
        activeVaultId ?? undefined,
        effectiveMode,
        useChatModeStore.getState().temperature,
        useChatModeStore.getState().retrievalMode,
        useChatModeStore.getState().citationMode,
        Object.keys(metadataFilter).length > 0 ? metadataFilter : undefined,
        scopeDocumentIds ?? undefined,
        // Issue #553: opt this turn into the server-side durable write — the
        // server pre-writes the user row (status "pending") before the first
        // token and finalizes the assistant row under this turn_id, so a
        // proxy drop, tab close, or crash never loses the turn. persistTurn
        // (still called at the terminal events below) becomes an idempotent
        // reconcile against those rows.
        { sessionId, turnId },
      );

      // The scope applied to this question only — it is consumed once the
      // message has been sent so it never leaks into a later question.
      if (scopeDocumentIds) {
        useChatModeStore.getState().clearScopeDocumentIds();
      }

      // Wrap the raw abort so any caller that aborts the stream — the Stop
      // button OR a session switch routed through the store (loadChat/newChat) —
      // also clears the hook-local in-flight guard. Without this, aborting via
      // navigation would leave sendingRef stuck true and block the next send.
      // Stop-path candidate clearing lives in stopStreaming, which owns the
      // message write in that flow.
      setAbortFn(() => {
        abort();
        clearTurnPersistence();
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
      onSessionCreated,
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
    async (
      content: string,
      historyMessages: Message[],
      opts?: { restoreRows?: Message[] }
    ) => {
      const { isStreaming: currentIsStreaming } = useChatStore.getState();
      if (currentIsStreaming || sendingRef.current) return;
      await sendCore(content, historyMessages, false, opts);
    },
    [sendCore]
  );

  const handleStop = useCallback(() => {
    // Invalidate the in-flight generation BEFORE touching the store: a Stop
    // pressed during session creation has no stream to abort, and without
    // this bump the send would start generating once creation resolved.
    sendGenRef.current += 1;
    // Issue #685 (PRR-001): the mounted instance may be a REMOUNT (PageShell
    // keys page content by pathname, so the first-send navigate replaces the
    // hook) whose ref never saw the in-flight turn — the module slot still
    // holds it, so Stop persists the interrupted turn across the remount.
    const persistence = currentTurnPersistenceRef.current ?? activeTurnPersistence;
    persistence?.persistStop();
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

  useEffect(() => {
    const handlePagehide = () => {
      // Issue #685 (PRR-001): fall back to the module slot — a PageShell
      // remount replaces this instance before pagehide, and the surviving
      // handle (installed by the pre-remount send) must still fire.
      const persistence = currentTurnPersistenceRef.current ?? activeTurnPersistence;
      persistence?.persistPagehide();
    };
    window.addEventListener("pagehide", handlePagehide);
    return () => window.removeEventListener("pagehide", handlePagehide);
  }, []);

  return { handleSend, handleStop, handleKeyDown, handleInputChange, sendDirect, currentStage };
}

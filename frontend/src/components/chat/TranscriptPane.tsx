// frontend/src/components/chat/TranscriptPane.tsx

import { useRef, useEffect, useState, useCallback, memo } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Sparkles,
  Database,
  ArrowDown,
  AlignLeft,
  GitCompare,
  ListChecks,
  Quote,
  AlertTriangle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { MessageBubble } from "./MessageBubble";
import { AssistantMessage } from "./AssistantMessage";
import { WaitingIndicator } from "./WaitingIndicator";
import { StageIndicator } from "./StageIndicator";
import { Composer } from "./Composer";
import {
  useChatStore,
  useMessageIds,
  useMessage,
  useStreamingMessageContentLength,
} from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useAuthStore } from "@/stores/useAuthStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useSendMessage } from "@/hooks/useSendMessage";
import { useChatHistory } from "@/hooks/useChatHistory";
import { forkChatSession, truncateChatSession } from "@/lib/api";
import { mapSessionMessage } from "@/lib/chatMessageMapper";
import { toast } from "sonner";
import type { Message } from "@/stores/useChatStore";

// =============================================================================
// Types
// =============================================================================

interface TranscriptPaneProps {
  className?: string;
}

interface EmptyTranscriptProps {
  onPromptClick: (prompt: string) => void;
  hasIndexedDocs: boolean;
  onNavigateToDocuments?: () => void;
  vaultName?: string | null;
  documentCount?: number;
}

// =============================================================================
// Constants
// =============================================================================

const SUGGESTED_PROMPTS = [
  { text: "Summarize the uploaded documents with citations", Icon: AlignLeft },
  { text: "Find contradictions or conflicts across sources", Icon: GitCompare },
  { text: "Create an action-item list from the documents", Icon: ListChecks },
  { text: "Show the strongest evidence for the main conclusion", Icon: Quote },
];

// =============================================================================
// EmptyTranscript
// =============================================================================

export function EmptyTranscript({
  onPromptClick,
  hasIndexedDocs,
  onNavigateToDocuments,
  vaultName,
  documentCount,
}: EmptyTranscriptProps) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-4 py-16" role="region" aria-label="Empty transcript">
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35 }}
        className="flex max-w-md flex-col items-center text-center"
      >
        <div className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10 border border-border" aria-hidden>
          <Sparkles className="h-8 w-8 text-primary" />
        </div>

        <h2 className="mb-2 text-lg font-semibold text-foreground">
          {hasIndexedDocs ? "What would you like to know?" : "Upload documents to get started"}
        </h2>

        {hasIndexedDocs && vaultName && (
          <p className="mb-2 text-xs text-muted-foreground">
            {documentCount && documentCount > 0
              ? `Searching ${documentCount} document${documentCount === 1 ? "" : "s"} in `
              : "Searching "}
            <span className="font-medium text-foreground/80">{vaultName}</span>
          </p>
        )}

        <p className="mb-8 text-sm text-muted-foreground">
          {hasIndexedDocs
            ? "Select a prompt below or type your own question."
            : "Add documents to your vault to start chatting."}
        </p>

        {hasIndexedDocs ? (
          <div className="grid w-full grid-cols-1 gap-2.5 sm:grid-cols-2" role="list" aria-label="Suggested prompts">
            {SUGGESTED_PROMPTS.map((prompt, i) => (
              <button
                key={i}
                onClick={() => onPromptClick(prompt.text)}
                className="group flex items-start gap-3 rounded-xl border border-border bg-card p-4 text-left transition-all duration-200 hover:border-primary/30 hover:bg-accent/5 hover:-translate-y-0.5 hover:shadow-xs focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={`Use prompt: ${prompt.text}`}
              >
                <prompt.Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground group-hover:text-primary transition-colors" aria-hidden />
                <span className="text-sm font-medium">{prompt.text}</span>
              </button>
            ))}
          </div>
        ) : (
          <Button onClick={onNavigateToDocuments} className="gap-2" aria-label="Go to documents page">
            <Database className="h-4 w-4" aria-hidden />
            Go to Documents
          </Button>
        )}
      </motion.div>
    </div>
  );
}

// =============================================================================
// MessageRow — granular subscriber per message to avoid full-list re-renders
// =============================================================================

interface MessageRowProps {
  messageId: string;
  isLast: boolean;
  isStreaming: boolean;
  streamingMessageId: string | null;
  userInitial: string;
  activeSessionId: string | null;
  showDebug: boolean;
  highlightedId: string | null;
  currentStage: string | null;
  onRetry: () => void;
  onEdit: (messageId: string, content: string) => void;
  onFork?: (messageId: string) => void;
  onFeedback: (messageId: string, feedback: "up" | "down" | null) => void;
}

const MessageRow = memo(function MessageRow({
  messageId,
  isLast,
  isStreaming,
  streamingMessageId,
  userInitial,
  activeSessionId,
  showDebug,
  highlightedId,
  currentStage,
  onRetry,
  onEdit,
  onFork,
  onFeedback,
}: MessageRowProps) {
  const message = useMessage(messageId);
  if (!message) return null;

  // Coerce types for safety
  const safeMessage: Message = {
    ...message,
    id: String(message.id ?? messageId),
    role: (message.role === "user" || message.role === "assistant") ? message.role : "user",
    content: typeof message.content === "string" ? message.content : String(message.content ?? ""),
  };

  const isAssistantStreaming = isStreaming && isLast && safeMessage.role === "assistant" && streamingMessageId === messageId;
  const isHighlighted = highlightedId === messageId;

  // Persisted terminal status (issue #507): an interrupted/partial/failed
  // turn must never look like a successful answer after reload. The
  // live-stream error paths already stamp `error` on the message (which
  // renders its own retry banner inside AssistantMessage), so this banner
  // targets the restored rows — status set, no error string. Never shown
  // while a stream is active.
  const showInterruptedStatusBanner =
    safeMessage.role === "assistant" &&
    !isStreaming &&
    !safeMessage.error &&
    (safeMessage.status === "interrupted" ||
      safeMessage.status === "partial" ||
      safeMessage.status === "failed");

  return (
    <div className={isHighlighted ? "ring-2 ring-primary/50 rounded-xl transition-all duration-500" : undefined}>
      {safeMessage.role === "assistant" ? (
        <>
          <AnimatePresence mode="wait">
            {isAssistantStreaming && !safeMessage.content && currentStage ? (
              <StageIndicator key="stage" stage={currentStage as "Searching" | "Reading" | "Drafting"} />
            ) : isAssistantStreaming && !safeMessage.content ? (
              <WaitingIndicator key="waiting" />
            ) : (
              <AssistantMessage
                key="message"
                message={safeMessage}
                isStreaming={isAssistantStreaming}
                showDebug={showDebug}
                onRetry={onRetry}
                onFork={onFork ? () => onFork(messageId) : undefined}
                sessionId={String(activeSessionId ?? "")}
                messageFeedback={safeMessage.feedback}
                onFeedback={(fb) => onFeedback(messageId, fb)}
              />
            )}
          </AnimatePresence>
          {showInterruptedStatusBanner && (
            <div
              className="mt-3 flex items-start gap-2 rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2"
              role="status"
              data-interrupted-status={safeMessage.status}
            >
              <AlertTriangle className="h-4 w-4 shrink-0 text-amber-500 mt-0.5" aria-hidden />
              <div className="min-w-0">
                <p className="text-xs font-medium text-amber-700 dark:text-amber-300">
                  {safeMessage.status === "partial"
                    ? "Response is incomplete — you can retry."
                    : safeMessage.status === "failed"
                      ? "Response failed — you can retry."
                      : "Response interrupted — you can retry."}
                </p>
                {/* Retry only makes sense for the latest exchange — the shared
                    handleRetry trims from the last user message. */}
                {isLast && (
                  <Button
                    variant="link"
                    size="sm"
                    className="h-auto p-0 mt-1 text-amber-700 dark:text-amber-300 text-xs"
                    onClick={onRetry}
                  >
                    Retry
                  </Button>
                )}
              </div>
            </div>
          )}
        </>
      ) : (
        <MessageBubble
          message={safeMessage}
          isStreaming={isAssistantStreaming}
          isEditDisabled={isStreaming}
          onFork={onFork ? () => onFork(messageId) : undefined}
          userInitial={userInitial}
          onEdit={onEdit}
        />
      )}
    </div>
  );
});

// =============================================================================
// TranscriptPane
// =============================================================================

export function TranscriptPane({ className }: TranscriptPaneProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const navigate = useNavigate();

  const messageIds = useMessageIds();
  const { isStreaming, streamingMessageId, setInput, removeMessagesFrom, updateMessage, loadChat } = useChatStore();

  const { getActiveVault } = useVaultStore();
  const activeVault = getActiveVault();
  const vaultId = useVaultStore((s) => s.activeVaultId);

  const authUser = useAuthStore((s) => s.user);
  const userInitial = (authUser?.full_name || authUser?.username || "U")[0].toUpperCase();

  const activeSessionId = useChatShellStore((s) => s.activeSessionId);

  const { refreshHistory } = useChatHistory(vaultId);
  const { handleSend, handleStop, sendDirect, currentStage } = useSendMessage(vaultId, refreshHistory);

  const [showScrollButton, setShowScrollButton] = useState(false);
  // setIsAtBottom is retained for legacy components that read isAtBottom via
  // refs higher up the tree; the auto-scroll logic itself uses isAtBottomRef
  // exclusively to avoid stale closures.
  const [, setIsAtBottom] = useState(true);
  const showDebug = import.meta.env.DEV;
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null);
  const [isForking, setIsForking] = useState(false);
  const isForkingRef = useRef(false);

  // Ref-backed pinned-bottom state — read inside scroll callbacks without
  // creating stale closures over isAtBottom (which is captured by useEffect).
  const isAtBottomRef = useRef(true);
  // User intent flag: once the user manually scrolls up, we stop auto-scroll
  // until they click "New messages" or reach the bottom themselves.
  const userScrolledUpRef = useRef(false);

  // Reactive token-length selector. Recomputes when streaming content grows;
  // does NOT subscribe to the full message body or sources/feedback fields.
  const streamingContentLength = useStreamingMessageContentLength();

  const hasIndexedDocs = activeVault ? activeVault.file_count > 0 : false;

  /**
   * Centralized auto-scroll using normal document flow.
   * No virtualizer — just scroll the container to its bottom.
   */
  const scrollToBottomNow = useCallback(
    (behavior: ScrollBehavior = "auto") => {
      if (messageIds.length === 0) return;
      const el = scrollRef.current;
      if (!el) return;
      el.scrollTo({ top: el.scrollHeight, behavior });
    },
    [messageIds.length]
  );

  // New-message auto-scroll: triggered when messageIds.length changes.
  useEffect(() => {
    if (messageIds.length === 0) return;
    if (!isAtBottomRef.current || userScrolledUpRef.current) return;
    scrollToBottomNow("auto");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messageIds.length]);

  // Token-growth auto-scroll: triggered when the active streaming message's
  // content length grows. ``messageIds.length`` does not change during
  // streaming, so the previous count-based effect missed every chunk.
  useEffect(() => {
    if (streamingContentLength === 0) return;
    if (!isAtBottomRef.current || userScrolledUpRef.current) return;
    scrollToBottomNow("auto");
  }, [streamingContentLength, scrollToBottomNow]);

  // After streaming completes, dynamic content (markdown headings, code
  // blocks, source cards) re-renders and changes height. Re-pin to the
  // bottom one more time if the user is still pinned.
  const wasStreamingRef = useRef(isStreaming);
  useEffect(() => {
    const wasStreaming = wasStreamingRef.current;
    wasStreamingRef.current = isStreaming;
    if (wasStreaming && !isStreaming) {
      if (isAtBottomRef.current && !userScrolledUpRef.current) {
        // Wait for the post-stream re-render to settle (source cards, etc).
        const t = setTimeout(() => scrollToBottomNow("auto"), 50);
        return () => clearTimeout(t);
      }
    }
  }, [isStreaming, scrollToBottomNow]);

  // Single evidence:jump-to-answer listener
  useEffect(() => {
    const handler = (e: Event) => {
      const { sourceId, messageId } = (
        e as CustomEvent<{ sourceId: string; messageId?: string | null }>
      ).detail;
      const { messageIds: ids, messagesById } = useChatStore.getState();
      const citesSource = (id: string) =>
        messagesById[id]?.sources?.some((s) => s.id === sourceId) ?? false;
      // Prefer the message the evidence selection came from when it still
      // exists and actually cites the source; otherwise fall back to the
      // first message citing it (persisted/forked selections have no anchor).
      let msgId: string | null = null;
      if (messageId && messagesById[messageId] && citesSource(messageId)) {
        msgId = messageId;
      } else {
        const idx = ids.findIndex((id) => citesSource(id));
        if (idx >= 0) msgId = ids[idx];
      }
      if (msgId) {
        const el = scrollRef.current?.querySelector(`[data-message-id="${msgId}"]`);
        el?.scrollIntoView({ behavior: "smooth", block: "center" });
        setHighlightedMessageId(msgId);
        setTimeout(() => setHighlightedMessageId(null), 1500);
      }
    };
    window.addEventListener("evidence:jump-to-answer", handler);
    return () => window.removeEventListener("evidence:jump-to-answer", handler);
  }, []);

  // Page title — updates whenever active session title changes
  const activeSessionTitle = useChatShellStore((s) => s.activeSessionTitle);
  useEffect(() => {
    document.title = activeSessionTitle ? `${activeSessionTitle} — RAGApp` : "RAGApp";
    return () => { document.title = "RAGApp"; };
  }, [activeSessionTitle]);

  // Auto-focus composer on mount
  useEffect(() => {
    if (!document.querySelector('[role="dialog"], [role="alertdialog"]')) {
      composerRef.current?.focus();
    }
  }, []);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = dist < 150;
    // Refs first so other effects firing in the same tick see fresh state.
    const wasAtBottom = isAtBottomRef.current;
    isAtBottomRef.current = atBottom;
    if (atBottom) {
      // Reaching the bottom resets the manual-scroll-up sentinel so
      // streaming auto-scroll resumes.
      userScrolledUpRef.current = false;
    } else if (wasAtBottom) {
      // User just scrolled up from a pinned position — hold auto-scroll.
      userScrolledUpRef.current = true;
    }
    setIsAtBottom(atBottom);
    setShowScrollButton(!atBottom);
  };

  const scrollToBottom = () => {
    // Explicit user request: reset the manual-scroll sentinel and pin.
    userScrolledUpRef.current = false;
    isAtBottomRef.current = true;
    setIsAtBottom(true);
    setShowScrollButton(false);
    scrollToBottomNow("smooth");
  };

  const handlePromptClick = (prompt: string) => {
    setInput(prompt);
    setTimeout(() => composerRef.current?.focus(), 0);
  };

  // PRR-003: let a just-interrupted turn's background save settle before any
  // server-side history revision, so the truncate/fork below observes (or
  // removes) those rows instead of racing them.
  const awaitPendingPersist = useCallback(async () => {
    const pending = useChatStore.getState().pendingTurnPersist;
    if (pending) {
      try {
        await pending;
      } catch {
        // Save failures are already surfaced on the message (UI-002).
      }
    }
  }, []);

  // PRR-020: anchor the server truncate at the highest durable seq among the
  // KEPT rows instead of a local array index — a local index diverges from
  // server seq whenever a turn exists locally but was never persisted (Stop,
  // empty response, failed save), so a positional keep_count would delete the
  // wrong range and duplicate Q&A pairs after reload.
  const durableKeepSeq = (keptIds: string[], byId: Record<string, Message>): number => {
    let keepSeq = 0;
    for (const id of keptIds) {
      const seq = byId[id]?.seq;
      if (typeof seq === "number" && seq > keepSeq) keepSeq = seq;
    }
    return keepSeq;
  };

  // Retry: find last user message, trim persisted history + store, call sendDirect
  const handleRetry = useCallback(async () => {
    if (isStreaming) return;
    await awaitPendingPersist();
    const { messageIds: ids, messagesById, activeChatId } = useChatStore.getState();
    let lastUserIdx = -1;
    for (let i = ids.length - 1; i >= 0; i--) {
      if (messagesById[ids[i]]?.role === "user") { lastUserIdx = i; break; }
    }
    if (lastUserIdx < 0) return;

    // Trim the persisted history first so the server matches the local trim;
    // on failure bail out before touching the local transcript.
    if (activeChatId) {
      try {
        await truncateChatSession(
          parseInt(activeChatId),
          durableKeepSeq(ids.slice(0, lastUserIdx), messagesById)
        );
      } catch {
        toast.error("Couldn't update conversation history");
        return;
      }
    }

    const userContent = messagesById[ids[lastUserIdx]].content;
    const history = ids.slice(0, lastUserIdx).map((id) => messagesById[id]);
    removeMessagesFrom(lastUserIdx);
    sendDirect(userContent, history);
  }, [isStreaming, removeMessagesFrom, sendDirect, awaitPendingPersist]);

  // Edit: trim persisted history + store from message index, restore content to composer
  const handleEdit = useCallback(async (messageId: string, content: string) => {
    if (isStreaming) return;
    await awaitPendingPersist();
    const { messageIds: ids, messagesById, activeChatId } = useChatStore.getState();
    const idx = ids.indexOf(messageId);
    if (idx < 0) return;
    if (activeChatId) {
      try {
        await truncateChatSession(
          parseInt(activeChatId),
          durableKeepSeq(ids.slice(0, idx), messagesById)
        );
      } catch {
        toast.error("Couldn't update conversation history");
        return;
      }
    }
    removeMessagesFrom(idx);
    setInput(content);
    composerRef.current?.focus();
  }, [isStreaming, removeMessagesFrom, setInput, awaitPendingPersist]);

  // Fork
  const handleFork = useCallback(async (messageId: string) => {
    if (isForkingRef.current) return;
    const { activeChatId } = useChatStore.getState();
    if (!activeChatId) return;
    await awaitPendingPersist();
    const { messageIds: ids } = useChatStore.getState();
    const msgIndex = ids.indexOf(messageId);
    if (msgIndex < 0) return;

    isForkingRef.current = true;
    setIsForking(true);
    try {
      const forked = await forkChatSession(parseInt(activeChatId), msgIndex);
      if (!forked.messages?.length) {
        toast.warning("Fork returned no messages — staying on the current session.");
        return;
      }
      const forkMessages = forked.messages.map(mapSessionMessage);
      loadChat(String(forked.id), forkMessages);
      await refreshHistory();
      navigate(`/chat/${forked.id}`);
    } catch (err) {
      console.error("Fork failed:", err);
      toast.error("Failed to branch conversation. Please try again.");
    } finally {
      isForkingRef.current = false;
      setIsForking(false);
    }
  }, [loadChat, refreshHistory, navigate, awaitPendingPersist]);

  const handleFeedback = useCallback((messageId: string, feedback: "up" | "down" | null) => {
    updateMessage(messageId, { feedback });
  }, [updateMessage]);

  return (
    <div className={cn("flex h-full flex-col", className)}>
      {/* Message list */}
      <div className="relative flex-1 min-h-0 overflow-hidden">
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="h-full overflow-y-auto scroll-smooth chat-scrollbar"
          aria-label="Chat messages"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
        >
          {/* Width-constrained column */}
          <div className="mx-auto w-full max-w-[760px] px-2 sm:px-4">
            {messageIds.length === 0 ? (
              <EmptyTranscript
                onPromptClick={handlePromptClick}
                hasIndexedDocs={hasIndexedDocs}
                onNavigateToDocuments={() => navigate("/documents")}
                vaultName={activeVault?.name ?? null}
                documentCount={activeVault?.file_count}
              />
            ) : (
              <div className="py-2">
                {messageIds.map((msgId, idx) => (
                  <div key={msgId} data-message-id={msgId}>
                    <MessageRow
                      messageId={msgId}
                      isLast={idx === messageIds.length - 1}
                      isStreaming={isStreaming}
                      streamingMessageId={streamingMessageId}
                      userInitial={userInitial}
                      activeSessionId={activeSessionId}
                      showDebug={showDebug}
                      highlightedId={highlightedMessageId}
                      currentStage={currentStage}
                      onRetry={handleRetry}
                      onEdit={handleEdit}
                      onFork={isForking ? undefined : handleFork}
                      onFeedback={handleFeedback}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Scroll to bottom button */}
        <AnimatePresence>
          {showScrollButton && (
            <motion.div
              initial={{ opacity: 0, scale: 0.85 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.85 }}
              transition={{ duration: 0.12 }}
              className="absolute bottom-4 left-1/2 -translate-x-1/2"
            >
              <Button
                variant="secondary"
                size="sm"
                onClick={scrollToBottom}
                className="h-8 gap-1.5 rounded-full px-3 shadow-lg border border-border"
                aria-label="Scroll to bottom"
              >
                <ArrowDown className="h-3.5 w-3.5" />
                <span className="text-xs">New messages</span>
              </Button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Composer */}
      <div className="border-t border-border bg-background/80 backdrop-blur-xs p-3 sm:p-4">
        <div className="mx-auto w-full max-w-[760px]">
          <Composer
            onSend={handleSend}
            onStop={handleStop}
            isStreaming={isStreaming}
            inputRef={composerRef}
          />
        </div>
      </div>
    </div>
  );
}

// Export for external consumption
export { Composer };
export type { TranscriptPaneProps };

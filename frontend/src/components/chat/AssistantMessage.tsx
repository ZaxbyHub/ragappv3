// frontend/src/components/chat/AssistantMessage.tsx
import { useState, useMemo, useCallback } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Bot, AlertCircle, Sparkles, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Message } from "@/stores/useChatStore";
import type { Source } from "@/lib/api";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { MarkdownMessage, parseCitationSegments } from "./MarkdownMessage";
import { SourceCards } from "./SourceCards";
import { MemoryCards } from "./MemoryCards";
import { WikiCards } from "./WikiCards";
import { KMSCards } from "./KMSCards";
import { AssistantMessageActions } from "./MessageActions";

// Re-export for backwards compat with tests
export { parseCitationSegments as parseCitations };

interface AssistantMessageProps {
  message: Message;
  isStreaming?: boolean;
  showDebug?: boolean;
  onSourceClick?: (source: Source) => void;
  onViewAllSources?: () => void;
  onCopy?: () => void;
  onRetry?: () => void;
  onDebugToggle?: (isActive: boolean) => void;
  feedback?: "up" | "down" | null;
  onFeedback?: (feedback: "up" | "down" | null) => void;
  onFork?: () => void;
  sessionId?: string;
  messageFeedback?: "up" | "down" | null;
}

export function AssistantMessage({
  message,
  isStreaming = false,
  showDebug,
  onSourceClick,
  onViewAllSources,
  onCopy,
  onRetry,
  onDebugToggle,
  feedback: externalFeedback,
  onFeedback,
  onFork,
  sessionId,
  messageFeedback,
}: AssistantMessageProps) {
  const [isDebugActive, setIsDebugActive] = useState(false);
  const { openRightPane, setSelectedEvidenceSource, setActiveRightTab } = useChatShellStore();
  const prefersReducedMotion = useReducedMotion();

  // Derive cited sources, memories, wiki refs, and KMS refs for evidence cards
  const { citedSources, citedMemories, citedWikis, citedKms } = useMemo(
    () =>
      parseCitationSegments(
        message.content,
        message.sources,
        message.memoriesUsed,
        message.wikiRefs,
        message.kmsRefs
      ),
    [message.content, message.sources, message.memoriesUsed, message.wikiRefs, message.kmsRefs]
  );

  const handleSourceClick = useCallback(
    (source: Source) => {
      setSelectedEvidenceSource(source);
      setActiveRightTab("evidence");
      openRightPane();
      onSourceClick?.(source);
    },
    [setSelectedEvidenceSource, setActiveRightTab, openRightPane, onSourceClick]
  );

  const handleViewAll = useCallback(() => {
    setActiveRightTab("evidence");
    openRightPane();
    onViewAllSources?.();
  }, [setActiveRightTab, openRightPane, onViewAllSources]);

  const handleDebugToggle = useCallback(() => {
    const next = !isDebugActive;
    setIsDebugActive(next);
    onDebugToggle?.(next);
  }, [isDebugActive, onDebugToggle]);

  // Source cards: show ONLY sources explicitly cited as [S#] in the answer.
  // Do NOT fall back to all sources — uncited sources must not appear as evidence.
  const sourcesForCards = citedSources;
  // Memory cards: show ONLY memories explicitly cited as [M#] in the answer.
  const memoriesForCards = citedMemories;
  // Wiki cards: show ONLY wiki refs explicitly cited as [W#] in the answer.
  const wikiRefsForCards = citedWikis;
  // KMS cards: show ONLY KMS refs explicitly cited as [K#] in the answer.
  const kmsRefsForCards = citedKms;

  return (
    <motion.div
      initial={prefersReducedMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={prefersReducedMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
      transition={{ duration: prefersReducedMotion ? 0.1 : 0.25 }}
      className="group flex gap-3 px-4 py-5"
      role="article"
      aria-label="Assistant message"
    >
      {/* Avatar */}
      <div
        className="flex-shrink-0 w-7 h-7 mt-0.5 rounded-full flex items-center justify-center bg-primary/10 text-primary"
        aria-hidden
      >
        <Bot className="h-3.5 w-3.5" />
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 max-w-[68ch]">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">Assistant</span>
          {message.mode === "thinking" && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300 text-[10px] font-semibold tracking-wide"
              title="Generated with the Thinking model"
              aria-label="Thinking model"
            >
              <Sparkles className="h-3 w-3" aria-hidden />
              Thinking
            </span>
          )}
          {message.mode === "instant" && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300 text-[10px] font-semibold tracking-wide"
              title="Generated with the Instant model"
              aria-label="Instant model"
            >
              <Zap className="h-3 w-3" aria-hidden />
              Instant
            </span>
          )}
        </div>

        {/* Markdown body */}
        <MarkdownMessage
          content={message.content}
          sources={message.sources}
          memories={message.memoriesUsed}
          wikiRefs={message.wikiRefs}
          kmsRefs={message.kmsRefs}
          isStreaming={isStreaming}
          onCitationClick={handleSourceClick}
          citedSources={citedSources}
          citationConfidence={message.citationConfidence}
          unverifiableClaims={message.unverifiableClaims}
        />

        {/* Wiki cards — compiled knowledge cited as [W#] */}
        {!isStreaming && wikiRefsForCards.length > 0 && (
          <WikiCards wikiRefs={wikiRefsForCards} />
        )}

        {/* KMS cards — user-curated knowledge cited as [K#] */}
        {!isStreaming && kmsRefsForCards.length > 0 && (
          <KMSCards kmsRefs={kmsRefsForCards} />
        )}

        {/* Source cards — shown below the message body */}
        {!isStreaming && sourcesForCards.length > 0 && (
          <SourceCards
            sources={sourcesForCards}
            onSourceClick={handleSourceClick}
            onViewAll={handleViewAll}
          />
        )}

        {/* Memory cards — distinct from document sources */}
        {!isStreaming && memoriesForCards.length > 0 && (
          <MemoryCards memories={memoriesForCards} />
        )}

        {/* Error */}
        {message.error && (
          <div className="mt-3 flex items-start gap-2 rounded-sm bg-destructive/10 border border-destructive/20 p-3">
            <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0 mt-0.5" aria-hidden />
            <div className="min-w-0">
              <p className="text-sm font-medium text-destructive">Error</p>
              <p className="text-xs text-destructive/80 mt-0.5">{message.error}</p>
              {onRetry && (
                <Button variant="link" size="sm" className="text-destructive text-xs h-auto p-0 mt-2" onClick={onRetry}>
                  Try again →
                </Button>
              )}
            </div>
          </div>
        )}

        {/* Stopped */}
        {message.stopped && !message.error && (
          <div className="mt-3 inline-flex items-center gap-2 rounded-sm bg-muted border border-border px-3 py-1.5">
            <span className="text-xs font-medium text-muted-foreground">Stopped</span>
          </div>
        )}

        {/* Action bar */}
        {!isStreaming && (
          <AssistantMessageActions
            content={message.content}
            onRetry={onRetry}
            onFork={onFork}
            onDebugToggle={handleDebugToggle}
            isDebugActive={isDebugActive}
            showDebug={showDebug}
            messageId={message.id}
            sessionId={sessionId}
            externalFeedback={externalFeedback}
            serverFeedback={messageFeedback}
            onFeedback={onFeedback}
            onCopy={onCopy}
          />
        )}

        {/* Debug panel */}
        {import.meta.env.DEV && isDebugActive && (
          <div className="mt-3 p-3 rounded-sm bg-muted border text-xs font-mono">
            <div className="text-muted-foreground mb-1">Debug Info:</div>
            <div>Message ID: {message.id}</div>
            <div>Sources: {message.sources?.length ?? 0}</div>
            <div>Cited: {citedSources.length}</div>
            <div>Length: {message.content.length} chars</div>
          </div>
        )}
      </div>
    </motion.div>
  );
}

export default AssistantMessage;

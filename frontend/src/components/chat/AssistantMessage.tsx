// frontend/src/components/chat/AssistantMessage.tsx
import { useState, useMemo, useCallback, useEffect, useImperativeHandle, useRef } from "react";
import type { RefObject } from "react";
import { useNavigate } from "react-router-dom";
import { motion, useReducedMotion } from "framer-motion";
import { Bot, AlertCircle, Sparkles, Zap } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import type { Message } from "@/stores/useChatStore";
import type { Source } from "@/lib/api";
import {
  createCanvasArtifact,
  mapFenceLanguageToExtension,
  type SourceRef,
} from "@/lib/api/canvas";
import { useCanvasCapabilities } from "@/hooks/useCanvasCapabilities";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { MarkdownMessage, parseCitationSegments } from "./MarkdownMessage";
import { SourceCards } from "./SourceCards";
import { MemoryCards } from "./MemoryCards";
import { WikiCards } from "./WikiCards";
import { KMSCards } from "./KMSCards";
import { AssistantMessageActions, stripCitations } from "./MessageActions";

// Re-export for backwards compat with tests
export { parseCitationSegments as parseCitations };

// ============================================================================
// Canvas entry points (issue #509)
// ============================================================================

const CANVAS_NAME_MAX_CHARS = 60;

/** Derives a short artifact name from a language/kind prefix and the first
 * non-empty line of the content (capped at ~60 chars). */
export function buildCanvasArtifactName(prefix: string | null, content: string): string {
  const firstLine =
    content
      .split("\n")
      .map((line) => line.trim())
      .find((line) => line.length > 0) ?? "";
  const suffix = firstLine.length > 40 ? `${firstLine.slice(0, 40)}…` : firstLine;
  const name = prefix && suffix ? `${prefix}: ${suffix}` : prefix ?? suffix;
  const trimmed = name.trim();
  return trimmed === "" ? "Untitled canvas" : trimmed.slice(0, CANVAS_NAME_MAX_CHARS);
}

function toNumericId(value: string | undefined): number | null {
  if (value == null || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function toCanvasSourceRefs(sources: Source[] | undefined): SourceRef[] {
  return (sources ?? []).map((source) => ({
    source_id: source.id ?? null,
    title: source.filename ?? null,
  }));
}

interface CanvasEntryHandle {
  openCode(code: string, language: string): void;
  openDocument(): void;
}

/**
 * Null-rendering bridge that owns the canvas capability query and the router
 * navigation. Mounted by AssistantMessage ONLY when both the session id and
 * message id are valid — messages rendered without a session id (tests,
 * legacy renders) never mount it, so it adds no provider requirements for
 * existing callers. The capability query fails closed: the parent renders the
 * canvas buttons only once this bridge reports `enabled === true`.
 */
function CanvasEntryPoints({
  sessionId,
  messageId,
  content,
  sources,
  entryRef,
  onEnabledChange,
}: {
  sessionId: number;
  messageId: number;
  content: string;
  sources: Source[] | undefined;
  entryRef: RefObject<CanvasEntryHandle | null>;
  onEnabledChange: (enabled: boolean) => void;
}) {
  const capabilitiesQuery = useCanvasCapabilities();
  const navigate = useNavigate();
  // Fail-closed: undefined while loading/error maps to false.
  const enabled = capabilitiesQuery.data?.enabled === true;

  useEffect(() => {
    onEnabledChange(enabled);
    return () => onEnabledChange(false);
  }, [enabled, onEnabledChange]);

  const openCanvas = useCallback(
    async (payload: { kind: "code" | "document"; name: string; language: string | null; content: string }) => {
      try {
        const result = await createCanvasArtifact(sessionId, {
          ...payload,
          message_id: messageId,
          source_refs: toCanvasSourceRefs(sources),
        });
        toast.success("Opened in canvas");
        navigate(`/chat/${sessionId}/canvas/${result.artifact.artifact_uid}`);
      } catch {
        toast.error("Couldn't open canvas");
      }
    },
    [sessionId, messageId, sources, navigate]
  );

  const contentRef = useRef(content);
  contentRef.current = content;

  useImperativeHandle(
    entryRef,
    () => ({
      openCode: (code: string, language: string) => {
        if (!enabled) return;
        // Display name keeps the fence language; the stored `language` is the
        // backend's extension key ("py", not "python" — contract delta #2).
        void openCanvas({
          kind: "code",
          name: buildCanvasArtifactName(language || null, code),
          language: mapFenceLanguageToExtension(language),
          content: code,
        });
      },
      openDocument: () => {
        if (!enabled) return;
        const clean = stripCitations(contentRef.current);
        void openCanvas({
          kind: "document",
          name: buildCanvasArtifactName(null, clean),
          language: "md",
          content: clean,
        });
      },
    }),
    [enabled, openCanvas]
  );

  return null;
}

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
  const {
    openRightPane,
    setSelectedEvidenceSource,
    setSelectedEvidenceMessageId,
    setEvidenceReturnFocusId,
    setActiveRightTab,
  } = useChatShellStore();
  const prefersReducedMotion = useReducedMotion();

  // Canvas entry points (issue #509). The capability query and navigation
  // live in the CanvasEntryPoints bridge, mounted only when both ids are
  // valid; `canvasEnabled` stays false until that bridge reports the backend
  // capability enabled (fail-closed while loading/error).
  const sessionIdNum = toNumericId(sessionId);
  const messageIdNum = toNumericId(message.id);
  const canvasEntryRef = useRef<CanvasEntryHandle | null>(null);
  const [canvasEnabled, setCanvasEnabled] = useState(false);
  const handleCanvasEnabledChange = useCallback((enabled: boolean) => setCanvasEnabled(enabled), []);
  const canvasIdsReady = sessionIdNum != null && messageIdNum != null;

  const handleOpenCodeInCanvas = useCallback((code: string, language: string) => {
    canvasEntryRef.current?.openCode(code, language);
  }, []);

  const handleOpenDocumentInCanvas = useCallback(() => {
    canvasEntryRef.current?.openDocument();
  }, []);

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
      // Anchor the selection to THIS message so jumps resolve here even when
      // a later answer cites the same chunk, and focus can return to its chip.
      setSelectedEvidenceMessageId(message.id);
      setEvidenceReturnFocusId(message.id);
      setActiveRightTab("evidence");
      openRightPane();
      onSourceClick?.(source);
    },
    [
      setSelectedEvidenceSource,
      setSelectedEvidenceMessageId,
      setEvidenceReturnFocusId,
      setActiveRightTab,
      openRightPane,
      onSourceClick,
      message.id,
    ]
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
  // Validated citation labels: the done event's citation_confidence carries
  // one entry per VALID [S#] label, so its keys are the valid-label set.
  // Undefined when the validator produced nothing — cards then show no badge.
  // An empty object means the same thing, so it maps to undefined too (an
  // empty set would otherwise flip every card to a "Retrieved" badge).
  const validCitationLabels =
    message.citationConfidence && Object.keys(message.citationConfidence).length > 0
      ? new Set(Object.keys(message.citationConfidence))
      : undefined;
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
        className="shrink-0 w-7 h-7 mt-0.5 rounded-full flex items-center justify-center bg-primary/10 text-primary"
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
          messageId={message.id}
          canvas={
            canvasEnabled && canvasIdsReady && sessionId != null
              ? {
                  sessionId,
                  // message.id is a store string; canvasIdsReady guarantees
                  // toNumericId(message.id) parsed to a positive integer, and
                  // the bridge re-validates before any API call sends it.
                  messageId: message.id,
                  onOpen: handleOpenCodeInCanvas,
                }
              : undefined
          }
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
            validCitationLabels={validCitationLabels}
          />
        )}

        {/* Memory cards — distinct from document sources */}
        {!isStreaming && memoriesForCards.length > 0 && (
          <MemoryCards memories={memoriesForCards} />
        )}

        {/* Error */}
        {message.error && (
          <div className="mt-3 flex items-start gap-2 rounded-sm bg-destructive/10 border border-destructive/20 p-3">
            <AlertCircle className="h-4 w-4 text-destructive shrink-0 mt-0.5" aria-hidden />
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
            onOpenDocumentInCanvas={
              canvasEnabled && canvasIdsReady ? handleOpenDocumentInCanvas : undefined
            }
          />
        )}

        {/* Canvas entry-point bridge — owns the capability query and the
            post-create navigation; renders nothing itself. */}
        {canvasIdsReady && (
          <CanvasEntryPoints
            sessionId={sessionIdNum as number}
            messageId={messageIdNum as number}
            content={message.content}
            sources={message.sources}
            entryRef={canvasEntryRef}
            onEnabledChange={handleCanvasEnabledChange}
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

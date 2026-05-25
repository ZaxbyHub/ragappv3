import { lazy, Suspense, useState, useMemo, useCallback, useEffect, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { FileText, Table, Code, ExternalLink, BookOpen, Layers, Loader2 } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import {
  type Message,
  useChatStore,
  useLastCompletedAssistantSources,
  useLastCompletedAssistantWikiRefs,
  useLastCompletedAssistantKmsRefs,
  useLastUserContent,
  useSourcesForSourceId,
  useCompletedAssistantMessageIdsKey,
  parseCompletedAssistantIds,
} from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { getChunkContext, getDocumentRawBlob, type ChunkContextResponse, type Source } from "@/lib/api";
import { WikiCards } from "./WikiCards";
import { KMSCards } from "./KMSCards";
import { getRelevanceLabel, type ScoreType } from "@/lib/relevance";
import { EmptyState } from "@/components/shared/EmptyState";

const PdfPreview = lazy(() => import("@/components/preview/PdfPreview"));

interface StructuredOutput {
  id: string;
  type: "code" | "table";
  title: string;
  content: string;
  size: number;
}

/**
 * Escape regex special characters in a string
 */
function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Highlight query terms in text by wrapping matches in <mark> elements
 */
function highlightQueryTerms(text: string, query: string): React.ReactNode[] {
  if (!query.trim()) {
    return [text];
  }

  const terms = query
    .split(/\s+/)
    .filter((term) => term.length > 0)
    .map((term) => escapeRegex(term));

  if (terms.length === 0) {
    return [text];
  }

  const pattern = new RegExp(`(${terms.join("|")})`, "gi");
  const parts = text.split(pattern);

  return parts.map((part, index) => {
    if (terms.some((term) => part.toLowerCase() === term.toLowerCase())) {
      return (
        <mark
          key={index}
          className="bg-amber-200/80 dark:bg-amber-500/30 rounded px-0.5"
          aria-label={`Search match: ${part}`}
        >
          {part}
        </mark>
      );
    }
    return part;
  });
}

/**
 * Extract structured outputs (code blocks and tables) from assistant messages
 */
function extractStructuredOutputs(messages: Message[]): StructuredOutput[] {
  const outputs: StructuredOutput[] = [];

  for (const message of messages) {
    if (message.role !== "assistant") continue;

    const content = message.content;
    if (!content) continue;

    // Extract code blocks (```...```)
    const codeBlockRegex = /```(?:(\w+)?\n)?([\s\S]*?)```/g;
    let codeMatch;
    while ((codeMatch = codeBlockRegex.exec(content)) !== null) {
      const language = codeMatch[1] || "";
      const codeContent = codeMatch[2].trim();
      const lines = codeContent.split("\n");
      const firstLine = lines[0] || "Code Block";
      const title = language
        ? `${language.charAt(0).toUpperCase() + language.slice(1)} Block`
        : firstLine.length > 30
          ? firstLine.slice(0, 30) + "..."
          : firstLine;

      // Generate a simple hash-based ID
      const id = `code-${codeContent.slice(0, 50).replace(/\s/g, "").slice(0, 20)}`;

      outputs.push({
        id,
        type: "code",
        title: title || "Code Block",
        content: codeContent,
        size: lines.length,
      });
    }

    // Extract markdown tables (lines starting with |)
    const lines = content.split("\n");
    let inTable = false;
    let tableStart = 0;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      const isTableLine = line.startsWith("|");

      if (isTableLine && !inTable) {
        inTable = true;
        tableStart = i;
      } else if (!isTableLine && inTable) {
        // Table ended
        const tableLines = lines.slice(tableStart, i);
        // Filter out separator lines (like |---|---|)
        const contentLines = tableLines.filter(
          (l) => !/^\|[-:|\s]+\|$/.test(l.trim())
        );
        const tableContent = tableLines.join("\n");
        const firstDataLine = contentLines[0] || "";
        const cells = firstDataLine
          .split("|")
          .filter((c) => c.trim())
          .map((c) => c.trim());
        const title =
          cells.slice(0, 2).join(" | ").slice(0, 40) || "Table";

        // Generate a simple hash-based ID
        const id = `table-${tableContent.slice(0, 50).replace(/\s/g, "").slice(0, 20)}`;

        outputs.push({
          id,
          type: "table",
          title: title.length > 40 ? title.slice(0, 40) + "..." : title,
          content: tableContent,
          size: contentLines.length,
        });

        inTable = false;
      }
    }

    // Handle table that goes to end of content
    if (inTable) {
      const tableLines = lines.slice(tableStart);
      const contentLines = tableLines.filter(
        (l) => !/^\|[-:|\s]+\|$/.test(l.trim())
      );
      const tableContent = tableLines.join("\n");
      const firstDataLine = contentLines[0] || "";
      const cells = firstDataLine
        .split("|")
        .filter((c) => c.trim())
        .map((c) => c.trim());
      const title =
        cells.slice(0, 2).join(" | ").slice(0, 40) || "Table";

      const id = `table-${tableContent.slice(0, 50).replace(/\s/g, "").slice(0, 20)}`;

      outputs.push({
        id,
        type: "table",
        title: title.length > 40 ? title.slice(0, 40) + "..." : title,
        content: tableContent,
        size: contentLines.length,
      });
    }
  }

  return outputs;
}

interface SourceListItemProps {
  source: Source;
  index: number;
  isSelected: boolean;
  scoreType?: ScoreType;
  onClick: () => void;
}

function SourceListItem({
  source,
  index,
  isSelected,
  scoreType,
  onClick,
}: SourceListItemProps) {
  const relevance =
    source.score !== undefined
      ? getRelevanceLabel(source.score, scoreType)
      : null;

  return (
    <button
      id={`evidence-source-${source.id}`}
      onClick={onClick}
      className={cn(
        "w-full text-left p-3 rounded-lg border transition-all",
        "hover:bg-accent/50 hover:border-accent",
        isSelected
          ? "bg-primary/10 border-primary ring-2 ring-primary/30"
          : "bg-card border-border"
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className="flex-shrink-0 min-w-[1.5rem] h-6 px-1 rounded-full bg-primary/10 flex items-center justify-center text-xs font-medium text-primary"
          aria-label={`Source label ${
            source.source_label && source.source_label.trim()
              ? source.source_label
              : `S${index + 1}`
          }`}
        >
          {source.source_label && source.source_label.trim()
            ? source.source_label
            : `S${index + 1}`}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <FileText className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
            <span className="text-sm font-medium truncate">
              {source.filename}
            </span>
          </div>
          {source.section && (
            <div className="text-[11px] text-muted-foreground/80 mt-0.5 truncate">
              <span className="font-medium">§</span> {source.section}
            </div>
          )}
          {source.snippet && (
            <div className="text-xs text-muted-foreground mt-0.5 line-clamp-2">
              {source.snippet}
            </div>
          )}
          {relevance && (
            <div className={cn("text-xs mt-1", relevance.color)}>
              {relevance.text}
            </div>
          )}
        </div>
      </div>
    </button>
  );
}

interface SourcePreviewProps {
  source: Source;
  query: string;
  onJumpToAnswer: () => void;
}

function SourcePreview({ source, query, onJumpToAnswer }: SourcePreviewProps) {
  const [chunkContext, setChunkContext] = useState<ChunkContextResponse | null>(null);
  const [isLoadingContext, setIsLoadingContext] = useState(false);
  const [contextError, setContextError] = useState<string | null>(null);
  const [documentPreview, setDocumentPreview] = useState<{
    url: string;
    isPdf: boolean;
    filename: string;
  } | null>(null);
  const [isLoadingDocument, setIsLoadingDocument] = useState(false);
  const [documentError, setDocumentError] = useState<string | null>(null);
  const documentAbortRef = useRef<AbortController | null>(null);
  const previewContent = chunkContext?.context_text || source.snippet || "";
  const highlightedContent = useMemo(
    () => highlightQueryTerms(previewContent, query),
    [previewContent, query]
  );
  const sourceFileId = source.file_id;
  const sourcePageNumber =
    typeof source.page_number === "number"
      ? source.page_number
      : typeof source.metadata?.page_number === "number"
        ? source.metadata.page_number
        : null;

  useEffect(() => {
    let cancelled = false;

    setChunkContext(null);
    setContextError(null);
    if (!source.id) return;

    setIsLoadingContext(true);
    getChunkContext(source.id)
      .then((context) => {
        if (!cancelled) setChunkContext(context);
      })
      .catch((err) => {
        if (!cancelled) {
          setContextError(err instanceof Error ? err.message : "Could not load source context");
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoadingContext(false);
      });

    return () => {
      cancelled = true;
    };
  }, [source.id]);

  useEffect(() => {
    setDocumentError(null);
    setIsLoadingDocument(false);
    documentAbortRef.current?.abort();
    documentAbortRef.current = null;
    setDocumentPreview((current) => {
      if (current) URL.revokeObjectURL(current.url);
      return null;
    });
  }, [source.id]);

  useEffect(() => {
    return () => {
      documentAbortRef.current?.abort();
      if (documentPreview) URL.revokeObjectURL(documentPreview.url);
    };
  }, [documentPreview]);

  const handleOpenDocument = useCallback(async () => {
    if (!sourceFileId) return;

    documentAbortRef.current?.abort();
    const controller = new AbortController();
    documentAbortRef.current = controller;
    setIsLoadingDocument(true);
    setDocumentError(null);

    try {
      const blob = await getDocumentRawBlob(sourceFileId, controller.signal);
      if (controller.signal.aborted || documentAbortRef.current !== controller) return;
      const nextUrl = URL.createObjectURL(blob);
      const contentType = blob.type.toLowerCase();
      const isPdf =
        contentType.includes("pdf") || source.filename.toLowerCase().endsWith(".pdf");
      setDocumentPreview((current) => {
        if (current) URL.revokeObjectURL(current.url);
        return { url: nextUrl, isPdf, filename: source.filename };
      });
    } catch (err) {
      if (controller.signal.aborted) return;
      setDocumentError(
        err instanceof Error ? err.message : "Could not open original document"
      );
    } finally {
      if (!controller.signal.aborted) {
        setIsLoadingDocument(false);
        documentAbortRef.current = null;
      }
    }
  }, [source.filename, sourceFileId]);

  return (
    <div className="flex flex-col h-full min-h-0 gap-4">
      <div className="flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <FileText className="h-4 w-4 text-primary" />
          <h3 className="font-semibold">{source.filename}</h3>
        </div>
        <div className="flex items-center gap-1">
          {sourceFileId && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleOpenDocument}
              disabled={isLoadingDocument}
            >
              {isLoadingDocument ? (
                <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
              ) : (
                <FileText className="h-3.5 w-3.5 mr-1" />
              )}
              Open document
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={onJumpToAnswer}>
            <ExternalLink className="h-3.5 w-3.5 mr-1" />
            Jump to answer
          </Button>
        </div>
      </div>

      {(source.snippet || chunkContext?.context_source || isLoadingContext || contextError || documentError) && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground flex-shrink-0">
          {isLoadingContext && (
            <span className="inline-flex items-center gap-1">
              <Loader2 className="h-3 w-3 animate-spin" />
              Loading context
            </span>
          )}
          {chunkContext?.context_source && (
            <span className="rounded border border-border px-1.5 py-0.5">
              {chunkContext.context_source === "parent_window" ? "Document context" : "Chunk context"}
            </span>
          )}
          {contextError && (
            <span className="text-warning-foreground">{contextError}</span>
          )}
          {documentError && (
            <span className="text-warning-foreground">{documentError}</span>
          )}
          {source.snippet && (
            <span className="min-w-0 flex-1 truncate italic">
              {source.snippet}
            </span>
          )}
        </div>
      )}

      {source.score !== undefined && source.score_type && (
        <div className="flex items-center gap-2 text-sm flex-shrink-0">
          <span className="text-muted-foreground">Relevance:</span>
          <span className={getRelevanceLabel(source.score, source.score_type).color}>
            {getRelevanceLabel(source.score, source.score_type).text}
          </span>
        </div>
      )}

      {documentPreview ? (
        <div className="flex-1 min-h-0">
          <Suspense
            fallback={
              <div className="flex h-full items-center justify-center rounded-md border text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Loading viewer
              </div>
            }
          >
            <PdfPreview
              blobUrl={documentPreview.url}
              filename={documentPreview.filename}
              isPdf={documentPreview.isPdf}
              pageNumber={sourcePageNumber}
            />
          </Suspense>
        </div>
      ) : (
        <ScrollArea className="flex-1 min-h-0 rounded-md border p-4">
          <div className="text-sm leading-relaxed whitespace-pre-wrap">
            {previewContent ? highlightedContent : "No preview content available."}
          </div>
        </ScrollArea>
      )}
    </div>
  );
}

interface StructuredOutputItemProps {
  output: StructuredOutput;
}

function StructuredOutputItem({ output }: StructuredOutputItemProps) {
  const Icon = output.type === "code" ? Code : Table;
  const label = output.type === "code" ? "Code" : "Table";

  return (
    <div className="p-3 rounded-lg border bg-card hover:bg-accent/50 transition-colors">
      <div className="flex items-center gap-2 mb-2">
        <Icon className="h-4 w-4 text-primary" />
        <span className="text-xs font-medium text-muted-foreground uppercase">
          {label}
        </span>
      </div>
      <div className="font-medium text-sm truncate">{output.title}</div>
      <div className="text-xs text-muted-foreground mt-1">
        {output.size} {output.type === "code" ? "lines" : "rows"}
      </div>
    </div>
  );
}

export function RightPane() {
  // Granular subscriptions — explicitly avoid useChatMessages() so streaming
  // token growth on the active assistant message does NOT re-render this
  // component or rerun the structured-output extraction. Each selector below
  // ignores the streaming message and recomputes only when its own slice
  // changes.
  const lastCompletedSources = useLastCompletedAssistantSources();
  const lastCompletedWikiRefs = useLastCompletedAssistantWikiRefs();
  const lastCompletedKmsRefs = useLastCompletedAssistantKmsRefs();
  const query = useLastUserContent();
  const completedAssistantIdsKey = useCompletedAssistantMessageIdsKey();
  const { selectedEvidenceSource, setSelectedEvidenceSource, activeRightTab, setActiveRightTab } = useChatShellStore();
  const sourcesForSelected = useSourcesForSourceId(selectedEvidenceSource?.id);
  const [selectedSource, setSelectedSource] = useState<Source | null>(null);
  const [activeTab, setActiveTab] = useState<string>("sources");

  // Sync with store - when selectedEvidenceSource changes, update local state and switch to evidence tab
  useEffect(() => {
    if (selectedEvidenceSource) {
      setSelectedSource(selectedEvidenceSource);
    }
  }, [selectedEvidenceSource]);

  // Sync active tab with store
  useEffect(() => {
    if (activeRightTab === "evidence") {
      setActiveTab("sources");
    } else if (activeRightTab === "preview") {
      setActiveTab("preview");
    } else if (activeRightTab === "wiki") {
      setActiveTab("wiki");
    }
  }, [activeRightTab]);

  // Sources to show: the parent message of the clicked citation when one is
  // selected, otherwise the most recent completed assistant message's sources.
  const sources = useMemo<Source[]>(() => {
    const list = selectedEvidenceSource ? sourcesForSelected : lastCompletedSources;
    if (!list) return [];
    return list.filter((s): s is Source => s != null);
  }, [selectedEvidenceSource, sourcesForSelected, lastCompletedSources]);

  // Extract structured outputs only from completed assistant messages.
  // Reading messagesById outside the React reactive system here is safe
  // because the dependency list (completedAssistantIds) already triggers
  // recompute when a message completes, and structured outputs are
  // exclusively derived from finished content.
  const structuredOutputs = useMemo(() => {
    const store = useChatStore.getState();
    const ids = parseCompletedAssistantIds(completedAssistantIdsKey);
    const completedMessages: Message[] = ids
      .map((id) => store.messagesById[id])
      .filter((m): m is Message => Boolean(m));
    return extractStructuredOutputs(completedMessages);
  }, [completedAssistantIdsKey]);

  const sourcesScrollRef = useRef<HTMLDivElement>(null);
  const shouldVirtualizeSources = sources.length > 20;

  const sourcesVirtualizer = useVirtualizer({
    count: sources.length,
    getScrollElement: () => shouldVirtualizeSources ? sourcesScrollRef.current : null,
    estimateSize: () => 80,
    overscan: 5,
    measureElement: (el) => el?.getBoundingClientRect().height ?? 0,
  });

  // Scroll the selected source into view when it changes. This makes the
  // inline citation pill (in the chat transcript) jump the user directly to
  // the matching source card here in the right pane.
  // - For the non-virtualized path (<=20 sources) every card is in the DOM,
  //   so `scrollIntoView` works directly.
  // - For the virtualized path (>20 sources) the target row may not be
  //   rendered yet, so tell the virtualizer to scroll to its index first.
  //   The virtualizer will then mount the row, at which point a second
  //   `scrollIntoView` fine-tunes the final position.
  useEffect(() => {
    if (!selectedEvidenceSource) return;
    const idx = sources.findIndex((s) => s.id === selectedEvidenceSource.id);
    const scrollToElement = () => {
      const el = document.getElementById(`evidence-source-${selectedEvidenceSource.id}`);
      if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    if (shouldVirtualizeSources && idx >= 0) {
      sourcesVirtualizer.scrollToIndex(idx, { align: "center" });
      const raf = requestAnimationFrame(scrollToElement);
      return () => cancelAnimationFrame(raf);
    }
    const timeout = setTimeout(scrollToElement, 0);
    return () => clearTimeout(timeout);
  }, [selectedEvidenceSource, sources, shouldVirtualizeSources, sourcesVirtualizer]);

  const handleSourceClick = useCallback((source: Source) => {
    setSelectedSource(source);
    setSelectedEvidenceSource(source);
    setActiveTab("preview");
    setActiveRightTab("preview");
  }, [setSelectedEvidenceSource, setActiveRightTab]);

  const handleJumpToAnswer = useCallback(() => {
    // Dispatch custom event for the transcript pane to handle
    if (selectedSource) {
      window.dispatchEvent(
        new CustomEvent("evidence:jump-to-answer", {
          detail: { sourceId: selectedSource.id },
        })
      );
    }
  }, [selectedSource]);

  const hasSources = sources.length > 0;
  const hasStructuredOutputs = structuredOutputs.length > 0;
  const hasWikiRefs = (lastCompletedWikiRefs?.length ?? 0) > 0;
  const hasKmsRefs = (lastCompletedKmsRefs?.length ?? 0) > 0;
  const tabCount = 3 + (hasWikiRefs ? 1 : 0) + (hasKmsRefs ? 1 : 0);
  const tabsGridCols =
    tabCount >= 5 ? "grid-cols-5" : tabCount === 4 ? "grid-cols-4" : "grid-cols-3";

  return (
    <div className="flex h-full flex-col">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
          Evidence
        </h2>
      </div>

      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="flex-1 flex flex-col min-h-0"
      >
        <TabsList className={`grid w-full flex-shrink-0 ${tabsGridCols}`}>
          <TabsTrigger value="sources">
            Sources
            {hasSources && (
              <span className="ml-1.5 text-xs text-muted-foreground">
                ({sources.length})
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="preview" disabled={!selectedSource}>
            Preview
          </TabsTrigger>
          <TabsTrigger value="extracted" disabled={!hasStructuredOutputs}>
            Extracted
            {hasStructuredOutputs && (
              <span className="ml-1.5 text-xs text-muted-foreground">
                ({structuredOutputs.length})
              </span>
            )}
          </TabsTrigger>
          {hasWikiRefs && (
            <TabsTrigger value="wiki">
              Wiki
              <span className="ml-1.5 text-xs text-muted-foreground">
                ({lastCompletedWikiRefs!.length})
              </span>
            </TabsTrigger>
          )}
          {hasKmsRefs && (
            <TabsTrigger value="kms">
              Knowledge
              <span className="ml-1.5 text-xs text-muted-foreground">
                ({lastCompletedKmsRefs!.length})
              </span>
            </TabsTrigger>
          )}
        </TabsList>

        <TabsContent value="sources" className="flex-1 min-h-0 mt-4">
          {!hasSources ? (
            <EmptyState
              icon={BookOpen}
              title="No sources yet"
              description="Send a message to see retrieved sources."
            />
          ) : shouldVirtualizeSources ? (
            <div
              ref={sourcesScrollRef}
              className="h-full overflow-y-auto"
              aria-label="Sources list"
              role="list"
            >
              <div style={{ height: sourcesVirtualizer.getTotalSize(), position: 'relative', paddingRight: '1rem' }}>
                {sourcesVirtualizer.getVirtualItems().map((virtualItem) => {
                  const source = sources[virtualItem.index];
                  return (
                    <div
                      key={source.id}
                      data-index={virtualItem.index}
                      ref={sourcesVirtualizer.measureElement}
                      style={{
                        position: 'absolute',
                        top: virtualItem.start,
                        left: 0,
                        width: '100%',
                        paddingTop: '0.5rem',
                      }}
                    >
                      <SourceListItem
                        source={source}
                        index={virtualItem.index}
                        isSelected={selectedSource?.id === source.id || selectedEvidenceSource?.id === source.id}
                        scoreType={source.score_type}
                        onClick={() => handleSourceClick(source)}
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          ) : (
            <ScrollArea className="h-full">
              <div className="space-y-2 pr-4">
                {sources.map((source, index) => (
                  <SourceListItem
                    key={source.id}
                    source={source}
                    index={index}
                    isSelected={selectedSource?.id === source.id || selectedEvidenceSource?.id === source.id}
                    scoreType={source.score_type}
                    onClick={() => handleSourceClick(source)}
                  />
                ))}
              </div>
            </ScrollArea>
          )}
        </TabsContent>

        <TabsContent value="preview" className="flex-1 min-h-0 mt-4">
          {!selectedSource ? (
            <EmptyState
              icon={FileText}
              title="No preview available"
              description="Select a source to preview it here."
            />
          ) : (
            <SourcePreview
              source={selectedSource}
              query={query}
              onJumpToAnswer={handleJumpToAnswer}
            />
          )}
        </TabsContent>

        <TabsContent value="extracted" className="flex-1 min-h-0 mt-4">
          <ScrollArea className="h-full">
            {!hasStructuredOutputs ? (
              <EmptyState
                icon={Layers}
                title="Nothing extracted yet"
                description="Extracted content will appear here."
              />
            ) : (
              <div className="space-y-2 pr-4">
                {structuredOutputs.map((output) => (
                  <StructuredOutputItem key={output.id} output={output} />
                ))}
              </div>
            )}
          </ScrollArea>
        </TabsContent>

        {hasWikiRefs && (
          <TabsContent value="wiki" className="flex-1 min-h-0 mt-4">
            <ScrollArea className="h-full">
              <div className="pr-4">
                <WikiCards wikiRefs={lastCompletedWikiRefs!} />
              </div>
            </ScrollArea>
          </TabsContent>
        )}

        {hasKmsRefs && (
          <TabsContent value="kms" className="flex-1 min-h-0 mt-4">
            <ScrollArea className="h-full">
              <div className="pr-4">
                <KMSCards kmsRefs={lastCompletedKmsRefs!} />
              </div>
            </ScrollArea>
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}

export default RightPane;

import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { FileText, ChevronDown, ChevronRight, BookOpen } from "lucide-react";
import type { Source } from "@/lib/api";
import { getRelevanceLabel } from "@/lib/relevance";
import { FileIcon } from "@/lib/fileIcon";

interface SourcesPanelProps {
  sources: Source[] | undefined;
  expandedSources: Set<string>;
  onToggleSource: (sourceId: string) => void;
}

export function SourcesPanel({
  sources,
  expandedSources,
  onToggleSource,
}: SourcesPanelProps) {
  const hasSources = !!(sources && sources.length > 0);

  return (
    <>
      {/* Desktop Sources Panel */}
      <Card className="h-full hidden lg:flex lg:flex-col">
        <DesktopSourcesContent
          sources={sources}
          hasSources={hasSources}
          expandedSources={expandedSources}
          onToggleSource={onToggleSource}
        />
      </Card>

      {/* Mobile Sources Accordion */}
      <div className="lg:hidden">
        {hasSources && (
          <MobileSourcesAccordion
            sources={sources}
            expandedSources={expandedSources}
            onToggleSource={onToggleSource}
          />
        )}
      </div>
    </>
  );
}

// Desktop Sources Content
interface DesktopSourcesContentProps {
  sources: Source[] | undefined;
  hasSources: boolean;
  expandedSources: Set<string>;
  onToggleSource: (sourceId: string) => void;
}

function DesktopSourcesContent({
  sources,
  hasSources,
  expandedSources,
  onToggleSource,
}: DesktopSourcesContentProps) {
  return (
    <>
      <CardHeader>
        <CardTitle className="text-lg">Sources</CardTitle>
        <CardDescription>
          {hasSources
            ? `${sources!.length} source(s) for the latest response`
            : "Sources will appear here after the AI responds"}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex-1 min-h-0 p-0">
        {hasSources ? (
          <ScrollArea className="h-full pr-4">
            <div className="space-y-3 p-4" role="list" aria-label="Document sources">
              {sources!.map((source: Source, index: number) => (
                <SourceCard
                  key={source.id}
                  source={source}
                  index={index}
                  isExpanded={expandedSources.has(source.id)}
                  onToggle={() => onToggleSource(source.id)}
                />
              ))}
            </div>
          </ScrollArea>
        ) : (
          <EmptySourcesState />
        )}
      </CardContent>
    </>
  );
}

// Mobile Sources Accordion
interface MobileSourcesAccordionProps {
  sources: Source[] | undefined;
  expandedSources: Set<string>;
  onToggleSource: (sourceId: string) => void;
}

function MobileSourcesAccordion({
  sources,
  expandedSources,
  onToggleSource,
}: MobileSourcesAccordionProps) {
  return (
    <Accordion type="single" collapsible defaultValue="sources">
      <AccordionItem value="sources" className="border rounded-sm bg-card">
        <AccordionTrigger className="px-4 py-3 hover:no-underline">
          <div className="flex items-center gap-2">
            <BookOpen className="w-4 h-4 text-muted-foreground" />
            <span className="text-sm font-medium">
              Sources ({sources!.length})
            </span>
          </div>
        </AccordionTrigger>
        <AccordionContent className="px-4 pb-4">
          <div className="space-y-3" role="list" aria-label="Document sources">
            {sources!.map((source: Source, index: number) => (
              <SourceCard
                key={source.id}
                source={source}
                index={index}
                isExpanded={expandedSources.has(source.id)}
                onToggle={() => onToggleSource(source.id)}
              />
            ))}
          </div>
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  );
}

// Source Card Component
interface SourceCardProps {
  source: Source;
  isExpanded: boolean;
  onToggle: () => void;
  index?: number;
}

function SourceCard({ source, isExpanded, onToggle, index }: SourceCardProps) {
  return (
    <Card className="border-border/50">
      <button
        type="button"
        className="w-full p-3 cursor-pointer hover:bg-muted/50 transition-colors text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
        onClick={onToggle}
        aria-expanded={isExpanded}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <FileIcon filename={source.filename} className="w-4 h-4 flex-shrink-0" />
            <span className="text-sm font-medium truncate lg:max-w-[180px] max-w-[200px]">
              {source.filename}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {source.score !== undefined && (
              <Badge
                variant="secondary"
                className={`text-xs ${getRelevanceLabel(source.score, source.score_type).color}`}
              >
                #{index !== undefined ? index + 1 : "?"} {getRelevanceLabel(source.score, source.score_type).text}
              </Badge>
            )}
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 text-muted-foreground" />
            ) : (
              <ChevronRight className="w-4 h-4 text-muted-foreground" />
            )}
          </div>
        </div>
      </button>
      {isExpanded && (
        <div className="px-3 pb-3">
          <div className="pt-2 border-t border-border/50">
            <p className="text-xs text-muted-foreground whitespace-pre-wrap">
              {source.snippet || "No content available"}
            </p>
          </div>
        </div>
      )}
    </Card>
  );
}

// Empty Sources State
function EmptySourcesState() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[120px] flex-1 text-center">
      <FileText className="w-8 h-8 text-muted-foreground/50 mb-2" />
      <p className="text-sm text-muted-foreground">No sources available</p>
      <p className="text-xs text-muted-foreground/70 mt-1">
        Ask a question to see relevant sources
      </p>
    </div>
  );
}

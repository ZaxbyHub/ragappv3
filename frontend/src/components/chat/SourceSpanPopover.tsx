// frontend/src/components/chat/SourceSpanPopover.tsx
import { FileText } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { CitationConfidence } from "./CitationConfidence";
import type { Source } from "@/lib/api";
import { cn } from "@/lib/utils";

interface SourceSpanPopoverProps {
  /** The source to display in the popover */
  source: Source;
  /** The citation label (e.g., "S1") */
  label: string;
  /** Confidence score for this citation (0-1), optional */
  confidence?: number;
  /** Whether the popover is open */
  open?: boolean;
  /** Callback when open state changes */
  onOpenChange?: (open: boolean) => void;
  /** Additional trigger content (usually the citation chip) */
  children: React.ReactNode;
}

const MAX_SNIPPET_DISPLAY = 300;

/**
 * Popover that shows source document metadata and snippet when a citation is clicked.
 * Used for SC-006 (source span inspection).
 */
export function SourceSpanPopover({
  source,
  label,
  confidence,
  open,
  onOpenChange,
  children,
}: SourceSpanPopoverProps) {
  const snippet = source.snippet ?? "";
  const displaySnippet =
    snippet.length > MAX_SNIPPET_DISPLAY
      ? snippet.slice(0, MAX_SNIPPET_DISPLAY) + "\u2026"
      : snippet;

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        className="w-80 max-h-64 overflow-y-auto"
        onOpenAutoFocus={(e) => { e.preventDefault(); void e; }}
      >
        <div className="flex flex-col gap-2">
          {/* Header: label + confidence */}
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 text-sm font-semibold text-foreground">
              <FileText className="h-4 w-4 flex-shrink-0 text-muted-foreground" aria-hidden />
              <span className="truncate">{source.filename}</span>
              <span className="text-muted-foreground font-normal">{label}</span>
            </div>
            {confidence !== undefined && confidence !== null && (
              <CitationConfidence score={confidence} />
            )}
          </div>

          {/* Metadata row (section, page, etc.) */}
          {(source.section || source.page_number !== undefined) && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              {source.section && (
                <span className="truncate">
                  <span className="font-medium">Section:</span> {source.section}
                </span>
              )}
              {source.page_number !== undefined && source.page_number !== null && (
                <span>
                  <span className="font-medium">Page:</span> {source.page_number}
                </span>
              )}
            </div>
          )}

          {/* Source type badge */}
          {source.evidence_type && (
            <div>
              <span
                className={cn(
                  "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                  source.evidence_type === "primary"
                    ? "border-primary/40 bg-primary/10 text-primary"
                    : "border-muted bg-muted/50 text-muted-foreground"
                )}
              >
                {source.evidence_type}
              </span>
            </div>
          )}

          {/* Snippet */}
          {displaySnippet && (
            <div className="mt-1 rounded-sm border bg-muted/30 p-2">
              <p className="text-xs leading-relaxed text-foreground whitespace-pre-wrap">
                {displaySnippet}
              </p>
            </div>
          )}

          {/* No snippet fallback */}
          {!displaySnippet && (
            <p className="text-xs text-muted-foreground italic">
              No preview available for this source.
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

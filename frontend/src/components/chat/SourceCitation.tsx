import { FileText } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { Source } from "@/lib/api";

interface SourceCitationProps {
  source: Source;
  /** 0-based display index */
  index: number;
  onClick: () => void;
  /**
   * "inline" — compact number pill for inline prose use.
   * "strip"  — full chip with file icon, used in evidence strip / source cards.
   */
  variant?: "inline" | "strip";
}

export function SourceCitation({ source, index, onClick, variant = "strip" }: SourceCitationProps) {
  // Display label tracks the source's stable [S#] when assigned so sparse
  // citations (e.g. only S2 and S4 cited) keep their original numbering
  // instead of being renumbered to 1/2 by display order.
  const displayLabel =
    source.source_label && source.source_label.trim()
      ? source.source_label
      : `S${index + 1}`;
  const label = `Source ${displayLabel}: ${source.filename}`;

  if (variant === "inline") {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={onClick}
              className={cn(
                "inline-flex items-center justify-center align-middle mx-0.5 cursor-pointer select-none",
                "min-w-[20px] h-[20px] px-1 rounded-sm",
                "text-[10px] font-semibold leading-none",
                "bg-primary/10 text-primary hover:bg-primary/20 active:scale-95",
                "border border-primary/20 hover:border-primary/35 transition-colors duration-150",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
                "[@media(pointer:coarse)]:min-w-[26px] [@media(pointer:coarse)]:h-[26px]"
              )}
              aria-label={label}
            >
              {displayLabel}
            </button>
          </TooltipTrigger>
          <TooltipContent side="top">
            <p className="max-w-[200px] truncate">{source.filename}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={onClick}
            className={cn(
              "inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs",
              "bg-primary/10 text-primary hover:bg-primary/20 active:scale-95 transition-all duration-150",
              "border border-primary/20 hover:border-primary/35 hover:shadow-sm"
            )}
            aria-label={label}
          >
            <FileText className="h-3 w-3 flex-shrink-0" />
            <span className="truncate max-w-[120px]">{source.filename}</span>
          </button>
        </TooltipTrigger>
        {source.snippet && (
          <TooltipContent className="max-w-[250px] text-xs">
            <p>{source.snippet.slice(0, 100)}{source.snippet.length > 100 ? "…" : ""}</p>
          </TooltipContent>
        )}
      </Tooltip>
    </TooltipProvider>
  );
}

// MessageContent — kept for backwards compat with tests and external code.
// Renders markdown via MarkdownMessage plus a copy button and sources list.
import { useState, useCallback } from "react";
import { Check, Copy } from "lucide-react";
import { MarkdownMessage } from "./MarkdownMessage";
import { getRelevanceLabel } from "@/lib/relevance";
import { cn } from "@/lib/utils";
import type { Source } from "@/lib/api";

// Re-export for consumers that import from here
export { MarkdownMessage as MemoizedMarkdown };

interface MessageContentProps {
  content: string;
  sources?: Source[];
  isStreaming?: boolean;
}

export function MessageContent({ content, sources, isStreaming }: MessageContentProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied">("idle");

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopyState("copied");
      setTimeout(() => setCopyState("idle"), 2000);
    } catch {
      // ignore
    }
  }, [content]);

  return (
    <div>
      <MarkdownMessage content={content} isStreaming={isStreaming} />

      <button
        type="button"
        onClick={handleCopy}
        className="mt-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
        aria-label={copyState === "copied" ? "Copied to clipboard" : "Copy message"}
      >
        {copyState === "copied" ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
        {copyState === "copied" ? "Copied" : "Copy"}
      </button>

      {sources && sources.length > 0 && (
        <div className="mt-3 border-t border-border pt-3">
          <p className="text-xs font-medium text-muted-foreground mb-2">Sources</p>
          <ul className="space-y-1.5">
            {sources.map((source, i) => {
              // Null for the uncalibrated per-source "fts" kind (issue #1070)
              // — the guard below suppresses the label rather than guessing.
              const relevance = source.score !== undefined
                ? getRelevanceLabel(source.score, source.score_type)
                : null;
              return (
                <li key={source.id} className="text-xs">
                  <div className="flex items-center gap-2">
                    <span className="text-muted-foreground font-mono">#{i + 1}</span>
                    <span className="font-medium truncate">{source.filename}</span>
                    {relevance && (
                      <span className={cn("text-[10px]", relevance.color)}>{relevance.text}</span>
                    )}
                  </div>
                  {source.snippet && (
                    <p className="text-muted-foreground mt-0.5 pl-5 leading-relaxed">{source.snippet}</p>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

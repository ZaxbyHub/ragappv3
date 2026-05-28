import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Source } from "@/types";

interface MessageContentProps {
  content: string;
  sources?: Source[];
  isStreaming?: boolean;
}

export function MessageContent({ content, sources, isStreaming }: MessageContentProps) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative group">
      <div className="prose prose-sm dark:prose-invert max-w-none">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        {isStreaming && (
          <span className="inline-block w-2 h-4 ml-1 bg-foreground animate-pulse" />
        )}
      </div>

      {sources && sources.length > 0 && (
        <div className="mt-4 pt-4 border-t border-border">
          <p className="text-sm font-semibold mb-2 text-muted-foreground">Sources</p>
          <div className="space-y-2">
            {sources.map((source) => (
              <div
                key={source.id}
                className="text-xs p-2 rounded bg-muted/50 border border-border"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">{source.filename}</span>
                  {source.score !== undefined && (
                    <span className="text-xs text-muted-foreground">
                      {Math.round(source.score * 100)}% match
                    </span>
                  )}
                </div>
                {source.snippet && (
                  <p className="mt-1 text-muted-foreground line-clamp-2">
                    {source.snippet}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <Button
        variant="ghost"
        size="icon"
        className="absolute -right-10 top-0 opacity-0 group-hover:opacity-100 transition-opacity"
        onClick={handleCopy}
      >
        {copied ? (
          <Check className="h-4 w-4 text-green-500" />
        ) : (
          <Copy className="h-4 w-4" />
        )}
      </Button>
    </div>
  );
}
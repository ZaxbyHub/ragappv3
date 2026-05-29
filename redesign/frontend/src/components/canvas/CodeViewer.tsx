"use client";

import React, { useEffect, useState } from "react";
import { Code2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { Source } from "@/types";

interface CodeViewerProps {
  source: Source;
}

export function CodeViewer({ source }: CodeViewerProps) {
  const [displayContent, setDisplayContent] = useState(source.content || source.snippet || "");
  const [originalContent] = useState(source.content || source.snippet || "");
  const [copied, setCopied] = useState(false);

  // Simulate live editing
  useEffect(() => {
    if (!source.content) return;

    const targetContent = source.content;
    if (targetContent === originalContent) return;

    const steps = 20;
    let step = 0;
    const interval = setInterval(() => {
      step++;
      const progress = step / steps;
      const currentLength = Math.floor(originalContent.length + (targetContent.length - originalContent.length) * progress);
      const morphContent = targetContent.slice(0, currentLength);
      setDisplayContent(morphContent);
      if (step >= steps) {
        clearInterval(interval);
      }
    }, 50);

    return () => clearInterval(interval);
  }, [source.content, originalContent]);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(displayContent);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const language = source.language || "text";

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between mb-4 pb-3 border-b border-border">
        <div className="flex items-center gap-2">
          <Code2 className="h-5 w-5 text-muted-foreground" />
          <span className="font-medium truncate">{source.filename}</span>
          <span className="text-xs px-2 py-0.5 rounded bg-accent text-accent-foreground">
            {language}
          </span>
        </div>
        <Button variant="ghost" size="sm" onClick={handleCopy}>
          {copied ? "Copied!" : "Copy"}
        </Button>
      </div>

      <div className="flex-1 overflow-auto">
        <pre className={cn(
          "p-4 rounded bg-muted/30 overflow-x-auto",
          "text-sm font-mono leading-relaxed"
        )}>
          <code>{displayContent || source.snippet || "No code available"}</code>
        </pre>
      </div>

      {source.content && source.content !== originalContent && (
        <div className="mt-4 text-xs text-muted-foreground animate-pulse">
          Live code editing in progress...
        </div>
      )}
    </div>
  );
}
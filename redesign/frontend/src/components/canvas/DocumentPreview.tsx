"use client";

import React, { useEffect, useState } from "react";
import { FileText } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Source } from "@/types";

interface DocumentPreviewProps {
  source: Source;
}

export function DocumentPreview({ source }: DocumentPreviewProps) {
  const [displayContent, setDisplayContent] = useState(source.content || source.snippet || "");
  const [originalContent] = useState(source.content || source.snippet || "");

  // Simulate live editing: gradually morph content towards a "final" version
  useEffect(() => {
    if (!source.content) return;

    const targetContent = source.content;
    if (targetContent === originalContent) return;

    const steps = 20;
    let step = 0;
    const interval = setInterval(() => {
      step++;
      const progress = step / steps;
      // Simple interpolation - in real app would receive diffs from API
      const currentLength = Math.floor(originalContent.length + (targetContent.length - originalContent.length) * progress);
      const morphContent = targetContent.slice(0, currentLength);
      setDisplayContent(morphContent);
      if (step >= steps) {
        clearInterval(interval);
      }
    }, 50);

    return () => clearInterval(interval);
  }, [source.content, originalContent]);

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-2 mb-4 pb-3 border-b border-border">
        <FileText className="h-5 w-5 text-muted-foreground" />
        <span className="font-medium truncate">{source.filename}</span>
      </div>

      <div className="flex-1 overflow-auto">
        <div className={cn("prose prose-sm dark:prose-invert max-w-none", "p-4 rounded bg-muted/30")}>
          <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed">
            {displayContent || source.snippet || "No content available"}
          </pre>
        </div>

        {source.content && source.content !== originalContent && (
          <div className="mt-4 text-xs text-muted-foreground animate-pulse">
            Live editing in progress...
          </div>
        )}
      </div>
    </div>
  );
}
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

import { CANVAS_PREVIEW_UNSUPPORTED_LABEL } from "./labels";
import { loadHighlighter } from "@/lib/highlighter";

// ============================================================================
// Canvas preview — deliberately bounded (issue #509).
//
// kind="code"     → Shiki highlight, lazily loaded (MarkdownMessage pattern);
//                   ANY failure falls back to plain text. The previewed source
//                   of truth is always the version content string — a renderer
//                   failure can never lose or mutate it.
// kind="document" → react-markdown + remark-gfm + rehype-sanitize
//                   (DraftPreview pattern; never dangerouslySetInnerHTML for
//                   markdown input).
// anything else   → explicit "Preview not supported for this format" label.
// ============================================================================

export interface CanvasPreviewProps {
  content: string;
  /** Runtime string, not the narrow union — unexpected kinds must hit the explicit unsupported branch. */
  kind: string;
  language?: string | null;
  className?: string;
}

// Kept deliberately small and auditable: GitHub-flavoured Markdown plus the
// unmodified default sanitize schema (DraftPreview pattern).
const REMARK_PLUGINS = [remarkGfm];
const REHYPE_PLUGINS: import("react-markdown").Options["rehypePlugins"] = [
  [rehypeSanitize, defaultSchema],
];

export function CanvasPreview({ content, kind, language, className }: CanvasPreviewProps) {
  const [html, setHtml] = useState<string | null>(null);

  useEffect(() => {
    if (kind !== "code") {
      setHtml(null);
      return;
    }
    let cancelled = false;
    loadHighlighter()
      .then((highlight) => highlight(content, language ?? ""))
      .then((result) => {
        if (!cancelled) setHtml(result);
      })
      .catch(() => {
        // Renderer failure must never lose the source — fall back to the
        // plain-text branch below, which renders `content` verbatim.
        if (!cancelled) setHtml(null);
      });
    return () => {
      cancelled = true;
    };
  }, [content, kind, language]);

  if (kind === "document") {
    if (!content.trim()) {
      return (
        <div className={`max-w-[80ch] text-sm text-muted-foreground ${className ?? ""}`}>
          Nothing to preview yet.
        </div>
      );
    }
    return (
      <div className={`max-w-[80ch] overflow-x-auto ${className ?? ""}`}>
        <div className="prose prose-sm dark:prose-invert max-w-none prose-table:my-3 prose-th:bg-muted/50">
          <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS}>
            {content}
          </ReactMarkdown>
        </div>
      </div>
    );
  }

  if (kind !== "code") {
    return (
      <p
        className={`text-sm text-muted-foreground ${className ?? ""}`}
        data-testid="canvas-preview-unsupported"
      >
        {CANVAS_PREVIEW_UNSUPPORTED_LABEL}
      </p>
    );
  }

  // kind === "code": highlighted html when available, plain text otherwise.
  return (
    <div className={className}>
      {html != null ? (
        <div
          className="shiki-wrapper overflow-x-auto text-sm [&>pre]:p-4 [&>pre]:m-0 [&>pre]:rounded-none [&>pre]:bg-transparent"
          data-testid="canvas-preview-highlighted"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : (
        <pre
          aria-label="Plain text preview"
          data-testid="canvas-preview-plain"
          className="overflow-x-auto p-4 text-sm font-mono bg-muted/40 rounded-sm border border-border"
        >
          <code>{content}</code>
        </pre>
      )}
    </div>
  );
}

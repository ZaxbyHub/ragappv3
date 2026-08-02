import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

import { cn } from "@/lib/utils";

export interface DraftPreviewProps {
  content: string;
  className?: string;
}

// Kept deliberately small and auditable: GitHub-flavoured Markdown plus the
// unmodified default sanitize schema. No citation remark plugin, no KaTeX, no
// Shiki — this is a read-only manuscript preview, not the chat renderer.
const REMARK_PLUGINS = [remarkGfm];
const REHYPE_PLUGINS: import("react-markdown").Options["rehypePlugins"] = [
  [rehypeSanitize, defaultSchema],
];

/**
 * Renders Markdown safely: `react-markdown` never emits raw HTML strings, and
 * `rehype-sanitize` strips any HTML nodes that slip through (script tags,
 * event-handler attributes, `javascript:` URLs). This component must never
 * use `dangerouslySetInnerHTML`.
 */
export function DraftPreview({ content, className }: DraftPreviewProps) {
  if (!content.trim()) {
    return (
      <div className={cn("max-w-[80ch] text-sm text-muted-foreground", className)}>
        Nothing to preview yet.
      </div>
    );
  }

  return (
    <div className={cn("max-w-[80ch] overflow-x-auto", className)}>
      <div className="prose prose-sm dark:prose-invert max-w-none prose-table:my-3 prose-th:bg-muted/50">
        <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS}>
          {content}
        </ReactMarkdown>
      </div>
    </div>
  );
}

import { isValidElement, memo, useMemo, useEffect, useState } from "react";
import type { ReactNode, HTMLAttributes } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import type { Schema } from "hast-util-sanitize";
import "katex/dist/katex.min.css";
import { CopyButton } from "@/components/shared/CopyButton";
import { SourceCitation } from "./SourceCitation";
import MermaidDiagram from "./MermaidDiagram";
import { CitationConfidence } from "./CitationConfidence";
import { SourceSpanPopover } from "./SourceSpanPopover";
import { AlertTriangle } from "lucide-react";
import type { Source, UsedMemory, WikiReference, KMSReference } from "@/lib/api";

// =============================================================================
// Syntax highlighter — lazily loaded so first chat render is not penalized
// =============================================================================

type HighlightFn = (code: string, lang: string) => Promise<string>;

let _highlightFn: HighlightFn | null = null;
let _highlightPromise: Promise<HighlightFn> | null = null;

function escapeCodeHtml(code: string) {
  return code
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderPlainCodeHtml(code: string) {
  return `<pre><code>${escapeCodeHtml(code)}</code></pre>`;
}

function codeChildrenToText(children: ReactNode): string {
  if (Array.isArray(children)) {
    return children.map(codeChildrenToText).join("");
  }
  if (children === null || children === undefined || typeof children === "boolean") {
    return "";
  }
  return String(children);
}

function loadHighlighter(): Promise<HighlightFn> {
  if (_highlightFn) return Promise.resolve(_highlightFn);
  if (_highlightPromise) return _highlightPromise;

  _highlightPromise = (async () => {
    try {
      const { createHighlighter } = await import("shiki");
      const hl = await createHighlighter({
        themes: ["github-light", "github-dark"],
        langs: [
          "javascript", "typescript", "tsx", "jsx",
          "python", "bash", "sh", "json", "yaml", "toml",
          "css", "html", "xml", "markdown", "sql",
          "rust", "go", "java", "c", "cpp", "csharp",
        ],
      });
      const fn: HighlightFn = (code, lang) => {
        const isDark = document.documentElement.classList.contains("dark");
        try {
          return Promise.resolve(
            hl.codeToHtml(code, {
              lang: lang || "text",
              theme: isDark ? "github-dark" : "github-light",
            })
          );
        } catch {
          // Unknown language — fallback to plain text
          return Promise.resolve(
            hl.codeToHtml(code, { lang: "text", theme: isDark ? "github-dark" : "github-light" })
          );
        }
      };
      _highlightFn = fn;
      return fn;
    } catch {
      // Shiki unavailable — return no-op so code still renders as plain text
      const fn: HighlightFn = (code) => Promise.resolve(renderPlainCodeHtml(code));
      _highlightFn = fn;
      return fn;
    }
  })();

  return _highlightPromise;
}

// =============================================================================
// CodeBlock with syntax highlighting
// =============================================================================

interface CodeBlockProps {
  language: string;
  code: string;
}

const CodeBlock = memo(function CodeBlock({ language, code }: CodeBlockProps) {
  const [html, setHtml] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadHighlighter().then((highlight) => {
      highlight(code, language).then((result) => {
        if (!cancelled) setHtml(result);
      });
    });
    return () => { cancelled = true; };
  }, [code, language]);

  return (
    <div className="relative my-3 rounded-sm overflow-hidden border border-border group/code">
      {language && (
        <div className="flex items-center justify-between px-4 py-1.5 bg-muted border-b border-border">
          <span className="text-[11px] text-muted-foreground font-mono">{language}</span>
          <CopyButton text={code} label="Copy code" className="h-6 w-6 opacity-60 hover:opacity-100" />
        </div>
      )}
      {!language && (
        <CopyButton
          text={code}
          label="Copy code"
          className="absolute top-2 right-2 h-6 w-6 opacity-0 group-hover/code:opacity-100 focus:opacity-100 transition-opacity z-10"
        />
      )}
      {html ? (
        <div
          className="shiki-wrapper overflow-x-auto text-sm [&>pre]:p-4 [&>pre]:m-0 [&>pre]:rounded-none [&>pre]:bg-transparent"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : (
        <pre className="overflow-x-auto p-4 text-sm font-mono bg-muted/40">
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
});

export const MarkdownMessageTestInternals = {
  codeChildrenToText,
  renderPlainCodeHtml,
};

// =============================================================================
// Citation-aware markdown renderer
// =============================================================================

interface ParsedSegment {
  type: "text" | "citation" | "memory_citation" | "wiki_citation" | "kms_citation";
  content?: string;
  sourceName?: string;
  memoryLabel?: string;
  wikiLabel?: string;
  kmsLabel?: string;
}

/**
 * Parse citation markers from assistant text:
 * - ``[S1]``, ``[S2]`` — document citations
 * - ``[M1]``, ``[M2]`` — memory citations (durable user context)
 * - ``[W1]``, ``[W2]`` — wiki knowledge citations
 * - ``[Source: filename]`` — legacy filename citation
 *
 * Returns text/citation segments so a single ReactMarkdown pass runs per
 * text segment while citations are rendered as interactive chips.
 */
export function parseCitationSegments(
  content: string,
  sources: Source[] | undefined,
  memories?: UsedMemory[],
  wikiRefs?: WikiReference[],
  kmsRefs?: KMSReference[]
): {
  segments: ParsedSegment[];
  citedSources: Source[];
  citedMemories: UsedMemory[];
  citedWikis: WikiReference[];
  citedKms: KMSReference[];
} {
  // Capture groups:
  //  1. document number (e.g. "2" from "[S2]")
  //  2. memory number   (e.g. "1" from "[M1]")
  //  3. wiki number     (e.g. "3" from "[W3]")
  //  4. kms number      (e.g. "4" from "[K4]")
  //  5. legacy filename (e.g. "report.pdf" from "[Source: report.pdf]")
  const regex = /\[S(\d+)\]|\[M(\d+)\]|\[W(\d+)\]|\[K(\d+)\]|\[Source:\s*([^\]]+)\]/g;
  const segments: ParsedSegment[] = [];
  const citedSources: Source[] = [];
  const citedMemories: UsedMemory[] = [];
  const citedWikis: WikiReference[] = [];
  const citedKms: KMSReference[] = [];
  const seenSourceIds = new Set<string>();
  const seenMemoryIds = new Set<string>();
  const seenWikiLabels = new Set<string>();
  const seenKmsLabels = new Set<string>();
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ type: "text", content: content.slice(lastIndex, match.index) });
    }

    if (match[1]) {
      // Document citation [S#]
      const label = `S${match[1]}`;
      let source = sources?.find((s) => s.source_label === label);
      if (!source) {
        const idx = parseInt(match[1], 10) - 1;
        if (sources && idx >= 0 && idx < sources.length) source = sources[idx];
      }
      segments.push({ type: "citation", sourceName: label });
      if (source && !seenSourceIds.has(source.id)) {
        citedSources.push(source);
        seenSourceIds.add(source.id);
      }
    } else if (match[2]) {
      // Memory citation [M#]
      const label = `M${match[2]}`;
      let memory = memories?.find((m) => m.memory_label === label);
      if (!memory) {
        const idx = parseInt(match[2], 10) - 1;
        if (memories && idx >= 0 && idx < memories.length) memory = memories[idx];
      }
      segments.push({ type: "memory_citation", memoryLabel: label });
      if (memory && !seenMemoryIds.has(memory.id)) {
        citedMemories.push(memory);
        seenMemoryIds.add(memory.id);
      }
    } else if (match[3]) {
      // Wiki citation [W#]
      const label = `W${match[3]}`;
      let wiki = wikiRefs?.find((w) => w.wiki_label === label);
      if (!wiki) {
        const idx = parseInt(match[3], 10) - 1;
        if (wikiRefs && idx >= 0 && idx < wikiRefs.length) wiki = wikiRefs[idx];
      }
      segments.push({ type: "wiki_citation", wikiLabel: label });
      if (wiki && !seenWikiLabels.has(label)) {
        citedWikis.push(wiki);
        seenWikiLabels.add(label);
      }
    } else if (match[4]) {
      // KMS citation [K#]
      const label = `K${match[4]}`;
      let kms = kmsRefs?.find((k) => k.kms_label === label);
      if (!kms) {
        const idx = parseInt(match[4], 10) - 1;
        if (kmsRefs && idx >= 0 && idx < kmsRefs.length) kms = kmsRefs[idx];
      }
      segments.push({ type: "kms_citation", kmsLabel: label });
      if (kms && !seenKmsLabels.has(label)) {
        citedKms.push(kms);
        seenKmsLabels.add(label);
      }
    } else if (match[5]) {
      // Legacy [Source: filename]
      const sourceName = match[5].trim();
      const source = sources?.find((s) => s.filename === sourceName);
      segments.push({ type: "citation", sourceName });
      if (source && !seenSourceIds.has(source.id)) {
        citedSources.push(source);
        seenSourceIds.add(source.id);
      }
    }

    lastIndex = regex.lastIndex;
  }

  if (lastIndex < content.length) {
    segments.push({ type: "text", content: content.slice(lastIndex) });
  }

  return { segments, citedSources, citedMemories, citedWikis, citedKms };
}

// =============================================================================
// Remark plugin: convert [S#] / [M#] / [W#] / [Source: file] markers in text
// nodes into inline span elements with data-citation attributes. This runs
// AFTER the markdown structure is parsed, so tables, lists, code blocks, and
// other block constructs remain intact. The component override for ``span``
// below renders each marker as an interactive citation chip.
// =============================================================================

const CITATION_REGEX = /\[(S|M|W|K)(\d+)\]|\[Source:\s*([^\]]+)\]/g;

interface MdastNode {
  type: string;
  value?: string;
  children?: MdastNode[];
  data?: Record<string, unknown>;
}

function remarkCitations() {
  return (tree: MdastNode) => {
    const transformNode = (node: MdastNode | undefined, parent: MdastNode | null): void => {
      if (!node) return;
      // Do not transform inside code spans / code blocks — citation markers
      // there must render as literal text.
      if (node.type === "code" || node.type === "inlineCode") return;
      if (Array.isArray(node.children)) {
        for (let i = node.children.length - 1; i >= 0; i--) {
          transformNode(node.children[i], node);
        }
      }
      if (node.type === "text" && parent && Array.isArray(parent.children)) {
        const text = node.value ?? "";
        if (!text) return;
        CITATION_REGEX.lastIndex = 0;
        let match: RegExpExecArray | null;
        let lastIndex = 0;
        const replacements: MdastNode[] = [];
        while ((match = CITATION_REGEX.exec(text)) !== null) {
          if (match.index > lastIndex) {
            replacements.push({ type: "text", value: text.slice(lastIndex, match.index) });
          }
          let citeType = "";
          let label = "";
          if (match[1]) {
            citeType = match[1];
            label = `${match[1]}${match[2]}`;
          } else if (match[3]) {
            citeType = "F";
            label = match[3].trim();
          }
          replacements.push({
            // mdast HTML/element bridge: emit a custom node that remark-rehype
            // will lower into a <span data-citation-type="..." data-citation-label="...">.
            type: "citationMarker",
            data: {
              hName: "span",
              hProperties: {
                "data-citation-type": citeType,
                "data-citation-label": label,
              },
            },
          });
          lastIndex = CITATION_REGEX.lastIndex;
        }
        if (lastIndex === 0) return;
        if (lastIndex < text.length) {
          replacements.push({ type: "text", value: text.slice(lastIndex) });
        }
        const idx = parent.children.indexOf(node);
        if (idx >= 0) {
          parent.children.splice(idx, 1, ...replacements);
        }
      }
    };
    transformNode(tree, null);
  };
}

// MathML tag names used by KaTeX output — extend hast-util-sanitize's default
// tagNames so rehypeSanitize does not strip the MathML tree that rehypeKatex produces.
const MATHML_TAG_NAMES = [
  // Layout
  "math", "semantics", "mrow", "mfrac", "msqrt", "mroot",
  "msup", "msub", "msubsup", "munder", "mover", "munderover",
  "mtable", "mtr", "mtd", "mlabeledtr",
  "mphantom", "mpadded", "mspace", "mglyph",
  // Tokens
  "mi", "mo", "mn", "ms", "mtext",
  // Semantics
  "annotation", "annotation-xml",
  // Alignment (KaTeX emits these)
  "maligngroup", "malignmark",
];

// Extend rehype-sanitize's default schema so our citation spans (with
// data-citation-* attributes) and MathML tags survive sanitization. Bare
// attribute names ("attr") mean "any value allowed" in hast-util-sanitize's
// PropertyDefinition format.
const CITATION_SANITIZE_SCHEMA: Schema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames ?? []),
    ...MATHML_TAG_NAMES,
  ],
  attributes: {
    ...(defaultSchema.attributes ?? {}),
    // Citation data attributes on span elements (merged — single span key)
    span: [
      ...((defaultSchema.attributes?.span as unknown as string[]) ?? []),
      "data-citation-type",
      "data-citation-label",
      "class",
      "style",
      "id",
    ],
    // KaTeX MathML elements (math, semantics, and all mrow-derived tokens)
    math: ["class", "style", "id", "xmlns", "display", "scriptsizem", "scriptminsize", "scriptlevel"],
    semantics: ["encoding"],
    // Token elements
    mi: ["class", "style", "id", "mathvariant", "mathsize"],
    mo: ["class", "style", "id", "stretchy", "fence", "separator", "lspace", "rspace", "accent", "accentunder", "minsize", "maxsize"],
    mn: ["class", "style", "id", "mathvariant"],
    ms: ["class", "style", "id", "mathvariant", "lquote", "rquote"],
    mtext: ["class", "style", "id", "mathvariant", "mathsize"],
    // General layout
    mrow: ["class", "style", "id"],
    mfrac: ["class", "style", "id", "linethickness", "numalign", "denomalign", "bevelled"],
    msqrt: ["class", "style", "id"],
    mroot: ["class", "style", "id"],
    msup: ["class", "style", "id"],
    msub: ["class", "style", "id"],
    msubsup: ["class", "style", "id"],
    munder: ["class", "style", "id", "accentunder", "align"],
    mover: ["class", "style", "id", "accent", "align"],
    munderover: ["class", "style", "id", "accent", "accentunder", "align"],
    mtable: ["class", "style", "id", "align", "columnalign", "rowalign", "columnspacing", "rowspacing", "framespacing", "equalrows", "equalcolumns", "columnwidth", "width", "height", "depth"],
    mtr: ["class", "style", "id", "columnalign", "rowalign"],
    mtd: ["class", "style", "id", "columnalign", "rowalign", "rowspan", "columnspan"],
    mlabeledtr: ["class", "style", "id", "columnalign", "rowalign"],
    mphantom: ["class", "style", "id"],
    mpadded: ["class", "style", "id", "width", "height", "depth", "lspace", "voffset"],
    mspace: ["class", "style", "id", "width", "height", "depth", "lspace", "voffset"],
    mglyph: ["class", "style", "id"],
    annotation: ["class", "style", "id", "encoding"],
    "annotation-xml": ["class", "style", "id", "encoding"],
    maligngroup: ["class", "style", "id"],
    malignmark: ["class", "style", "id"],
  },
};

// Stable plugin arrays — prevent re-creating on every render
const REMARK_PLUGINS = [remarkGfm, remarkMath, remarkCitations];
const REHYPE_PLUGINS: import("react-markdown").Options["rehypePlugins"] = [
  rehypeKatex,
  [rehypeSanitize, CITATION_SANITIZE_SCHEMA],
];

interface MarkdownMessageProps {
  content: string;
  sources?: Source[];
  memories?: UsedMemory[];
  wikiRefs?: WikiReference[];
  kmsRefs?: KMSReference[];
  isStreaming?: boolean;
  onCitationClick?: (source: Source) => void;
  onMemoryCitationClick?: (memory: UsedMemory) => void;
  onWikiCitationClick?: (wiki: WikiReference) => void;
  onKmsCitationClick?: (kms: KMSReference) => void;
  citedSources?: Source[];
  citationConfidence?: Record<string, number>;
  unverifiableClaims?: string[];
}

/**
 * Unified markdown + citation renderer used by both assistant and user messages.
 *
 * - Renders the full message through a single ReactMarkdown pass. A custom
 *   remark plugin (``remarkCitations``) converts ``[S#]``/``[M#]``/``[W#]``
 *   markers into inline span nodes, and the ``span`` component override
 *   below renders each one as an interactive citation chip. Splitting the
 *   message at citation boundaries is intentionally avoided so block
 *   constructs (tables, lists, code blocks) survive intact.
 * - Lazily loads Shiki for syntax-highlighted code blocks.
 * - Inline citation chips are interactive and call onCitationClick.
 * - Streaming caret blinks while isStreaming is true.
 */
export const MarkdownMessage = memo(function MarkdownMessage({
  content,
  sources,
  memories,
  wikiRefs,
  kmsRefs,
  isStreaming,
  onCitationClick,
  onMemoryCitationClick,
  onWikiCitationClick,
  onKmsCitationClick,
  citedSources: externalCitedSources,
  citationConfidence,
  unverifiableClaims,
}: MarkdownMessageProps) {
  const { segments, citedSources: internalCitedSources } = useMemo(
    () => parseCitationSegments(content, sources, memories, wikiRefs, kmsRefs),
    [content, sources, memories, wikiRefs, kmsRefs]
  );

  const citedSources = externalCitedSources ?? internalCitedSources;
  // ``segments`` is still used by the aggregation path above; it does not
  // drive rendering anymore. Avoid an unused-variable lint without changing
  // ``parseCitationSegments``' exported shape.
  void segments;

  // Renderer for citation chips produced by the remarkCitations plugin.
  // The plugin emits ``<span data-citation-type data-citation-label>`` and
  // this component picks them up to render the existing interactive chip.
  // No explicit key is needed: ReactMarkdown assigns positional keys for
  // each rendered span via its own children-array reconciliation.
  const renderCitationSpan = (type: string, label: string): ReactNode => {
    if (type === "W") {
      const wiki =
        wikiRefs?.find((w) => w.wiki_label === label) ??
        (() => {
          const m = label.match(/^W(\d+)$/);
          if (m && wikiRefs) {
            const idx = parseInt(m[1], 10) - 1;
            return idx >= 0 && idx < wikiRefs.length ? wikiRefs[idx] : undefined;
          }
          return undefined;
        })();
      const titleText = wiki
        ? `Wiki ${label}: ${wiki.title}${wiki.claim_text ? ` — ${wiki.claim_text.slice(0, 100)}` : ""}`
        : `Wiki ${label}`;
      return (
        <button
          type="button"
          className="inline-flex items-center align-baseline px-1.5 py-0.5 mx-0.5 rounded-sm border border-indigo-500/40 bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 text-[10px] font-semibold tracking-wide hover:bg-indigo-500/20 hover:border-indigo-500/60 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => wiki && onWikiCitationClick?.(wiki)}
          disabled={!wiki}
          title={titleText}
          aria-label={titleText}
          data-citation-type="wiki"
          data-citation-label={label}
        >
          {label}
        </button>
      );
    }
    if (type === "K") {
      const kms =
        kmsRefs?.find((k) => k.kms_label === label) ??
        (() => {
          const m = label.match(/^K(\d+)$/);
          if (m && kmsRefs) {
            const idx = parseInt(m[1], 10) - 1;
            return idx >= 0 && idx < kmsRefs.length ? kmsRefs[idx] : undefined;
          }
          return undefined;
        })();
      const titleText = kms
        ? `Knowledge ${label}: ${kms.title}${kms.excerpt ? ` — ${kms.excerpt.slice(0, 100)}` : ""}`
        : `Knowledge ${label}`;
      return (
        <button
          type="button"
          className="inline-flex items-center align-baseline px-1.5 py-0.5 mx-0.5 rounded-md border border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 text-[10px] font-semibold tracking-wide hover:bg-emerald-500/20 hover:border-emerald-500/60 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => kms && onKmsCitationClick?.(kms)}
          disabled={!kms}
          title={titleText}
          aria-label={titleText}
          data-citation-type="kms"
          data-citation-label={label}
        >
          {label}
        </button>
      );
    }
    if (type === "M") {
      const memory =
        memories?.find((m) => m.memory_label === label) ||
        (() => {
          const m = label.match(/^M(\d+)$/);
          if (m && memories) {
            const idx = parseInt(m[1], 10) - 1;
            return idx >= 0 && idx < memories.length ? memories[idx] : undefined;
          }
          return undefined;
        })();
      const titleText = memory?.content
        ? `Memory ${label}: ${memory.content.slice(0, 200)}${memory.content.length > 200 ? "…" : ""}`
        : `Memory ${label}`;
      return (
        <button
          type="button"
          className="inline-flex items-center align-baseline px-1.5 py-0.5 mx-0.5 rounded-sm border border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300 text-[10px] font-semibold tracking-wide hover:bg-amber-500/20 hover:border-amber-500/60 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => memory && onMemoryCitationClick?.(memory)}
          disabled={!memory}
          title={titleText}
          aria-label={titleText}
          data-citation-type="memory"
          data-citation-label={label}
        >
          {label}
        </button>
      );
    }
    // Document citations: type "S" (label like "S1") or legacy filename "F".
    const lookupName = label;
    const source =
      sources?.find((s) => s.source_label === lookupName) ||
      sources?.find((s) => s.filename === lookupName) ||
      (() => {
        const m = lookupName.match(/^S(\d+)$/);
        if (m && sources) {
          const idx = parseInt(m[1], 10) - 1;
          return idx >= 0 && idx < sources.length ? sources[idx] : undefined;
        }
        return undefined;
      })();
    if (source) {
      const dispIdx = citedSources.findIndex((s) => s.id === source.id);
      const confidenceScore = citationConfidence?.[label];
      return (
        <SourceSpanPopover
          source={source}
          label={label}
          confidence={confidenceScore}
        >
          <span className="inline-flex items-center align-baseline">
            <SourceCitation
              source={source}
              index={dispIdx >= 0 ? dispIdx : 0}
              onClick={() => onCitationClick?.(source)}
              variant="inline"
            />
            {confidenceScore !== undefined && (
              <CitationConfidence score={confidenceScore} className="ml-0.5" />
            )}
          </span>
        </SourceSpanPopover>
      );
    }
    return <span>[{lookupName}]</span>;
  };

  return (
    <div className="prose prose-sm dark:prose-invert max-w-none prose-headings:font-semibold prose-headings:font-sans prose-headings:mt-4 prose-headings:mb-1 prose-strong:font-semibold prose-p:leading-relaxed prose-p:mb-3 prose-p:mt-0 prose-li:my-0.5 prose-ul:my-2 prose-ol:my-2 prose-blockquote:border-l-2 prose-blockquote:border-primary/40 prose-blockquote:pl-3 prose-blockquote:italic prose-code:font-mono prose-table:my-3 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5 prose-th:bg-muted/50">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={{
          span: (props: HTMLAttributes<HTMLSpanElement> & {
            "data-citation-type"?: string;
            "data-citation-label"?: string;
            node?: unknown;
          }) => {
            // Destructure to separate hast node from DOM props — node must NOT
            // be spread onto the DOM span (causes node="[object Object]" attr).
            const { node: _node, className, children, ...domProps } = props;
            const citeType = domProps["data-citation-type"];
            const citeLabel = domProps["data-citation-label"];
            if (citeType && citeLabel) {
              return renderCitationSpan(citeType, citeLabel);
            }
            return <span className={className} {...domProps}>{children}</span>;
          },
          code: ({ className, children }) => (
            <code
              className={
                className?.startsWith("language-")
                  ? className
                  : "bg-muted px-1 py-0.5 rounded-sm text-[0.85em] font-mono"
              }
            >
              {children}
            </code>
          ),
          pre: ({ children }) => {
            const codeElement = Array.isArray(children) ? children[0] : children;
            if (!isValidElement<{ className?: string; children?: ReactNode }>(codeElement)) {
              return <pre>{children}</pre>;
            }
            const className = codeElement.props.className;
            const lang = className?.startsWith("language-")
              ? className.replace("language-", "")
              : "";
            const codeText = codeChildrenToText(codeElement.props.children).replace(/\n$/, "");
            if (lang === "mermaid") {
              return <MermaidDiagram chart={codeText} />;
            }
            return <CodeBlock language={lang} code={codeText} />;
          },
          table: ({ children }) => (
            <div className="overflow-x-auto my-3">
              <table className="border-collapse border border-border text-sm w-full">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-border bg-muted/50 px-3 py-1.5 text-left font-semibold">{children}</th>
          ),
          td: ({ children }) => (
            <td className="border border-border px-3 py-1.5 align-top">{children}</td>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
      {isStreaming && (
        <span
          className="streaming-caret"
          role="status"
          aria-label="Message streaming"
        />
      )}
      {unverifiableClaims && unverifiableClaims.length > 0 && (
        <div
          className="mt-3 flex items-start gap-2 rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2"
          role="alert"
        >
          <AlertTriangle className="h-4 w-4 flex-shrink-0 text-amber-500 mt-0.5" aria-hidden />
          <div className="flex flex-col gap-1">
            <p className="text-xs font-semibold text-amber-600 dark:text-amber-400">
              Unverifiable Claims
            </p>
            {unverifiableClaims.map((claim, i) => (
              <p key={i} className="text-xs text-amber-600/80 dark:text-amber-400/80">
                {claim}
              </p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
});

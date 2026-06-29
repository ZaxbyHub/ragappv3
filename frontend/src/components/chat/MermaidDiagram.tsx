// frontend/src/components/chat/MermaidDiagram.tsx
/**
 * MermaidDiagram — lazy-loaded, error-tolerant Mermaid diagram renderer.
 *
 * - Uses mermaid.render() with securityLevel='strict' to prevent script injection.
 * - Renders to an SVG div; falls back to a readable error message on invalid syntax.
 * - Accepts arbitrary mermaid source (end-user content in chat messages).
 */
import { memo, useEffect, useRef, useState } from "react";

interface MermaidDiagramProps {
  /** Raw mermaid chart definition from the markdown code block */
  chart: string;
}

/** Stable counter for generating unique mermaid element ids */
let _mermaidCounter = 0;
function nextMermaidId() {
  return `mermaid-diagram-${++_mermaidCounter}`;
}

export const MermaidDiagramTestInternals = {
  nextMermaidId,
};

const MermaidDiagram = memo(function MermaidDiagram({ chart }: MermaidDiagramProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const chartRef = useRef(chart);
  chartRef.current = chart;

  useEffect(() => {
    let cancelled = false;
    const id = nextMermaidId();

    // Lazily import mermaid to avoid penalizing first paint
    import("mermaid").then((mermaidModule) => {
      if (cancelled) return;

      const mermaid = mermaidModule.default;

      // securityLevel='strict' prevents script injection via malicious diagrams
      mermaid.initialize({
        securityLevel: "strict",
        startOnLoad: false,
      });

      mermaid
        .render(id, chartRef.current)
        .then(({ svg: renderedSvg }) => {
          if (!cancelled) {
            setSvg(renderedSvg);
            setError(null);
          }
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          // mermaid.render throws on invalid syntax — show a clean fallback
          const message =
            err instanceof Error ? err.message : typeof err === "string" ? err : "Invalid diagram";
          setError(message);
          setSvg(null);
        });
    });

    return () => {
      cancelled = true;
    };
  }, [chart]);

  if (error) {
    return (
      <div
        className="my-3 rounded-sm border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-700 dark:text-red-300"
        role="alert"
        data-testid="mermaid-error"
      >
        <p className="font-semibold">Diagram rendering error</p>
        <pre className="mt-1 overflow-x-auto text-xs whitespace-pre-wrap">{error}</pre>
      </div>
    );
  }

  if (!svg) {
    return (
      <div
        className="my-3 flex items-center gap-2 rounded-sm border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground"
        data-testid="mermaid-loading"
      >
        <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
        Rendering diagram…
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="my-3 overflow-x-auto"
      data-testid="mermaid-diagram"
      role="img"
      aria-label={`Mermaid diagram: ${chart.substring(0, 100)}`}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
});

export default MermaidDiagram;

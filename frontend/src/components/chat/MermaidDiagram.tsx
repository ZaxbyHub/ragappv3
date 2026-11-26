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
  // Render output is tagged with the chart it was produced from (issue #836:
  // stale mermaid diagram on chart change). While a redraw for a newly
  // selected chart is pending, output tagged for a previous chart is never
  // displayed, and a late out-of-order completion cannot present stale
  // output for a chart that is no longer selected.
  const [rendered, setRendered] = useState<{ chart: string; svg: string } | null>(null);
  const [error, setError] = useState<{ chart: string; message: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const id = nextMermaidId();
    // The chart this draw belongs to, captured at draw start so the result is
    // always tagged with the chart that was selected when it began.
    const drawChart = chart;

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
        .render(id, drawChart)
        .then(({ svg: renderedSvg }) => {
          if (!cancelled) {
            setRendered({ chart: drawChart, svg: renderedSvg });
            setError(null);
          }
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          // mermaid.render throws on invalid syntax — show a clean fallback
          const message =
            err instanceof Error ? err.message : typeof err === "string" ? err : "Invalid diagram";
          setError({ chart: drawChart, message });
          setRendered(null);
        });
    })
    .catch((err: unknown) => {
      if (cancelled) return;
      // The dynamic import itself can reject (chunk load failure) — without
      // this handler the spinner would stay up forever (issue #689 /
      // TQ-sibling-batch-01-06). Same fallback shape as a render failure.
      const message =
        err instanceof Error ? err.message : typeof err === "string" ? err : "Failed to load diagram renderer";
      setError({ chart: drawChart, message });
      setRendered(null);
    });

    return () => {
      cancelled = true;
    };
  }, [chart]);

  // Only output produced for the currently selected chart is displayed.
  const svg = rendered?.chart === chart ? rendered.svg : null;
  const errorMessage = error?.chart === chart ? error.message : null;

  if (errorMessage) {
    return (
      <div
        className="my-3 rounded-sm border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive"
        role="alert"
        data-testid="mermaid-error"
      >
        <p className="font-semibold">Diagram rendering error</p>
        <pre className="mt-1 overflow-x-auto text-xs whitespace-pre-wrap">{errorMessage}</pre>
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

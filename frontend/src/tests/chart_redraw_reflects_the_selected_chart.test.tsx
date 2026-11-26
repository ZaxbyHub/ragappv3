// frontend/src/tests/chart_redraw_reflects_the_selected_chart.test.tsx
/**
 * Issue #836 (Workstream K, PR 9 of 9) — AC1:
 * WHILE a chart redraw is pending, THE SYSTEM SHALL remove stale output and
 * present only the newest diagram after out-of-order completions.
 *
 * Binds the tracker-recorded symptom "stale mermaid diagram on chart
 * change": when the `chart` prop changes while the second draw is pending,
 * neither the previous diagram nor the previous error may remain visible
 * while the label moves to the newly selected chart, and a late
 * (out-of-order) completion of the older draw must not overwrite the newest
 * diagram.
 *
 * mermaid is module-mocked with controllable deferreds so draw completion
 * order is deterministic. The production import-chain rejection surface is
 * covered separately by dynamicImportRejection.a07.guardrail.test.ts and
 * MermaidDiagram.fb.fallback-pin.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import React from "react";
import MermaidDiagram from "@/components/chat/MermaidDiagram";

interface MermaidRenderResult {
  svg: string;
}

const mockMermaid = {
  initialize: vi.fn(),
  render: vi.fn(),
};

vi.mock("mermaid", () => ({ default: mockMermaid }));

const CHART_A = "graph TD\n  A[Start] --> B[End]";
const CHART_B = "graph LR\n  X --> Y --> Z";
// Distinct svg payloads so the final chart identity is assertable by content.
const SVG_A = '<svg data-chart="chart-a"><rect width="10" height="10"/></svg>';
const SVG_B = '<svg data-chart="chart-b"><circle r="5"/></svg>';

/** A controllable pending mermaid.render() result. */
function deferredRender() {
  let resolve!: (result: MermaidRenderResult) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<MermaidRenderResult>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Flush the dynamic-import microtask chain so render() has been called. */
async function settle() {
  await act(async () => {});
}

beforeEach(() => {
  vi.clearAllMocks();
  mockMermaid.initialize.mockResolvedValue(undefined);
});

describe("Issue #836 AC1: chart redraw reflects the selected chart", () => {
  it("stale diagram is removed while the redraw for the selected chart is pending; only the newest diagram is presented", async () => {
    const firstDraw = deferredRender();
    mockMermaid.render.mockReturnValueOnce(firstDraw.promise);

    const { rerender } = render(<MermaidDiagram chart={CHART_A} />);
    await settle();
    expect(mockMermaid.render).toHaveBeenCalledWith(expect.any(String), CHART_A);

    // First draw completes → diagram for chart A is presented.
    await act(async () => {
      firstDraw.resolve({ svg: SVG_A });
    });
    const diagram = screen.getByTestId("mermaid-diagram");
    expect(diagram.innerHTML).toContain('data-chart="chart-a"');

    // Select chart B and leave its draw pending.
    const secondDraw = deferredRender();
    mockMermaid.render.mockReturnValueOnce(secondDraw.promise);
    rerender(<MermaidDiagram chart={CHART_B} />);
    await settle();
    expect(mockMermaid.render).toHaveBeenCalledWith(expect.any(String), CHART_B);

    // AC1: while the second draw is pending, neither the previous diagram
    // nor a previous error remains — only the loading state.
    expect(screen.queryByTestId("mermaid-diagram")).toBeNull();
    expect(screen.queryByTestId("mermaid-error")).toBeNull();
    expect(screen.getByTestId("mermaid-loading")).toBeInTheDocument();

    // Newest draw completes → only chart B's diagram is presented.
    await act(async () => {
      secondDraw.resolve({ svg: SVG_B });
    });
    const finalDiagram = screen.getByTestId("mermaid-diagram");
    expect(finalDiagram.innerHTML).toContain('data-chart="chart-b"');
    expect(finalDiagram.innerHTML).not.toContain('data-chart="chart-a"');
    // Final chart identity: the accessible label names the selected chart.
    expect(finalDiagram).toHaveAttribute("aria-label", `Mermaid diagram: ${CHART_B}`);
    expect(screen.queryByTestId("mermaid-loading")).toBeNull();
  });

  it("stale error is removed while the redraw for the selected chart is pending", async () => {
    const firstDraw = deferredRender();
    mockMermaid.render.mockReturnValueOnce(firstDraw.promise);

    const { rerender } = render(<MermaidDiagram chart={CHART_A} />);
    await settle();

    // First draw fails → error fallback for chart A is presented.
    await act(async () => {
      firstDraw.reject(new Error("bad diagram syntax"));
    });
    expect(screen.getByTestId("mermaid-error")).toHaveTextContent("bad diagram syntax");

    // Select chart B and leave its draw pending.
    const secondDraw = deferredRender();
    mockMermaid.render.mockReturnValueOnce(secondDraw.promise);
    rerender(<MermaidDiagram chart={CHART_B} />);
    await settle();

    // AC1: the previous error must not remain while the second draw is
    // pending — neither the error nor any diagram, only the loading state.
    expect(screen.queryByTestId("mermaid-error")).toBeNull();
    expect(screen.queryByTestId("mermaid-diagram")).toBeNull();
    expect(screen.getByTestId("mermaid-loading")).toBeInTheDocument();

    // Newest draw succeeds → only chart B's diagram is presented.
    await act(async () => {
      secondDraw.resolve({ svg: SVG_B });
    });
    expect(screen.getByTestId("mermaid-diagram").innerHTML).toContain('data-chart="chart-b"');
    expect(screen.queryByTestId("mermaid-error")).toBeNull();
  });

  it("out-of-order completions: the older draw resolving last cannot overwrite the newest diagram", async () => {
    const drawA = deferredRender();
    mockMermaid.render.mockReturnValueOnce(drawA.promise);

    const { rerender } = render(<MermaidDiagram chart={CHART_A} />);
    await settle();

    // Chart B is selected while draw A is still pending; draw B starts.
    const drawB = deferredRender();
    mockMermaid.render.mockReturnValueOnce(drawB.promise);
    rerender(<MermaidDiagram chart={CHART_B} />);
    await settle();

    // AC1 "while a redraw is pending": the stale chart-A output is gone.
    expect(screen.queryByTestId("mermaid-diagram")).toBeNull();
    expect(screen.getByTestId("mermaid-loading")).toBeInTheDocument();

    // The newest draw (B) completes first.
    await act(async () => {
      drawB.resolve({ svg: SVG_B });
    });
    expect(screen.getByTestId("mermaid-diagram").innerHTML).toContain('data-chart="chart-b"');

    // The older draw (A) now completes out of order — the displayed diagram
    // must still be chart B's (final chart identity).
    await act(async () => {
      drawA.resolve({ svg: SVG_A });
    });
    const finalDiagram = screen.getByTestId("mermaid-diagram");
    expect(finalDiagram.innerHTML).toContain('data-chart="chart-b"');
    expect(finalDiagram.innerHTML).not.toContain('data-chart="chart-a"');
    expect(finalDiagram).toHaveAttribute("aria-label", `Mermaid diagram: ${CHART_B}`);
    expect(screen.queryByTestId("mermaid-loading")).toBeNull();
    expect(screen.queryByTestId("mermaid-error")).toBeNull();
  });
});

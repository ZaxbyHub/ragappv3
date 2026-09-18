// CanvasPreview loader verification (issue #572): the canvas code preview
// renders through the shared lazy highlighter. Covers the highlighted path
// for a real language and the plain-text fallback when the renderer fails.
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/lib/highlighter", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/highlighter")>();
  return { ...actual, loadHighlighter: vi.fn(actual.loadHighlighter) };
});

import { loadHighlighter } from "@/lib/highlighter";
import { CanvasPreview } from "./CanvasPreview";

describe("CanvasPreview code preview (shared highlighter)", () => {
  it(
    "highlights a python code preview through the real shared loader",
    { timeout: 60_000 },
    async () => {
      render(<CanvasPreview kind="code" language="python" content="print('canvas')" />);
      const highlighted = await screen.findByTestId("canvas-preview-highlighted", {}, { timeout: 45_000 });
      expect(highlighted.innerHTML).toContain("shiki");
      expect(highlighted.innerHTML).toContain("print");
      expect(loadHighlighter).toHaveBeenCalled();
    },
  );

  it(
    "falls back to the plain-text preview when the renderer fails",
    { timeout: 30_000 },
    async () => {
      vi.mocked(loadHighlighter).mockRejectedValueOnce(new Error("renderer failed"));
      render(<CanvasPreview kind="code" language="python" content="print('fallback')" />);
      const plain = await screen.findByTestId("canvas-preview-plain", {}, { timeout: 20_000 });
      expect(plain.textContent).toContain("print('fallback')");
    },
  );

  it("labels unsupported preview kinds", () => {
    render(<CanvasPreview kind="binary" content="..." />);
    expect(screen.getByTestId("canvas-preview-unsupported")).toBeInTheDocument();
  });
});

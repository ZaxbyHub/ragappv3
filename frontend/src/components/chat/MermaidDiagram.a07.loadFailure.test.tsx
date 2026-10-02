// frontend/src/components/chat/MermaidDiagram.a07.loadFailure.test.tsx
// A07 (AC10): MermaidDiagram lazy-imports the mermaid module inside its
// useEffect. When that import REJECTS (bundle fails to load), the component
// currently has no catch on the import itself: the rejection goes unhandled
// and the permanent "Rendering diagram…" spinner stays mounted. The loading
// state must resolve into some visible fallback/error content instead.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

describe("MermaidDiagram load failure (A07 AC10)", () => {
  afterEach(() => {
    vi.doUnmock("mermaid");
  });

  it("failed mermaid import shows a fallback", async () => {
    // The component imports mermaid lazily inside useEffect, so a fresh
    // module registry plus a doMock'ed failing factory makes that import
    // itself reject.
    vi.resetModules();
    vi.doMock("mermaid", () => {
      throw new Error("mermaid bundle unavailable");
    });
    const { default: MermaidDiagram } = await import("./MermaidDiagram");

    const { container } = render(<MermaidDiagram chart="graph TD; A-->B" />);

    await waitFor(() => {
      expect(screen.queryByTestId("mermaid-loading")).toBeNull();
    });

    // Something readable replaced the spinner.
    const text = (container.textContent ?? "").trim();
    expect(text.length).toBeGreaterThan(0);
  });
});

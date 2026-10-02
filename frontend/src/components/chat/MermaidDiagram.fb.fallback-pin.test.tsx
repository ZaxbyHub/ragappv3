// frontend/src/components/chat/MermaidDiagram.fb.fallback-pin.test.tsx
// PR #835 feedback PRR-006 / external F-6: the a07 load-failure check only
// asserts "spinner gone + some text exists", which any non-empty node
// satisfies. This pin asserts the SPECIFIC fallback: the mermaid-error
// alert (data-testid="mermaid-error", role="alert") carrying the failure
// message.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

describe("MermaidDiagram load failure fallback identity (fb PRR-006)", () => {
  afterEach(() => {
    vi.doUnmock("mermaid");
  });

  it("failed import shows the mermaid-error alert, not just any content", async () => {
    vi.resetModules();
    vi.doMock("mermaid", () => {
      throw new Error("mermaid bundle unavailable");
    });
    const { default: MermaidDiagram } = await import("./MermaidDiagram");

    render(<MermaidDiagram chart="graph TD; A-->B" />);

    const alert = await screen.findByRole("alert");
    expect(alert.getAttribute("data-testid")).toBe("mermaid-error");
    // The fallback identity: the component's own error alert with its
    // heading (the thrown factory message is wrapped by vitest, so pin the
    // rendered heading rather than the raw message).
    expect(alert.textContent).toContain("Diagram rendering error");
    expect(screen.queryByTestId("mermaid-loading")).toBeNull();

    // The assertion above must stay honest: a tree that merely renders any
    // non-empty node (the weakness the a07 check tolerates) fails here.
    await waitFor(() => {
      expect(screen.queryByTestId("mermaid-error")).not.toBeNull();
    });
  });
});

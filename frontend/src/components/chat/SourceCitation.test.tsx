// frontend/src/components/chat/SourceCitation.test.tsx
// Regression tests for the snippet tooltip on SourceCitation (issue #43).
// Covers:
//   - absence of snippet (undefined, null, empty string) → no tooltip text rendered
//   - boundary length: exactly 100 chars → no ellipsis
//   - overflow length: 101+ chars → first 100 chars + Unicode ellipsis
//   - escaping: HTML-like snippet text renders as literal text, not DOM
//
// The Radix-based Tooltip is mocked as a transparent passthrough (matches the
// pattern in MessageContent.memoization.test.tsx) so the snippet text becomes
// directly queryable in the rendered DOM, which is what we actually want to
// assert: did the truncation + conditional render logic do the right thing?

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SourceCitation } from "./SourceCitation";
import type { Source } from "@/lib/api";

vi.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children, asChild: _asChild }: { children: React.ReactNode; asChild?: boolean }) => (
    <>{children}</>
  ),
}));

const noop = () => {};

const ELLIPSIS = "\u2026"; // the single-character ellipsis used in SourceCitation.tsx

function makeSource(overrides: Partial<Source> = {}): Source {
  return {
    id: "src-1",
    filename: "report.pdf",
    ...overrides,
  };
}

describe("SourceCitation snippet tooltip (issue #43)", () => {
  it("does not render any snippet text when source.snippet is undefined", () => {
    const source = makeSource();
    expect(source.snippet).toBeUndefined();

    const { container } = render(
      <SourceCitation source={source} index={0} onClick={noop} />
    );

    // The trigger button still renders, but no <p> with snippet text.
    expect(screen.getByRole("button", { name: /Source S1: report\.pdf/ })).toBeInTheDocument();
    expect(container.querySelector("p")).toBeNull();
  });

  it("does not render any snippet text when source.snippet is explicitly null", () => {
    // Cast through unknown to construct a Source with a null snippet on purpose.
    const source = makeSource({ snippet: null as unknown as string });

    const { container } = render(
      <SourceCitation source={source} index={0} onClick={noop} />
    );

    expect(screen.getByRole("button", { name: /Source S1: report\.pdf/ })).toBeInTheDocument();
    expect(container.querySelector("p")).toBeNull();
  });

  it("does not render any snippet text when source.snippet is the empty string", () => {
    // The conditional render uses `source.snippet && ...`, so "" must short-circuit.
    const source = makeSource({ snippet: "" });

    const { container } = render(
      <SourceCitation source={source} index={0} onClick={noop} />
    );

    expect(screen.getByRole("button", { name: /Source S1: report\.pdf/ })).toBeInTheDocument();
    expect(container.querySelector("p")).toBeNull();
  });

  it("renders the snippet in full with no ellipsis when it is exactly 100 characters", () => {
    const exact = "a".repeat(100);
    const source = makeSource({ snippet: exact });

    render(<SourceCitation source={source} index={0} onClick={noop} />);

    // Full 100-char string is present, no ellipsis appended.
    const para = screen.getByText(exact);
    expect(para.tagName.toLowerCase()).toBe("p");
    expect(para.textContent).toBe(exact);
    expect(para.textContent).not.toContain(ELLIPSIS);
  });

  it("renders the snippet in full with no ellipsis when it is exactly 1 character", () => {
    // Sanity-check the lower boundary, complements the 100-char boundary.
    const source = makeSource({ snippet: "x" });

    render(<SourceCitation source={source} index={0} onClick={noop} />);

    // The aria-label "Source S1: report.pdf" contains "S1" which is not "x",
    // so getByText("x", { exact: true }) is unambiguous here.
    const para = screen.getByText("x");
    expect(para.tagName.toLowerCase()).toBe("p");
    expect(para.textContent).toBe("x");
    expect(para.textContent).not.toContain(ELLIPSIS);
  });

  it("truncates to the first 100 characters and appends an ellipsis at 101 characters", () => {
    const exact = "a".repeat(100);
    const overflow = exact + "Z"; // 101 chars total
    const source = makeSource({ snippet: overflow });

    render(<SourceCitation source={source} index={0} onClick={noop} />);

    const expected = exact + ELLIPSIS;
    const para = screen.getByText(expected);
    expect(para.tagName.toLowerCase()).toBe("p");
    // The trailing "Z" must NOT appear — only the truncated prefix + ellipsis.
    expect(para.textContent).toBe(expected);
    expect(para.textContent?.endsWith("Z")).toBe(false);
  });

  it("truncates to the first 100 characters and appends an ellipsis for much longer snippets", () => {
    const overflow = "lorem ipsum ".repeat(200); // ~2400 chars
    const source = makeSource({ snippet: overflow });

    render(<SourceCitation source={source} index={0} onClick={noop} />);

    const expected = overflow.slice(0, 100) + ELLIPSIS;
    const para = screen.getByText(expected);
    expect(para.tagName.toLowerCase()).toBe("p");
    expect(para.textContent).toBe(expected);
    // The 101st character ("d" from the second "lorem") must not be present.
    expect(para.textContent).not.toContain("dolor");
  });

  it("renders HTML-like snippet text as literal text (escaped), not as a DOM element", () => {
    // The snippet is rendered as a child of <p>, not via dangerouslySetInnerHTML,
    // so React's text-node escaping must keep the angle brackets as text.
    const evil = "<script>alert('xss')</script> & \"quotes\" 'apos'";
    const source = makeSource({ snippet: evil });

    const { container } = render(
      <SourceCitation source={source} index={0} onClick={noop} />
    );

    // The literal string is present as text in the <p>.
    const para = screen.getByText(evil);
    expect(para.tagName.toLowerCase()).toBe("p");
    // No <script> element was ever created from the snippet.
    expect(container.querySelector("script")).toBeNull();
    // The angle brackets are encoded as entities in the rendered HTML — proves
    // the text was escaped rather than parsed as markup. (The exact entity
    // encoding for ' varies between text nodes and attribute values; we only
    // care that the dangerous characters were escaped at all.)
    expect(para.innerHTML).toContain("&lt;script&gt;");
    expect(para.innerHTML).toContain("&lt;/script&gt;");
    expect(para.innerHTML).toContain("&amp;");
  });

  it("the inline variant never renders the snippet (only filename) — guard against future drift", () => {
    // The inline variant intentionally shows the filename in the tooltip, not
    // the snippet. We assert that explicitly so a future refactor cannot quietly
    // start leaking the snippet into the inline tooltip. Note: the inline
    // variant DOES render a <p> (with the filename), so we can't use absence of
    // <p> as the signal — we have to look at its text content.
    const source = makeSource({ snippet: "should-not-appear" });

    render(
      <SourceCitation source={source} index={0} onClick={noop} variant="inline" />
    );

    // The snippet text must not appear anywhere in the rendered output.
    expect(screen.queryByText("should-not-appear")).not.toBeInTheDocument();
    // The filename does appear (this is what the inline tooltip is for).
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
  });
});

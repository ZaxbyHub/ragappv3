import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { DraftPreview } from "./DraftPreview";

describe("DraftPreview", () => {
  it("renders a heading, a list and a GFM table", () => {
    const content = [
      "# Title",
      "",
      "- one",
      "- two",
      "",
      "| A | B |",
      "| --- | --- |",
      "| 1 | 2 |",
    ].join("\n");

    render(<DraftPreview content={content} />);

    expect(screen.getByRole("heading", { level: 1, name: "Title" })).toBeInTheDocument();
    expect(screen.getByText("one")).toBeInTheDocument();
    expect(screen.getByText("two")).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("XSS gate: strips a <script> tag and never executes it", () => {
    const marker = "__draft_preview_script_xss__";
    const { container } = render(
      <DraftPreview content={`Hello\n\n<script>window.${marker} = true;</script>\n\nWorld`} />
    );

    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as Record<string, unknown>)[marker]).toBeUndefined();
    expect(screen.getByText("Hello")).toBeInTheDocument();
    expect(screen.getByText("World")).toBeInTheDocument();
  });

  it("XSS gate: never renders a raw <img onerror> as a live element, and never executes it", () => {
    // react-markdown does not opt in to `allowDangerousHtml`, so raw HTML such
    // as this is dropped entirely rather than passed through — stronger than
    // attribute stripping, but the property under test is the same: the
    // handler is never wired up to the DOM and never runs.
    const marker = "__draft_preview_onerror_xss__";
    const { container } = render(
      <DraftPreview content={`<img src="x" onerror="window.${marker} = true">`} />
    );

    const img = container.querySelector("img");
    expect(img).toBeNull();
    expect(container.innerHTML).not.toMatch(/onerror/i);
    expect((window as unknown as Record<string, unknown>)[marker]).toBeUndefined();
  });

  it("XSS gate: strips a javascript: href so the link can never execute script", () => {
    const { container } = render(
      <DraftPreview content="[click me](javascript:window.__draft_preview_href_xss__=true)" />
    );

    const link = container.querySelector("a");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBeNull();
    expect(
      (window as unknown as Record<string, unknown>).__draft_preview_href_xss__
    ).toBeUndefined();
  });

  it("renders an empty state instead of crashing on empty content", () => {
    render(<DraftPreview content="" />);
    expect(screen.getByText("Nothing to preview yet.")).toBeInTheDocument();
  });

  it("renders an empty state for whitespace-only content", () => {
    render(<DraftPreview content={"   \n  "} />);
    expect(screen.getByText("Nothing to preview yet.")).toBeInTheDocument();
  });
});

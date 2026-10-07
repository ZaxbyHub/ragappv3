import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { PageTitleHeader } from "./PageTitleHeader";

/**
 * Issue #776 (UI-ENH-12): PageTitleHeader is the one page-header component.
 * These tests pin the slot surface the 18-page migration relies on: the
 * actions row renders and wraps, srOnly renders a bare landmark, and the
 * heading id anchor forwards.
 */
describe("PageTitleHeader (issue #776 shared header)", () => {
  it("renders title, description and actions in wrapping rows", () => {
    render(
      <PageTitleHeader
        title="Wiki"
        description="Knowledge base pages"
        actions={
          <button type="button" onClick={() => undefined}>
            New Page
          </button>
        }
      />
    );
    const heading = screen.getByRole("heading", { level: 1, name: "Wiki" });
    expect(heading).toBeInTheDocument();

    // Every base-flex row in the header carries flex-wrap (AC4/AC5 rule).
    const rows = heading.closest("div")?.parentElement;
    expect(rows?.className).toContain("flex-wrap");
    const actionRow = screen.getByRole("button", { name: "New Page" }).parentElement;
    expect(actionRow?.className).toContain("flex-wrap");

    expect(screen.getByText("Knowledge base pages")).toBeInTheDocument();
  });

  it("renders only a visually-hidden h1 in srOnly mode", () => {
    const { container } = render(<PageTitleHeader title="Chat" srOnly />);
    const heading = screen.getByRole("heading", { level: 1, name: "Chat" });
    expect(heading.className).toBe("sr-only");
    expect(container.querySelector(".bg-accent\\/50")).toBeNull();
  });

  it("forwards id to the h1 and renders the before slot first", () => {
    render(
      <PageTitleHeader
        id="canvas-page-heading"
        before={<button type="button">Back</button>}
        title="Canvas"
      />
    );
    expect(screen.getByRole("heading", { level: 1, name: "Canvas" }).id).toBe("canvas-page-heading");
    const row = screen.getByRole("heading", { level: 1 }).closest("div")?.parentElement;
    expect(row?.firstElementChild?.tagName).toBe("BUTTON");
  });
});

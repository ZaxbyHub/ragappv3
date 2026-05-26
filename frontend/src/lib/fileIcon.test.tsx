import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { FileIcon } from "./fileIcon";

function renderIcon(filename: string | null | undefined) {
  const { container } = render(<FileIcon filename={filename} className="h-4 w-4" />);
  const svg = container.querySelector("svg");
  if (!svg) throw new Error("expected FileIcon to render an svg");
  return svg;
}

describe("FileIcon", () => {
  it("renders a red PDF icon and normalizes uppercase extensions", () => {
    const svg = renderIcon("REPORT.PDF");

    expect(svg).toHaveClass("h-4", "w-4");
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveStyle({ color: "#ef4444" });
  });

  it.each([
    ["proposal.doc", "#3b82f6"],
    ["proposal.docx", "#3b82f6"],
    ["deck.pptx", "#f97316"],
    ["notes.md", "#14b8a6"],
    ["notes.mdx", "#14b8a6"],
    ["budget.xlsx", "#22c55e"],
    ["budget.xls", "#22c55e"],
    ["export.csv", "#22c55e"],
    ["data.json", "#eab308"],
    ["script.py", "#8b5cf6"],
    ["app.js", "#8b5cf6"],
    ["main.ts", "#8b5cf6"],
    ["page.html", "#8b5cf6"],
    ["style.css", "#8b5cf6"],
    ["config.xml", "#8b5cf6"],
    ["conf.yaml", "#8b5cf6"],
    ["conf.yml", "#8b5cf6"],
    ["query.sql", "#8b5cf6"],
  ])("renders the expected colored branch for %s", (filename, color) => {
    expect(renderIcon(filename)).toHaveStyle({ color });
  });

  it.each(["readme.txt", "server.log"])(
    "renders the neutral text icon branch for %s",
    (filename) => {
      const svg = renderIcon(filename);

      expect(svg).toHaveClass("lucide-file-text");
      expect(svg.getAttribute("style") ?? "").not.toContain("color:");
    }
  );

  it.each([null, undefined, "", "README", "archive.unknown"])(
    "falls back to the generic file icon for %s",
    (filename) => {
      const svg = renderIcon(filename);

      expect(svg).toHaveClass("lucide-file");
      expect(svg.getAttribute("style") ?? "").not.toContain("color:");
    }
  );
});

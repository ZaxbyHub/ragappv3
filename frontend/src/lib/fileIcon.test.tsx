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

    expect(svg).toHaveClass("h-4", "w-4", "text-filetype-pdf");
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it.each([
    ["proposal.doc", "text-filetype-docx"],
    ["proposal.docx", "text-filetype-docx"],
    ["deck.pptx", "text-filetype-pptx"],
    ["notes.md", "text-filetype-md"],
    ["notes.mdx", "text-filetype-md"],
    ["budget.xlsx", "text-filetype-xlsx"],
    ["budget.xls", "text-filetype-xlsx"],
    ["export.csv", "text-filetype-xlsx"],
    ["data.json", "text-filetype-json"],
    ["script.py", "text-filetype-code"],
    ["app.js", "text-filetype-code"],
    ["main.ts", "text-filetype-code"],
    ["page.html", "text-filetype-code"],
    ["style.css", "text-filetype-code"],
    ["config.xml", "text-filetype-code"],
    ["conf.yaml", "text-filetype-code"],
    ["conf.yml", "text-filetype-code"],
    ["query.sql", "text-filetype-code"],
  ])("renders the expected colored branch for %s", (filename, cls) => {
    expect(renderIcon(filename)).toHaveClass(cls);
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

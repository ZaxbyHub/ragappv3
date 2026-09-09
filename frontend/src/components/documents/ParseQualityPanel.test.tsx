import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { ParseQualityPanel } from "./ParseQualityPanel";
import type { Document } from "@/lib/api";

const DIAGNOSTICS = {
  pages_total: 2,
  pages_with_text: 1,
  ocr_used: true,
  low_content_pages: [2],
  tables_detected: 1,
  captions_detected: 3,
  extraction_version: "ext-9f",
};

function makeDoc(extractionDiagnostics: unknown): Document {
  return {
    id: "7",
    filename: "scanned-tables.png",
    size: 2048,
    created_at: "2026-01-01",
    metadata: { status: "indexed", chunk_count: 5 },
    ...((typeof extractionDiagnostics === "object" && extractionDiagnostics !== null)
      ? { extraction_diagnostics: extractionDiagnostics }
      : {}),
  } as Document;
}

describe("ParseQualityPanel (issue #514 / PRODUCT-ENH-06)", () => {
  it("renders extraction facts from the document's extraction_diagnostics field", () => {
    render(<ParseQualityPanel doc={makeDoc(DIAGNOSTICS)} />);

    expect(screen.getByRole("heading", { name: /parse quality/i })).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText("Used")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("ext-9f")).toBeInTheDocument();
  });

  it("renders nothing when the document carries no diagnostics", () => {
    const { container } = render(<ParseQualityPanel doc={makeDoc(null)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("reads the diagnostics from every carrier shape the payloads use", () => {
    // Top-level extraction_diagnostics (detail/status payload wire shape).
    const { rerender } = render(
      <ParseQualityPanel doc={makeDoc(DIAGNOSTICS)} />
    );
    expect(screen.getByText("ext-9f")).toBeInTheDocument();

    // metadata.extraction_diagnostics (older detail payloads).
    const metaCarrier = {
      ...makeDoc(null),
      metadata: { status: "indexed", chunk_count: 5, extraction_diagnostics: DIAGNOSTICS },
    } as Document;
    rerender(<ParseQualityPanel doc={metaCarrier} />);
    expect(screen.getByText("ext-9f")).toBeInTheDocument();
  });

  it("shows extraction facts only — never the word Chunks (that belongs to the Details card)", () => {
    render(<ParseQualityPanel doc={makeDoc(DIAGNOSTICS)} />);
    expect(screen.queryByText("Chunks")).not.toBeInTheDocument();
  });
});

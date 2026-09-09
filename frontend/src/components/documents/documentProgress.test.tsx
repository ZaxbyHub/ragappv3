import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { DocumentProgressCell, documentProgress } from "./documentProgress";
import type { Document } from "@/lib/api";

function makeDoc(overrides: Record<string, unknown>): Document {
  return {
    id: "9",
    filename: "report.pdf",
    size: 1024,
    created_at: "2024-01-01",
    ...overrides,
  } as Document;
}

describe("documentProgress partial classification (LIVE-03)", () => {
  it("renders the failure description for a partial document as completed-with-failures", () => {
    const doc = makeDoc({
      error_message: "2 chunks failed to embed",
      metadata: {
        status: "partial",
        chunk_count: 7,
        chunks_failed: 2,
        error_message: "2 chunks failed to embed",
      },
    });

    const { container } = render(<DocumentProgressCell doc={doc} />);

    expect(screen.getByText(/chunks failed to embed/i)).toBeInTheDocument();
    expect(screen.queryByText(/waiting/i)).not.toBeInTheDocument();
    expect(container.querySelector('[title*="chunks failed to embed" i]')).not.toBeNull();
  });

  it("derives the description from chunks_failed when no error message is present", () => {
    const doc = makeDoc({
      metadata: { status: "partial", chunk_count: 7, chunks_failed: 2 },
    });

    render(<DocumentProgressCell doc={doc} />);

    expect(screen.getByText(/2 chunks failed to embed/i)).toBeInTheDocument();
  });

  it("falls back to a generic completed-with-failures label with no failure detail", () => {
    const progress = documentProgress(makeDoc({ metadata: { status: "partial" } }));

    expect(progress.label).toBe("Completed with failures");
    expect(progress.isPartial).toBe(true);
    expect(progress.isActive).toBe(false);
    expect(progress.isFailed).toBe(false);
  });

  it("never renders an indeterminate progress bar for a terminal partial document", () => {
    const doc = makeDoc({
      metadata: { status: "partial", chunk_count: 7, chunks_failed: 2 },
    });

    render(<DocumentProgressCell doc={doc} />);

    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("keeps pending documents in an active state with a Waiting-style label", () => {
    const progress = documentProgress(makeDoc({ metadata: { status: "pending" } }));

    expect(progress.isActive).toBe(true);
    expect(progress.shouldRender).toBe(true);
  });
});

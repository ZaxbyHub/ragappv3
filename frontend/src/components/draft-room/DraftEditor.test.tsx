import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import * as fs from "fs";
import * as path from "path";

import { DraftEditor } from "./DraftEditor";
import { SAVE_REVISION_CONSEQUENCE } from "./labels";
import type { DraftRevisionDetail } from "@/lib/api/draftRoom";

const baseRevision: DraftRevisionDetail = {
  summary: {
    id: 1,
    revision_no: 1,
    parent_revision_id: null,
    job_id: null,
    source: "pipeline",
    content_sha256: "abc123",
    fact_status: "not_run",
    is_current: true,
    created_by: null,
    created_at: "2026-01-01T00:00:00Z",
  },
  content_md: "Original content",
  sections: [],
  citations: [],
  qa_summary: {},
};

describe("DraftEditor", () => {
  it("is fully controlled: typing calls onChange with the new value", () => {
    const onChange = vi.fn();
    render(
      <DraftEditor draftId={1} revision={baseRevision} value="Original content" onChange={onChange} />
    );
    const textarea = screen.getByLabelText("Draft content") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "Original content, revised." } });

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("Original content, revised.");
    // The component does not manage its own state — the DOM value stays at
    // the controlling `value` prop until the parent re-renders with a new one.
    expect(textarea.value).toBe("Original content");
  });

  it("shows the dirty indicator and save consequence only once value diverges from the revision", () => {
    const { rerender } = render(
      <DraftEditor draftId={1} revision={baseRevision} value="Original content" onChange={vi.fn()} />
    );
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(screen.queryByText(SAVE_REVISION_CONSEQUENCE)).not.toBeInTheDocument();

    rerender(
      <DraftEditor draftId={1} revision={baseRevision} value="Changed content" onChange={vi.fn()} />
    );
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.getByText(SAVE_REVISION_CONSEQUENCE)).toBeInTheDocument();
  });

  it("has no dirty indicator when there is no revision yet, even with non-empty value", () => {
    render(<DraftEditor draftId={1} revision={null} value="Draft from scratch" onChange={vi.fn()} />);
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("renders read-only with the disabled reason visible when disabled", () => {
    render(
      <DraftEditor
        draftId={1}
        revision={baseRevision}
        value="Original content"
        onChange={vi.fn()}
        disabled
        disabledReason="A newsroom run is active for this project."
      />
    );
    const textarea = screen.getByLabelText("Draft content") as HTMLTextAreaElement;
    expect(textarea.readOnly).toBe(true);
    expect(screen.getByText("A newsroom run is active for this project.")).toBeInTheDocument();
  });

  it("imports no rich-text editor framework and has no autosave timer", () => {
    const sourceCode = fs.readFileSync(path.resolve(__dirname, "DraftEditor.tsx"), "utf-8");
    expect(sourceCode).not.toMatch(/monaco/i);
    expect(sourceCode).not.toMatch(/codemirror/i);
    expect(sourceCode).not.toMatch(/prosemirror/i);
    expect(sourceCode).not.toMatch(/setInterval/);
  });
});

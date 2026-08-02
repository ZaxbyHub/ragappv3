import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { DraftRevisionDiff } from "./DraftRevisionDiff";
import { DIFF_LEGEND } from "./labels";
import type { DraftRevisionSummary } from "@/lib/api/draftRoom";

// Radix `Select` cannot be opened in jsdom (pointer-capture / scrollIntoView
// are unimplemented there). Replace it with a plain context-driven stand-in
// so `onValueChange` wiring is still exercised end to end. See
// `.claude/skills/ci-compatibility-audit/references/frontend-testing-gotchas.md` §2.
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  type OnValueChange = (value: string) => void;
  const SelectCtx = React.createContext<OnValueChange>(() => {});

  function Select({
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: OnValueChange;
    children?: ReactNode;
  }) {
    return React.createElement(SelectCtx.Provider, { value: onValueChange ?? (() => {}) }, children);
  }

  function SelectTrigger({ id, children }: { id?: string; children?: ReactNode }) {
    return React.createElement("div", { "data-testid": id }, children);
  }

  function SelectValue({ placeholder }: { placeholder?: string }) {
    return React.createElement("span", null, placeholder);
  }

  function SelectContent({ children }: { children?: ReactNode }) {
    return React.createElement("div", null, children);
  }

  function SelectItem({ value, children }: { value: string; children?: ReactNode }) {
    const onValueChange = React.useContext(SelectCtx);
    return React.createElement(
      "button",
      { type: "button", onClick: () => onValueChange(value) },
      children
    );
  }

  return { Select, SelectTrigger, SelectValue, SelectContent, SelectItem };
});

function makeRevision(overrides: Partial<DraftRevisionSummary>): DraftRevisionSummary {
  return {
    id: 1,
    revision_no: 1,
    parent_revision_id: null,
    job_id: null,
    source: "pipeline",
    content_sha256: "sha-1",
    fact_status: "not_run",
    is_current: false,
    created_by: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const revisions: DraftRevisionSummary[] = [
  makeRevision({ id: 1, revision_no: 1, fact_status: "passed", created_at: "2026-01-01T00:00:00Z" }),
  makeRevision({
    id: 2,
    revision_no: 2,
    source: "manual",
    fact_status: "not_run",
    created_at: "2026-01-02T00:00:00Z",
  }),
];

describe("DraftRevisionDiff", () => {
  it("renders the legend text from DIFF_LEGEND", () => {
    render(
      <DraftRevisionDiff
        revisions={revisions}
        fromRevisionId={1}
        toRevisionId={2}
        onSelectFrom={vi.fn()}
        onSelectTo={vi.fn()}
        fromContent="a"
        toContent="a"
      />
    );
    expect(screen.getByText(DIFF_LEGEND.added)).toBeInTheDocument();
    expect(screen.getByText(DIFF_LEGEND.removed)).toBeInTheDocument();
    expect(screen.getByText(DIFF_LEGEND.unchanged)).toBeInTheDocument();
  });

  it("renders added, removed and unchanged lines each with their marker and screen-reader prefix", () => {
    render(
      <DraftRevisionDiff
        revisions={revisions}
        fromRevisionId={1}
        toRevisionId={2}
        onSelectFrom={vi.fn()}
        onSelectTo={vi.fn()}
        fromContent={"line1\nline2\nline3"}
        toContent={"line1\nlineX\nline3"}
      />
    );

    const rows = screen.getAllByTestId("draft-diff-row");
    expect(rows).toHaveLength(4);

    const unchangedRow = rows.find((row) => row.dataset.diffKind === "unchanged" && row.textContent?.includes("line1"));
    const removedRow = rows.find((row) => row.dataset.diffKind === "removed");
    const addedRow = rows.find((row) => row.dataset.diffKind === "added");

    expect(unchangedRow).toBeDefined();
    expect(removedRow).toBeDefined();
    expect(addedRow).toBeDefined();

    expect(unchangedRow).toHaveTextContent("line1");
    expect(unchangedRow?.textContent).toMatch(new RegExp(`${DIFF_LEGEND.unchanged}:`));

    expect(removedRow).toHaveTextContent("line2");
    expect(removedRow?.textContent).toMatch(new RegExp(`${DIFF_LEGEND.removed}:`));

    expect(addedRow).toHaveTextContent("lineX");
    expect(addedRow?.textContent).toMatch(new RegExp(`${DIFF_LEGEND.added}:`));
  });

  it("renders the explicit 'No differences' message for identical content", () => {
    render(
      <DraftRevisionDiff
        revisions={revisions}
        fromRevisionId={1}
        toRevisionId={2}
        onSelectFrom={vi.fn()}
        onSelectTo={vi.fn()}
        fromContent={"identical text"}
        toContent={"identical text"}
      />
    );
    expect(screen.getByText("No differences between these revisions.")).toBeInTheDocument();
    expect(screen.queryByTestId("draft-diff-row")).not.toBeInTheDocument();
  });

  it("does not throw when fromContent or toContent is null", () => {
    expect(() =>
      render(
        <DraftRevisionDiff
          revisions={revisions}
          fromRevisionId={1}
          toRevisionId={null}
          onSelectFrom={vi.fn()}
          onSelectTo={vi.fn()}
          fromContent="some text"
          toContent={null}
        />
      )
    ).not.toThrow();
    expect(screen.queryByTestId("draft-diff-row")).not.toBeInTheDocument();

    expect(() =>
      render(
        <DraftRevisionDiff
          revisions={revisions}
          fromRevisionId={null}
          toRevisionId={null}
          onSelectFrom={vi.fn()}
          onSelectTo={vi.fn()}
          fromContent={null}
          toContent={null}
        />
      )
    ).not.toThrow();
  });

  it("renders a skeleton while loading instead of a flash of empty diff", () => {
    render(
      <DraftRevisionDiff
        revisions={revisions}
        fromRevisionId={1}
        toRevisionId={2}
        onSelectFrom={vi.fn()}
        onSelectTo={vi.fn()}
        fromContent="a"
        toContent="b"
        loading
      />
    );
    expect(screen.getByTestId("draft-diff-skeleton")).toBeInTheDocument();
    expect(screen.queryByTestId("draft-diff-row")).not.toBeInTheDocument();
    expect(screen.queryByText("No differences between these revisions.")).not.toBeInTheDocument();
  });

  it("calls onSelectFrom and onSelectTo when the selects change, with option text including revision metadata", () => {
    const onSelectFrom = vi.fn();
    const onSelectTo = vi.fn();
    render(
      <DraftRevisionDiff
        revisions={revisions}
        fromRevisionId={1}
        toRevisionId={2}
        onSelectFrom={onSelectFrom}
        onSelectTo={onSelectTo}
        fromContent="a"
        toContent="b"
      />
    );

    // Option text must include revision number, source, fact status and timestamp.
    const options = screen.getAllByText(/Revision 1 — Newsroom — Fact-checked —/);
    expect(options.length).toBeGreaterThan(0);
    const manualOptions = screen.getAllByText(/Revision 2 — Manual edit — Not fact-checked —/);
    expect(manualOptions.length).toBeGreaterThan(0);

    fireEvent.click(options[0]);
    expect(onSelectFrom).toHaveBeenCalledWith(1);

    fireEvent.click(manualOptions[manualOptions.length - 1]);
    expect(onSelectTo).toHaveBeenCalledWith(2);
  });

  it("has real labelled selects for From and To", () => {
    render(
      <DraftRevisionDiff
        revisions={revisions}
        fromRevisionId={1}
        toRevisionId={2}
        onSelectFrom={vi.fn()}
        onSelectTo={vi.fn()}
        fromContent="a"
        toContent="b"
      />
    );
    expect(screen.getByText("Compare from")).toBeInTheDocument();
    expect(screen.getByText("Compare to")).toBeInTheDocument();
  });
});

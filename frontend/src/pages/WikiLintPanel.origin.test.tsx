import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

// Mock API module before importing the component.
vi.mock("@/lib/api", () => ({
  resolveWikiLintFinding: vi.fn().mockResolvedValue({}),
}));

import { WikiLintPanel } from "./WikiLintPanel";
import type { WikiLintFinding } from "@/lib/api";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

beforeEach(() => {
  vi.clearAllMocks();
});

const makeFinding = (overrides: Partial<WikiLintFinding> = {}): WikiLintFinding => ({
  id: 1,
  vault_id: 1,
  finding_type: "contradiction",
  severity: "high",
  title: "Conflicting claim detected",
  details: "Two claims disagree.",
  related_page_ids_json: "[]",
  related_claim_ids_json: "[]",
  status: "open",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

const defaultProps = () => ({
  findings: [makeFinding()],
  loading: false,
  onRunLint: vi.fn(),
  vaultId: 5 as number | null,
});

// Issue #949 (AC3): the panel must surface producer provenance for every
// non-linter writer so an operator can tell a reconciler-owned row from one
// written by the invalidation pass, the compiler, or the curator.
describe("WikiLintPanel origin badge (issue #949)", () => {
  it.each(["curator", "compiler", "invalidation"] as const)(
    "renders the origin when the finding was written by %s",
    (origin) => {
      render(
        <WikiLintPanel
          {...defaultProps()}
          findings={[makeFinding({ origin })]}
        />
      );
      expect(screen.getByText(new RegExp(origin))).toBeInTheDocument();
    }
  );

  it("does not render an origin marker for linter-written findings", () => {
    render(
      <WikiLintPanel {...defaultProps()} findings={[makeFinding({ origin: "linter" })]} />
    );
    // The meta line must stay exactly the #791 shape: severity · finding_type
    // (open status adds no third segment).
    expect(screen.getByText("high · contradiction")).toBeInTheDocument();
    expect(screen.queryByText(/linter/)).not.toBeInTheDocument();
  });

  it("tolerates a payload without origin (back-compat) without rendering one", () => {
    const { origin: _origin, ...legacy } = makeFinding();
    expect(_origin).toBeUndefined();
    render(<WikiLintPanel {...defaultProps()} findings={[legacy]} />);
    expect(screen.getByText("high · contradiction")).toBeInTheDocument();
    expect(screen.getByText("Conflicting claim detected")).toBeInTheDocument();
  });

  it("renders origin alongside the status label for non-open findings", () => {
    render(
      <WikiLintPanel
        {...defaultProps()}
        findings={[makeFinding({ origin: "curator", status: "acknowledged" })]}
      />
    );
    expect(screen.getByText(/curator/)).toBeInTheDocument();
    expect(screen.getByText(/acknowledged/)).toBeInTheDocument();
  });
});

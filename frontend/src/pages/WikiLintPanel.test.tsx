import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

// Mock API module before importing the component.
vi.mock("@/lib/api", () => ({
  resolveWikiLintFinding: vi.fn().mockResolvedValue({}),
}));

import { WikiLintPanel } from "./WikiLintPanel";
import { resolveWikiLintFinding } from "@/lib/api";
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

describe("WikiLintPanel", () => {
  it("renders the finding count and finding title", () => {
    render(<WikiLintPanel {...defaultProps()} />);
    expect(screen.getByText("Lint Findings (1)")).toBeInTheDocument();
    expect(screen.getByText("Conflicting claim detected")).toBeInTheDocument();
  });

  it("renders severity and finding type metadata", () => {
    render(<WikiLintPanel {...defaultProps()} />);
    expect(screen.getByText("high · contradiction")).toBeInTheDocument();
  });

  it("Run Lint button calls onRunLint", () => {
    const props = defaultProps();
    render(<WikiLintPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /run lint/i }));
    expect(props.onRunLint).toHaveBeenCalledTimes(1);
  });

  it("Run Lint button is disabled while loading", () => {
    render(<WikiLintPanel {...defaultProps()} loading={true} />);
    expect(screen.getByRole("button", { name: /running/i })).toBeDisabled();
  });

  it("shows the empty state when there are no findings and not loading", () => {
    render(<WikiLintPanel {...defaultProps()} findings={[]} />);
    expect(screen.getByText("No findings. Run lint to check.")).toBeInTheDocument();
  });

  it("Resolve calls resolveWikiLintFinding(id, vaultId, 'resolved') then onRunLint", async () => {
    const props = defaultProps();
    render(<WikiLintPanel {...props} />);
    fireEvent.click(screen.getByTitle("Resolve"));
    await waitFor(() => {
      expect(resolveWikiLintFinding).toHaveBeenCalledWith(1, 5, "resolved");
    });
    await waitFor(() => {
      expect(props.onRunLint).toHaveBeenCalledTimes(1);
    });
  });

  it("Dismiss calls resolveWikiLintFinding(id, vaultId, 'dismissed') then onRunLint", async () => {
    const props = defaultProps();
    render(<WikiLintPanel {...props} />);
    fireEvent.click(screen.getByTitle("Dismiss"));
    await waitFor(() => {
      expect(resolveWikiLintFinding).toHaveBeenCalledWith(1, 5, "dismissed");
    });
    await waitFor(() => {
      expect(props.onRunLint).toHaveBeenCalledTimes(1);
    });
  });

  it("does NOT render Resolve/Dismiss buttons when vaultId is null", () => {
    render(<WikiLintPanel {...defaultProps()} vaultId={null} />);
    expect(screen.queryByTitle("Resolve")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Dismiss")).not.toBeInTheDocument();
  });
});

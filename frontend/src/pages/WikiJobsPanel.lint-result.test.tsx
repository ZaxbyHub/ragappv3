import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

// #1161 AC3 (frozen check C3): after a lint run, the Jobs view KEEPS the
// lint result available — persisted server-side findings (with the
// #1161-joined filename label and time labels) render in the Jobs panel.

vi.mock("@/lib/api", () => ({
  listWikiJobs: vi.fn(async () => ({ jobs: [] })),
  retryWikiJob: vi.fn(async () => ({})),
  cancelWikiJob: vi.fn(async () => ({})),
  recompileVaultWiki: vi.fn(async () => ({})),
  listWikiLintFindings: vi.fn(async () => ({ findings: [] })),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import { listWikiJobs, listWikiLintFindings } from "@/lib/api";
import { WikiJobsPanel } from "./WikiJobsPanel";

function finding(overrides: Record<string, unknown> = {}) {
  return {
    id: 11,
    vault_id: 7,
    finding_type: "stale",
    severity: "medium",
    title: "Stale claim after recompile",
    details: "",
    related_page_ids_json: "[]",
    related_claim_ids_json: "[]",
    status: "open",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-02-02T00:00:00Z",
    origin: "linter",
    source_filename: "report.txt",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  (listWikiJobs as ReturnType<typeof vi.fn>).mockResolvedValue({ jobs: [] });
});

describe("WikiJobsPanel persisted lint (#1161 AC3 / C3)", () => {
  it("keeps the lint result available in the Jobs view with filename and time labels", async () => {
    (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({
      findings: [
        finding(),
        finding({ id: 12, status: "resolved", title: "Duplicate entity resolved", source_filename: null }),
      ],
    });
    render(<WikiJobsPanel vaultId={7} />);
    const section = await screen.findByTestId("jobs-lint-section");
    expect(section).toBeInTheDocument();
    // Persisted history: the run's findings remain visible from the Jobs view.
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.getByText("Duplicate entity resolved")).toBeInTheDocument();
    // Filename label (joined source) and time label on the finding rows
    // (the panel-local formatter renders month/day + time).
    expect(screen.getByText("report.txt")).toBeInTheDocument();
    expect(
      screen.getAllByText(/^([A-Z][a-z]{2} \d)/).length
    ).toBeGreaterThan(0);
    // Status counts over the persisted history.
    expect(section).toHaveTextContent("open: 1");
    expect(section).toHaveTextContent("resolved: 1");
  });

  it("fetches the persisted findings through the same lease discipline as jobs", async () => {
    (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({
      findings: [],
    });
    render(<WikiJobsPanel vaultId={7} />);
    await waitFor(() => {
      expect(listWikiJobs).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(listWikiLintFindings).toHaveBeenCalledWith({ vault_id: 7 });
    });
    expect(
      await screen.findByText(/No lint findings recorded/i)
    ).toBeInTheDocument();
  });

  it("counts every finding status in the summary line", async () => {
    (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({
      findings: [
        finding({ id: 21 }),
        finding({ id: 22, status: "acknowledged", title: "Acknowledged finding" }),
        finding({ id: 23, status: "auto_resolved", title: "Auto resolved finding" }),
      ],
    });
    render(<WikiJobsPanel vaultId={7} />);
    const section = await screen.findByTestId("jobs-lint-section");
    expect(await screen.findByText("Auto resolved finding")).toBeInTheDocument();
    // The list shows acknowledged and auto-resolved rows, so the summary
    // must account for them too.
    expect(section).toHaveTextContent("open: 1");
    expect(section).toHaveTextContent("acknowledged: 1");
    expect(section).toHaveTextContent("resolved: 0");
    expect(section).toHaveTextContent("dismissed: 0");
    expect(section).toHaveTextContent("auto-resolved: 1");
  });

  it("does not present a failed lint read as 'no findings' and recovers on the next read", async () => {
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new Error("lint unavailable"))
      .mockResolvedValue({ findings: [] });
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);

    expect(await screen.findByTestId("jobs-lint-error")).toHaveTextContent(
      /Could not load lint findings/i
    );
    expect(screen.queryByText(/No lint findings recorded/i)).toBeNull();

    // The next refresh (parent-driven signal) re-reads and lands the verdict.
    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    expect(await screen.findByText(/No lint findings recorded/i)).toBeInTheDocument();
    expect(screen.queryByTestId("jobs-lint-error")).toBeNull();
  });

  it("remounts with fresh lint state on a vault switch", async () => {
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ findings: [finding()] })
      .mockRejectedValueOnce(new Error("lint unavailable for vault 8"));
    const { rerender } = render(<WikiJobsPanel vaultId={7} />);
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();

    rerender(<WikiJobsPanel vaultId={8} />);
    expect(await screen.findByTestId("jobs-lint-error")).toBeInTheDocument();
    // The previous vault's finding is never shown under the new vault.
    expect(screen.queryByText("Stale claim after recompile")).toBeNull();
  });
});

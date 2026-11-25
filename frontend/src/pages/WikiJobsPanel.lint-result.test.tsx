import { act, render, screen, waitFor } from "@testing-library/react";
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

type LintPayload = { findings: ReturnType<typeof finding>[] };

/** A lint read the test settles by hand, so "pending" is a real state. */
function deferredLint() {
  let resolve!: (value: LintPayload) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<LintPayload>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A jobs read the test settles by hand, so the refresh can be held before the lint read starts. */
function deferredJobs() {
  let resolve!: (value: { jobs: never[] }) => void;
  const promise = new Promise<{ jobs: never[] }>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Settle a held read and flush the state update it triggers. */
async function settle(work: () => void) {
  await act(async () => {
    work();
    await Promise.resolve();
  });
}

// resetAllMocks (not clearAllMocks): a per-test mockResolvedValueOnce queue
// must not leak a queued value into the next spec.
beforeEach(() => {
  vi.resetAllMocks();
  (listWikiJobs as ReturnType<typeof vi.fn>).mockResolvedValue({ jobs: [] });
  (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({ findings: [] });
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

  it("does not keep an earlier empty verdict on screen while a later lint read is pending", async () => {
    // Implementation review round 5: a refresh after a successful EMPTY read
    // (e.g. the parent-driven refresh right after a lint run) must not leave
    // "no findings" reading as current while the new read is in flight.
    const later = deferredLint();
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ findings: [] })
      .mockReturnValueOnce(later.promise);
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    expect(await screen.findByText(/No lint findings recorded/i)).toBeInTheDocument();

    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    await waitFor(() => expect(listWikiLintFindings).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/No lint findings recorded/i)).toBeNull();
    expect(screen.getByTestId("jobs-lint-loading")).toBeInTheDocument();

    await settle(() => later.resolve({ findings: [finding()] }));
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.queryByTestId("jobs-lint-loading")).toBeNull();
  });

  it("keeps the last findings visible, labelled as refreshing, while a later lint read is pending", async () => {
    const later = deferredLint();
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ findings: [finding()] })
      .mockReturnValueOnce(later.promise);
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.queryByTestId("jobs-lint-refreshing")).toBeNull();

    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    await waitFor(() => expect(listWikiLintFindings).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.getByTestId("jobs-lint-refreshing")).toBeInTheDocument();

    await settle(() => later.resolve({ findings: [] }));
    expect(await screen.findByText(/No lint findings recorded/i)).toBeInTheDocument();
    expect(screen.queryByText("Stale claim after recompile")).toBeNull();
    expect(screen.queryByTestId("jobs-lint-refreshing")).toBeNull();
  });

  it("keeps the last findings visible under the error line when a later lint read fails", async () => {
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ findings: [finding()] })
      .mockRejectedValueOnce(new Error("lint unavailable"));
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();

    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    expect(await screen.findByTestId("jobs-lint-error")).toBeInTheDocument();
    expect(screen.getByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.queryByText(/No lint findings recorded/i)).toBeNull();
  });

  it("ignores an older lint reply that lands after a newer one", async () => {
    // Only the read that still owns the token may publish.
    const older = deferredLint();
    const newer = deferredLint();
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    await waitFor(() => expect(listWikiLintFindings).toHaveBeenCalledTimes(1));
    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    await waitFor(() => expect(listWikiLintFindings).toHaveBeenCalledTimes(2));

    await settle(() =>
      newer.resolve({ findings: [finding({ id: 41, title: "Newer read finding" })] }),
    );
    expect(await screen.findByText("Newer read finding")).toBeInTheDocument();

    // The older read's reply arrives last; its token was superseded.
    await settle(() =>
      older.resolve({ findings: [finding({ id: 40, title: "Older read finding" })] }),
    );
    expect(screen.queryByText("Older read finding")).toBeNull();
    expect(screen.getByText("Newer read finding")).toBeInTheDocument();
  });

  it("ignores an older lint REJECTION that lands after a newer success", async () => {
    // Final critic round 7, mutant K: the catch arm must respect the same
    // read-token guard as the success arm — a superseded read's failure must
    // not flip the newer snapshot to the error state.
    const older = deferredLint();
    const newer = deferredLint();
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    await waitFor(() => expect(listWikiLintFindings).toHaveBeenCalledTimes(1));
    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    await waitFor(() => expect(listWikiLintFindings).toHaveBeenCalledTimes(2));

    await settle(() =>
      newer.resolve({ findings: [finding({ id: 51, title: "Newer read finding" })] }),
    );
    expect(await screen.findByText("Newer read finding")).toBeInTheDocument();

    await settle(() => older.reject(new Error("older read failed")));
    expect(screen.getByText("Newer read finding")).toBeInTheDocument();
    expect(screen.queryByTestId("jobs-lint-error")).toBeNull();
  });

  it("treats a malformed lint payload as a failed read, not as 'no findings'", async () => {
    (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({} as never);
    render(<WikiJobsPanel vaultId={7} />);
    expect(await screen.findByTestId("jobs-lint-error")).toBeInTheDocument();
    expect(screen.queryByText(/No lint findings recorded/i)).toBeNull();
  });

  it("keeps the last findings on screen when a later read's payload is malformed", async () => {
    // Final critic round 7, mutant H: the malformed arm must keep the last
    // successful findings, exactly like the rejection arm — not reset them.
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ findings: [finding()] })
      .mockResolvedValueOnce({} as never);
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();

    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    expect(await screen.findByTestId("jobs-lint-error")).toBeInTheDocument();
    expect(screen.getByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.queryByText(/No lint findings recorded/i)).toBeNull();
  });

  it("remounts with fresh lint state on a vault switch", async () => {
    // A failed read inside ONE instance keeps that instance's last findings
    // (spec above), so the old finding can only disappear because the keyed
    // state remounted for the new vault.
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

  it("marks an earlier empty lint verdict pending as soon as a refresh starts, while jobs are still loading", async () => {
    // Implementation review round 6: a refresh is jobs request FIRST, lint
    // request second. The parent-driven refresh right after a lint run must not
    // leave "no findings" on screen during the jobs half either.
    const jobsLater = deferredJobs();
    const lintLater = deferredLint();
    (listWikiJobs as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ jobs: [] })
      .mockReturnValueOnce(jobsLater.promise);
    (listWikiLintFindings as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ findings: [] })
      .mockReturnValueOnce(lintLater.promise);
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    expect(await screen.findByText(/No lint findings recorded/i)).toBeInTheDocument();

    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    await waitFor(() => expect(listWikiJobs).toHaveBeenCalledTimes(2));
    // Jobs are still pending, so the lint request has not even started.
    expect(listWikiLintFindings).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/No lint findings recorded/i)).toBeNull();
    expect(screen.getByTestId("jobs-lint-loading")).toBeInTheDocument();

    await settle(() => jobsLater.resolve({ jobs: [] }));
    await waitFor(() => expect(listWikiLintFindings).toHaveBeenCalledTimes(2));
    await settle(() => lintLater.resolve({ findings: [finding()] }));
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.queryByTestId("jobs-lint-loading")).toBeNull();
  });

  it("labels the last findings as refreshing as soon as a refresh starts, while jobs are still loading", async () => {
    const jobsLater = deferredJobs();
    (listWikiJobs as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ jobs: [] })
      .mockReturnValueOnce(jobsLater.promise);
    (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({
      findings: [finding()],
    });
    const { rerender } = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    expect(await screen.findByText("Stale claim after recompile")).toBeInTheDocument();
    expect(screen.queryByTestId("jobs-lint-refreshing")).toBeNull();

    rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    await waitFor(() => expect(listWikiJobs).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("jobs-lint-refreshing")).toBeInTheDocument();
    expect(screen.getByText("Stale claim after recompile")).toBeInTheDocument();

    await settle(() => jobsLater.resolve({ jobs: [] }));
    await waitFor(() => expect(screen.queryByTestId("jobs-lint-refreshing")).toBeNull());
    expect(screen.getByText("Stale claim after recompile")).toBeInTheDocument();
  });
});

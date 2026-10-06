/**
 * Issue #774 L03 red checkpoint — the Maintenance tab's "Recent wiki jobs"
 * list treats a failed jobs fetch as an empty list ("No recent jobs."), and
 * it fetches the unbounded job list (server-side limit never requested).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

const { mockListWikiJobs } = vi.hoisted(() => ({
  mockListWikiJobs: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listWikiJobs: mockListWikiJobs,
  recompileVaultWiki: vi.fn(),
  runWikiLint: vi.fn(),
  testConnections: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

import { MaintenanceSettings } from "./MaintenanceSettings";

describe("issue 774 MaintenanceSettings jobs fetch failure", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not show No recent jobs when the jobs fetch fails", async () => {
    mockListWikiJobs.mockRejectedValue(new Error("wiki jobs endpoint down"));

    render(<MaintenanceSettings vaultId={1} />);

    // Wait for the rejection to settle: the current catch resets the list to
    // [] and the empty-state paragraph appears — that is the defect.
    await waitFor(() => {
      expect(screen.getByText(/No recent jobs/i)).toBeInTheDocument();
    });

    expect(screen.queryAllByText("No recent jobs.").length).toBe(0);
  });
});

describe("issue 774 MaintenanceSettings jobs request limit", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListWikiJobs.mockResolvedValue({ jobs: [] });
  });

  it("requests at most 10 recent jobs from the server", async () => {
    render(<MaintenanceSettings vaultId={1} />);

    await waitFor(() => {
      expect(mockListWikiJobs).toHaveBeenCalledTimes(1);
    });

    const firstCallArg = mockListWikiJobs.mock.calls[0]?.[0] as
      | { limit?: number }
      | undefined;
    expect(firstCallArg?.limit).toBe(10);
  });
});

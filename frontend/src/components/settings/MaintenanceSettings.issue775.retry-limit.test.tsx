/**
 * Candidate coverage for the MaintenanceSettings public jobs flow.
 *
 * J currently renders the jobs error alert without a Retry control. This
 * candidate therefore records the consumer gap: it should become executable
 * after the consumer owns a retry action that reuses the same limit-10 fetch.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { MaintenanceSettings } from "./MaintenanceSettings";

describe("MaintenanceSettings recent wiki jobs retry contract", () => {
  it("shows Retry after the failed fetch and recovers with the server limit of 10", async () => {
    mockListWikiJobs
      .mockRejectedValueOnce(new Error("classified wiki jobs transport failure"))
      .mockResolvedValueOnce({
        jobs: [{ id: 42, trigger_type: "manual", trigger_id: null, status: "completed" }],
      });

    render(<MaintenanceSettings vaultId={7} />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/couldn't load/i);
    });

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => {
      expect(screen.getByText("#42")).toBeInTheDocument();
    });

    expect(mockListWikiJobs).toHaveBeenNthCalledWith(1, { vault_id: 7, limit: 10 });
    expect(mockListWikiJobs).toHaveBeenNthCalledWith(2, { vault_id: 7, limit: 10 });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

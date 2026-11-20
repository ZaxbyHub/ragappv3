// frontend/src/pages/KMSPage.audit1115.targetedpoll.test.tsx
// #1157 (audit 2026-11-15): the recompile poll's targeted read must survive
// the list route's new default page. GET /kms/jobs with an omitted limit now
// returns the default 50-row page (#1157 AC3), so a watched job older than
// the newest 50 would be invisible to the old list-and-find poll; the page
// now reads GET /kms/jobs/{id} directly. This fixture pins exactly that:
// the watched job sits BEYOND the page the list would return, and the poll
// still reaches terminal + refetch + toast through the single-job client.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render as rtlRender, screen, fireEvent, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/api", () => ({
  listKMSEntries: vi
    .fn()
    .mockResolvedValue({ entries: [], total: 0, page: 1, per_page: 200 }),
  createKMSEntry: vi.fn(),
  recompileVaultKMS: vi.fn().mockResolvedValue({ job_id: 7, status: "pending" }),
  // The list would return ONLY the newest 50 jobs — the watched job (id 7)
  // is NOT among them.
  listKMSJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  getKMSJob: vi.fn().mockResolvedValue(null),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import KMSPage from "@/pages/KMSPage";
import { listKMSEntries, recompileVaultKMS, getKMSJob } from "@/lib/api";
import { toast } from "sonner";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

const COMPLETED_JOB = {
  id: 7,
  vault_id: 1,
  status: "completed",
  error: null,
  result_json: "{}",
  created_at: "2024-01-01T00:00:00Z",
  started_at: null,
  completed_at: null,
  input_json: null,
  retry_count: 0,
};

describe("KMSPage targeted poll beyond the default page (#1157)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("watches the job through the single-job route when the list page would miss it", async () => {
    vi.useFakeTimers();
    // The single-job route answers; the list mock stays the newest-50 page
    // WITHOUT the watched job (empty here — the strongest form of "beyond
    // the page").
    vi.mocked(getKMSJob).mockResolvedValue(
      COMPLETED_JOB as Awaited<ReturnType<typeof getKMSJob>>
    );

    render(<KMSPage />);
    await act(async () => {
      vi.advanceTimersByTime(0);
    });

    fireEvent.click(screen.getByRole("button", { name: "Recompile" }));
    for (let i = 0; i < 10; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
    }

    // The poll asked the single-job route for the watched handle...
    expect(getKMSJob).toHaveBeenCalled();
    // ...reached the terminal outcome (toast + entries refetch) even though
    // the list page the old poll relied on never contained the job.
    expect(toast.success).toHaveBeenCalledWith(
      "Recompile complete — entries refreshed"
    );
    expect(listKMSEntries.mock.calls.length).toBeGreaterThan(1);
  });
});

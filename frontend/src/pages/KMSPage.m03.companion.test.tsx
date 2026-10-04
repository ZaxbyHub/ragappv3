// frontend/src/pages/KMSPage.m03.companion.test.tsx
// Issue-trace 783-kms-jobhandle-ingest-cancel — companion pins for the
// frozen C1/C2 surface (the frozen file is never edited): the terminal
// success path refetches entries and toasts success, and the unconditional
// "will refresh shortly" toast is gone. Mock idioms identical to the frozen
// m03 file. The partial-factory render pin lives in
// KMSPage.m03.partialfactory.test.tsx; the cancelled StatusBadge chip is
// pinned in StatusBadge's own suite.

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
  listKMSJobs: vi.fn().mockResolvedValue({ jobs: [] }),
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
import { listKMSEntries, listKMSJobs, recompileVaultKMS } from "@/lib/api";
import { toast } from "sonner";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

const COMPLETED_JOB = {
  id: 7,
  vault_id: 1,
  trigger_type: "settings_reindex",
  trigger_id: "vault:1",
  status: "completed",
  error: null,
  result_json: "{}",
  created_at: "2024-01-01T00:00:00Z",
  started_at: null,
  completed_at: null,
  input_json: null,
  retry_count: 0,
};

describe("KMSPage m03 companions (issue-trace 783)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("a terminal recompile job refetches entries and toasts success", async () => {
    vi.useFakeTimers();
    vi.mocked(listKMSJobs).mockResolvedValue({ jobs: [COMPLETED_JOB] });

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

    expect(recompileVaultKMS).toHaveBeenCalled();
    expect(listKMSEntries.mock.calls.length).toBeGreaterThan(1);
    expect(toast.success).toHaveBeenCalledWith(
      "Recompile complete — entries refreshed"
    );
    // The unconditional pre-fix toast copy is gone (AC2's wording).
    expect(toast.info).not.toHaveBeenCalledWith(
      "Recompile queued — document entries will refresh shortly"
    );
  });

  it("an already-terminal handle skips polling and toasts immediately (PRR-026)", async () => {
    vi.useFakeTimers();
    vi.mocked(recompileVaultKMS).mockResolvedValue({
      job_id: 11,
      status: "completed",
    });
    vi.mocked(listKMSJobs).mockResolvedValue({ jobs: [] });

    render(<KMSPage />);
    await act(async () => {
      vi.advanceTimersByTime(0);
    });

    fireEvent.click(screen.getByRole("button", { name: "Recompile" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    // The handle was already terminal: no poll, immediate toast + refetch.
    expect(listKMSJobs).not.toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith(
      "Recompile complete — entries refreshed"
    );
    expect(listKMSEntries.mock.calls.length).toBeGreaterThan(1);
  });

  it("a 'cancelled' terminal job toasts info, not an error (PRR-026)", async () => {
    vi.useFakeTimers();
    vi.mocked(recompileVaultKMS).mockResolvedValue({
      job_id: 12,
      status: "pending",
    });
    vi.mocked(listKMSJobs).mockResolvedValue({
      jobs: [{ ...COMPLETED_JOB, id: 12, status: "cancelled" }],
    });

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

    expect(toast.info).toHaveBeenCalledWith("Recompile cancelled");
    expect(listKMSEntries.mock.calls.length).toBeGreaterThan(1);
  });
});

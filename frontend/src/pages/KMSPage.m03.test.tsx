// frontend/src/pages/KMSPage.m03.test.tsx
// Issue-trace 783-kms-jobhandle-ingest-cancel — acceptance checks C1/C2 (AC1).
//
// KMSPage.handleRecompile currently discards the {job_id, status} handle
// returned by recompileVaultKMS and fires an unconditional toast.info. The
// fix must keep the handle and poll the job (listKMSJobs or a new getKMSJob
// single-job client) until it reaches a TERMINAL status ("completed",
// "failed", "cancelled"), then refetch the entries list and toast the real
// outcome.
//
// Expected pre-fix (base) verdicts:
//   C1 RED with "expected 0 to be greater than 0" (no poll client is called)
//   C2 RED with "expected 1 to be greater than 1"  (entries are never
//   refetched after the queued recompile; the initial load is the only call)
//
// Mock idioms reused (not invented):
// - @/lib/api factory mock set from frontend/src/tests/issue515-kms.test.tsx
//   (recompileVaultKMS / listKMSJobs members), extended with a getKMSJob
//   member: the factory defines the mocked module surface, so the post-fix
//   single-job client resolves there too without a named import that would
//   break module linking at base (getKMSJob does not exist yet).
// - useVaultStore hook mock + VaultSelector data-testid stub + sonner toast
//   mock from frontend/src/pages/KMSPage.m02.test.tsx (issue-trace 782).
// - MemoryRouter wrapper: KMSPage calls useNavigate.
// - Fake timers with advanceTimersByTimeAsync (never runAllTimers: a polling
//   loop with real awaits hangs under runAllTimers).

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
  // The factory defines the module surface: getKMSJob is not exported today,
  // but the post-fix single-job client resolves here when the page adopts it.
  getKMSJob: vi.fn().mockResolvedValue(null),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

// Same data-testid stub as the m01/m02 fixtures.
vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import KMSPage from "@/pages/KMSPage";
import { listKMSEntries, listKMSJobs, recompileVaultKMS } from "@/lib/api";
import type { KMSCompileJob } from "@/lib/api";
import * as apiModule from "@/lib/api";
import { toast } from "sonner";

// getKMSJob is accessed through the mocked module namespace (cast, not a
// named import) so the file links at base where the export does not exist.
type MockFn = ReturnType<typeof vi.fn>;
const getKMSJobMock = (apiModule as unknown as Record<string, MockFn>).getKMSJob;

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

function makeJob(overrides: Partial<KMSCompileJob> = {}): KMSCompileJob {
  return {
    id: 7,
    vault_id: 1,
    trigger_type: "manual",
    trigger_id: null,
    status: "completed",
    error: null,
    result_json: "{}",
    created_at: "2024-01-01T00:00:00Z",
    started_at: null,
    completed_at: null,
    input_json: null,
    retry_count: 0,
    ...overrides,
  };
}

/** Advance fake timers generously but boundedly: 10 poll ticks of 1s. */
async function advancePollWindow() {
  for (let i = 0; i < 10; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
  }
}

describe("KMSPage m03 (issue-trace 783-kms-jobhandle-ingest-cancel)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("recompile polls the returned job handle", async () => {
    vi.useFakeTimers();
    vi.mocked(recompileVaultKMS).mockResolvedValue({
      job_id: 7,
      status: "pending",
    });
    // BOTH client shapes resolve a terminal job: whichever the fix adopts,
    // polling must happen. listKMSJobs returns the handle's terminal job;
    // getKMSJob returns the single job directly.
    vi.mocked(listKMSJobs).mockResolvedValue({
      jobs: [makeJob({ id: 7, status: "completed" })],
    });
    getKMSJobMock.mockResolvedValue(makeJob({ id: 7, status: "completed" }));

    render(<KMSPage />);

    // Initial load fires (empty search -> 0ms debounce under fake timers).
    await act(async () => {
      vi.advanceTimersByTime(0);
    });

    fireEvent.click(screen.getByRole("button", { name: "Recompile" }));
    await advancePollWindow();

    // The queued handle must be polled through SOME KMS job client. At base
    // neither is called: 0 polls.
    const polls =
      vi.mocked(listKMSJobs).mock.calls.length + getKMSJobMock.mock.calls.length;
    expect(polls).toBeGreaterThan(0);
  });

  it("terminal recompile job refreshes entries and toasts the outcome", async () => {
    vi.useFakeTimers();
    const listMock = vi.mocked(listKMSEntries);
    listMock.mockResolvedValue({ entries: [], total: 0, page: 1, per_page: 200 });
    vi.mocked(recompileVaultKMS).mockResolvedValue({
      job_id: 9,
      status: "pending",
    });
    // The polled job reaches the terminal "failed" status.
    vi.mocked(listKMSJobs).mockResolvedValue({
      jobs: [makeJob({ id: 9, status: "failed", error: "compile exploded" })],
    });
    getKMSJobMock.mockResolvedValue(makeJob({ id: 9, status: "failed" }));

    render(<KMSPage />);

    // Initial load (call 1 at base).
    await act(async () => {
      vi.advanceTimersByTime(0);
    });

    fireEvent.click(screen.getByRole("button", { name: "Recompile" }));
    await advancePollWindow();

    // Once the polled job reaches a terminal status the entries list must be
    // refetched (initial load + refetch = 2+ calls). At base the recompile
    // click never refetches: exactly 1 call.
    expect(listMock.mock.calls.length).toBeGreaterThan(1);

    // A failed compile outcome must surface as an error toast.
    expect(toast.error).toHaveBeenCalled();
  });
});

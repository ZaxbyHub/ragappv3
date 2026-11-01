import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MaintenanceSettings } from "@/components/settings/MaintenanceSettings";
import { WikiJobsPanel } from "@/pages/WikiJobsPanel";
import { useAuthStore } from "@/stores/useAuthStore";
import { reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";

const api = vi.hoisted(() => ({
  listWikiJobs: vi.fn(),
  retryWikiJob: vi.fn(),
  cancelWikiJob: vi.fn(),
  recompileVaultWiki: vi.fn(),
  runWikiLint: vi.fn(),
  testConnections: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, ...api };
});

const userA = {
  id: 301,
  username: "jobs-a",
  full_name: "Jobs A",
  role: "member" as const,
  is_active: true,
};
const userB = {
  id: 302,
  username: "jobs-b",
  full_name: "Jobs B",
  role: "member" as const,
  is_active: true,
};

type JobsResponse = { jobs: WikiJob[] };
type WikiJob = {
  id: number;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  trigger_type: "ingest" | "query" | "memory" | "manual" | "settings_reindex";
  trigger_id: string | null;
  created_at: string;
  completed_at: string | null;
  error: string | null;
  result_json: string | null;
};

let authSnapshot: ReturnType<typeof useAuthStore.getState>;
const pendingSettlements = new Set<() => void>();
type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

function setAuth(user: typeof userA, token: string) {
  act(() => {
    useAuthStore.setState({
      user,
      accessToken: token,
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
    });
  });
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  let settled = false;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = (value) => {
      if (settled) return;
      settled = true;
      pendingSettlements.delete(cleanupSettlement);
      nextResolve(value);
    };
    reject = (reason) => {
      if (settled) return;
      settled = true;
      pendingSettlements.delete(cleanupSettlement);
      nextReject(reason);
    };
  });
  const cleanupSettlement = () => resolve(undefined as T);
  pendingSettlements.add(cleanupSettlement);
  return { promise, resolve, reject };
}

async function resolveDeferred<T>(held: Deferred<T>, value: T) {
  await act(async () => {
    held.resolve(value);
    await Promise.resolve();
  });
}

function job(id: number, status: WikiJob["status"] = "failed"): WikiJob {
  return {
    id,
    status,
    trigger_type: "manual",
    trigger_id: null,
    created_at: "2026-10-07T12:00:00Z",
    completed_at: null,
    error: status === "failed" ? "compile failed" : null,
    result_json: null,
  };
}

// Radix Select uses browser pointer/scroll APIs absent from jsdom.
const browserMethods = ["hasPointerCapture", "setPointerCapture", "releasePointerCapture", "scrollIntoView"] as const;
const originalMethods = new Map(browserMethods.map((name) => [name, Object.getOwnPropertyDescriptor(HTMLElement.prototype, name)]));
beforeAll(() => {
  for (const name of browserMethods) {
    if (!originalMethods.get(name)) Object.defineProperty(HTMLElement.prototype, name, { configurable: true, value: name === "hasPointerCapture" ? () => false : () => {} });
  }
});
afterAll(() => {
  for (const name of browserMethods) {
    const original = originalMethods.get(name);
    if (original) Object.defineProperty(HTMLElement.prototype, name, original);
    else Reflect.deleteProperty(HTMLElement.prototype, name);
  }
});

beforeEach(() => {
  authSnapshot = useAuthStore.getState();
  setAuth(userA, "jobs-jwt-a");
  vi.resetAllMocks();
  api.listWikiJobs.mockResolvedValue({ jobs: [job(7)] });
  api.retryWikiJob.mockResolvedValue({ job_id: 8 });
  api.cancelWikiJob.mockResolvedValue({});
  api.recompileVaultWiki.mockResolvedValue({ job_id: 99 });
  api.runWikiLint.mockResolvedValue({ count: 0, findings: [] });
  api.testConnections.mockResolvedValue({});
});

afterEach(async () => {
  cleanup();
  reserveReplacementAuthOwner();
  await act(async () => {
    for (const settle of pendingSettlements) settle();
    await Promise.resolve();
  });
  act(() => useAuthStore.setState(authSnapshot, true));
});

describe("WikiJobsPanel issue #775 lifecycle", () => {
  it("renders a failed job and dispatches Retry with the real vault scope", async () => {
    const user = userEvent.setup();
    render(<WikiJobsPanel vaultId={42} />);
    expect(await screen.findByText("#7")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.retryWikiJob).toHaveBeenCalledWith(7, 42);
    await waitFor(() => expect(api.listWikiJobs.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("renders a load error with Retry and recovers without the false empty verdict", async () => {
    const user = userEvent.setup();
    api.listWikiJobs.mockRejectedValueOnce(new Error("jobs unavailable")).mockResolvedValueOnce({ jobs: [job(8)] });
    render(<WikiJobsPanel vaultId={42} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No jobs found")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.listWikiJobs.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(await screen.findByText("#8")).toBeInTheDocument();
  });

  it("admits a retry mutation before dispatch and gates duplicate clicks", async () => {
    const user = userEvent.setup();
    const heldRetry = deferred<{ job_id: number }>();
    api.retryWikiJob.mockReturnValue(heldRetry.promise);
    render(<WikiJobsPanel vaultId={42} />);
    await screen.findByText("#7");
    const retry = screen.getByRole("button", { name: "Retry" });

    await user.click(retry);
    expect(api.retryWikiJob).toHaveBeenCalledWith(7, 42);
    expect(api.retryWikiJob).toHaveBeenCalledTimes(1);
    expect(retry).toBeDisabled();
    await user.click(retry);
    expect(api.retryWikiJob).toHaveBeenCalledTimes(1);

    await resolveDeferred(heldRetry, { job_id: 8 });
    await waitFor(() => expect(retry).toBeEnabled());
  });

  it("ignores an old filter read in A/B/A order and publishes only the current response", async () => {
    const user = userEvent.setup();
    const first = deferred<JobsResponse>();
    const filtered = deferred<JobsResponse>();
    const final = deferred<JobsResponse>();
    api.listWikiJobs
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => filtered.promise)
      .mockImplementationOnce(() => final.promise);
    render(<WikiJobsPanel vaultId={42} />);
    await waitFor(() => expect(api.listWikiJobs).toHaveBeenCalledWith({ vault_id: 42, status: undefined }));

    const filter = screen.getByRole("combobox");
    await user.click(filter);
    await user.click(screen.getByRole("option", { name: "Failed" }));
    await waitFor(() => expect(api.listWikiJobs).toHaveBeenCalledWith({ vault_id: 42, status: "failed" }));
    await user.click(screen.getByRole("combobox"));
    await user.click(screen.getByRole("option", { name: "All statuses" }));
    await waitFor(() => expect(api.listWikiJobs).toHaveBeenCalledTimes(3));

    await resolveDeferred(first, { jobs: [job(1)] });
    await resolveDeferred(filtered, { jobs: [job(2)] });
    expect(screen.queryByText("#1")).not.toBeInTheDocument();
    expect(screen.queryByText("#2")).not.toBeInTheDocument();
    await resolveDeferred(final, { jobs: [job(3)] });
    expect(await screen.findByText("#3")).toBeInTheDocument();
  });

  it("does not let an old retry finalizer clear the replacement owner's busy state", async () => {
    const user = userEvent.setup();
    const retryA = deferred<{ job_id: number }>();
    const retryB = deferred<{ job_id: number }>();
    api.retryWikiJob.mockImplementationOnce(() => retryA.promise).mockImplementationOnce(() => retryB.promise);
    render(<WikiJobsPanel vaultId={42} />);
    await screen.findByText("#7");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.retryWikiJob).toHaveBeenCalledTimes(1);

    setAuth(userB, "jobs-jwt-b");
    const replacementRetry = await screen.findByRole("button", { name: "Retry" });
    await user.click(replacementRetry);
    expect(api.retryWikiJob).toHaveBeenCalledTimes(2);
    expect(replacementRetry).toBeDisabled();
    await resolveDeferred(retryA, { job_id: 8 });
    expect(replacementRetry).toBeDisabled();
    await resolveDeferred(retryB, { job_id: 9 });
    await waitFor(() => expect(replacementRetry).toBeEnabled());
  });
});

describe("MaintenanceSettings issue #775 lifecycle", () => {
  it("forwards limit=10 and reads the current job list after recompile", async () => {
    const user = userEvent.setup();
    api.listWikiJobs.mockResolvedValue({ jobs: [] });
    render(<MaintenanceSettings vaultId={9} />);
    await waitFor(() => expect(api.listWikiJobs).toHaveBeenCalledWith({ vault_id: 9, limit: 10 }));

    await user.click(screen.getByRole("button", { name: "Recompile wiki (current vault)" }));
    expect(api.recompileVaultWiki).toHaveBeenCalledWith(9);
    await waitFor(() => expect(api.listWikiJobs).toHaveBeenCalledTimes(2));
    expect(api.listWikiJobs).toHaveBeenLastCalledWith({ vault_id: 9, limit: 10 });
  });

  it("keeps the replacement owner's recompile busy state when the old request settles", async () => {
    const user = userEvent.setup();
    const recompileA = deferred<{ job_id: number }>();
    const recompileB = deferred<{ job_id: number }>();
    api.recompileVaultWiki.mockImplementationOnce(() => recompileA.promise).mockImplementationOnce(() => recompileB.promise);
    render(<MaintenanceSettings vaultId={9} />);
    await waitFor(() => expect(api.listWikiJobs).toHaveBeenCalledWith({ vault_id: 9, limit: 10 }));

    const button = screen.getByRole("button", { name: "Recompile wiki (current vault)" });
    await user.click(button);
    expect(api.recompileVaultWiki).toHaveBeenCalledWith(9);
    expect(button).toBeDisabled();

    setAuth(userB, "jobs-jwt-b");
    const replacementButton = await screen.findByRole("button", { name: "Recompile wiki (current vault)" });
    await user.click(replacementButton);
    expect(api.recompileVaultWiki).toHaveBeenCalledTimes(2);
    expect(replacementButton).toBeDisabled();
    await resolveDeferred(recompileA, { job_id: 100 });
    expect(replacementButton).toBeDisabled();
    await resolveDeferred(recompileB, { job_id: 101 });
    await waitFor(() => expect(replacementButton).toBeEnabled());
  });
});

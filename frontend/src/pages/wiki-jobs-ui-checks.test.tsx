import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WikiCompileJob } from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";

let publicAuthSnapshot: ReturnType<typeof useAuthStore.getState>;
const publicUserA = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "member" as const,
  is_active: true,
};
const publicUserSameAccount = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "member" as const,
  is_active: true,
};
const publicUserDifferentAccount = {
  id: 202,
  username: "owner-b",
  full_name: "Owner B",
  role: "member" as const,
  is_active: true,
};

function setPublicAuthUser(user: typeof publicUserA, token: string) {
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

function restorePublicAuthState() {
  act(() => useAuthStore.setState(publicAuthSnapshot, true));
}
import { MaintenanceSettings } from "@/components/settings/MaintenanceSettings";
import { WikiJobsPanel } from "@/pages/WikiJobsPanel";

const {
  cancelWikiJobMock,
  listWikiJobsMock,
  recompileVaultWikiMock,
  retryWikiJobMock,
  runWikiLintMock,
  testConnectionsMock,
  toastErrorMock,
  toastSuccessMock,
} = vi.hoisted(() => {
  return {
    cancelWikiJobMock: vi.fn(),
    listWikiJobsMock: vi.fn(),
    recompileVaultWikiMock: vi.fn(),
    retryWikiJobMock: vi.fn(),
    runWikiLintMock: vi.fn(),
    testConnectionsMock: vi.fn(),
    toastErrorMock: vi.fn(),
    toastSuccessMock: vi.fn(),
  };
});

const buttonHandlers = new Map<string, () => unknown>();

function renderedText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(renderedText).join(" ");
  if (value && typeof value === "object" && "props" in value) {
    return renderedText((value as { props?: { children?: unknown } }).props?.children);
  }
  return "";
}

function renderedIconKey(value: unknown): string {
  if (Array.isArray(value)) return value.map(renderedIconKey).find(Boolean) || "";
  if (value && typeof value === "object" && "type" in value) {
    const key = (value as { type?: { issue774HandlerKey?: string } }).type?.issue774HandlerKey;
    if (key) return key;
  }
  if (value && typeof value === "object" && "props" in value) {
    const props = (value as { props?: { children?: unknown; "data-handler-key"?: unknown } }).props;
    if (typeof props?.["data-handler-key"] === "string") return props["data-handler-key"];
    return renderedIconKey(props?.children);
  }
  return "";
}

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    cancelWikiJob: cancelWikiJobMock,
    listWikiJobs: listWikiJobsMock,
    recompileVaultWiki: recompileVaultWikiMock,
    retryWikiJob: retryWikiJobMock,
    runWikiLint: runWikiLintMock,
    testConnections: testConnectionsMock,
  };
});
vi.mock("sonner", () => ({
  toast: { error: toastErrorMock, success: toastSuccessMock },
}));

vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: React.ReactNode }) => <section>{children}</section>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <header>{children}</header>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/ui/button", async () => {
  const React = await import("react");
  return {
    Button: ({
      children,
      disabled,
      onClick,
      title,
      type,
      ...props
    }: any) => {
      const label = String(props["aria-label"] || title || renderedText(children)).trim();
      if (onClick && label) buttonHandlers.set(label, onClick);
      const iconKey = renderedIconKey(children);
      if (onClick && iconKey) buttonHandlers.set(`icon:${iconKey}`, onClick);
      return React.createElement(
        "button",
        { ...props, disabled, onClick, title, type },
        children,
      );
    },
  };
});

vi.mock("@/components/ui/select", async () => {
  return {
    Select: ({ children, onValueChange, value }: any) => (
      <select
        aria-label="Status filter"
        value={value}
        onChange={(event) => onValueChange(event.currentTarget.value)}
      >
        {children}
      </select>
    ),
    SelectContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    SelectItem: ({ children, value }: { children: React.ReactNode; value: string }) => (
      <option value={value}>{children}</option>
    ),
    SelectTrigger: () => null,
    SelectValue: () => null,
  };
});

vi.mock("@/components/EmptyState", () => ({
  EmptyState: ({ title, description }: { title: string; description?: string }) => (
    <div role="status">
      <p>{title}</p>
      {description && <p>{description}</p>}
    </div>
  ),
}));

vi.mock("lucide-react", () => ({
  AlertCircle: () => <span />,
  ClipboardList: () => <span />,
  Loader2: () => <span />,
  RefreshCw: Object.assign(() => <span data-handler-key="refresh" />, { issue774HandlerKey: "refresh" }),
  RotateCcw: () => <span data-handler-key="rotate" />,
  X: () => <span />,
}));

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

const deferredCleanups = new Set<() => void>();

function deferred<T>(fallback: T): Deferred<T> {
  let settled = false;
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  promise.catch(() => undefined);
  const resolve = (value: T) => {
    if (settled) return;
    settled = true;
    resolvePromise(value);
  };
  const reject = (reason?: unknown) => {
    if (settled) return;
    settled = true;
    rejectPromise(reason);
  };
  deferredCleanups.add(() => resolve(fallback));
  return { promise, resolve, reject };
}

function makeJob(
  id: number,
  status: WikiCompileJob["status"],
  vaultId = 1,
  overrides: Partial<WikiCompileJob> = {},
): WikiCompileJob {
  return {
    id,
    vault_id: vaultId,
    trigger_type: "manual",
    trigger_id: `manual:${id}`,
    status,
    error: status === "failed" ? "compile failed" : null,
    result_json: "{}",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    completed_at: status === "completed" ? "2026-01-01T00:01:00Z" : null,
    retry_count: 0,
    ...overrides,
  };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  publicAuthSnapshot = useAuthStore.getState();
  setPublicAuthUser(publicUserA, "A-token");
  vi.resetAllMocks();
  buttonHandlers.clear();
  listWikiJobsMock.mockResolvedValue({ jobs: [] });
  retryWikiJobMock.mockResolvedValue(makeJob(1, "pending"));
  cancelWikiJobMock.mockResolvedValue({ job_id: 1, status: "cancelled" });
  recompileVaultWikiMock.mockResolvedValue({ job_id: 50, status: "pending" });
  runWikiLintMock.mockResolvedValue({ findings: [], count: 0 });
  testConnectionsMock.mockResolvedValue({
    backend: { ok: true },
    embeddings: { ok: true },
  });
});

afterEach(async () => {
  cleanup();
  buttonHandlers.clear();
  await act(async () => {
    for (const settle of deferredCleanups) settle();
    await Promise.resolve();
  });
  deferredCleanups.clear();
  vi.restoreAllMocks();
  restorePublicAuthState();
});

describe("MaintenanceSettings read outcomes and limit contract", () => {
  it("keeps the initial jobs state unknown and preserves the successful empty state", async () => {
    const initial = deferred<{ jobs: WikiCompileJob[] }>({ jobs: [] });
    listWikiJobsMock.mockReturnValueOnce(initial.promise);
    render(<MaintenanceSettings vaultId={42} />);

    expect(screen.queryByText("No recent jobs.")).not.toBeInTheDocument();
    expect(listWikiJobsMock).toHaveBeenCalledWith({ vault_id: 42, limit: 10 });

    await act(async () => {
      initial.resolve({ jobs: [] });
      await Promise.resolve();
    });
    expect(screen.getByText("No recent jobs.")).toBeInTheDocument();
  });

  it("distinguishes a failed initial read, retries successfully, and shows the rows", async () => {
    listWikiJobsMock
      .mockRejectedValueOnce(new Error("jobs unavailable"))
      .mockResolvedValueOnce({ jobs: [makeJob(11, "completed", 42)] });
    render(<MaintenanceSettings vaultId={42} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No recent jobs.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await flush();

    expect(listWikiJobsMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("#11")).toBeInTheDocument();
  });

  it("retains current-context rows when a refresh fails", async () => {
    listWikiJobsMock
      .mockResolvedValueOnce({ jobs: [makeJob(12, "completed", 42)] })
      .mockRejectedValueOnce(new Error("refresh unavailable"));
    render(<MaintenanceSettings vaultId={42} />);
    expect(await screen.findByText("#12")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh wiki jobs" }));
    await flush();

    expect(screen.getByText("#12")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/refresh|unavailable|load/i);
    expect(screen.queryByText("No recent jobs.")).not.toBeInTheDocument();
  });

  it("clears obsolete data and ignores a captured old refresh after owner replacement", async () => {
    const jobsA = deferred({ jobs: [makeJob(13, "completed", 42)] });
    listWikiJobsMock.mockReturnValueOnce(jobsA.promise).mockResolvedValue({ jobs: [] });
    render(<MaintenanceSettings vaultId={42} />);
    const oldRefresh = buttonHandlers.get("Refresh wiki jobs");
    expect(oldRefresh).toBeDefined();

    await act(async () => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      await Promise.resolve();
    });
    await flush();
    const callsAfterReplacement = listWikiJobsMock.mock.calls.length;
    await act(async () => {
      oldRefresh?.();
      await Promise.resolve();
    });
    expect(listWikiJobsMock).toHaveBeenCalledTimes(callsAfterReplacement);

    await act(async () => {
      jobsA.resolve({ jobs: [makeJob(13, "completed", 42)] });
      await Promise.resolve();
    });
    expect(screen.queryByText("#13")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the latest vault read and ignores an older vault completion", async () => {
    const jobsA = deferred({ jobs: [makeJob(14, "completed", 1)] });
    listWikiJobsMock
      .mockReturnValueOnce(jobsA.promise)
      .mockResolvedValueOnce({ jobs: [makeJob(15, "completed", 2)] });
    const view = render(<MaintenanceSettings vaultId={1} />);
    view.rerender(<MaintenanceSettings vaultId={2} />);
    await flush();

    expect(screen.getByText("#15")).toBeInTheDocument();
    await act(async () => {
      jobsA.resolve({ jobs: [makeJob(14, "completed", 1)] });
      await Promise.resolve();
    });
    expect(screen.getByText("#15")).toBeInTheDocument();
    expect(screen.queryByText("#14")).not.toBeInTheDocument();
  });

  it("keeps maintenance actions disabled without a vault while Test connections remains usable", async () => {
    render(<MaintenanceSettings vaultId={null} />);
    await flush();

    expect(screen.getByRole("button", { name: /recompile wiki/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /run wiki lint/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /test connections/i })).not.toBeDisabled();
    expect(screen.getByText(/require an active vault/i)).toBeInTheDocument();
    expect(listWikiJobsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /test connections/i }));
    await flush();
    expect(testConnectionsMock).toHaveBeenCalledTimes(1);
    expect(toastSuccessMock).toHaveBeenCalledWith("Connection test ok for 2/2 services");
  });

  it("rejects a captured null-vault action after owner replacement without transport or toast", async () => {
    render(<MaintenanceSettings vaultId={null} />);
    const oldRecompile = buttonHandlers.get("Recompile wiki (current vault)");
    expect(oldRecompile).toBeDefined();

    await act(async () => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      oldRecompile?.();
      await Promise.resolve();
    });

    expect(recompileVaultWikiMock).not.toHaveBeenCalled();
    expect(runWikiLintMock).not.toHaveBeenCalled();
    expect(testConnectionsMock).not.toHaveBeenCalled();
    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(toastSuccessMock).not.toHaveBeenCalled();
  });

  it("keeps Test connections busy across a vault-only rerender and re-enables after settlement", async () => {
    const connections = deferred({
      backend: { ok: true },
      embeddings: { ok: true },
    });
    testConnectionsMock.mockReturnValueOnce(connections.promise);
    const view = render(
      <StrictMode>
        <MaintenanceSettings vaultId={1} />
      </StrictMode>,
    );
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /test connections/i }));
    expect(screen.getByRole("button", { name: /test connections/i })).toBeDisabled();

    await act(async () => {
      view.rerender(
        <StrictMode>
          <MaintenanceSettings vaultId={2} />
        </StrictMode>,
      );
      await Promise.resolve();
    });
    await flush();
    expect(screen.getByRole("button", { name: /test connections/i })).toBeDisabled();

    await act(async () => {
      connections.resolve({ backend: { ok: true }, embeddings: { ok: true } });
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: /test connections/i })).not.toBeDisabled();
    expect(toastSuccessMock).toHaveBeenCalledWith("Connection test ok for 2/2 services");
  });

  it("ignores an old-vault recompile rejection while the replacement vault remains actionable", async () => {
    const recompileA = deferred({ job_id: 61, status: "pending" });
    const recompileB = deferred({ job_id: 62, status: "pending" });
    recompileVaultWikiMock.mockReturnValueOnce(recompileA.promise).mockReturnValueOnce(recompileB.promise);
    const view = render(<MaintenanceSettings vaultId={1} />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /recompile wiki/i }));
    await act(async () => {
      view.rerender(<MaintenanceSettings vaultId={2} />);
      await Promise.resolve();
    });
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /recompile wiki/i }));
    expect(recompileVaultWikiMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: /recompile wiki/i })).toBeDisabled();

    await act(async () => {
      recompileA.reject(new Error("old vault recompile failed"));
      await Promise.resolve();
    });
    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /recompile wiki/i })).toBeDisabled();

    await act(async () => {
      recompileB.resolve({ job_id: 62, status: "pending" });
      await Promise.resolve();
    });
    await flush();
    expect(toastSuccessMock).toHaveBeenCalledWith("Wiki recompile queued (job 62)");
    expect(screen.getByRole("button", { name: /recompile wiki/i })).not.toBeDisabled();
  });

  it("ignores an old-vault lint rejection while the replacement vault remains actionable", async () => {
    const lintA = deferred({ findings: [], count: 0 });
    const lintB = deferred({ findings: [], count: 0 });
    runWikiLintMock.mockReturnValueOnce(lintA.promise).mockReturnValueOnce(lintB.promise);
    const view = render(<MaintenanceSettings vaultId={1} />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /run wiki lint/i }));
    await act(async () => {
      view.rerender(<MaintenanceSettings vaultId={2} />);
      await Promise.resolve();
    });
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /run wiki lint/i }));
    expect(runWikiLintMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: /run wiki lint/i })).toBeDisabled();

    await act(async () => {
      lintA.reject(new Error("old vault lint failed"));
      await Promise.resolve();
    });
    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /run wiki lint/i })).toBeDisabled();

    await act(async () => {
      lintB.resolve({ findings: [], count: 0 });
      await Promise.resolve();
    });
    expect(toastSuccessMock).toHaveBeenCalledWith("Wiki lint produced 0 finding(s)");
    expect(screen.getByRole("button", { name: /run wiki lint/i })).not.toBeDisabled();
  });

  it("keeps recompile busy through its required refresh", async () => {
    const refresh = deferred({ jobs: [] as WikiCompileJob[] });
    const recompile = deferred({ job_id: 51, status: "pending" });
    listWikiJobsMock.mockResolvedValueOnce({ jobs: [] }).mockReturnValueOnce(refresh.promise);
    recompileVaultWikiMock.mockReturnValueOnce(recompile.promise);
    render(<MaintenanceSettings vaultId={42} />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /recompile wiki/i }));
    expect(screen.getByRole("button", { name: /recompile wiki/i })).toBeDisabled();
    await act(async () => {
      recompile.resolve({ job_id: 51, status: "pending" });
      await Promise.resolve();
    });
    await flush();
    expect(listWikiJobsMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: /recompile wiki/i })).toBeDisabled();

    await act(async () => {
      refresh.resolve({ jobs: [] });
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: /recompile wiki/i })).not.toBeDisabled();
  });
});

describe("WikiJobsPanel read outcomes, filters, and preserved display", () => {
  it("keeps the initial jobs state unknown and then shows a successful empty history", async () => {
    const initial = deferred<{ jobs: WikiCompileJob[] }>({ jobs: [] });
    listWikiJobsMock.mockReturnValueOnce(initial.promise);
    render(<WikiJobsPanel vaultId={1} />);

    expect(screen.queryByText("No jobs found")).not.toBeInTheDocument();
    expect(listWikiJobsMock).toHaveBeenCalledWith({ vault_id: 1, status: undefined });
    await act(async () => {
      initial.resolve({ jobs: [] });
      await Promise.resolve();
    });
    expect(screen.getByText("No jobs found")).toBeInTheDocument();
  });

  it("preserves the full-history omission, status filter, and refresh-signal query", async () => {
    const allJobs = Array.from({ length: 11 }, (_, index) =>
      makeJob(index + 1, "completed"),
    );
    const failedJobs = [makeJob(40, "failed")];
    listWikiJobsMock
      .mockResolvedValueOnce({ jobs: allJobs })
      .mockResolvedValue({ jobs: failedJobs });
    const view = render(<WikiJobsPanel vaultId={7} refreshSignal={0} />);
    await flush();

    expect(listWikiJobsMock).toHaveBeenCalledWith({ vault_id: 7, status: undefined });
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#11")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "failed" } });
    await flush();
    expect(listWikiJobsMock).toHaveBeenLastCalledWith({ vault_id: 7, status: "failed" });
    expect(screen.getByText("#40")).toBeInTheDocument();
    expect(screen.queryByText("#1")).not.toBeInTheDocument();

    view.rerender(<WikiJobsPanel vaultId={7} refreshSignal={1} />);
    await flush();
    expect(listWikiJobsMock).toHaveBeenLastCalledWith({ vault_id: 7, status: "failed" });
  });

  it("distinguishes a failed initial read, retries, and renders the current rows", async () => {
    listWikiJobsMock
      .mockRejectedValueOnce(new Error("wiki jobs unavailable"))
      .mockResolvedValueOnce({ jobs: [makeJob(21, "completed")] });
    render(<WikiJobsPanel vaultId={1} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No jobs found")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await flush();

    expect(listWikiJobsMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("#21")).toBeInTheDocument();
  });

  it("retains current-context rows when a WikiJobsPanel refresh fails", async () => {
    listWikiJobsMock
      .mockResolvedValueOnce({ jobs: [makeJob(22, "completed")] })
      .mockRejectedValueOnce(new Error("panel refresh unavailable"));
    render(<WikiJobsPanel vaultId={1} />);
    expect(await screen.findByText("#22")).toBeInTheDocument();

    const refreshIcon = document.querySelector('[data-handler-key="refresh"]');
    expect(refreshIcon).not.toBeNull();
    fireEvent.click(refreshIcon!);
    await flush();

    expect(screen.getByText("#22")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/refresh|unavailable|load/i);
    expect(screen.queryByText("No jobs found")).not.toBeInTheDocument();
  });

  it("hides old rows and errors across vault, principal, and status-filter replacements", async () => {
    const filterRead = deferred({ jobs: [makeJob(25, "failed")] });
    listWikiJobsMock.mockImplementation(
      ({ vault_id, status }: { vault_id?: number; status?: string }) => {
        if (status === "failed") return filterRead.promise;
        return Promise.resolve({
          jobs: [makeJob(vault_id === 2 ? 24 : 23, "completed", vault_id)],
        });
      },
    );
    const view = render(<WikiJobsPanel vaultId={1} />);
    expect(await screen.findByText("#23")).toBeInTheDocument();

    await act(async () => {
      setPublicAuthUser(publicUserDifferentAccount, "B-token");
      view.rerender(<WikiJobsPanel vaultId={2} />);
      await Promise.resolve();
    });
    await flush();
    expect(screen.getByText("#24")).toBeInTheDocument();
    expect(screen.queryByText("#23")).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "failed" } });
    await flush();
    expect(listWikiJobsMock).toHaveBeenLastCalledWith({ vault_id: 2, status: "failed" });
    expect(screen.queryByText("#24")).not.toBeInTheDocument();
    await act(async () => {
      filterRead.resolve({ jobs: [makeJob(25, "failed", 2)] });
      await Promise.resolve();
    });
    expect(screen.getByText("#25")).toBeInTheDocument();
  });

  it("does not let captured A refresh and retry handlers issue work after replacement", async () => {
    listWikiJobsMock
      .mockResolvedValueOnce({ jobs: [makeJob(26, "failed")] })
      .mockResolvedValue({ jobs: [] });
    render(<WikiJobsPanel vaultId={1} />);
    expect(await screen.findByText("#26")).toBeInTheDocument();
    const oldRefresh = buttonHandlers.get("icon:refresh");
    const oldRetry = buttonHandlers.get("Retry");
    expect(oldRefresh).toBeDefined();
    expect(oldRetry).toBeDefined();

    await act(async () => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      await Promise.resolve();
    });
    await flush();
    const listCallsAfterReplacement = listWikiJobsMock.mock.calls.length;
    const retryCallsAfterReplacement = retryWikiJobMock.mock.calls.length;
    await act(async () => {
      oldRefresh?.();
      oldRetry?.();
      await Promise.resolve();
    });
    expect(listWikiJobsMock).toHaveBeenCalledTimes(listCallsAfterReplacement);
    expect(retryWikiJobMock).toHaveBeenCalledTimes(retryCallsAfterReplacement);
  });

  it("keeps the latest vault read after an old read settles and does nothing after unmount", async () => {
    const jobsA = deferred({ jobs: [makeJob(27, "completed", 1)] });
    const late = deferred({ jobs: [makeJob(29, "completed", 2)] });
    const vault2Reads = [
      Promise.resolve({ jobs: [makeJob(28, "completed", 2)] }),
      late.promise,
    ];
    listWikiJobsMock.mockImplementation(({ vault_id }: { vault_id?: number }) => {
      if (vault_id === 1) return jobsA.promise;
      return vault2Reads.shift() ?? Promise.resolve({ jobs: [] });
    });
    const view = render(<WikiJobsPanel vaultId={1} />);
    await act(async () => {
      setPublicAuthUser(publicUserDifferentAccount, "B-token");
      view.rerender(<WikiJobsPanel vaultId={2} />);
      await Promise.resolve();
    });
    await flush();
    expect(screen.getByText("#28")).toBeInTheDocument();
    await act(async () => {
      jobsA.resolve({ jobs: [makeJob(27, "completed", 1)] });
      await Promise.resolve();
    });
    expect(screen.getByText("#28")).toBeInTheDocument();
    expect(screen.queryByText("#27")).not.toBeInTheDocument();

    await act(async () => {
      view.rerender(<WikiJobsPanel vaultId={2} refreshSignal={1} />);
      await Promise.resolve();
    });
    await flush();
    view.unmount();
    await act(async () => {
      late.resolve({ jobs: [makeJob(29, "completed", 2)] });
      await Promise.resolve();
    });
    expect(toastErrorMock).not.toHaveBeenCalled();
  });

  it("keeps a current retry busy through its required refresh when A's finalizer settles", async () => {
    const retryA = deferred(makeJob(30, "pending"));
    const retryB = deferred(makeJob(31, "pending"));
    const refreshB = deferred({ jobs: [makeJob(31, "completed")] });
    listWikiJobsMock
      .mockResolvedValueOnce({ jobs: [makeJob(30, "failed")] })
      .mockResolvedValueOnce({ jobs: [makeJob(31, "failed")] })
      .mockReturnValueOnce(refreshB.promise);
    retryWikiJobMock.mockReturnValueOnce(retryA.promise).mockReturnValueOnce(retryB.promise);
    render(<WikiJobsPanel vaultId={1} />);
    expect(await screen.findByText("#30")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await act(async () => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      await Promise.resolve();
    });
    await flush();
    expect(screen.getByText("#31")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retryWikiJobMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      retryA.resolve(makeJob(30, "pending"));
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(toastSuccessMock).not.toHaveBeenCalledWith("Job queued for retry");

    await act(async () => {
      retryB.resolve(makeJob(31, "pending"));
      await Promise.resolve();
    });
    await flush();
    expect(listWikiJobsMock).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
    await act(async () => {
      refreshB.resolve({ jobs: [makeJob(31, "completed")] });
      await Promise.resolve();
    });
    expect(toastSuccessMock).toHaveBeenCalledTimes(1);
  });

  it("does not reactivate an old retry when the vault changes away and returns", async () => {
    const retryA = deferred(makeJob(33, "pending", 1));
    retryWikiJobMock.mockReturnValueOnce(retryA.promise);
    listWikiJobsMock.mockImplementation(({ vault_id }: { vault_id: number }) =>
      Promise.resolve({ jobs: vault_id === 1 ? [makeJob(33, "failed", 1)] : [] }),
    );
    const view = render(<WikiJobsPanel vaultId={1} />);
    expect(await screen.findByText("#33")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retryWikiJobMock).toHaveBeenCalledTimes(1);
    view.rerender(<WikiJobsPanel vaultId={2} />);
    await flush();
    view.rerender(<WikiJobsPanel vaultId={1} />);
    await flush();
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Recompile" })).not.toBeDisabled();
    const currentReadCount = listWikiJobsMock.mock.calls.length;
    await act(async () => {
      retryA.resolve(makeJob(33, "pending", 1));
      await Promise.resolve();
    });
    await flush();
    expect(listWikiJobsMock).toHaveBeenCalledTimes(currentReadCount);
    expect(toastSuccessMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeDisabled();
  });
  it("preserves curator summary counts while rendering completed jobs", async () => {
    const job = makeJob(32, "completed", 1, {
      result_json: JSON.stringify({
        curator: { accepted: 3, rejected: 2, lint: 1, errors: ["timeout"], calls: 1 },
      }),
    });
    listWikiJobsMock.mockResolvedValueOnce({ jobs: [job] });
    render(<WikiJobsPanel vaultId={1} />);

    expect(await screen.findByText(/Curator accepted:/i)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("1", { selector: "strong.text-destructive" })).toBeInTheDocument();
  });
});

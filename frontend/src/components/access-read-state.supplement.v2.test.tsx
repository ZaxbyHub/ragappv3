import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VaultGroupAccessPanel } from "@/components/VaultGroupAccessPanel";
import { VaultMembersPanel } from "@/components/VaultMembersPanel";
import { useAuthStore } from "@/stores/useAuthStore";

const fixture = vi.hoisted(() => ({
  client: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

const buttonFixture = vi.hoisted(() => ({
  callbacks: [] as Array<{ label: unknown; children: unknown; onClick: () => void }>,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    default: fixture.client,
    apiClient: fixture.client,
  };
});


vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/components/ui/card", () => ({
  Card: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
  CardContent: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
  CardHeader: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
  CardTitle: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <h3 {...props}>{children}</h3>,
  CardDescription: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <p {...props}>{children}</p>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => {
    if (typeof props.onClick === "function") {
      buttonFixture.callbacks.push({ label: props["aria-label"], children, onClick: props.onClick as () => void });
    }
    return <button {...props}>{children}</button>;
  },
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/label", () => ({
  Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => <label {...props}>{children}</label>,
}));

vi.mock("@/components/ui/select", () => ({
  Select: ({ value, onValueChange, disabled, children }: React.PropsWithChildren<{ value?: string; onValueChange?: (value: string) => void; disabled?: boolean }>) => {
    const trigger = React.Children.toArray(children)[0] as React.ReactElement<{ "aria-label"?: string }> | undefined;
    return (
      <select aria-label={trigger?.props["aria-label"]} value={value} onChange={(event) => onValueChange?.(event.currentTarget.value)} disabled={disabled}>
        {children}
      </select>
    );
  },
  SelectContent: ({ children }: React.PropsWithChildren) => <>{children}</>,
  SelectItem: ({ children, value }: React.PropsWithChildren<{ value: string }>) => <option value={value}>{children}</option>,
  SelectTrigger: () => null,
  SelectValue: () => null,
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: React.PropsWithChildren<{ open?: boolean }>) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
  DialogDescription: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <p {...props}>{children}</p>,
  DialogFooter: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
  DialogHeader: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
  DialogTitle: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <h2 {...props}>{children}</h2>,
}));

vi.mock("@/components/ui/table", () => ({
  Table: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <table {...props}>{children}</table>,
  TableBody: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <tbody {...props}>{children}</tbody>,
  TableCaption: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <caption {...props}>{children}</caption>,
  TableCell: ({ children, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) => <td {...props}>{children}</td>,
  TableHead: ({ children, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) => <th {...props}>{children}</th>,
  TableHeader: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <thead {...props}>{children}</thead>,
  TableRow: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <tr {...props}>{children}</tr>,
}));

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
  settled: boolean;
};

const liveDeferreds = new Set<Deferred<unknown>>();

function deferred<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (reason?: unknown) => void;
  const state: Deferred<T> = {
    promise: undefined as unknown as Promise<T>,
    resolve: undefined as unknown as (value: T) => void,
    reject: undefined as unknown as (reason?: unknown) => void,
    settled: false,
  };
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolvePromise = promiseResolve;
    rejectPromise = promiseReject;
  });
  promise.catch(() => undefined);
  state.promise = promise;
  state.resolve = (value) => { state.settled = true; resolvePromise(value); };
  state.reject = (reason) => { state.settled = true; rejectPromise(reason); };
  liveDeferreds.add(state as Deferred<unknown>);
  return state;
}

const groupRow = (overrides: Partial<Record<string, unknown>> = {}) => ({
  group_id: 7,
  group_name: "Editors",
  org_name: "Acme",
  permission: "read",
  granted_at: "2024-01-01T00:00:00Z",
  granted_by: "owner-a",
  ...overrides,
});

const memberRow = (overrides: Partial<Record<string, unknown>> = {}) => ({
  user_id: 17,
  username: "alice",
  full_name: "Alice Example",
  permission: "read",
  granted_at: "2024-01-01T00:00:00Z",
  ...overrides,
});

describe("access panel read ownership and outcome controls", () => {
  let originalAuthState: ReturnType<typeof useAuthStore.getState>;

  beforeEach(() => {
    vi.clearAllMocks();
    Object.values(fixture.client).forEach((mock) => mock.mockReset());
    buttonFixture.callbacks.length = 0;
    originalAuthState = useAuthStore.getState();
    act(() => {
      useAuthStore.setState({
        ...originalAuthState,
        user: { id: 11, username: "owner-a", full_name: "Owner A", role: "admin", is_active: true },
        accessToken: "fixture-token-a",
        isAuthenticated: true,
        isLoading: false,
        isInitialized: true,
      });
    });
  });

  afterEach(async () => {
    cleanup();
    await act(async () => {
      for (const pending of liveDeferreds) {
        if (!pending.settled) pending.resolve(undefined);
      }
      await Promise.resolve();
    });
    liveDeferreds.clear();
    act(() => useAuthStore.setState(originalAuthState, true));
    vi.restoreAllMocks();
    buttonFixture.callbacks.length = 0;
  });

  it("keeps group reads unknown while pending, reports failure without affirmative empty, and retries through a real control", async () => {
    const first = deferred<{ data: { group_access: unknown[]; total: number } }>();
    const retry = deferred<{ data: { group_access: unknown[]; total: number } }>();
    fixture.client.get.mockReturnValueOnce(first.promise).mockReturnValueOnce(retry.promise);

    render(<VaultGroupAccessPanel vaultId={41} />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByText(/No groups have access yet/i)).not.toBeInTheDocument();

    await act(async () => first.reject(new Error("temporary read failure")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    expect(screen.queryByText(/No groups have access yet/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await act(async () => retry.resolve({ data: { group_access: [], total: 0 } }));
    await waitFor(() => expect(screen.getByText(/No groups have access yet/i)).toBeInTheDocument());
  });

  it("publishes successful empty group and member reads, while positive rows retain permission context", async () => {
    fixture.client.get
      .mockResolvedValueOnce({ data: { group_access: [], total: 0 } })
      .mockResolvedValueOnce({ data: { members: [memberRow()], total: 1 } });

    const groupView = render(<VaultGroupAccessPanel vaultId={42} />);
    await waitFor(() => expect(screen.getByText(/No groups have access yet/i)).toBeInTheDocument());
    groupView.unmount();

    render(<VaultMembersPanel vaultId={42} />);
    await waitFor(() => {
      expect(screen.getByText("Alice Example")).toBeInTheDocument();
      expect(screen.getByRole("combobox", { name: "Change permission for alice" })).toHaveValue("read");
    });
  });

  it("does not publish a previous vault response after the current vault changes", async () => {
    const first = deferred<{ data: { group_access: unknown[]; total: number } }>();
    const second = deferred<{ data: { group_access: unknown[]; total: number } }>();
    fixture.client.get.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const view = render(<VaultGroupAccessPanel vaultId={101} />);

    view.rerender(<VaultGroupAccessPanel vaultId={202} />);
    await act(async () => first.resolve({ data: { group_access: [groupRow({ group_name: "Old Vault Group" })], total: 1 } }));
    expect(screen.queryByText("Old Vault Group")).not.toBeInTheDocument();

    await act(async () => second.resolve({ data: { group_access: [groupRow({ group_name: "Current Vault Group" })], total: 1 } }));
    await waitFor(() => expect(screen.getByText("Current Vault Group")).toBeInTheDocument());
  });

  it("clears group rows, draft input, and selected removal when B is pending or fails after A", async () => {
    const bRead = deferred<{ data: { group_access: unknown[]; total: number } }>();
    fixture.client.get
      .mockResolvedValueOnce({ data: { group_access: [groupRow()], total: 1 } })
      .mockReturnValueOnce(bRead.promise);
    const view = render(<VaultGroupAccessPanel vaultId={710} />);
    await waitFor(() => expect(screen.getByText("Editors")).toBeInTheDocument());
    const draft = screen.getByRole("textbox", { name: "Group ID to grant vault access" });
    fireEvent.change(draft, { target: { value: "81" } });
    fireEvent.click(screen.getByRole("button", { name: /Revoke access for Editors/i }));
    expect(screen.getByText(/Are you sure you want to revoke vault access for/i)).toBeInTheDocument();

    view.rerender(<VaultGroupAccessPanel vaultId={711} />);
    expect(screen.getByRole("textbox", { name: "Group ID to grant vault access" })).toHaveValue("");
    expect(screen.queryByText("Editors")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Revoke$/i })).not.toBeInTheDocument();
    await act(async () => bRead.reject(new Error("B group read failed")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    expect(screen.queryByText("Editors")).not.toBeInTheDocument();
  });

  it("clears member rows, draft input, and selected removal when B is pending or fails after A", async () => {
    const bRead = deferred<{ data: { members: unknown[]; total: number } }>();
    fixture.client.get
      .mockResolvedValueOnce({ data: { members: [memberRow()], total: 1 } })
      .mockReturnValueOnce(bRead.promise);
    const view = render(<VaultMembersPanel vaultId={720} />);
    await waitFor(() => expect(screen.getByText("Alice Example")).toBeInTheDocument());
    const draft = screen.getByRole("textbox", { name: "User ID to add as vault member" });
    fireEvent.change(draft, { target: { value: "82" } });
    fireEvent.click(screen.getByRole("button", { name: /Remove alice from vault/i }));
    expect(screen.getByText(/Are you sure you want to remove/i)).toBeInTheDocument();

    view.rerender(<VaultMembersPanel vaultId={721} />);
    expect(screen.getByRole("textbox", { name: "User ID to add as vault member" })).toHaveValue("");
    expect(screen.queryByText("Alice Example")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove$/i })).not.toBeInTheDocument();
    await act(async () => bRead.reject(new Error("B member read failed")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    expect(screen.queryByText("Alice Example")).not.toBeInTheDocument();
  });

  it("ignores an old A completion across A to B to A even when the final context is A again", async () => {
    const oldA = deferred<{ data: { group_access: unknown[]; total: number } }>();
    const b = deferred<{ data: { group_access: unknown[]; total: number } }>();
    const currentA = deferred<{ data: { group_access: unknown[]; total: number } }>();
    fixture.client.get.mockReturnValueOnce(oldA.promise).mockReturnValueOnce(b.promise).mockReturnValueOnce(currentA.promise);
    const view = render(<VaultGroupAccessPanel vaultId={730} />);
    view.rerender(<VaultGroupAccessPanel vaultId={731} />);
    view.rerender(<VaultGroupAccessPanel vaultId={730} />);

    await act(async () => oldA.resolve({ data: { group_access: [groupRow({ group_name: "Stale A Group" })], total: 1 } }));
    expect(screen.queryByText("Stale A Group")).not.toBeInTheDocument();
    await act(async () => b.resolve({ data: { group_access: [groupRow({ group_name: "Stale B Group" })], total: 1 } }));
    expect(screen.queryByText("Stale B Group")).not.toBeInTheDocument();
    await act(async () => currentA.resolve({ data: { group_access: [groupRow({ group_name: "Current A Group" })], total: 1 } }));
    await waitFor(() => expect(screen.getByText("Current A Group")).toBeInTheDocument());
  });

  it("ignores an old member A completion across A to B to A even when the final context is A again", async () => {
    const oldA = deferred<{ data: { members: unknown[]; total: number } }>();
    const b = deferred<{ data: { members: unknown[]; total: number } }>();
    const currentA = deferred<{ data: { members: unknown[]; total: number } }>();
    fixture.client.get.mockReturnValueOnce(oldA.promise).mockReturnValueOnce(b.promise).mockReturnValueOnce(currentA.promise);
    const view = render(<VaultMembersPanel vaultId={740} />);
    view.rerender(<VaultMembersPanel vaultId={741} />);
    view.rerender(<VaultMembersPanel vaultId={740} />);

    await act(async () => oldA.resolve({ data: { members: [memberRow({ full_name: "Stale A Member" })], total: 1 } }));
    expect(screen.queryByText("Stale A Member")).not.toBeInTheDocument();
    await act(async () => b.resolve({ data: { members: [memberRow({ full_name: "Stale B Member" })], total: 1 } }));
    expect(screen.queryByText("Stale B Member")).not.toBeInTheDocument();
    await act(async () => currentA.resolve({ data: { members: [memberRow({ full_name: "Current A Member" })], total: 1 } }));
    await waitFor(() => expect(screen.getByText("Current A Member")).toBeInTheDocument());
  });

  it("keeps a captured Retry handler inert after principal replacement", async () => {
    const oldRead = deferred<{ data: { group_access: unknown[]; total: number } }>();
    const replacementRead = deferred<{ data: { group_access: unknown[]; total: number } }>();
    fixture.client.get.mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(replacementRead.promise);
    render(<VaultGroupAccessPanel vaultId={606} />);
    await act(async () => oldRead.reject(new Error("old vault failure")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    const obsoleteRetry = [...buttonFixture.callbacks].reverse().find(({ label, children }) => /retry/i.test(String(label ?? children)));
    expect(obsoleteRetry).toBeDefined();

    await act(async () => {
      useAuthStore.setState({
        ...originalAuthState,
        user: { id: 12, username: "owner-b", full_name: "Owner B", role: "admin", is_active: true },
        accessToken: "fixture-token-b",
        isAuthenticated: true,
        isLoading: false,
        isInitialized: true,
      });
    });
    await waitFor(() => expect(fixture.client.get).toHaveBeenCalledTimes(2));
    const callsBeforeObsoleteRetry = fixture.client.get.mock.calls.length;
    await act(async () => obsoleteRetry!.onClick());
    expect(fixture.client.get.mock.calls.length).toBe(callsBeforeObsoleteRetry);
    await act(async () => replacementRead.resolve({ data: { group_access: [], total: 0 } }));
  });

  it("does not publish a read that finishes after the auth owner is replaced", async () => {
    const pending = deferred<{ data: { members: unknown[]; total: number } }>();
    const replacement = deferred<{ data: { members: unknown[]; total: number } }>();
    fixture.client.get.mockReturnValueOnce(pending.promise).mockReturnValueOnce(replacement.promise);
    render(<VaultMembersPanel vaultId={303} />);
    await act(async () => {
      useAuthStore.setState({
        ...originalAuthState,
        user: { id: 22, username: "owner-b", full_name: "Owner B", role: "admin", is_active: true },
        accessToken: "fixture-token-b",
        isAuthenticated: true,
        isLoading: false,
        isInitialized: true,
      });
    });
    // The source has no public auth-store subscription. This wait is the required B read boundary.
    await waitFor(() => expect(fixture.client.get).toHaveBeenCalledTimes(2));

    await act(async () => pending.resolve({ data: { members: [memberRow({ full_name: "Stale Owner Row" })], total: 1 } }));
    expect(screen.queryByText("Stale Owner Row")).not.toBeInTheDocument();
    await act(async () => replacement.resolve({ data: { members: [memberRow({ full_name: "Current Owner Row" })], total: 1 } }));
    await waitFor(() => expect(screen.getByText("Current Owner Row")).toBeInTheDocument());
  });

  it("retains same-vault rows during refresh and preserves group grant payload and busy state", async () => {
    const refresh = deferred<{ data: { group_access: unknown[]; total: number } }>();
    const post = deferred<unknown>();
    fixture.client.get
      .mockResolvedValueOnce({ data: { group_access: [groupRow()], total: 1 } })
      .mockReturnValueOnce(refresh.promise);
    fixture.client.post.mockReturnValueOnce(post.promise);

    render(<VaultGroupAccessPanel vaultId={404} />);
    await waitFor(() => expect(screen.getByText("Editors")).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox", { name: "Group ID to grant vault access" }), { target: { value: "19" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Permission level for group access" }), { target: { value: "write" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Group ID to grant vault access" }).closest("form")!);
    expect(screen.getByRole("button", { name: /grant/i })).toBeDisabled();
    const postCall = fixture.client.post.mock.calls.at(-1)!;
    expect(postCall[0]).toBe("/vaults/404/group-access");
    expect(postCall[1]).toEqual({ group_id: 19, permission: "write" });
    if (postCall[2]) expect(postCall[2].signal).toBeInstanceOf(AbortSignal);

    await act(async () => post.resolve({}));
    await waitFor(() => expect(fixture.client.get).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Editors")).toBeInTheDocument();
    await act(async () => refresh.resolve({ data: { group_access: [groupRow({ permission: "write", group_name: "Editors Updated" })], total: 1 } }));
    await waitFor(() => expect(screen.getByText("Editors Updated")).toBeInTheDocument());
  });

  it("preserves member permission mutation payload, disabled busy readback, and latest permission", async () => {
    const patch = deferred<unknown>();
    fixture.client.get.mockResolvedValueOnce({ data: { members: [memberRow()], total: 1 } });
    fixture.client.patch.mockReturnValueOnce(patch.promise);

    render(<VaultMembersPanel vaultId={505} />);
    await waitFor(() => expect(screen.getByText("Alice Example")).toBeInTheDocument());
    const permission = screen.getByRole("combobox", { name: "Change permission for alice" });
    fireEvent.change(permission, { target: { value: "admin" } });
    expect(permission).toBeDisabled();
    const patchCall = fixture.client.patch.mock.calls.at(-1)!;
    expect(patchCall[0]).toBe("/vaults/505/members/17");
    expect(patchCall[1]).toEqual({ permission: "admin" });
    if (patchCall[2]) expect(patchCall[2].signal).toBeInstanceOf(AbortSignal);
    await act(async () => patch.resolve({}));
    await waitFor(() => expect(permission).toHaveValue("admin"));
    expect(permission).not.toBeDisabled();
  });
});

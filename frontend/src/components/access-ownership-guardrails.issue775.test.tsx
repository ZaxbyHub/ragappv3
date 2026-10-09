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

const dialogFixture = vi.hoisted(() => ({
  callbacks: [] as Array<{
    open?: boolean;
    onOpenChange?: (open: boolean) => void;
  }>,
}));

const selectFixture = vi.hoisted(() => ({
  callbacks: [] as Array<{
    label: unknown;
    onValueChange?: (value: string) => void;
  }>,
}));

const formFixture = vi.hoisted(() => ({
  callbacks: [] as Array<{
    onSubmit: (event: React.FormEvent<HTMLFormElement>) => unknown;
  }>,
}));

const tableFixture = vi.hoisted(() => ({
  callbacks: [] as Array<{
    label: unknown;
    onChange: (event: React.ChangeEvent<HTMLSelectElement>) => unknown;
  }>,
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
  CardContent: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => {
    const form = React.Children.toArray(children).find(
      (child) => React.isValidElement(child) && child.type === "form"
    ) as React.ReactElement<{ onSubmit?: (event: React.FormEvent<HTMLFormElement>) => unknown }> | undefined;
    if (form && typeof form.props.onSubmit === "function") {
      formFixture.callbacks.push({ onSubmit: form.props.onSubmit });
    }
    return <div {...props}>{children}</div>;
  },
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
    if (typeof onValueChange === "function") {
      selectFixture.callbacks.push({ label: trigger?.props["aria-label"], onValueChange });
    }
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
  Dialog: ({ children, open, onOpenChange }: React.PropsWithChildren<{
    open?: boolean;
    onOpenChange?: (open: boolean) => void;
  }>) => {
    if (typeof onOpenChange === "function") {
      dialogFixture.callbacks.push({ open, onOpenChange });
    }
    return open ? <div>{children}</div> : null;
  },
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
  TableCell: ({ children, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) => {
    const select = React.Children.toArray(children).find(
      (child) => React.isValidElement(child) && child.type === "select"
    ) as React.ReactElement<{ "aria-label"?: string; onChange?: (event: React.ChangeEvent<HTMLSelectElement>) => unknown }> | undefined;
    if (select && typeof select.props.onChange === "function") {
      tableFixture.callbacks.push({ label: select.props["aria-label"], onChange: select.props.onChange });
    }
    return <td {...props}>{children}</td>;
  },
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
    dialogFixture.callbacks.length = 0;
    selectFixture.callbacks.length = 0;
    formFixture.callbacks.length = 0;
    tableFixture.callbacks.length = 0;
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
    dialogFixture.callbacks.length = 0;
    selectFixture.callbacks.length = 0;
    formFixture.callbacks.length = 0;
    tableFixture.callbacks.length = 0;
  });

  function latestOpenDialog() {
    return [...dialogFixture.callbacks].reverse().find((entry) => entry.open === true);
  }

  function latestButton(pattern: RegExp) {
    return [...buttonFixture.callbacks]
      .reverse()
      .find(({ children }) => pattern.test(String(children)))?.onClick;
  }

  function latestSelect(label: string) {
    return [...selectFixture.callbacks]
      .reverse()
      .find((entry) => entry.label === label)?.onValueChange;
  }

  function latestFormSubmit() {
    return formFixture.callbacks[formFixture.callbacks.length - 1]?.onSubmit;
  }

  function latestTableChange(label: string) {
    return [...tableFixture.callbacks]
      .reverse()
      .find((entry) => entry.label === label)?.onChange;
  }

  it.each([
    {
      name: "group response missing group_access",
      panel: "group",
      malformed: { data: {} },
      valid: { data: { group_access: [groupRow({ group_name: "Recovered Group" })], total: 1 } },
      empty: /No groups have access yet/i,
      row: "Recovered Group",
    },
    {
      name: "group response with non-array group_access",
      panel: "group",
      malformed: { data: { group_access: "invalid" } },
      valid: { data: { group_access: [groupRow({ group_name: "Recovered Group" })], total: 1 } },
      empty: /No groups have access yet/i,
      row: "Recovered Group",
    },
    {
      name: "member response missing members",
      panel: "member",
      malformed: { data: {} },
      valid: { data: { members: [memberRow({ username: "recovered", full_name: "Recovered Member" })], total: 1 } },
      empty: /No members yet/i,
      row: "Recovered Member",
    },
    {
      name: "member response with non-array members",
      panel: "member",
      malformed: { data: { members: 42 } },
      valid: { data: { members: [memberRow({ username: "recovered", full_name: "Recovered Member" })], total: 1 } },
      empty: /No members yet/i,
      row: "Recovered Member",
    },
  ])("keeps $name unknown, blocks mutations, and recovers through Retry", async ({ panel, malformed, valid, empty, row }) => {
    fixture.client.get.mockResolvedValueOnce(malformed).mockResolvedValueOnce(valid);
    if (panel === "group") {
      render(<VaultGroupAccessPanel vaultId={740} />);
    } else {
      render(<VaultMembersPanel vaultId={741} />);
    }

    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    expect(screen.queryByText(empty)).not.toBeInTheDocument();
    const draft = screen.getByRole("textbox", {
      name: panel === "group" ? "Group ID to grant vault access" : "User ID to add as vault member",
    });
    fireEvent.change(draft, { target: { value: "81" } });
    expect(screen.getByRole("button", { name: panel === "group" ? /^Grant$/i : /^Add$/i })).toBeDisabled();
    fireEvent.submit(draft.closest("form")!);
    expect(fixture.client.post).not.toHaveBeenCalled();
    expect(fixture.client.patch).not.toHaveBeenCalled();
    expect(fixture.client.delete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(screen.getByText(row)).toBeInTheDocument());
  });

  it.each([
    {
      kind: "group",
      vaultId: 750,
      openButton: /Revoke access for Editors/i,
      confirm: /Are you sure you want to revoke vault access for/i,
      action: /^Revoke$/i,
    },
    {
      kind: "member",
      vaultId: 751,
      openButton: /Remove alice from vault/i,
      confirm: /Are you sure you want to remove/i,
      action: /^Remove$/i,
    },
  ])("rejects retained $kind Cancel, onOpenChange, and destructive callbacks after the same row reopens", async ({ kind, vaultId, openButton, confirm, action }) => {
    fixture.client.get.mockResolvedValue(
      kind === "group"
        ? { data: { group_access: [groupRow()], total: 1 } }
        : { data: { members: [memberRow()], total: 1 } }
    );
    fixture.client.delete.mockResolvedValue({});
    if (kind === "group") {
      render(<VaultGroupAccessPanel vaultId={vaultId} />);
      await waitFor(() => expect(screen.getByText("Editors")).toBeInTheDocument());
    } else {
      render(<VaultMembersPanel vaultId={vaultId} />);
      await waitFor(() => expect(screen.getByText("Alice Example")).toBeInTheDocument());
    }

    fireEvent.click(screen.getByRole("button", { name: openButton }));
    await waitFor(() => expect(screen.getByText(confirm)).toBeInTheDocument());
    const dialogA = latestOpenDialog();
    const closeA = dialogA?.onOpenChange;
    const cancelA = latestButton(/^Cancel$/i);
    const actionA = latestButton(action);
    expect(closeA).toBeTypeOf("function");
    expect(cancelA).toBeTypeOf("function");
    expect(actionA).toBeTypeOf("function");

    await act(async () => { closeA!(false); });
    fireEvent.click(screen.getByRole("button", { name: openButton }));
    await waitFor(() => expect(screen.getByText(confirm)).toBeInTheDocument());
    const actionB = latestButton(action);
    expect(actionB).toBeTypeOf("function");

    await act(async () => { closeA!(false); });
    expect(screen.getByText(confirm)).toBeInTheDocument();
    expect(fixture.client.delete).not.toHaveBeenCalled();
    await act(async () => { cancelA!(); });
    expect(screen.getByText(confirm)).toBeInTheDocument();
    expect(fixture.client.delete).not.toHaveBeenCalled();
    await act(async () => {
      actionA!();
      await Promise.resolve();
    });
    expect(screen.getByText(confirm)).toBeInTheDocument();
    expect(fixture.client.delete).not.toHaveBeenCalled();

    await act(async () => {
      actionB!();
      await Promise.resolve();
    });
    await waitFor(() => expect(fixture.client.delete).toHaveBeenCalledTimes(1));
  });

  it("declines a retained group Grant while a same-key read is pending or failed, then accepts current Grant", async () => {
    const initialRead = deferred<{ data: { group_access: unknown[]; total: number } }>();
    const newerRead = deferred<{ data: { group_access: unknown[]; total: number } }>();
    fixture.client.get
      .mockReturnValueOnce(initialRead.promise)
      .mockResolvedValueOnce({ data: { group_access: [], total: 0 } })
      .mockReturnValueOnce(newerRead.promise)
      .mockResolvedValueOnce({ data: { group_access: [], total: 0 } });
    fixture.client.post.mockResolvedValue({});
    render(<VaultGroupAccessPanel vaultId={760} />);
    await act(async () => initialRead.reject(new Error("initial group read failed")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    const retryA = latestButton(/retry/i);
    expect(retryA).toBeTypeOf("function");
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(screen.getByText(/No groups have access yet/i)).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox", { name: "Group ID to grant vault access" }), { target: { value: "81" } });
    const grantA = latestFormSubmit();
    expect(grantA).toBeTypeOf("function");

    await act(async () => {
      retryA!();
      await Promise.resolve();
    });
    await act(async () => {
      grantA!({ preventDefault: vi.fn() } as unknown as React.FormEvent<HTMLFormElement>);
      await Promise.resolve();
    });
    expect(fixture.client.post).not.toHaveBeenCalled();
    await act(async () => newerRead.reject(new Error("new group read failed")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    await act(async () => {
      grantA!({ preventDefault: vi.fn() } as unknown as React.FormEvent<HTMLFormElement>);
      await Promise.resolve();
    });
    expect(fixture.client.post).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(screen.getByText(/No groups have access yet/i)).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox", { name: "Group ID to grant vault access" }), { target: { value: "82" } });
    const grantCurrent = latestFormSubmit();
    expect(grantCurrent).toBeTypeOf("function");
    fireEvent.submit(screen.getByRole("textbox", { name: "Group ID to grant vault access" }).closest("form")!);
    await waitFor(() => expect(fixture.client.post).toHaveBeenCalledTimes(1));
  });

  it("declines a retained member PATCH while a same-key read is pending or failed, then accepts current PATCH", async () => {
    const initialRead = deferred<{ data: { members: unknown[]; total: number } }>();
    const newerRead = deferred<{ data: { members: unknown[]; total: number } }>();
    fixture.client.get
      .mockReturnValueOnce(initialRead.promise)
      .mockResolvedValueOnce({ data: { members: [memberRow()], total: 1 } })
      .mockReturnValueOnce(newerRead.promise)
      .mockResolvedValueOnce({ data: { members: [memberRow()], total: 1 } });
    fixture.client.patch.mockResolvedValue({});
    render(<VaultMembersPanel vaultId={770} />);
    await act(async () => initialRead.reject(new Error("initial member read failed")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    const retryA = latestButton(/retry/i);
    expect(retryA).toBeTypeOf("function");
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(screen.getByText("Alice Example")).toBeInTheDocument());
    const patchA = latestTableChange("Change permission for alice");
    expect(patchA).toBeTypeOf("function");

    await act(async () => {
      retryA!();
      await Promise.resolve();
    });
    await act(async () => {
      patchA!({ currentTarget: { value: "write" }, target: { value: "write" } } as unknown as React.ChangeEvent<HTMLSelectElement>);
      await Promise.resolve();
    });
    expect(fixture.client.patch).not.toHaveBeenCalled();
    await act(async () => newerRead.reject(new Error("new member read failed")));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());
    await act(async () => {
      patchA!({ currentTarget: { value: "write" }, target: { value: "write" } } as unknown as React.ChangeEvent<HTMLSelectElement>);
      await Promise.resolve();
    });
    expect(fixture.client.patch).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(screen.getByText("Alice Example")).toBeInTheDocument());
    const patchCurrent = latestTableChange("Change permission for alice");
    expect(patchCurrent).toBeTypeOf("function");
    fireEvent.change(screen.getByRole("combobox", { name: "Change permission for alice" }), { target: { value: "write" } });
    await waitFor(() => expect(fixture.client.patch).toHaveBeenCalledTimes(1));
  });

});

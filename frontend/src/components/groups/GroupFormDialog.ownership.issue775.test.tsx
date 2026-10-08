import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  LabelHTMLAttributes,
  ReactNode,
  TextareaHTMLAttributes,
} from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setJwtAccessToken } from "@/lib/api";
import { captureAuthOwner, reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "@/stores/useAuthStore";
import { GroupFormDialog } from "@/components/groups/GroupFormDialog";

const mockListOrganizations = vi.hoisted(() => vi.fn());
const ui = vi.hoisted(() => ({
  dialogCallbacks: [] as Array<(open: boolean) => void>,
  buttonCallbacks: new Map<string, (event: unknown) => void>(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, listOrganizations: mockListOrganizations };
});

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({
    children,
    open,
    onOpenChange,
  }: {
    children: ReactNode;
    open: boolean;
    onOpenChange?: (open: boolean) => void;
  }) => {
    if (onOpenChange) ui.dialogCallbacks.push(onOpenChange);
    return open ? <div data-testid="group-dialog">{children}</div> : null;
  },
  DialogContent: ({ children }: { children: ReactNode }) => <section>{children}</section>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: ReactNode }) => <footer>{children}</footer>,
  DialogHeader: ({ children }: { children: ReactNode }) => <header>{children}</header>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({
    children,
    onClick,
    ...props
  }: ButtonHTMLAttributes<HTMLButtonElement>) => {
    if (typeof children === "string" && onClick) {
      ui.buttonCallbacks.set(children, onClick as unknown as (event: unknown) => void);
    }
    return (
      <button {...props} onClick={onClick}>
        {children}
      </button>
    );
  },
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));
vi.mock("@/components/ui/textarea", () => ({
  Textarea: (props: TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} />,
}));
vi.mock("@/components/ui/label", () => ({
  Label: ({ children, ...props }: LabelHTMLAttributes<HTMLLabelElement>) => (
    <label {...props}>{children}</label>
  ),
}));
vi.mock("@/components/ui/select", () => ({
  Select: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children, value }: { children: ReactNode; value: string }) => (
    <option value={value}>{children}</option>
  ),
  SelectTrigger: ({ children }: { children: ReactNode }) => (
    <button type="button">{children}</button>
  ),
  SelectValue: () => null,
}));
vi.mock("lucide-react", () => ({ Loader2: () => null }));

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  promise.catch(() => undefined);
  return { promise, resolve, reject };
}

const userA = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "admin" as const,
  is_active: true,
};
const userB = {
  id: 202,
  username: "owner-b",
  full_name: "Owner B",
  role: "admin" as const,
  is_active: true,
};
const organization = { id: 7, name: "Operations" };

function resetAuth() {
  setJwtAccessToken(null);
  reserveReplacementAuthOwner();
  useAuthStore.setState({
    user: null,
    accessToken: null,
    isAuthenticated: false,
    isInitialized: false,
    initializationFailed: false,
    isLoading: false,
    needsSetup: null,
    authMode: "unknown",
  });
}

describe("GroupFormDialog ownership and organization recovery", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListOrganizations.mockReset();
    ui.dialogCallbacks.length = 0;
    ui.buttonCallbacks.clear();
    resetAuth();
  });

  afterEach(() => {
    cleanup();
    resetAuth();
  });

  it("gates a valid draft through pending and failed organization reads before current success", async () => {
    const first = deferred<unknown>();
    const retry = deferred<unknown>();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    mockListOrganizations.mockReturnValueOnce(first.promise).mockReturnValueOnce(retry.promise);

    render(
      <GroupFormDialog
        mode="create"
        open
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );
    const form = screen.getByRole("button", { name: "Create Group" }).closest("form");
    if (!form) throw new Error("create form was not rendered");
    fireEvent.change(screen.getByLabelText(/Group Name/i), {
      target: { value: "Finance" },
    });

    await waitFor(() => expect(mockListOrganizations).toHaveBeenCalledTimes(1));
    fireEvent.submit(form);
    expect(onSubmit).not.toHaveBeenCalled();

    await act(async () => {
      first.reject(new Error("organization read failed"));
      await first.promise.catch(() => undefined);
    });
    const retryButton = await screen.findByRole("button", { name: "Retry organizations" });
    fireEvent.click(retryButton);
    await waitFor(() => expect(mockListOrganizations).toHaveBeenCalledTimes(2));

    fireEvent.submit(form);
    expect(onSubmit).not.toHaveBeenCalled();
    await act(async () => {
      retry.resolve([organization]);
      await retry.promise;
    });
    expect(await screen.findByDisplayValue("Operations")).toBeInTheDocument();

    fireEvent.submit(form);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith({
      name: "Finance",
      description: "",
      org_id: 7,
    });
  });

  it.each([
    ["missing catalog", undefined],
    ["non-array catalog", { malformed: true }],
  ])("keeps a %s unknown and never treats it as an empty catalog", async (_label, malformed) => {
    mockListOrganizations
      .mockResolvedValueOnce(malformed as never)
      .mockResolvedValueOnce([organization]);

    render(
      <GroupFormDialog
        mode="create"
        open
        onOpenChange={vi.fn()}
        onSubmit={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    const retryButton = await screen.findByRole("button", { name: "Retry organizations" });
    expect(screen.queryByText("No organizations available")).not.toBeInTheDocument();
    fireEvent.click(retryButton);
    expect(await screen.findByDisplayValue("Operations")).toBeInTheDocument();
    expect(screen.queryByText("No organizations available")).not.toBeInTheDocument();
  });

  it("blocks duplicate submits from one opening before React commits loading state", async () => {
    const submit = deferred<void>();
    const onSubmit = vi.fn().mockReturnValue(submit.promise);
    mockListOrganizations.mockResolvedValue([organization]);

    render(
      <GroupFormDialog
        mode="create"
        open
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );
    expect(await screen.findByDisplayValue("Operations")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Group Name/i), {
      target: { value: "Finance" },
    });
    const form = screen.getByRole("button", { name: "Create Group" }).closest("form");
    if (!form) throw new Error("create form was not rendered");

    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(onSubmit).toHaveBeenCalledTimes(1);

    await act(async () => {
      submit.resolve();
      await submit.promise;
    });
  });

  it("rejects retained opening A callbacks after close and reopen to B", async () => {
    const submitA = deferred<void>();
    const onOpenChange = vi.fn();
    const onSubmit = vi.fn().mockReturnValue(submitA.promise);
    mockListOrganizations.mockResolvedValue([organization]);

    const view = render(
      <GroupFormDialog
        mode="create"
        open
        onOpenChange={onOpenChange}
        onSubmit={onSubmit}
      />,
    );
    expect(await screen.findByDisplayValue("Operations")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Group Name/i), {
      target: { value: "Group A" },
    });
    const formA = screen.getByRole("button", { name: "Create Group" }).closest("form");
    if (!formA) throw new Error("create form A was not rendered");
    const dialogA = ui.dialogCallbacks[ui.dialogCallbacks.length - 1];
    const cancelA = ui.buttonCallbacks.get("Cancel");
    if (!dialogA || !cancelA) throw new Error("opening A callbacks were not captured");

    fireEvent.submit(formA);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    view.rerender(
      <GroupFormDialog
        mode="create"
        open={false}
        onOpenChange={onOpenChange}
        onSubmit={onSubmit}
      />,
    );
    view.rerender(
      <GroupFormDialog
        mode="create"
        open
        onOpenChange={onOpenChange}
        onSubmit={onSubmit}
      />,
    );
    expect(await screen.findByDisplayValue("Operations")).toBeInTheDocument();
    const callsBeforeStale = onOpenChange.mock.calls.length;

    await act(async () => {
      dialogA(false);
      cancelA({});
      submitA.resolve();
      await submitA.promise;
    });

    expect(onOpenChange.mock.calls.length).toBe(callsBeforeStale);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("group-dialog")).toBeInTheDocument();
  });

  it("retires opening A callbacks on same-owner principal replacement before rerender", async () => {
    mockListOrganizations.mockResolvedValue([organization]);
    const onOpenChange = vi.fn();
    const submitA = deferred<void>();
    const onSubmit = vi.fn().mockReturnValue(submitA.promise);

    setJwtAccessToken("jwt-a");
    useAuthStore.setState({
      user: userA,
      accessToken: "jwt-a",
      isAuthenticated: true,
    });
    const ownerA = captureAuthOwner();

    render(
      <GroupFormDialog
        mode="create"
        open
        onOpenChange={onOpenChange}
        onSubmit={onSubmit}
      />,
    );
    expect(await screen.findByDisplayValue("Operations")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Group Name/i), {
      target: { value: "Group A" },
    });
    const formA = screen.getByRole("button", { name: "Create Group" }).closest("form");
    if (!formA) throw new Error("create form was not rendered");
    const dialogA = ui.dialogCallbacks[ui.dialogCallbacks.length - 1];
    const cancelA = ui.buttonCallbacks.get("Cancel");
    if (!dialogA || !cancelA) throw new Error("opening A callbacks were not captured");

    fireEvent.submit(formA);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));

    await act(async () => {
      useAuthStore.setState({
        user: userB,
        accessToken: "jwt-a",
        isAuthenticated: true,
      });
      dialogA(false);
      cancelA({});
      submitA.resolve();
      await submitA.promise;
    });

    expect(captureAuthOwner()).toBe(ownerA);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

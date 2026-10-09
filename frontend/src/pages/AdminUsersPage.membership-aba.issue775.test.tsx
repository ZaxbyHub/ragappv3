import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AdminUsersPage from "@/pages/AdminUsersPage";
import {
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
};

const mocks = vi.hoisted(() => ({
  api: {
    get: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
  auth: {
    state: {
      user: {
        id: 1,
        username: "admin",
        full_name: "Admin",
        role: "superadmin",
        is_active: true,
      },
      isAuthenticated: true,
      isLoading: false,
    },
    useAuthStore: vi.fn(),
  },
  toast: { error: vi.fn(), success: vi.fn() },
  selectHandlers: new Map<string, (value: string) => void>(),
  sheetOnOpenChange: null as ((open: boolean) => void) | null,
  saveHandlers: [] as Array<() => unknown>,
}));

vi.mock("@/lib/api", () => ({ default: mocks.api }));
vi.mock("@/stores/useAuthStore", () => ({ useAuthStore: mocks.auth.useAuthStore }));
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => false }));
vi.mock("@/hooks/useDebounce", () => ({ useDebounce: (value: string) => [value] }));
vi.mock("sonner", () => ({ toast: mocks.toast }));
vi.mock("@/components/auth/RoleGuard", () => ({
  AdminGuard: ({ children }: any) => <>{children}</>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: any) => {
    if (props["aria-label"] === "Save group changes" && typeof props.onClick === "function") {
      mocks.saveHandlers.push(props.onClick);
    }
    return <button {...props}>{children}</button>;
  },
}));
vi.mock("@/components/ui/input", () => ({
  Input: (props: any) => <input {...props} />,
}));
vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children }: any) => <span>{children}</span>,
}));
vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({ checked, onCheckedChange, children, ...props }: any) => (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={() => onCheckedChange?.(!checked)}
      {...props}
    >
      {children}
    </button>
  ),
}));
vi.mock("@/components/ui/label", () => ({
  Label: ({ children, ...props }: any) => <label {...props}>{children}</label>,
}));
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: any) => <div>{children}</div>,
}));
vi.mock("@/components/ui/skeleton", () => ({ Skeleton: () => <span data-testid="skeleton" /> }));
vi.mock("@/components/ui/table", () => ({
  Table: ({ children }: any) => <table>{children}</table>,
  TableBody: ({ children }: any) => <tbody>{children}</tbody>,
  TableCaption: ({ children }: any) => <caption>{children}</caption>,
  TableCell: ({ children, ...props }: any) => <td {...props}>{children}</td>,
  TableHead: ({ children, ...props }: any) => <th {...props}>{children}</th>,
  TableHeader: ({ children }: any) => <thead>{children}</thead>,
  TableRow: ({ children }: any) => <tr>{children}</tr>,
}));
vi.mock("@/components/ui/sheet", () => ({
  Sheet: ({ children, open, onOpenChange }: any) => {
    if (open) mocks.sheetOnOpenChange = onOpenChange;
    return open ? <div data-testid="sheet">{children}</div> : null;
  },
  SheetContent: ({ children }: any) => <section>{children}</section>,
  SheetDescription: ({ children }: any) => <p>{children}</p>,
  SheetFooter: ({ children }: any) => <footer>{children}</footer>,
  SheetHeader: ({ children }: any) => <header>{children}</header>,
  SheetTitle: ({ children, ...props }: any) => <h2 {...props}>{children}</h2>,
}));

vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const triggerType = Symbol("SelectTrigger");
  const itemType = Symbol("SelectItem");
  const findTrigger = (children: React.ReactNode): Record<string, unknown> => {
    let result: Record<string, unknown> = {};
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== "object") return;
      if (child.type?.__type === triggerType) result = child.props ?? {};
      else if (child.props?.children) {
        const nested = findTrigger(child.props.children);
        if (Object.keys(nested).length) result = nested;
      }
    });
    return result;
  };
  const findItems = (children: React.ReactNode): Array<{ value: string; label: React.ReactNode }> => {
    const result: Array<{ value: string; label: React.ReactNode }> = [];
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== "object") return;
      if (child.type?.__type === itemType) result.push({ value: child.props.value, label: child.props.children });
      else if (child.props?.children) result.push(...findItems(child.props.children));
    });
    return result;
  };
  return {
    Select: ({ children, value, onValueChange, disabled }: {
      children: React.ReactNode;
      value?: string;
      onValueChange?: (value: string) => void;
      disabled?: boolean;
    }) => {
      const trigger = findTrigger(children);
      const label = trigger["aria-label"];
      if (typeof label === "string" && onValueChange) mocks.selectHandlers.set(label, onValueChange);
      return (
        <select
          value={value}
          disabled={disabled}
          aria-label={trigger["aria-label"] as string | undefined}
          onChange={(event) => onValueChange?.(event.target.value)}
        >
          {findItems(children).map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
      );
    },
    SelectContent: ({ children }: any) => <>{children}</>,
    SelectItem: Object.assign(({ children }: any) => <>{children}</>, { __type: itemType }),
    SelectTrigger: Object.assign(({ children, ...props }: any) => <span {...props}>{children}</span>, { __type: triggerType }),
    SelectValue: () => null,
  };
});

vi.mock("@/components/ui/pagination", () => ({
  Pagination: ({ page, onPageChange }: { page: number; onPageChange: (page: number) => void }) => (
    <nav>
      <button type="button" aria-label="Previous page" onClick={() => onPageChange(Math.max(1, page - 1))}>Previous</button>
      <button type="button" aria-label="Next page" onClick={() => onPageChange(page + 1)}>Next</button>
    </nav>
  ),
}));

vi.mock("@/components/layout/PageTitleHeader", () => ({
  PageTitleHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));
vi.mock("@/components/LoadingSpinner", () => ({ LoadingSpinner: ({ label }: { label?: string }) => <div role="status">{label}</div> }));
vi.mock("@/components/EmptyState", () => ({ EmptyState: ({ title }: { title: string }) => <div role="status">{title}</div> }));
vi.mock("./AdminUsersPage/DeleteUserDialog", () => ({ DeleteUserDialog: () => null }));
vi.mock("./AdminUsersPage/EditUserDialog", () => ({ EditUserDialog: () => null }));
vi.mock("./AdminUsersPage/ResetPasswordDialog", () => ({ ResetPasswordDialog: () => null }));
vi.mock("./AdminUsersPage/CreateUserDialog", () => ({ CreateUserDialog: () => null }));
vi.mock("lucide-react", () => {
  const Icon = () => null;
  return {
    Search: Icon, Trash2: Icon, Users: Icon, Pencil: Icon, KeyRound: Icon, Plus: Icon,
    Building2: Icon, ChevronUp: Icon, ChevronDown: Icon, Loader2: Icon,
  };
});

const deferredCleanup: Array<() => void> = [];

function deferred<T>(): Deferred<T> {
  let resolve!: Deferred<T>["resolve"];
  let reject!: Deferred<T>["reject"];
  let settled = false;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = (value) => {
      settled = true;
      resolvePromise(value);
    };
    reject = (reason) => {
      settled = true;
      rejectPromise(reason);
    };
  });
  void promise.catch(() => undefined);
  deferredCleanup.push(() => {
    if (!settled) {
      settled = true;
      resolve(undefined as T);
    }
  });
  return { promise, resolve, reject };
}

const targetUser = {
  id: 2,
  username: "target",
  full_name: "Target User",
  role: "member" as const,
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
};

const usersUrl = (page = 1, search = "") =>
  `/users/?skip=${(page - 1) * 20}&limit=20&q=${encodeURIComponent(search)}`;

const getQueues = new Map<string, Array<Promise<unknown>>>();
function queueGet(url: string, value: Promise<unknown> | unknown): void {
  const queue = getQueues.get(url) ?? [];
  queue.push(Promise.resolve(value));
  getQueues.set(url, queue);
}

function installGetQueue(): void {
  mocks.api.get.mockImplementation((url: string) => {
    const queue = getQueues.get(url);
    if (!queue?.length) throw new Error(`unexpected GET ${url}`);
    return queue.shift();
  });
}

function renderPage(): void {
  render(<AdminUsersPage />);
}

function queueInitialUsers(user = targetUser, total = 40): void {
  queueGet(usersUrl(), { data: { users: [user], total } });
}

describe("AdminUsersPage issue #775 ownership and query races", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    mocks.api.get.mockReset();
    mocks.api.put.mockReset();
    mocks.api.patch.mockReset();
    getQueues.clear();
    mocks.selectHandlers.clear();
    mocks.sheetOnOpenChange = null;
    mocks.saveHandlers.length = 0;
    mocks.auth.useAuthStore.mockImplementation((selector?: (state: typeof mocks.auth.state) => unknown) =>
      selector ? selector(mocks.auth.state) : mocks.auth.state
    );
    reserveReplacementAuthOwner();
    publishAuthPrincipal({ id: 1, role: "superadmin" });
    installGetQueue();
  });

  afterEach(() => {
    for (const settle of deferredCleanup.splice(0)) settle();
    cleanup();
    reserveReplacementAuthOwner();
    publishAuthPrincipal(null);
    getQueues.clear();
    mocks.selectHandlers.clear();
    mocks.sheetOnOpenChange = null;
    mocks.saveHandlers.length = 0;
    vi.restoreAllMocks();
  });


  it("rejects a retained same-auth A membership handler after close and reopen for B", async () => {
    const groupsA = deferred<{ data: { groups: unknown[] } }>();
    const membershipsA = deferred<{ data: { groups: unknown[] } }>();
    const groupsB = deferred<{ data: { groups: unknown[] } }>();
    const membershipsB = deferred<{ data: { groups: unknown[] } }>();
    const saveB = deferred<unknown>();
    const groupA = { id: 1, name: "A Group", description: "old" };
    const groupB = { id: 2, name: "B Group", description: "current" };

    queueInitialUsers();
    queueGet("/groups", groupsA.promise);
    queueGet("/users/2/groups", membershipsA.promise);
    mocks.api.put.mockReturnValueOnce(saveB.promise);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Manage groups for target" }));
    await act(async () => {
      groupsA.resolve({ data: { groups: [groupA] } });
      membershipsA.resolve({ data: { groups: [] } });
    });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select A Group" }));
    const saveAHandler = mocks.saveHandlers.at(-1);
    if (!saveAHandler) throw new Error("expected the A save handler to be captured");

    const closeA = mocks.sheetOnOpenChange;
    if (!closeA) throw new Error("expected the A sheet close callback to be captured");
    await act(async () => closeA(false));
    expect(screen.queryByRole("button", { name: "Save group changes" })).not.toBeInTheDocument();

    queueGet("/groups", groupsB.promise);
    queueGet("/users/2/groups", membershipsB.promise);
    fireEvent.click(screen.getByRole("button", { name: "Manage groups for target" }));
    await act(async () => {
      groupsB.resolve({ data: { groups: [groupB] } });
      membershipsB.resolve({ data: { groups: [] } });
    });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select B Group" }));
    const saveBHandler = mocks.saveHandlers.at(-1);
    if (!saveBHandler) throw new Error("expected the B save handler to be captured");
    expect(saveBHandler).not.toBe(saveAHandler);

    let oldASave: unknown;
    act(() => {
      oldASave = saveAHandler();
    });
    expect(mocks.api.put).not.toHaveBeenCalled();

    let firstBSave: unknown;
    let duplicateBSave: unknown;
    act(() => {
      firstBSave = saveBHandler();
      duplicateBSave = saveBHandler();
    });
    expect(mocks.api.put).toHaveBeenCalledTimes(1);
    expect(mocks.api.put).toHaveBeenLastCalledWith("/users/2/groups", { group_ids: [2] });
    expect(screen.getByRole("button", { name: "Save group changes" })).toBeDisabled();

    await act(async () => saveB.resolve({ data: {} }));
    await oldASave;
    await firstBSave;
    await duplicateBSave;
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save group changes" })).not.toBeInTheDocument());
    expect(mocks.api.put).toHaveBeenCalledTimes(1);
    expect(mocks.toast.success).toHaveBeenCalledExactlyOnceWith("Groups updated successfully");
  });
});

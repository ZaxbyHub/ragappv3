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
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
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
    vi.restoreAllMocks();
  });

  it("keeps a reopened groups opening B authoritative and sends B's full replacement", async () => {
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
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Manage groups for target" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    queueGet("/groups", groupsB.promise);
    queueGet("/users/2/groups", membershipsB.promise);
    fireEvent.click(screen.getByRole("button", { name: "Manage groups for target" }));

    await act(async () => {
      groupsA.resolve({ data: { groups: [groupA] } });
      membershipsA.reject(new Error("retired membership read"));
    });
    expect(screen.queryByText("A Group")).not.toBeInTheDocument();
    expect(mocks.toast.error).not.toHaveBeenCalled();

    await act(async () => {
      groupsB.resolve({ data: { groups: [groupA, groupB] } });
      membershipsB.resolve({ data: { groups: [groupB] } });
    });
    expect(await screen.findByText("B Group")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select A Group" }));

    mocks.api.put.mockReturnValueOnce(saveB.promise);
    fireEvent.click(screen.getByRole("button", { name: "Save group changes" }));
    expect(mocks.api.put).toHaveBeenCalledWith("/users/2/groups", { group_ids: [2, 1] });
    await act(async () => saveB.resolve({ data: {} }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save group changes" })).not.toBeInTheDocument());
  });

  it("does not let an A organization save close or release B after same-account owner replacement", async () => {
    const orgsA = deferred<{ data: { organizations: unknown[] } }>();
    const membershipsA = deferred<{ data: { organizations: unknown[] } }>();
    const saveA = deferred<unknown>();
    const usersB = deferred<unknown>();
    const orgsB = deferred<{ data: { organizations: unknown[] } }>();
    const membershipsB = deferred<{ data: { organizations: unknown[] } }>();
    const saveB = deferred<unknown>();
    const orgA = { id: 10, name: "A Organization", description: "old" };
    const orgB = { id: 20, name: "B Organization", description: "current" };

    queueInitialUsers();
    queueGet("/organizations/", orgsA.promise);
    queueGet("/users/2/organizations", membershipsA.promise);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Manage organizations for target" }));
    await act(async () => {
      orgsA.resolve({ data: { organizations: [orgA] } });
      membershipsA.resolve({ data: { organizations: [] } });
    });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select A Organization" }));
    mocks.api.put.mockReturnValueOnce(saveA.promise).mockReturnValueOnce(saveB.promise);
    fireEvent.click(screen.getByRole("button", { name: "Save organization changes" }));
    expect(mocks.api.put).toHaveBeenCalledWith("/users/2/organizations", {
      memberships: [{ org_id: 10, role: "member" }],
    });

    queueGet(usersUrl(), usersB.promise.then(() => ({ data: { users: [targetUser], total: 40 } })));
    const closeA = mocks.sheetOnOpenChange;
    if (!closeA) throw new Error("expected the A organization sheet close callback to be captured");
    reserveReplacementAuthOwner();
    await act(async () => closeA(false));
    expect(screen.queryByRole("button", { name: "Save organization changes" })).not.toBeInTheDocument();
    await act(async () => usersB.resolve({ data: { users: [targetUser], total: 40 } }));
    expect(await screen.findByText("target")).toBeInTheDocument();

    queueGet("/organizations/", orgsB.promise);
    queueGet("/users/2/organizations", membershipsB.promise);
    fireEvent.click(screen.getByRole("button", { name: "Manage organizations for target" }));
    await act(async () => {
      orgsB.resolve({ data: { organizations: [orgB] } });
      membershipsB.resolve({ data: { organizations: [] } });
    });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select B Organization" }));

    fireEvent.click(screen.getByRole("button", { name: "Save organization changes" }));
    expect(mocks.api.put).toHaveBeenLastCalledWith("/users/2/organizations", {
      memberships: [{ org_id: 20, role: "member" }],
    });
    expect(screen.getByRole("button", { name: "Save organization changes" })).toBeDisabled();

    await act(async () => closeA(false));
    expect(screen.getByRole("button", { name: "Save organization changes" })).toBeDisabled();
    await act(async () => saveA.resolve({ data: {} }));
    expect(screen.getByRole("button", { name: "Save organization changes" })).toBeDisabled();
    expect(mocks.toast.success).not.toHaveBeenCalledWith("Organizations updated successfully");

    await act(async () => saveB.resolve({ data: {} }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save organization changes" })).not.toBeInTheDocument());
  });

  it("keeps a retained row mutation callback inert after same-account owner replacement", async () => {
    const patch = deferred<unknown>();
    const usersB = deferred<unknown>();
    queueInitialUsers();
    renderPage();
    const role = await screen.findByRole("combobox", { name: "Change role for target" });
    const retainedA = mocks.selectHandlers.get("Change role for target");
    if (!retainedA) throw new Error("expected the A role callback to be captured");
    mocks.api.patch.mockReturnValueOnce(patch.promise);
    fireEvent.change(role, { target: { value: "admin" } });
    expect(mocks.api.patch).toHaveBeenCalledWith("/users/2/role", { role: "admin" });

    queueGet(usersUrl(), usersB.promise.then(() => ({ data: { users: [targetUser], total: 40 } })));
    reserveReplacementAuthOwner();
    await act(async () => usersB.resolve({ data: { users: [targetUser], total: 40 } }));
    const replacementRole = await screen.findByRole("combobox", { name: "Change role for target" });

    await act(async () => retainedA("viewer"));
    expect(mocks.api.patch).toHaveBeenCalledTimes(1);

    const patchB = deferred<unknown>();
    mocks.api.patch.mockReturnValueOnce(patchB.promise);
    const currentB = mocks.selectHandlers.get("Change role for target");
    if (!currentB) throw new Error("expected the B role callback to be captured");
    act(() => { void currentB("admin"); });
    expect(mocks.api.patch).toHaveBeenCalledTimes(2);
    expect(mocks.api.patch).toHaveBeenLastCalledWith("/users/2/role", { role: "admin" });

    await act(async () => patch.resolve({ data: {} }));
    expect(replacementRole).toHaveValue("member");
    expect(replacementRole).toBeDisabled();
    expect(mocks.toast.success).not.toHaveBeenCalledWith("Role updated successfully");
    await act(async () => patchB.resolve({ data: {} }));
    await waitFor(() => expect(replacementRole).toHaveValue("admin"));
    expect(replacementRole).toBeEnabled();
    expect(mocks.toast.success).toHaveBeenCalledExactlyOnceWith("Role updated successfully");
  });

  it("reports row-mutation success after a search change without publishing the stale row update", async () => {
    const patch = deferred<unknown>();
    const searchResults = deferred<unknown>();
    queueInitialUsers();
    queueGet(usersUrl(1, "target"), searchResults.promise);
    renderPage();
    const role = await screen.findByRole("combobox", { name: "Change role for target" });
    mocks.api.patch.mockReturnValueOnce(patch.promise);
    fireEvent.change(role, { target: { value: "admin" } });

    fireEvent.change(screen.getByRole("textbox", { name: "Search users" }), { target: { value: "target" } });
    await waitFor(() => expect(mocks.api.get).toHaveBeenCalledTimes(2));
    await act(async () => patch.resolve({ data: {} }));

    expect(mocks.toast.success).toHaveBeenCalledExactlyOnceWith("Role updated successfully");
    expect(role).toHaveValue("member");
    await act(async () => searchResults.resolve({ data: { users: [targetUser], total: 40 } }));
    expect(await screen.findByRole("combobox", { name: "Change role for target" })).toHaveValue("member");
  });

  it("reports row-mutation failure after a search change without publishing a stale row update", async () => {
    const patch = deferred<unknown>();
    const searchResults = deferred<unknown>();
    queueInitialUsers();
    queueGet(usersUrl(1, "target"), searchResults.promise);
    renderPage();
    const role = await screen.findByRole("combobox", { name: "Change role for target" });
    mocks.api.patch.mockReturnValueOnce(patch.promise);
    fireEvent.change(role, { target: { value: "admin" } });

    fireEvent.change(screen.getByRole("textbox", { name: "Search users" }), { target: { value: "target" } });
    await waitFor(() => expect(mocks.api.get).toHaveBeenCalledTimes(2));
    await act(async () => patch.reject(new Error("request failed")));

    expect(mocks.toast.error).toHaveBeenCalledTimes(1);
    expect(role).toHaveValue("member");
    await act(async () => searchResults.resolve({ data: { users: [targetUser], total: 40 } }));
    expect(await screen.findByRole("combobox", { name: "Change role for target" })).toHaveValue("member");
  });

  it("lets only the newest same-user mutation publish after a query change", async () => {
    const olderPatch = deferred<unknown>();
    const newerPatch = deferred<unknown>();
    const searchResults = deferred<unknown>();
    queueInitialUsers();
    queueGet(usersUrl(1, "target"), searchResults.promise);
    renderPage();
    const role = await screen.findByRole("combobox", { name: "Change role for target" });
    mocks.api.patch.mockReturnValueOnce(olderPatch.promise);
    fireEvent.change(role, { target: { value: "admin" } });

    fireEvent.change(screen.getByRole("textbox", { name: "Search users" }), { target: { value: "target" } });
    await waitFor(() => expect(mocks.api.get).toHaveBeenCalledTimes(2));
    await act(async () => searchResults.resolve({ data: { users: [{ ...targetUser, role: "member" }], total: 40 } }));
    const currentRoleInput = await screen.findByRole("combobox", { name: "Change role for target" });
    mocks.api.patch.mockReturnValueOnce(newerPatch.promise);
    const currentRole = mocks.selectHandlers.get("Change role for target");
    if (!currentRole) throw new Error("expected the current role callback");
    await act(async () => {
      void currentRole("admin");
    });
    expect(mocks.api.patch).toHaveBeenCalledTimes(2);

    await act(async () => olderPatch.resolve({ data: {} }));
    expect(mocks.toast.success).not.toHaveBeenCalledWith("Role updated successfully");
    expect(currentRoleInput).toHaveValue("member");
    await act(async () => newerPatch.resolve({ data: {} }));
    expect(mocks.toast.success).toHaveBeenCalledExactlyOnceWith("Role updated successfully");
    expect(await screen.findByRole("combobox", { name: "Change role for target" })).toHaveValue("admin");
  });

  it("drops an in-flight page response after the page intent changes and shows the fresh page", async () => {
    const pageOne = deferred<unknown>();
    const pageTwo = deferred<unknown>();
    queueGet(usersUrl(), pageOne.promise);
    queueGet(usersUrl(2), pageTwo.promise);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Next page" }));
    await waitFor(() => expect(mocks.api.get).toHaveBeenCalledTimes(2));

    await act(async () => pageOne.resolve({ data: { users: [{ ...targetUser, username: "page-one" }], total: 40 } }));
    expect(screen.queryByText("page-one")).not.toBeInTheDocument();
    await act(async () => pageTwo.resolve({ data: { users: [{ ...targetUser, username: "page-two" }], total: 40 } }));
    expect(await screen.findByText("page-two")).toBeInTheDocument();
  });

  it("drops an in-flight search response after the search intent changes and shows the fresh result", async () => {
    const initial = deferred<unknown>();
    const search = deferred<unknown>();
    queueGet(usersUrl(), initial.promise);
    queueGet(usersUrl(1, "fresh"), search.promise);
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("Search by username or name..."), { target: { value: "fresh" } });
    await waitFor(() => expect(mocks.api.get).toHaveBeenCalledTimes(2));

    await act(async () => initial.resolve({ data: { users: [{ ...targetUser, username: "old-search" }], total: 1 } }));
    expect(screen.queryByText("old-search")).not.toBeInTheDocument();
    await act(async () => search.resolve({ data: { users: [{ ...targetUser, username: "fresh-search" }], total: 1 } }));
    expect(await screen.findByText("fresh-search")).toBeInTheDocument();
  });

  it("keeps a same-auth A save error visible without releasing reopened B", async () => {
    const groupsA = deferred<{ data: { groups: unknown[] } }>();
    const membershipsA = deferred<{ data: { groups: unknown[] } }>();
    const groupsB = deferred<{ data: { groups: unknown[] } }>();
    const membershipsB = deferred<{ data: { groups: unknown[] } }>();
    const saveA = deferred<unknown>();
    const saveB = deferred<unknown>();
    const group = { id: 1, name: "Shared Group", description: "current" };

    queueInitialUsers();
    queueGet("/groups", groupsA.promise);
    queueGet("/users/2/groups", membershipsA.promise);
    mocks.api.put.mockReturnValueOnce(saveA.promise).mockReturnValueOnce(saveB.promise);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Manage groups for target" }));
    await act(async () => {
      groupsA.resolve({ data: { groups: [group] } });
      membershipsA.resolve({ data: { groups: [] } });
    });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Shared Group" }));
    fireEvent.click(screen.getByRole("button", { name: "Save group changes" }));
    expect(screen.getByRole("button", { name: "Save group changes" })).toBeDisabled();

    const closeA = mocks.sheetOnOpenChange;
    if (!closeA) throw new Error("expected the A sheet close callback to be captured");
    await act(async () => closeA(false));
    expect(screen.queryByRole("button", { name: "Save group changes" })).not.toBeInTheDocument();

    queueGet("/groups", groupsB.promise);
    queueGet("/users/2/groups", membershipsB.promise);
    fireEvent.click(screen.getByRole("button", { name: "Manage groups for target" }));
    await act(async () => {
      groupsB.resolve({ data: { groups: [group] } });
      membershipsB.resolve({ data: { groups: [] } });
    });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Shared Group" }));
    fireEvent.click(screen.getByRole("button", { name: "Save group changes" }));
    expect(mocks.api.put).toHaveBeenLastCalledWith("/users/2/groups", { group_ids: [1] });
    expect(screen.getByRole("button", { name: "Save group changes" })).toBeDisabled();

    await act(async () => saveA.reject(new Error("legacy A save failure")));
    await waitFor(() => expect(mocks.toast.error).toHaveBeenCalledWith("Failed to update groups"));
    expect(screen.getByRole("button", { name: "Save group changes" })).toBeDisabled();

    await act(async () => saveB.resolve({ data: {} }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save group changes" })).not.toBeInTheDocument());
  });

  it("keeps the current-auth users error toast while a retired users error stays silent", async () => {
    const currentFailure = deferred<unknown>();
    const retiredFailure = deferred<unknown>();
    queueGet(usersUrl(), currentFailure.promise);
    queueGet(usersUrl(), retiredFailure.promise);
    renderPage();
    await waitFor(() => expect(mocks.api.get).toHaveBeenCalledTimes(1));
    await act(async () => currentFailure.reject(new Error("current users failure")));
    await waitFor(() => expect(mocks.toast.error).toHaveBeenCalledWith("Failed to load users"));
    expect(screen.getByRole("alert")).toHaveTextContent("Failed to load users");

    fireEvent.click(screen.getByRole("button", { name: "Retry users" }));
    await waitFor(() => expect(mocks.api.get).toHaveBeenCalledTimes(2));

    queueGet(usersUrl(), { data: { users: [targetUser], total: 1 } });
    reserveReplacementAuthOwner();
    await act(async () => retiredFailure.reject(new Error("retired users failure")));
    expect(mocks.toast.error).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("target")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

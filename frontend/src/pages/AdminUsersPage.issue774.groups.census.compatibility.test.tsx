import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import AdminUsersPage from "@/pages/AdminUsersPage";

const mockGet = vi.hoisted(() => vi.fn());
const mockPut = vi.hoisted(() => vi.fn());
const mockUseAuthStore = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({
  default: { get: mockGet, put: mockPut, post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: mockUseAuthStore,
}));
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => false }));
vi.mock("@/hooks/useDebounce", () => ({ useDebounce: (value: string) => [value] }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/components/auth/RoleGuard", () => ({ AdminGuard: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props}>{children}</button>,
}));
vi.mock("@/components/ui/input", () => ({ Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} /> }));
vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({ checked, onCheckedChange, ...props }: { checked?: boolean; onCheckedChange?: () => void } & React.ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button role="checkbox" aria-checked={checked} onClick={onCheckedChange} {...props} />
  ),
}));
vi.mock("@/components/ui/label", () => ({ Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => <label {...props}>{children}</label> }));
vi.mock("@/components/ui/sheet", () => ({
  Sheet: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SheetContent: ({ children }: { children: React.ReactNode }) => <aside>{children}</aside>,
  SheetDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  SheetFooter: ({ children }: { children: React.ReactNode }) => <footer>{children}</footer>,
  SheetHeader: ({ children }: { children: React.ReactNode }) => <header>{children}</header>,
  SheetTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ({ children }: { children: React.ReactNode }) => <div>{children}</div> }));
vi.mock("@/components/ui/skeleton", () => ({ Skeleton: () => <span data-testid="skeleton" /> }));
vi.mock("@/components/ui/select", () => ({
  Select: ({ children, value, onValueChange, disabled }: { children: React.ReactNode; value?: string; onValueChange?: (value: string) => void; disabled?: boolean }) => (
    <select value={value} disabled={disabled} onChange={(event) => onValueChange?.(event.target.value)}>{children}</select>
  ),
  SelectContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SelectItem: ({ children, value }: { children: React.ReactNode; value: string }) => <option value={value}>{children}</option>,
  SelectTrigger: () => null,
  SelectValue: () => null,
}));
vi.mock("@/components/ui/badge", () => ({ Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span> }));
vi.mock("@/components/layout/PageTitleHeader", () => ({ PageTitleHeader: ({ title }: { title: string }) => <h1>{title}</h1> }));
vi.mock("@/components/LoadingSpinner", () => ({ LoadingSpinner: ({ label }: { label: string }) => <div role="status">{label}</div> }));
vi.mock("@/components/EmptyState", () => ({ EmptyState: ({ title }: { title: string }) => <div role="status">{title}</div> }));
vi.mock("@/components/ui/pagination", () => ({ Pagination: () => null }));
vi.mock("@/components/ui/table", () => ({
  Table: ({ children }: { children: React.ReactNode }) => <table>{children}</table>,
  TableBody: ({ children }: { children: React.ReactNode }) => <tbody>{children}</tbody>,
  TableCaption: ({ children }: { children: React.ReactNode }) => <caption>{children}</caption>,
  TableCell: ({ children, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) => <td {...props}>{children}</td>,
  TableHead: ({ children }: { children: React.ReactNode }) => <th>{children}</th>,
  TableHeader: ({ children }: { children: React.ReactNode }) => <thead>{children}</thead>,
  TableRow: ({ children }: { children: React.ReactNode }) => <tr>{children}</tr>,
}));
vi.mock("./AdminUsersPage/DeleteUserDialog", () => ({ DeleteUserDialog: () => null }));
vi.mock("./AdminUsersPage/EditUserDialog", () => ({ EditUserDialog: () => null }));
vi.mock("./AdminUsersPage/ResetPasswordDialog", () => ({ ResetPasswordDialog: () => null }));
vi.mock("./AdminUsersPage/ManageOrgsSheet", () => ({ ManageOrgsSheet: () => null }));
vi.mock("./AdminUsersPage/CreateUserDialog", () => ({ CreateUserDialog: () => null }));
vi.mock("lucide-react", () => ({
  Search: () => null,
  Loader2: () => null,
  Trash2: () => null,
  Users: () => null,
  Pencil: () => null,
  KeyRound: () => null,
  Plus: () => null,
  Building2: () => null,
  ChevronUp: () => null,
  ChevronDown: () => null,
}));

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const user = { id: 2, username: "john", full_name: "John Doe", role: "member" as const, is_active: true, created_at: "2026-01-01" };
const group = { id: 4, name: "Research", description: "Research group" };
const usersPayload = { data: { users: [user], total: 1 } };

function installAuthStoreMock() {
  const state = { user: { id: 1, username: "admin", full_name: "Admin", role: "superadmin", is_active: true }, isAuthenticated: true };
  mockUseAuthStore.mockImplementation((selector?: (state: unknown) => unknown) => (
    selector ? selector(state) : state
  ));
  Object.assign(mockUseAuthStore, {
    getState: () => mockUseAuthStore((currentState: unknown) => currentState),
  });
}

describe("AdminUsersPage group reads issue #774 census", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("C21-A01 exposes a failed users read, retries it, and never claims there are no users", async () => {
    installAuthStoreMock();
    const first = deferred<typeof usersPayload>();
    mockGet.mockReturnValueOnce(first.promise);
    try {
      render(<AdminUsersPage />);
      await act(async () => first.reject(new Error("users read failed")));
      expect(screen.getByRole("alert")).toHaveTextContent("users");
      expect(screen.queryByText("No users found")).not.toBeInTheDocument();

      mockGet.mockResolvedValueOnce(usersPayload);
      fireEvent.click(screen.getByRole("button", { name: "Retry users" }));
      expect(await screen.findByText("john")).toBeInTheDocument();
    } finally {
      first.resolve(usersPayload);
    }
  });

  it("C21-A02 keeps Save disabled when the group catalog read fails and recovers through the real retry", async () => {
    installAuthStoreMock();
    const allGroups = deferred<{ data: { groups: typeof group[] } }>();
    const allGroupsRetry = deferred<{ data: { groups: typeof group[] } }>();
    mockGet.mockImplementation((url: string) => {
      if (url.startsWith("/users/?")) return Promise.resolve(usersPayload);
      if (url === "/groups") return allGroups.promise;
      return Promise.resolve({ data: { groups: [group] } });
    });

    try {
      render(<AdminUsersPage />);
      fireEvent.click(await screen.findByRole("button", { name: "Manage groups for john" }));
      await act(async () => allGroups.reject(new Error("groups catalog read failed")));
      expect(screen.getByRole("alert")).toHaveTextContent("groups");
      expect(screen.queryByText("No groups available")).not.toBeInTheDocument();
      const save = screen.getByRole("button", { name: "Save group changes" });
      expect(save).toBeDisabled();
      fireEvent.click(save);
      expect(mockPut).not.toHaveBeenCalled();

      mockGet.mockImplementation((url: string) => url === "/groups" ? allGroupsRetry.promise : Promise.resolve({ data: { groups: [group] } }));
      fireEvent.click(screen.getByRole("button", { name: "Retry groups" }));
      await act(async () => allGroupsRetry.resolve({ data: { groups: [group] } }));
      expect(await screen.findByText("Research")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Save group changes" })).toBeEnabled();
    } finally {
      allGroups.resolve({ data: { groups: [] } });
      allGroupsRetry.resolve({ data: { groups: [] } });
    }
  });

  it("C21-A04 keeps Save disabled when the membership read fails and recovers through the real retry", async () => {
    installAuthStoreMock();
    const memberships = deferred<{ data: { groups: typeof group[] } }>();
    const membershipsRetry = deferred<{ data: { groups: typeof group[] } }>();
    mockGet.mockImplementation((url: string) => {
      if (url.startsWith("/users/?")) return Promise.resolve(usersPayload);
      if (url === "/groups") return Promise.resolve({ data: { groups: [group] } });
      return memberships.promise;
    });

    try {
      render(<AdminUsersPage />);
      fireEvent.click(await screen.findByRole("button", { name: "Manage groups for john" }));
      await act(async () => memberships.reject(new Error("membership read failed")));
      expect(screen.getByRole("alert")).toHaveTextContent("groups");
      expect(screen.queryByText("No groups available")).not.toBeInTheDocument();
      const save = screen.getByRole("button", { name: "Save group changes" });
      expect(save).toBeDisabled();
      fireEvent.click(save);
      expect(mockPut).not.toHaveBeenCalled();

      mockGet.mockImplementation((url: string) => url === "/groups" ? Promise.resolve({ data: { groups: [group] } }) : membershipsRetry.promise);
      fireEvent.click(screen.getByRole("button", { name: "Retry groups" }));
      await act(async () => membershipsRetry.resolve({ data: { groups: [group] } }));
      expect(await screen.findByText("Research")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Save group changes" })).toBeEnabled();
    } finally {
      memberships.resolve({ data: { groups: [] } });
      membershipsRetry.resolve({ data: { groups: [] } });
    }
  });

  it("C21-A03 preserves the successful authoritative empty group response", async () => {
    installAuthStoreMock();
    mockGet.mockImplementation((url: string) => {
      if (url.startsWith("/users/?")) return Promise.resolve(usersPayload);
      return Promise.resolve({ data: { groups: [] } });
    });
    render(<AdminUsersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Manage groups for john" }));
    expect(await screen.findByText("No groups available")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save group changes" })).toBeEnabled();
  });
});

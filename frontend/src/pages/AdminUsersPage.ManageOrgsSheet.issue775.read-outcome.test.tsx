import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import AdminUsersPage from "@/pages/AdminUsersPage";
import { useAuthStore } from "@/stores/useAuthStore";

const mockGet = vi.hoisted(() => vi.fn());
const mockPut = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    default: { ...(actual.default as object), get: mockGet, put: mockPut },
  };
});
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => false }));
vi.mock("@/hooks/useDebounce", () => ({ useDebounce: (value: string) => [value] }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/components/auth/RoleGuard", () => ({ AdminGuard: ({ children }: { children: ReactNode }) => <>{children}</> }));
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
  Sheet: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SheetContent: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
  SheetDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  SheetFooter: ({ children }: { children: ReactNode }) => <footer>{children}</footer>,
  SheetHeader: ({ children }: { children: ReactNode }) => <header>{children}</header>,
  SheetTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ({ children }: { children: ReactNode }) => <div>{children}</div> }));
vi.mock("@/components/ui/skeleton", () => ({ Skeleton: () => <span data-testid="skeleton" /> }));
vi.mock("@/components/ui/select", () => ({
  Select: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  SelectTrigger: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  SelectValue: () => null,
}));
vi.mock("@/components/ui/badge", () => ({ Badge: ({ children }: { children: ReactNode }) => <span>{children}</span> }));
vi.mock("@/components/layout/PageTitleHeader", () => ({ PageTitleHeader: ({ title }: { title: string }) => <h1>{title}</h1> }));
vi.mock("@/components/LoadingSpinner", () => ({ LoadingSpinner: ({ label }: { label: string }) => <div role="status">{label}</div> }));
vi.mock("@/components/EmptyState", () => ({ EmptyState: ({ title }: { title: string }) => <div role="status">{title}</div> }));
vi.mock("@/components/ui/pagination", () => ({ Pagination: () => null }));
vi.mock("@/components/ui/table", () => ({
  Table: ({ children }: { children: ReactNode }) => <table>{children}</table>,
  TableBody: ({ children }: { children: ReactNode }) => <tbody>{children}</tbody>,
  TableCaption: ({ children }: { children: ReactNode }) => <caption>{children}</caption>,
  TableCell: ({ children, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) => <td {...props}>{children}</td>,
  TableHead: ({ children }: { children: ReactNode }) => <th>{children}</th>,
  TableHeader: ({ children }: { children: ReactNode }) => <thead>{children}</thead>,
  TableRow: ({ children }: { children: ReactNode }) => <tr>{children}</tr>,
}));
vi.mock("./AdminUsersPage/DeleteUserDialog", () => ({ DeleteUserDialog: () => null }));
vi.mock("./AdminUsersPage/EditUserDialog", () => ({ EditUserDialog: () => null }));
vi.mock("./AdminUsersPage/ResetPasswordDialog", () => ({ ResetPasswordDialog: () => null }));
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

const admin = { id: 1, username: "admin", full_name: "Admin", role: "superadmin" as const, is_active: true };
const user = { id: 2, username: "john", full_name: "John Doe", role: "member" as const, is_active: true, created_at: "2026-01-01" };
const usersResponse = { data: { users: [user], total: 1 } };

describe("C25 ManageOrgsSheet read outcome", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;

  afterEach(() => {
    cleanup();
    act(() => useAuthStore.setState(priorAuthState));
    mockGet.mockReset();
    mockPut.mockReset();
  });

  it("keeps a failed organization membership read unknown and exposes retry before Save", async () => {
    priorAuthState = useAuthStore.getState();
    act(() => useAuthStore.setState({ user: admin, isAuthenticated: true, isInitialized: true }));
    const allOrgs = deferred<unknown>();
    const memberships = deferred<unknown>();
    let retryRequested = false;
    const successfulOrganizations = { data: { organizations: [{ id: 9, name: "Acme" }], total: 1 } };
    const successfulMemberships = { data: { organizations: [{ id: 9, name: "Acme" }] } };
    mockGet.mockImplementation((url: string) => {
      if (url.startsWith("/users/?")) return Promise.resolve(usersResponse);
      if (url === "/organizations/") return retryRequested ? Promise.resolve(successfulOrganizations) : allOrgs.promise;
      if (url === "/users/2/organizations") return retryRequested ? Promise.resolve(successfulMemberships) : memberships.promise;
      return Promise.resolve({ data: [] });
    });

    try {
      render(<AdminUsersPage />);
      expect(await screen.findByText("john")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Manage organizations for john" }));
      await waitFor(() => {
        expect(mockGet).toHaveBeenCalledWith("/organizations/");
        expect(mockGet).toHaveBeenCalledWith("/users/2/organizations");
      });

      await act(async () => {
        allOrgs.reject(new Error("organization catalog failed"));
        memberships.reject(new Error("membership read failed"));
        await Promise.all([allOrgs.promise.catch(() => undefined), memberships.promise.catch(() => undefined)]);
      });

      expect(screen.queryByText("No organizations available")).not.toBeInTheDocument();
      const retry = screen.getByRole("button", { name: "Retry organizations" });
      expect(retry).toBeEnabled();
      expect(screen.getByRole("button", { name: "Save organization changes" })).toBeDisabled();

      retryRequested = true;
      await act(async () => {
        fireEvent.click(retry);
        await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/organizations/"));
        await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/users/2/organizations"));
      });
      expect(await screen.findByText("Acme")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Save organization changes" })).toBeEnabled();
    } finally {
      allOrgs.resolve({ data: [] });
      memberships.resolve({ data: [] });
      await Promise.allSettled([allOrgs.promise, memberships.promise]);
    }
  });
});

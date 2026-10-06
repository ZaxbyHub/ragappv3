/**
 * Issue #774 regression test (non-frozen, implementation-review round 2):
 * a FIRST-attempt member-search failure must render the "User search failed"
 * line (the dropdown opens on failure), and "No users found" stays reserved
 * for a successful empty search.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import OrgsPage from "@/pages/OrgsPage";

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: unknown) => {
    const state = {
      user: {
        id: 1,
        username: "superadmin",
        full_name: "Super Admin",
        role: "superadmin",
      },
      isAuthenticated: true,
      isLoading: false,
    };
    return typeof selector === "function"
      ? (selector as (s: typeof state) => unknown)(state)
      : state;
  }),
}));

const apiGet = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGet,
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div data-testid="card-content">{children}</div>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3>{children}</h3>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, disabled, ...props }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => (open ? <div data-testid="dialog">{children}</div> : null),
  DialogContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/auth/RoleGuard", () => ({
  AdminGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  RoleGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const ORG = {
  id: 1,
  name: "Acme",
  description: "",
  member_count: 0,
  vault_count: 0,
  created_at: "2024-01-01T00:00:00",
};

beforeEach(() => {
  vi.clearAllMocks();
  apiGet.mockImplementation(async (url: string) => {
    if (url === "/organizations/") {
      return { data: { organizations: [ORG], total: 1 } };
    }
    if (url === "/organizations/1/members") {
      return { data: { members: [] } };
    }
    if (url.startsWith("/users/")) {
      throw new Error("user directory unreachable");
    }
    return { data: {} };
  });
});

describe("issue 774 OrgsPage member-search failure", () => {
  it("renders the search failure line on a first-attempt failure and never claims No users found", async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    // Expand the org to reveal the member-add form (current user is not a
    // member of the freshly loaded org).
    const expand = await screen.findByRole("button", { name: /expand/i }, { timeout: 3000 });
    fireEvent.click(expand);

    const search = await screen.findByPlaceholderText(/Search by name or username/i, undefined, { timeout: 3000 });
    fireEvent.change(search, { target: { value: "alice" } });

    // The query is debounced 300 ms (real timers), then the request rejects.
    expect(
      await screen.findByText(/user search failed/i, {}, { timeout: 4000 })
    ).toBeInTheDocument();
    expect(screen.queryByText("No users found")).not.toBeInTheDocument();
  });

  it("keeps No users found for a successful empty search", async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === "/organizations/") {
        return { data: { organizations: [ORG], total: 1 } };
      }
      if (url === "/organizations/1/members") {
        return { data: { members: [] } };
      }
      if (url.startsWith("/users/")) {
        return { data: { users: [] } };
      }
      return { data: {} };
    });

    await act(async () => {
      render(<OrgsPage />);
    });
    fireEvent.click(await screen.findByRole("button", { name: /expand/i }, { timeout: 3000 }));

    const search = await screen.findByPlaceholderText(/Search by name or username/i, undefined, { timeout: 3000 });
    fireEvent.change(search, { target: { value: "alice" } });

    expect(
      await screen.findByText("No users found", {}, { timeout: 4000 })
    ).toBeInTheDocument();
    expect(screen.queryByText(/user search failed/i)).not.toBeInTheDocument();
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(expect.stringContaining("/users/"), expect.anything()));
  });
});

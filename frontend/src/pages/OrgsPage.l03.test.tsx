/**
 * Issue #774 L03 red checkpoint — a failed organizations load must not render
 * the "No organizations found" empty state (a definite negative). OrgsPage's
 * catch only toasts; orgs stays [] and the page reads as "you have none".
 *
 * Mirrors the mock layout of ./OrgsPage.test.tsx. useTestMode defaults to
 * false without a provider, so the real fetch path runs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import OrgsPage from "@/pages/OrgsPage";

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector) => {
    if (typeof selector === "function") {
      return selector({
        user: {
          id: 1,
          username: "superadmin",
          full_name: "Super Admin",
          role: "superadmin",
        },
        isAuthenticated: true,
        isLoading: false,
      });
    }
    return {
      user: {
        id: 1,
        username: "superadmin",
        full_name: "Super Admin",
        role: "superadmin",
      },
      isAuthenticated: true,
      isLoading: false,
    };
  }),
}));

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn(),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
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
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => open ? <div data-testid="dialog">{children}</div> : null,
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

describe("issue 774 OrgsPage load failure", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not show the empty state when loading organizations fails", async () => {
    const api = await import("@/lib/api");
    api.default.get.mockRejectedValue(new Error("Network error"));

    await act(async () => {
      render(<OrgsPage />);
    });

    // Wait for the rejection to settle (loading flips to false after the
    // catch), then the empty state must NOT be present.
    await waitFor(() => {
      expect(screen.getByText(/no organizations found/i)).toBeInTheDocument();
    });

    expect(screen.queryAllByText("No organizations found").length).toBe(0);
  });
});

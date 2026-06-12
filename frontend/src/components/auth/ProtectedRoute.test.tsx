import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ProtectedRoute } from "./ProtectedRoute";
import { RoleGuard, AdminGuard, SuperAdminGuard } from "./RoleGuard";
import * as useAuthStoreModule from "@/stores/useAuthStore";

// Mock lucide-react to avoid a jsdom ARIA live-region segfault on cleanup.
// The Loader2 in ProtectedRoute uses aria-live="polite" which triggers a native
// crash in jsdom when the element is unmounted. Strip aria-live; keep role="status"
// so the existing assertion still passes.
vi.mock("lucide-react", () => ({
  Loader2: ({ role, className }: { role?: string; className?: string }) => (
    <div role={role} className={className} data-testid="loader2" />
  ),
}));

// Mock the dependencies
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(),
}));

// Helper to render with router
const renderWithRouter = (ui: React.ReactElement, initialEntry = "/") => {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      {ui}
    </MemoryRouter>
  );
};

describe("ProtectedRoute", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading spinner when loading", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: false,
      isLoading: true,
      needsSetup: false,
    } as any);

    renderWithRouter(<ProtectedRoute><div>Protected Content</div></ProtectedRoute>);

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
  });

  it("redirects to /login when not authenticated", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      needsSetup: false,
    } as any);

    renderWithRouter(<ProtectedRoute><div>Protected Content</div></ProtectedRoute>);

    expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
  });

  it("redirects to /setup when needsSetup is true", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      needsSetup: true,
    } as any);

    renderWithRouter(<ProtectedRoute><div>Protected Content</div></ProtectedRoute>);

    expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
  });

  it("renders children when authenticated via store", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
      needsSetup: false,
    } as any);

    renderWithRouter(<ProtectedRoute><div>Protected Content</div></ProtectedRoute>);

    expect(screen.getByText("Protected Content")).toBeInTheDocument();
  });

  it("renders children when authenticated via store (JWT)", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
      needsSetup: false,
    } as any);

    renderWithRouter(<ProtectedRoute><div>Protected Content</div></ProtectedRoute>);

    expect(screen.getByText("Protected Content")).toBeInTheDocument();
  });

  // For cross-route redirects, map the destination to a distinct sentinel so the
  // redirect target is observable (a bare router would just re-render the same
  // ProtectedRoute at the new path).
  const renderWithRoutes = (initialEntry: string) =>
    render(
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/change-password" element={<div>CP Screen</div>} />
          <Route path="/" element={<div>Home Sentinel</div>} />
          <Route
            path="*"
            element={
              <ProtectedRoute>
                <div>Protected Content</div>
              </ProtectedRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    );

  it("redirects flagged user away from a normal route to /change-password", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
      needsSetup: false,
      user: { id: 1, username: "new", role: "member", must_change_password: true },
    } as any);

    renderWithRoutes("/documents");

    expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
    expect(screen.getByText("CP Screen")).toBeInTheDocument();
  });

  it("renders the change-password screen for a flagged user on /change-password", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
      needsSetup: false,
      user: { id: 1, username: "new", role: "member", must_change_password: true },
    } as any);

    // ProtectedRoute itself wraps the change-password route at runtime, so render
    // it directly at that path: a flagged user should see the wrapped children.
    renderWithRouter(
      <ProtectedRoute><div>Change Password Form</div></ProtectedRoute>,
      "/change-password"
    );

    expect(screen.getByText("Change Password Form")).toBeInTheDocument();
  });

  it("redirects a non-flagged user away from /change-password to home", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
      needsSetup: false,
      user: { id: 1, username: "user", role: "member", must_change_password: false },
    } as any);

    render(
      <MemoryRouter initialEntries={["/change-password"]}>
        <Routes>
          <Route path="/" element={<div>Home Sentinel</div>} />
          <Route
            path="/change-password"
            element={
              <ProtectedRoute>
                <div>Change Password Form</div>
              </ProtectedRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.queryByText("Change Password Form")).not.toBeInTheDocument();
    expect(screen.getByText("Home Sentinel")).toBeInTheDocument();
  });
});

describe("RoleGuard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders children when user has sufficient role", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { id: "1", username: "admin", role: "admin" },
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin", "superadmin"]}>
        <div>Admin Content</div>
      </RoleGuard>
    );

    expect(screen.getByText("Admin Content")).toBeInTheDocument();
  });

  it("redirects to / when role insufficient", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { id: "1", username: "member", role: "member" },
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin", "superadmin"]}>
        <div>Admin Content</div>
      </RoleGuard>
    );

    expect(screen.queryByText("Admin Content")).not.toBeInTheDocument();
  });

  it("shows fallback when provided and role insufficient", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { id: "1", username: "member", role: "member" },
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin", "superadmin"]} fallback={<div>Access Denied</div>}>
        <div>Admin Content</div>
      </RoleGuard>
    );

    expect(screen.getByText("Access Denied")).toBeInTheDocument();
    expect(screen.queryByText("Admin Content")).not.toBeInTheDocument();
  });

  it("shows loading spinner when store is loading", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: false,
      isLoading: true,
      user: null,
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin"]}>
        <div>Content</div>
      </RoleGuard>
    );

    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("redirects to /login when not authenticated (no fallback)", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      user: null,
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin"]}>
        <div>Content</div>
      </RoleGuard>
    );

    expect(screen.queryByText("Content")).not.toBeInTheDocument();
  });

  it("shows fallback when not authenticated and fallback provided", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      user: null,
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin"]} fallback={<div>Please Login</div>}>
        <div>Content</div>
      </RoleGuard>
    );

    expect(screen.getByText("Please Login")).toBeInTheDocument();
  });

  it("handles invalid role string gracefully", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { id: "1", username: "unknown", role: "unknown_role" },
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin"]} fallback={<div>Invalid Role</div>}>
        <div>Content</div>
      </RoleGuard>
    );

    expect(screen.getByText("Invalid Role")).toBeInTheDocument();
  });

  it("handles undefined user role gracefully", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { id: "1", username: "norole" },
    } as any);

    renderWithRouter(
      <RoleGuard allowedRoles={["admin"]} fallback={<div>No Role</div>}>
        <div>Content</div>
      </RoleGuard>
    );

    expect(screen.getByText("No Role")).toBeInTheDocument();
  });

  it("AdminGuard allows admin role", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { id: "1", username: "admin", role: "admin" },
    } as any);

    renderWithRouter(
      <AdminGuard>
        <div>Admin Only Content</div>
      </AdminGuard>
    );

    expect(screen.getByText("Admin Only Content")).toBeInTheDocument();
  });

  it("SuperAdminGuard only allows superadmin", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { id: "1", username: "superadmin", role: "superadmin" },
    } as any);

    renderWithRouter(
      <SuperAdminGuard>
        <div>Superadmin Content</div>
      </SuperAdminGuard>
    );

    expect(screen.getByText("Superadmin Content")).toBeInTheDocument();
  });
});
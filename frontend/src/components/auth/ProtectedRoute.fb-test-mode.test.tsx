import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ProtectedRoute } from "./ProtectedRoute";
import * as useAuthStoreModule from "@/stores/useAuthStore";

vi.mock("lucide-react", () => ({
  Loader2: ({ role, className }: { role?: string; className?: string }) => (
    <div role={role} className={className} data-testid="loader2" />
  ),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(),
}));

describe("ProtectedRoute test-mode auth guard", () => {
  it("redirects an unauthenticated test-mode session to login", () => {
    vi.mocked(useAuthStoreModule.useAuthStore).mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      isInitialized: true,
      initializationFailed: false,
      needsSetup: false,
    } as any);

    render(
      <MemoryRouter initialEntries={["/documents"]}>
        <Routes>
          <Route path="/login" element={<div>Login Screen</div>} />
          <Route
            path="*"
            element={
              <ProtectedRoute testMode>
                <div>Protected Content</div>
              </ProtectedRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
    expect(screen.getByText("Login Screen")).toBeInTheDocument();
  });
});
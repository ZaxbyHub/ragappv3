import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: Object.assign(
    vi.fn(() => ({
      login: vi.fn(),
      needsSetup: false,
      isLoading: false,
      authMode: "single_admin",
    })),
    { getState: () => ({ init: vi.fn() }) }
  ),
}));

vi.mock("@/components/icons/MeridianLogo", () => ({
  MeridianLogo: ({ className }: { className?: string }) => (
    <div className={className} aria-hidden="true" />
  ),
}));

import LoginPage from "@/pages/LoginPage";

describe("LoginPage single-admin mode", () => {
  it("explains single-admin mode instead of rendering the sign-in form", () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    );

    expect(screen.getByText("Single-Admin Mode")).toBeInTheDocument();
    expect(screen.getByText(/User accounts are disabled/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/username/i)).not.toBeInTheDocument();
  });
});

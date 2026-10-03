// frontend/src/pages/SetupLanding.m02.test.tsx
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki (unfrozen pin for
// the NON-EXECUTABLE AC7 substitute evidence): SetupPage's final step
// onFinish navigates to "/", App's root route redirects to /documents, and
// the shell that every protected route renders carries the FirstRunChecklist
// — so finishing the wizard lands the user where the checklist is visible.
// The ModelEndpointStep child is stubbed (its internals are #622's surface);
// the real SetupPage account step and the real route shapes are exercised.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import React from "react";
import {
  MemoryRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";

const onboardingMocks = vi.hoisted(() => ({
  getOnboardingMilestones: vi.fn().mockResolvedValue({
    vault_created: false,
    upload_indexed: false,
    first_question_asked: false,
    first_citation_opened: false,
    show_checklist: true,
  }),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

vi.mock("@/lib/api/onboarding", () => onboardingMocks);

const authMocks = vi.hoisted(() => ({
  register: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: () => ({
    register: authMocks.register,
    needsSetup: true,
    isLoading: false,
  }),
}));

vi.mock("@/components/setup/ModelEndpointStep", () => ({
  default: ({ onFinish }: { onFinish: () => void }) => (
    <div>
      <h1>Models</h1>
      <button type="button" onClick={onFinish}>
        Finish setup
      </button>
    </div>
  ),
}));

import SetupPage from "@/pages/SetupPage";
import { PageShell } from "@/components/layout/PageShell";

vi.mock("@/lib/api", () => ({
  getSettings: vi.fn().mockResolvedValue({ chat_configured: true }),
}));

vi.mock("@/components/layout/Navigation", () => ({
  Navigation: () => <nav data-testid="navigation-stub" />,
}));

vi.mock("@/components/shared/UploadIndicator", () => ({
  UploadIndicator: () => null,
}));

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

function DocumentsStub() {
  return (
    <PageShell
      activeItem="documents"
      onItemSelect={() => undefined}
      healthStatus={{ backend: true, embeddings: true, chat: true, loading: false, lastChecked: null }}
    >
      <div>Documents page</div>
    </PageShell>
  );
}

describe("Setup finish lands on the checklist surface (issue #782, AC7 pin)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("onFinish -> / -> /documents renders the shell's FirstRunChecklist", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/setup"]}>
        <LocationProbe />
        <Routes>
          {/* Route shapes mirror App.tsx: /setup public, "/" redirects to
              /documents, protected routes render inside the PageShell. */}
          <Route path="/setup" element={<SetupPage />} />
          <Route path="/" element={<Navigate to="/documents" replace />} />
          <Route path="/documents" element={<DocumentsStub />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/setup");

    // Account step: create the first admin account (the real form).
    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Full name"), "Ada Admin");
    await user.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await user.type(screen.getByLabelText("Confirm Password"), "correct-horse-battery");
    await user.click(screen.getByRole("button", { name: /create superadmin account/i }));

    // Models step (stubbed child): finish -> onFinish -> navigate("/").
    await user.click(await screen.findByRole("button", { name: /finish setup/i }));

    // The root redirect lands on /documents, and the shell renders the
    // server-driven checklist on that surface.
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/documents");
    expect(
      await screen.findByTestId("first-run-checklist"),
    ).toBeInTheDocument();
    expect(onboardingMocks.getOnboardingMilestones).toHaveBeenCalled();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import ProfilePage from "@/pages/ProfilePage";
import { publishAuthPrincipal } from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "@/stores/useAuthStore";

const mocks = vi.hoisted(() => ({
  authPatch: vi.fn(),
  listOrganizations: vi.fn(),
  listAccessibleVaults: vi.fn(),
  listSessions: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("axios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("axios")>();
  const originalAxios = actual.default;
  const originalCreate = originalAxios.create.bind(originalAxios);
  const create = vi.fn((...args: Parameters<typeof actual.default.create>) => {
    const client = originalCreate(...args);
    vi.spyOn(client, "patch").mockImplementation(mocks.authPatch);
    return client;
  });
  const mockedDefault = Object.assign(
    ((...args: Parameters<typeof originalAxios>) => originalAxios(...args)) as typeof originalAxios,
    originalAxios,
    { create },
  );
  return { ...actual, default: mockedDefault };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listOrganizations: mocks.listOrganizations,
    listAccessibleVaults: mocks.listAccessibleVaults,
    listSessions: mocks.listSessions,
  };
});

vi.mock("sonner", () => ({ toast: { success: mocks.toastSuccess, error: mocks.toastError } }));
vi.mock("lucide-react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("lucide-react")>();
  return {
    ...actual,
    Loader2: ({ role, className }: { role?: string; className?: string }) => (
      <div role={role} className={className} data-testid="loader2" />
    ),
  };
});

const user = {
  id: 17,
  username: "profile-owner",
  full_name: "Profile Owner",
  role: "member" as const,
  is_active: true,
};

function renderProtectedProfile() {
  return render(
    <MemoryRouter initialEntries={["/profile"]}>
      <Routes>
        <Route path="/profile" element={<ProtectedRoute><ProfilePage /></ProtectedRoute>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ProfilePage with the real ProtectedRoute", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.listOrganizations.mockResolvedValue([]);
    mocks.listAccessibleVaults.mockResolvedValue({ vaults: [] });
    mocks.listSessions.mockResolvedValue({ sessions: [] });
    publishAuthPrincipal({ id: user.id, role: user.role });
    useAuthStore.setState({
      user,
      accessToken: "current-profile-token",
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
      initializationFailed: false,
      needsSetup: false,
      authMode: "jwt",
    });
  });

  afterEach(() => {
    publishAuthPrincipal(null);
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isLoading: false,
      isInitialized: false,
      initializationFailed: false,
      needsSetup: null,
      authMode: "unknown",
    });
  });

  it.each([
    { outcome: "success", toast: mocks.toastSuccess },
    { outcome: "failure", toast: mocks.toastError },
  ])("keeps the $outcome toast when ProtectedRoute unmounts the page", async ({ outcome, toast }) => {
    let resolveUpdate!: (response: { data: typeof user }) => void;
    let rejectUpdate!: (error: Error) => void;
    mocks.authPatch.mockReturnValue(new Promise<{ data: typeof user }>((resolve, reject) => {
      resolveUpdate = resolve;
      rejectUpdate = reject;
    }));

    renderProtectedProfile();
    fireEvent.change(screen.getByLabelText("Full name"), { target: { value: "Changed Name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));
    await waitFor(() => expect(mocks.authPatch).toHaveBeenCalledOnce());

    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    expect(screen.queryByLabelText("Full name")).not.toBeInTheDocument();

    await act(async () => {
      if (outcome === "success") resolveUpdate({ data: { ...user, full_name: "Changed Name" } });
      else rejectUpdate(new Error("profile save failed"));
    });
    expect(toast).toHaveBeenCalledWith(
      outcome === "success" ? "Profile updated successfully" : "Failed to update profile",
    );
  });
});

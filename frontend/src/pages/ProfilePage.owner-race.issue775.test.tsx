import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProfilePage from "@/pages/ProfilePage";
import { useAuthStore } from "@/stores/useAuthStore";

const api = vi.hoisted(() => ({
  changePassword: vi.fn(),
  listOrganizations: vi.fn(),
  listAccessibleVaults: vi.fn(),
  listSessions: vi.fn(),
  listVaults: vi.fn(),
  revokeAllSessions: vi.fn(),
  revokeSession: vi.fn(),

}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));
vi.mock("sonner", () => ({ toast }));
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => false }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function user(id: number, fullName: string) {
  return { id, username: `user-${id}`, full_name: fullName, role: "member" as const };
}

describe("ProfilePage ownership boundaries (issue #775)", () => {
  const originalStore = useAuthStore.getState();

  beforeEach(() => {
    vi.resetAllMocks();
    useAuthStore.setState({
      ...originalStore,
      user: user(1, "A User"),
      accessToken: "token-a",
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.setState(originalStore, true);
  });

  it("retains B's rendered vault, name, and password token after every held A operation settles", async () => {
    const aOrganizations = deferred<Array<{ id: number; name: string }>>();
    const aVaults = deferred<{ vaults: Array<{ id: number; name: string }> }>();
    const aSessions = deferred<{ sessions: Array<{ id: string; user_agent: string }> }>();
    const aPassword = deferred<{ access_token: string }>();
    api.listOrganizations.mockImplementationOnce(() => aOrganizations.promise).mockResolvedValueOnce([{ id: 2, name: "B organization" }]);
    api.listAccessibleVaults.mockImplementationOnce(() => aVaults.promise).mockResolvedValueOnce({ vaults: [{ id: 22, name: "B vault" }] });
    api.listSessions.mockImplementationOnce(() => aSessions.promise).mockResolvedValueOnce({ sessions: [{ id: "b", user_agent: "B browser" }] });
    api.changePassword.mockImplementationOnce(() => aPassword.promise).mockResolvedValueOnce({ access_token: "token-b-refreshed" });

    try {
      render(<ProfilePage />);
      await waitFor(() => expect(api.listAccessibleVaults).toHaveBeenCalledTimes(1));
      fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "a-current" } });
      fireEvent.change(screen.getByLabelText("New password"), { target: { value: "a-new-password" } });
      fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "a-new-password" } });
      fireEvent.click(screen.getByRole("button", { name: "Change Password" }));
      await waitFor(() => expect(api.changePassword).toHaveBeenCalledWith("a-current", "a-new-password"));

      act(() => {
        useAuthStore.setState({ user: user(2, "B User"), accessToken: "token-b", isAuthenticated: true });
      });
      await waitFor(() => expect(api.listAccessibleVaults).toHaveBeenCalledTimes(2));
      await screen.findByText("B vault");
      expect(screen.getByDisplayValue("B User")).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "b-current" } });
      fireEvent.change(screen.getByLabelText("New password"), { target: { value: "b-new-password" } });
      fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "b-new-password" } });
      fireEvent.click(screen.getByRole("button", { name: "Change Password" }));
      await waitFor(() => expect(api.changePassword).toHaveBeenLastCalledWith("b-current", "b-new-password"));
      await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
      expect(useAuthStore.getState().accessToken).toBe("token-b-refreshed");

      await act(async () => {
        aOrganizations.resolve([{ id: 1, name: "A organization" }]);
        aVaults.resolve({ vaults: [{ id: 11, name: "A vault" }] });
        aSessions.resolve({ sessions: [{ id: "a", user_agent: "A browser" }] });
        aPassword.resolve({ access_token: "token-a-stale" });
        await Promise.resolve();
      });

      expect(screen.getByText("B vault")).toBeInTheDocument();
      expect(screen.queryByText("A vault")).not.toBeInTheDocument();
      expect(toast.success).toHaveBeenCalledTimes(1);
      expect(useAuthStore.getState().user?.id).toBe(2);
      expect(useAuthStore.getState().accessToken).toBe("token-b-refreshed");
    } finally {
      await act(async () => {
        aOrganizations.resolve([]);
        aVaults.resolve({ vaults: [] });
        aSessions.resolve({ sessions: [] });
        aPassword.resolve({ access_token: "cleanup-token" });
        await Promise.resolve();
      });
    }
  });
});

import { Children, StrictMode } from "react";
import type { ButtonHTMLAttributes } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProfilePage from "@/pages/ProfilePage";
import { useAuthStore } from "@/stores/useAuthStore";
import { publishAuthPrincipal, reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";

const api = vi.hoisted(() => ({
  changePassword: vi.fn(),
  listOrganizations: vi.fn(),
  listAccessibleVaults: vi.fn(),
  listSessions: vi.fn(),
  revokeAllSessions: vi.fn(),
  revokeSession: vi.fn(),
  setJwtAccessToken: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
const controls = vi.hoisted(() => ({ callbacks: new Map<string, Array<() => void>>() }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));
vi.mock("sonner", () => ({ toast }));
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => false }));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => {
    const label = Children.toArray(children).filter((child): child is string => typeof child === "string").join("").trim();
    if (onClick && label) {
      const callbacks = controls.callbacks.get(label) ?? [];
      if (callbacks.at(-1) !== onClick) callbacks.push(onClick as unknown as () => void);
      controls.callbacks.set(label, callbacks);
    }
    return <button {...props} onClick={onClick}>{children}</button>;
  },
}));

function deferred<T>() {
  let resolve!: (value?: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = (value) => resolvePromise(value as T);
    reject = rejectPromise;
  });
  void promise.catch(() => undefined);
  return { promise, resolve, reject };
}

function user(id: number, fullName: string, role: "member" | "viewer" = "member") {
  return { id, username: `user-${id}`, full_name: fullName, role, is_active: true } as const;
}

const sessions = [
  { id: "1", user_id: 1, user_agent: "A browser", ip_address: "127.0.0.1", created_at: "2026-01-01", expires_at: "2027-01-01", is_current: false },
  { id: "2", user_id: 1, user_agent: "B browser", ip_address: "127.0.0.2", created_at: "2026-01-01", expires_at: "2027-01-01", is_current: false },
];

describe("ProfilePage supplemental ownership contracts (issue #775)", () => {
  const originalStore = useAuthStore.getState();

  beforeEach(() => {
    vi.resetAllMocks();
    controls.callbacks.clear();
    api.listOrganizations.mockResolvedValue([]);
    api.listAccessibleVaults.mockResolvedValue({ vaults: [] });
    api.listSessions.mockResolvedValue({ sessions: [] });
    api.changePassword.mockResolvedValue(undefined);
    api.revokeAllSessions.mockResolvedValue(undefined);
    api.revokeSession.mockResolvedValue(undefined);
    useAuthStore.setState({
      ...originalStore,
      user: user(1, "A User"),
      accessToken: "token-a",
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
    });
    publishAuthPrincipal({ id: 1, role: "member" });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.setState(originalStore, true);
    controls.callbacks.clear();
  });

  it("distinguishes an organization failure from authoritative empty access and retries only that family", async () => {
    api.listOrganizations.mockRejectedValueOnce(new Error("organization down")).mockResolvedValueOnce([{ id: 2, name: "B org" }]);
    api.listAccessibleVaults.mockResolvedValueOnce({ vaults: [{ id: 22, name: "B vault" }] });
    render(<ProfilePage />);
    await waitFor(() => expect(screen.getByText("Unable to load organizations.")).toBeInTheDocument());
    expect(screen.queryByText("No organization memberships found.")).not.toBeInTheDocument();
    expect(screen.getByText("B vault")).toBeInTheDocument();
    const retry = controls.callbacks.get("Retry organization access")?.at(-1);
    expect(retry).toBeDefined();
    await act(async () => { retry?.(); });
    await waitFor(() => expect(screen.getByText("B org")).toBeInTheDocument());
    expect(api.listOrganizations).toHaveBeenCalledTimes(2);
    expect(api.listAccessibleVaults).toHaveBeenCalledTimes(1);
    expect(api.listSessions).toHaveBeenCalledTimes(1);
  });

  it("does not dispatch a retained A retry after replacement, while B can retry its own failed read", async () => {
    const orgFailure = deferred<never>();
    api.listOrganizations.mockImplementationOnce(() => orgFailure.promise)
      .mockRejectedValueOnce(new Error("B failed"))
      .mockResolvedValueOnce([{ id: 2, name: "B recovered" }]);
    render(<ProfilePage />);
    orgFailure.reject(new Error("A failed"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry organization access" })).toBeInTheDocument());
    const retainedRetry = controls.callbacks.get("Retry organization access")?.[0];
    expect(retainedRetry).toBeDefined();

    await act(async () => {
      reserveReplacementAuthOwner();
      publishAuthPrincipal({ id: 2, role: "member" });
      useAuthStore.setState({ user: user(2, "B User"), accessToken: "token-b" });
    });
    await waitFor(() => expect(api.listOrganizations).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry organization access" })).toBeInTheDocument());
    const currentRetry = controls.callbacks.get("Retry organization access")?.at(-1);
    expect(currentRetry).toBeDefined();
    expect(currentRetry).not.toBe(retainedRetry);
    await act(async () => { retainedRetry?.(); });
    expect(api.listOrganizations).toHaveBeenCalledTimes(2);
    await act(async () => { currentRetry?.(); });
    await waitFor(() => expect(screen.getByText("B recovered")).toBeInTheDocument());
    expect(api.listOrganizations).toHaveBeenCalledTimes(3);
  });

  it("keeps B password drafts, busy state, and token safe until A settles, then clears B only after B succeeds", async () => {
    const aPassword = deferred<{ access_token: string }>();
    const aRevokeAll = deferred<{ access_token: string }>();
    const bPassword = deferred<void>();
    api.changePassword.mockImplementationOnce(() => aPassword.promise).mockImplementationOnce(() => bPassword.promise);
    api.revokeAllSessions.mockImplementationOnce(() => aRevokeAll.promise);
    api.listSessions.mockResolvedValue({ sessions });
    try {
      render(<ProfilePage />);
      await waitFor(() => expect(screen.getByText("A browser")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-password" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password" } });
    fireEvent.submit(screen.getByLabelText("Current password").closest("form")!);
    fireEvent.click(screen.getByRole("button", { name: "Sign Out Other Devices" }));
    await waitFor(() => expect(api.revokeAllSessions).toHaveBeenCalledTimes(1));

    await act(async () => {
      reserveReplacementAuthOwner();
      publishAuthPrincipal({ id: 2, role: "member" });
      useAuthStore.setState({ user: user(2, "B User"), accessToken: "token-b" });
    });
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "b-old" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "b-new-password" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "b-new-password" } });
    fireEvent.submit(screen.getByLabelText("Current password").closest("form")!);
    await waitFor(() => expect(api.changePassword).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Change Password" })).toBeDisabled();
    expect(screen.getByDisplayValue("b-old")).toBeInTheDocument();
    expect(useAuthStore.getState().accessToken).toBe("token-b");

    await act(async () => {
      aPassword.resolve({ access_token: "token-a-late" });
      aRevokeAll.resolve({ access_token: "token-a-late" });
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Change Password" })).toBeDisabled());
    expect(screen.getByDisplayValue("b-old")).toBeInTheDocument();
    expect(useAuthStore.getState().accessToken).toBe("token-b");

    await act(async () => { bPassword.resolve(); });
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Password changed successfully"));
    expect(screen.queryByDisplayValue("b-old")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("b-new-password")).not.toBeInTheDocument();
      expect(useAuthStore.getState().accessToken).toBe("token-b");
    } finally {
      await act(async () => {
        aPassword.resolve({ access_token: "cleanup-a" });
        aRevokeAll.resolve({ access_token: "cleanup-a" });
        bPassword.resolve();
      });
    }
  });

  it("keeps each session row independently pending while the other row completes", async () => {
    const revokeA = deferred<void>();
    const revokeB = deferred<void>();
    api.listSessions.mockResolvedValue({ sessions });
    api.revokeSession.mockImplementation((id: number) => id === 1 ? revokeA.promise : revokeB.promise);
    try {
      render(<ProfilePage />);
      await waitFor(() => expect(screen.getByText("A browser")).toBeInTheDocument());
      const rows = screen.getAllByRole("button", { name: "Revoke session" });
      fireEvent.click(rows[0]);
      fireEvent.click(rows[1]);
      await waitFor(() => expect(api.revokeSession).toHaveBeenCalledTimes(2));
      expect(rows[0]).toBeDisabled();
      expect(rows[1]).toBeDisabled();
      await act(async () => { revokeA.resolve(); });
      await waitFor(() => expect(rows[0]).not.toBeDisabled());
      expect(rows[1]).toBeDisabled();
      await act(async () => { revokeB.resolve(); });
      await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Session revoked"));
    } finally {
      await act(async () => { revokeA.resolve(); revokeB.resolve(); });
    }
  });

  it("retires same-owner principal downgrade and ABA operations before a fresh B password", async () => {
    const aPassword = deferred<void>();
    api.changePassword.mockImplementationOnce(() => aPassword.promise).mockResolvedValueOnce(undefined);
    try {
      render(<ProfilePage />);
      fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "a-old" } });
      fireEvent.change(screen.getByLabelText("New password"), { target: { value: "a-new-password" } });
      fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "a-new-password" } });
      fireEvent.submit(screen.getByLabelText("Current password").closest("form")!);
      await waitFor(() => expect(api.changePassword).toHaveBeenCalledTimes(1));
      await act(async () => {
        publishAuthPrincipal({ id: 1, role: "viewer" });
        useAuthStore.setState({ user: user(1, "A User", "viewer") });
        publishAuthPrincipal({ id: 1, role: "member" });
        useAuthStore.setState({ user: user(1, "A User", "member") });
        aPassword.resolve();
      });
      await waitFor(() => expect(screen.getByLabelText("Current password")).toBeInTheDocument());
      expect(toast.success).not.toHaveBeenCalledWith("Password changed successfully");

      fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "b-old" } });
      fireEvent.change(screen.getByLabelText("New password"), { target: { value: "b-new-password" } });
      fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "b-new-password" } });
      fireEvent.submit(screen.getByLabelText("Current password").closest("form")!);
      await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Password changed successfully"));
    } finally {
      await act(async () => { aPassword.resolve(); });
    }
  });

  it("stops credential publication and refresh after a setter reenters with a replacement owner", async () => {
    const revokeAll = deferred<{ access_token: string }>();
    api.revokeAllSessions.mockImplementationOnce(() => revokeAll.promise);
    api.listSessions.mockResolvedValue({ sessions });
    api.setJwtAccessToken.mockImplementationOnce(() => {
      reserveReplacementAuthOwner();
      publishAuthPrincipal({ id: 2, role: "member" });
      useAuthStore.setState({ user: user(2, "B User") });
    });
    try {
      render(<ProfilePage />);
      await waitFor(() => expect(screen.getByText("A browser")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: "Sign Out Other Devices" }));
      await waitFor(() => expect(api.revokeAllSessions).toHaveBeenCalledTimes(1));
      await act(async () => { revokeAll.resolve({ access_token: "late-token" }); });
      expect(api.setJwtAccessToken).toHaveBeenCalledWith("late-token");
      expect(useAuthStore.getState().accessToken).toBe("token-a");
      expect(toast.success).not.toHaveBeenCalledWith("Other sessions revoked");
      expect(api.listSessions).toHaveBeenCalledTimes(2);
    } finally {
      await act(async () => { revokeAll.resolve({ access_token: "cleanup-token" }); });
    }
  });

  it("preserves successful password and read behavior under StrictMode effect replay", async () => {
    api.listSessions.mockResolvedValue({ sessions });
    api.changePassword.mockResolvedValue(undefined);
    render(<StrictMode><ProfilePage /></StrictMode>);
    await waitFor(() => expect(screen.getByText("A browser")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-password" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password" } });
    fireEvent.submit(screen.getByLabelText("Current password").closest("form")!);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Password changed successfully"));
    expect(screen.queryByDisplayValue("old-password")).not.toBeInTheDocument();
    expect(api.listSessions).toHaveBeenCalled();
  });

  it("preserves both committed revoke notifications while the newest sessions read wins", async () => {
    const olderRead = deferred<{ sessions: typeof sessions }>();
    const newerRead = deferred<{ sessions: typeof sessions }>();
    api.listSessions.mockResolvedValueOnce({ sessions })
      .mockImplementationOnce(() => olderRead.promise)
      .mockImplementationOnce(() => newerRead.promise);
    try {
      render(<ProfilePage />);
      await waitFor(() => expect(screen.getByText("A browser")).toBeInTheDocument());
      const rows = screen.getAllByRole("button", { name: "Revoke session" });
      fireEvent.click(rows[0]);
      await waitFor(() => expect(api.listSessions).toHaveBeenCalledTimes(2));
      fireEvent.click(rows[1]);
      await waitFor(() => expect(api.listSessions).toHaveBeenCalledTimes(3));
      await act(async () => { newerRead.resolve({ sessions: [{ ...sessions[0], user_agent: "Current sessions" }] }); });
      await waitFor(() => expect(screen.getByText("Current sessions")).toBeInTheDocument());
      await act(async () => { olderRead.resolve({ sessions: [{ ...sessions[0], user_agent: "Stale sessions" }] }); });
      await waitFor(() => expect(toast.success.mock.calls.filter(([message]) => message === "Session revoked")).toHaveLength(2));
      expect(screen.getByText("Current sessions")).toBeInTheDocument();
      expect(screen.queryByText("Stale sessions")).not.toBeInTheDocument();
      expect(api.revokeSession.mock.calls).toEqual([[1], [2]]);
    } finally {
      await act(async () => { olderRead.resolve({ sessions: [] }); newerRead.resolve({ sessions: [] }); });
    }
  });
});

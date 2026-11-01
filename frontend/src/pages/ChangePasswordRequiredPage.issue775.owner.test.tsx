import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChangePasswordRequiredPage from "@/pages/ChangePasswordRequiredPage";
import { useAuthStore } from "@/stores/useAuthStore";

const mockChangePassword = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, changePassword: mockChangePassword };
});
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props}>{children}</button>,
}));
vi.mock("@/components/ui/input", () => ({ Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} /> }));
vi.mock("@/components/ui/label", () => ({ Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => <label {...props}>{children}</label> }));
vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: ReactNode }) => <section>{children}</section>,
  CardContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  CardHeader: ({ children }: { children: ReactNode }) => <header>{children}</header>,
  CardTitle: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
}));
vi.mock("@/components/icons/MeridianLogo", () => ({ MeridianLogo: () => <span aria-hidden="true" /> }));
vi.mock("lucide-react", () => ({ Loader2: () => null }));

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const userA = { id: 11, username: "alice", full_name: "Alice", role: "member" as const, is_active: true, must_change_password: true };
const userB = { id: 12, username: "bob", full_name: "Bob", role: "member" as const, is_active: true, must_change_password: true };

describe("C26 ChangePasswordRequiredPage owner guard", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;

  afterEach(() => {
    cleanup();
    act(() => useAuthStore.setState(priorAuthState));
    mockChangePassword.mockReset();
  });

  it("rejects a stale password completion after the auth owner is replaced", async () => {
    priorAuthState = useAuthStore.getState();
    const fetchMe = vi.fn().mockResolvedValue(undefined);
    act(() => useAuthStore.setState({ user: userA, isAuthenticated: true, isInitialized: true, fetchMe }));
    const first = deferred<void>();
    mockChangePassword.mockReturnValue(first.promise);

    try {
      render(
        <MemoryRouter initialEntries={["/change-password"]}>
          <Routes>
            <Route path="/change-password" element={<ChangePasswordRequiredPage />} />
            <Route path="/" element={<div data-testid="home">home</div>} />
          </Routes>
        </MemoryRouter>,
      );
      fireEvent.change(screen.getByPlaceholderText("Enter current password..."), { target: { value: "current-pass" } });
      fireEvent.change(screen.getByPlaceholderText("Enter new password..."), { target: { value: "Newpass1" } });
      fireEvent.change(screen.getByPlaceholderText("Confirm new password..."), { target: { value: "Newpass1" } });
      fireEvent.click(screen.getByRole("button", { name: "Change password" }));
      await waitFor(() => expect(mockChangePassword).toHaveBeenCalledWith("current-pass", "Newpass1"));

      await act(async () => {
        useAuthStore.setState({ user: userB, isAuthenticated: true });
      });
      await act(async () => first.resolve());

      expect(useAuthStore.getState().user).toEqual(userB);
      expect(fetchMe).not.toHaveBeenCalled();
      expect(screen.queryByTestId("home")).not.toBeInTheDocument();
    } finally {
      first.resolve();
      await Promise.allSettled([first.promise]);
    }
  });
});

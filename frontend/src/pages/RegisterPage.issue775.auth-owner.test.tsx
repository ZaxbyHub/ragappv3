import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import RegisterPage from "@/pages/RegisterPage";
import { useAuthStore } from "@/stores/useAuthStore";

const navigation = vi.hoisted(() => ({ navigate: vi.fn() }));
vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigation.navigate,
}));

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const replacementUser = { id: 12, username: "bob", full_name: "Bob", role: "member" as const, is_active: true };

describe("C33 RegisterPage auth-owner completion", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;

  afterEach(() => {
    cleanup();
    act(() => useAuthStore.setState(priorAuthState));
    navigation.navigate.mockReset();
  });

  it("does not navigate or publish a stale registration completion after owner replacement", async () => {
    priorAuthState = useAuthStore.getState();
    const register = vi.fn();
    const first = deferred<void>();
    register.mockReturnValue(first.promise);
    act(() => useAuthStore.setState({ user: null, isAuthenticated: false, isInitialized: true, register }));

    try {
      render(
        <MemoryRouter initialEntries={["/register"]}>
          <Routes>
            <Route path="/register" element={<RegisterPage />} />
            <Route path="/" element={<div data-testid="home">home</div>} />
          </Routes>
        </MemoryRouter>,
      );
      fireEvent.change(screen.getByPlaceholderText("Username (required)"), { target: { value: "alice" } });
      fireEvent.change(screen.getByPlaceholderText("Full name (optional)"), { target: { value: "Alice" } });
      fireEvent.change(screen.getByPlaceholderText("Password (min 8 characters)"), { target: { value: "Newpass1" } });
      fireEvent.change(screen.getByPlaceholderText("Confirm password"), { target: { value: "Newpass1" } });
      fireEvent.click(screen.getByRole("button", { name: /create account/i }));
      await waitFor(() => expect(register).toHaveBeenCalledWith("alice", "Newpass1", "Alice"));

      await act(async () => {
        useAuthStore.setState({
          user: replacementUser,
          accessToken: "fixture-token-b",
          isAuthenticated: true,
          isInitialized: true,
        });
      });
      await waitFor(() => expect(screen.getByTestId("home")).toBeInTheDocument());
      await act(async () => first.resolve());

      expect(useAuthStore.getState().user).toEqual(replacementUser);
      expect(navigation.navigate).not.toHaveBeenCalled();
      expect(screen.getByTestId("home")).toBeInTheDocument();
    } finally {
      first.resolve();
      await Promise.allSettled([first.promise]);
    }
  });
});

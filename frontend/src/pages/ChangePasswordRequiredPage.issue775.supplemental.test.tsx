import { isValidElement, type FormEvent, type ReactElement, type ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChangePasswordRequiredPage from "@/pages/ChangePasswordRequiredPage";
import { useAuthStore } from "@/stores/useAuthStore";
import { getJwtAccessToken, setJwtAccessToken as setRealJwtAccessToken } from "@/lib/api/core";

const mockChangePassword = vi.hoisted(() => vi.fn());
const mockSetJwtAccessToken = vi.hoisted(() => vi.fn());
const mockNavigate = vi.hoisted(() => vi.fn());
const capturedFormSubmit = vi.hoisted(() => ({ current: null as ((event: FormEvent<HTMLFormElement>) => Promise<void>) | null }));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, changePassword: mockChangePassword, setJwtAccessToken: mockSetJwtAccessToken };
});
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props}>{children}</button>,
}));
vi.mock("@/components/ui/input", () => ({ Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} /> }));
vi.mock("@/components/ui/label", () => ({ Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => <label {...props}>{children}</label> }));
vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: ReactNode }) => <section>{children}</section>,
  CardContent: ({ children }: { children: ReactNode }) => {
    if (isValidElement(children) && children.type === "form") {
      const form = children as ReactElement<{ onSubmit?: (event: FormEvent<HTMLFormElement>) => Promise<void> }>;
      capturedFormSubmit.current = form.props.onSubmit ?? null;
    }
    return <div>{children}</div>;
  },
  CardDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  CardHeader: ({ children }: { children: ReactNode }) => <header>{children}</header>,
  CardTitle: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
}));
vi.mock("@/components/icons/MeridianLogo", () => ({ MeridianLogo: () => <span aria-hidden="true" /> }));
vi.mock("lucide-react", () => ({ Loader2: () => null }));

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const userA = { id: 11, username: "alice", full_name: "Alice", role: "member" as const, is_active: true, must_change_password: true };
const userB = { id: 12, username: "bob", full_name: "Bob", role: "admin" as const, is_active: true, must_change_password: true };
const credentials = { access_token: "new-token", token_type: "bearer", expires_in: 1800 };

function renderPage() {
  return render(<MemoryRouter initialEntries={["/change-password"]}><ChangePasswordRequiredPage /></MemoryRouter>);
}

function fillValidForm() {
  fireEvent.change(screen.getByPlaceholderText("Enter current password..."), { target: { value: "current-pass" } });
  fireEvent.change(screen.getByPlaceholderText("Enter new password..."), { target: { value: "Newpass1" } });
  fireEvent.change(screen.getByPlaceholderText("Confirm new password..."), { target: { value: "Newpass1" } });
}

function submitEvent(): FormEvent<HTMLFormElement> {
  return { preventDefault: vi.fn() } as unknown as FormEvent<HTMLFormElement>;
}

async function submitValidForm() {
  fillValidForm();
  fireEvent.click(screen.getByRole("button", { name: "Change password" }));
  await waitFor(() => expect(mockChangePassword).toHaveBeenCalledTimes(1));
}

describe("ChangePasswordRequiredPage supplemental concurrency contract", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState> | undefined;

  function prime(fetchMe = vi.fn().mockResolvedValue(undefined)) {
    priorAuthState = useAuthStore.getState();
    mockSetJwtAccessToken.mockImplementation(setRealJwtAccessToken);
    act(() => {
      setRealJwtAccessToken("old-token");
      useAuthStore.setState({ user: userA, accessToken: "old-token", isAuthenticated: true, isInitialized: true, fetchMe });
    });
    mockSetJwtAccessToken.mockClear();
    return fetchMe;
  }

  afterEach(() => {
    cleanup();
    mockSetJwtAccessToken.mockImplementation(setRealJwtAccessToken);
    if (priorAuthState) {
      const previous = priorAuthState;
      act(() => {
        setRealJwtAccessToken(previous.accessToken);
        useAuthStore.setState(previous);
      });
    }
    priorAuthState = undefined;
    mockChangePassword.mockReset();
    mockSetJwtAccessToken.mockReset();
    mockNavigate.mockReset();
    capturedFormSubmit.current = null;
  });

  it("publishes typed returned credentials before clearing the flag and calling fetchMe", async () => {
    const fetchMe = vi.fn().mockImplementation(() => {
      expect(mockSetJwtAccessToken).toHaveBeenCalledWith("new-token");
      expect(useAuthStore.getState().accessToken).toBe("new-token");
      expect(useAuthStore.getState().user?.must_change_password).toBe(false);
      return Promise.resolve();
    });
    prime(fetchMe);
    mockChangePassword.mockResolvedValue(credentials);
    renderPage();
    await submitValidForm();
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true }));
    expect(getJwtAccessToken()).toBe("new-token");
    expect(mockSetJwtAccessToken.mock.calls.every(([token]) => token === "new-token")).toBe(true);
    expect(fetchMe).toHaveBeenCalledTimes(1);
  });

  it("retires completion after role and null/user ABA principal changes", async () => {
    const fetchMe = prime();
    const first = deferred<typeof credentials>();
    mockChangePassword.mockReturnValue(first.promise);
    renderPage();
    await submitValidForm();
    act(() => {
      useAuthStore.setState({ user: { ...userA, role: "admin" } });
      useAuthStore.setState({ user: null });
      useAuthStore.setState({ user: userA });
    });
    await act(async () => first.resolve(credentials));
    expect(mockSetJwtAccessToken).not.toHaveBeenCalled();
    expect(fetchMe).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("retires the retained A callback while an independent B submission stays intact", async () => {
    const fetchMe = prime();
    const first = deferred<typeof credentials>();
    const second = deferred<typeof credentials>();
    mockChangePassword.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    renderPage();
    fillValidForm();
    const retainedA = capturedFormSubmit.current;
    expect(retainedA).toEqual(expect.any(Function));
    act(() => { void retainedA?.(submitEvent()); });
    await waitFor(() => expect(mockChangePassword).toHaveBeenCalledTimes(1));

    act(() => useAuthStore.setState({ user: userB, accessToken: "b-token" }));
    mockSetJwtAccessToken.mockClear();
    fireEvent.change(screen.getByPlaceholderText("Enter current password..."), { target: { value: "b-current" } });
    fireEvent.change(screen.getByPlaceholderText("Enter new password..."), { target: { value: "Bpass123" } });
    fireEvent.change(screen.getByPlaceholderText("Confirm new password..."), { target: { value: "Bpass123" } });
    const submitB = capturedFormSubmit.current;
    expect(submitB).toEqual(expect.any(Function));
    act(() => { void submitB?.(submitEvent()); });
    await waitFor(() => expect(mockChangePassword).toHaveBeenCalledTimes(2));

    // The retained A callback is explicitly invoked and must not create a third dispatch.
    act(() => { void retainedA?.(submitEvent()); });
    expect(mockChangePassword).toHaveBeenCalledTimes(2);

    await act(async () => first.resolve(credentials));
    expect(screen.getByRole("button", { name: /Updating/ })).toBeDisabled();
    expect(useAuthStore.getState().user).toEqual(userB);
    expect(useAuthStore.getState().user?.must_change_password).toBe(true);
    expect(useAuthStore.getState().accessToken).toBe("b-token");
    expect(screen.getByPlaceholderText("Enter current password...")).toHaveValue("b-current");
    expect(screen.getByPlaceholderText("Enter new password...")).toHaveValue("Bpass123");
    expect(screen.getByPlaceholderText("Confirm new password...")).toHaveValue("Bpass123");
    expect(fetchMe).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();

    await act(async () => second.resolve({ ...credentials, access_token: "b-new-token" }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true }));
    expect(mockChangePassword).toHaveBeenCalledTimes(2);
    expect(mockNavigate).toHaveBeenCalledTimes(1);
    expect(getJwtAccessToken()).toBe("b-new-token");
    expect(mockSetJwtAccessToken.mock.calls.every(([token]) => token === "b-new-token")).toBe(true);
    expect(mockSetJwtAccessToken).toHaveBeenCalledWith("b-new-token");
    expect(useAuthStore.getState().accessToken).toBe("b-new-token");
    expect(useAuthStore.getState().user?.must_change_password).toBe(false);
    expect(fetchMe).toHaveBeenCalledTimes(1);
  });

  it("suppresses all completion effects after local unmount", async () => {
    const fetchMe = prime();
    const first = deferred<typeof credentials>();
    mockChangePassword.mockReturnValue(first.promise);
    const view = renderPage();
    await submitValidForm();
    view.unmount();
    await act(async () => first.resolve(credentials));
    expect(mockSetJwtAccessToken).not.toHaveBeenCalled();
    expect(fetchMe).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("retires publication when JWT publication reenters the store", async () => {
    const fetchMe = prime();
    mockSetJwtAccessToken.mockImplementation(() => { useAuthStore.setState({ user: userB }); });
    mockChangePassword.mockResolvedValue(credentials);
    renderPage();
    await submitValidForm();
    await waitFor(() => expect(mockSetJwtAccessToken).toHaveBeenCalledWith("new-token"));
    expect(useAuthStore.getState().accessToken).toBe("old-token");
    expect(fetchMe).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("retires publication when store token publication reenters the principal", async () => {
    const fetchMe = prime();
    const unsubscribe = useAuthStore.subscribe((state, previous) => {
      if (state.accessToken === "new-token" && previous.accessToken !== "new-token") {
        useAuthStore.setState({ user: userB, accessToken: "foreign-token" });
      }
    });
    try {
      mockChangePassword.mockResolvedValue(credentials);
      renderPage();
      await submitValidForm();
      await waitFor(() => expect(mockSetJwtAccessToken).toHaveBeenCalledWith("new-token"));
      expect(useAuthStore.getState().accessToken).toBe("foreign-token");
      expect(useAuthStore.getState().user).toEqual(userB);
      expect(fetchMe).not.toHaveBeenCalled();
      expect(mockNavigate).not.toHaveBeenCalled();
    } finally {
      unsubscribe();
    }
  });

  it("admits exactly one synchronous submission", async () => {
    prime();
    const first = deferred<typeof credentials>();
    mockChangePassword.mockReturnValue(first.promise);
    renderPage();
    fillValidForm();
    const submit = capturedFormSubmit.current;
    expect(submit).toEqual(expect.any(Function));
    let firstCall!: Promise<void>;
    let secondCall!: Promise<void>;
    act(() => {
      firstCall = submit!(submitEvent());
      secondCall = submit!(submitEvent());
    });
    expect(mockChangePassword).toHaveBeenCalledTimes(1);
    await act(async () => {
      first.resolve(credentials);
      await firstCall;
      await secondCall;
    });
  });

  it("keeps the original failure outcome scoped to the current operation", async () => {
    prime();
    mockChangePassword.mockRejectedValue(new Error("bad password"));
    renderPage();
    await submitValidForm();
    expect(await screen.findByText("bad password")).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("navigates after a nonfatal fetchMe failure", async () => {
    const fetchMe = prime(vi.fn().mockRejectedValue(new Error("refresh failed")));
    mockChangePassword.mockResolvedValue(credentials);
    renderPage();
    await submitValidForm();
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true }));
    expect(fetchMe).toHaveBeenCalledTimes(1);
  });
});

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserRouter, MemoryRouter } from "react-router-dom";
import LoginPage from "./LoginPage";

const mocks = vi.hoisted(() => ({
  login: vi.fn(),
  init: vi.fn(),
  navigate: vi.fn(),
  currentOwner: null as { id: number } | null,
  captureOwner: vi.fn(),
  isCurrentOwner: vi.fn(),
  state: {
    login: undefined as unknown,
    init: undefined as unknown,
    needsSetup: false as boolean | null,
    isLoading: false,
    authMode: "multi_user",
    initializationFailed: false,
  },
}));

vi.mock("@/stores/useAuthStore", () => {
  const useAuthStore = Object.assign(
    (selector?: (state: typeof mocks.state) => unknown) => selector ? selector(mocks.state) : mocks.state,
    { getState: () => mocks.state },
  );
  return { useAuthStore };
});

vi.mock("@/lib/api/auth-lifecycle", () => ({
  captureAuthOwner: mocks.captureOwner,
  captureAuthPrincipalGeneration: vi.fn(() => 0),
  isCurrentAuthOwner: mocks.isCurrentOwner,
}));

vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => mocks.navigate,
}));

type Deferred = { promise: Promise<void>; resolve: () => void; reject: (reason: Error) => void };
function deferred(): Deferred {
  let resolve!: () => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function submitLogin() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/username/i), "alice");
  await user.type(screen.getByLabelText(/^password$/i), "secret");
  await user.click(screen.getByRole("button", { name: /sign in/i }));
}

describe("LoginPage login ownership", () => {
  beforeEach(() => {
    mocks.login.mockReset();
    mocks.init.mockReset();
    mocks.navigate.mockReset();
    mocks.currentOwner = { id: 0 };
    mocks.captureOwner.mockImplementation(() => mocks.currentOwner);
    mocks.isCurrentOwner.mockImplementation((owner) => owner === mocks.currentOwner);
    mocks.state.login = mocks.login;
    mocks.state.init = mocks.init;
    mocks.state.needsSetup = false;
    mocks.state.isLoading = false;
    mocks.state.authMode = "multi_user";
    mocks.state.initializationFailed = false;
  });

  afterEach(() => vi.clearAllMocks());

  it("does not navigate when an owner is replaced while its login is pending", async () => {
    const pending = deferred();
    const ownerA = { id: 1 };
    mocks.login.mockImplementation(() => {
      mocks.currentOwner = ownerA;
      return pending.promise;
    });
    render(<BrowserRouter><LoginPage /></BrowserRouter>);
    await submitLogin();

    mocks.currentOwner = { id: 2 };
    await act(async () => pending.resolve());

    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not navigate when ownership changes after settlement but before the page continuation", async () => {
    const pending = deferred();
    const ownerA = { id: 7 };
    mocks.login.mockImplementation(() => {
      mocks.currentOwner = ownerA;
      return pending.promise;
    });
    render(<BrowserRouter><LoginPage /></BrowserRouter>);
    await submitLogin();

    pending.resolve();
    mocks.currentOwner = { id: 8 };
    await act(async () => { await Promise.resolve(); });

    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it("does not publish a rejection from an invocation superseded by a newer owner", async () => {
    const pending = deferred();
    mocks.login.mockImplementation(() => {
      mocks.currentOwner = { id: 4 };
      return pending.promise;
    });
    render(<BrowserRouter><LoginPage /></BrowserRouter>);
    await submitLogin();

    mocks.currentOwner = { id: 5 };
    await act(async () => pending.reject(new Error("old invocation failed")));

    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(screen.queryByText("old invocation failed")).not.toBeInTheDocument();
  });

  it("keeps a current invalid-credential failure visible", async () => {
    mocks.login.mockImplementation(() => {
      mocks.currentOwner = { id: 3 };
      return Promise.reject(new Error("Invalid credentials"));
    });
    render(<BrowserRouter><LoginPage /></BrowserRouter>);
    await submitLogin();

    expect(await screen.findByText("Invalid credentials")).toBeInTheDocument();
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it("navigates a current successful login to its saved return route", async () => {
    mocks.login.mockImplementation(() => {
      mocks.currentOwner = { id: 6 };
      return Promise.resolve();
    });
    render(
      <MemoryRouter initialEntries={[{ pathname: "/login", state: { from: { pathname: "/protected" } } }]}>
        <LoginPage />
      </MemoryRouter>,
    );
    await submitLogin();

    expect(mocks.navigate).toHaveBeenCalledWith("/protected", { replace: true });
  });
});

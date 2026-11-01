import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentPropsWithoutRef, ReactElement, ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cardBoundary = vi.hoisted(() => ({ latest: null as ReactNode | null }));

vi.mock("@/components/ui/card", async () => {
  const actual = await vi.importActual<typeof import("@/components/ui/card")>("@/components/ui/card");
  return {
    ...actual,
    CardContent: (props: ComponentPropsWithoutRef<typeof actual.CardContent>) => {
      cardBoundary.latest = props.children;
      return <actual.CardContent {...props} />;
    },
  };
});

const authClient = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  interceptors: {
    request: { use: vi.fn() },
    response: { use: vi.fn() },
  },
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => authClient),
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import RegisterPage from "@/pages/RegisterPage";
import {
  captureRegisterPublicationScope,
  isRegisterPublicationAdmissionCurrent,
  useAuthStore,
} from "@/stores/useAuthStore";
import {
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";
import { getJwtAccessToken, setJwtAccessToken } from "@/lib/api/core";

type Deferred<T> = {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
};

const pendingDeferreds = new Set<() => void>();
const testUnsubscribers = new Set<() => void>();

function deferred<T>(cleanupValue: T): Deferred<T> {
  let settled = false;
  let resolvePromise: (value: T) => void = () => undefined;
  let rejectPromise: (error: unknown) => void = () => undefined;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = (value) => { settled = true; resolve(value); };
    rejectPromise = (error) => { settled = true; reject(error); };
  });
  pendingDeferreds.add(() => { if (!settled) resolvePromise(cleanupValue); });
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

function user(id: number, role: "admin" | "member" = "member") {
  return {
    id,
    username: `user-${id}`,
    full_name: `User ${id}`,
    role,
    is_active: true,
  } as const;
}

function renderRegister() {
  return render(
    <MemoryRouter initialEntries={["/register"]}>
      <Routes>
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/" element={<div data-testid="home">home</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function capturedForm(): ReactElement<{ onSubmit?: (event: React.FormEvent) => void }> {
  return cardBoundary.latest as ReactElement<{ onSubmit?: (event: React.FormEvent) => void }>;
}

function invokeCapturedForm(form: ReactElement<{ onSubmit?: (event: React.FormEvent) => void }>) {
  const preventDefault = vi.fn();
  return form.props.onSubmit?.({ preventDefault } as React.FormEvent);
}

async function submitValidForm() {
  await act(async () => undefined);
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: "new-user" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "GoodPass1" } });
  fireEvent.change(screen.getByLabelText("Confirm Password"), { target: { value: "GoodPass1" } });
  fireEvent.submit(screen.getByRole("button", { name: /create account/i }).closest("form")!);
}

describe("Register publication ownership", () => {
  let originalJwt: string | null;
  let originalState: ReturnType<typeof useAuthStore.getState>;

  beforeEach(() => {
    originalJwt = getJwtAccessToken();
    originalState = useAuthStore.getState();
    vi.clearAllMocks();
    reserveReplacementAuthOwner();
    setJwtAccessToken(null);
    publishAuthPrincipal(null);
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isLoading: false,
      needsSetup: null,
      initializationFailed: false,
      isInitialized: false,
      authMode: "unknown",
    });
    authClient.get.mockResolvedValue({ data: { csrf_token: "csrf-test" } });
  });

  afterEach(async () => {
    reserveReplacementAuthOwner();
    for (const unsubscribe of testUnsubscribers) unsubscribe();
    testUnsubscribers.clear();
    for (const settle of pendingDeferreds) settle();
    pendingDeferreds.clear();
    await act(async () => undefined);
    setJwtAccessToken(originalJwt);
    await act(async () => {
      useAuthStore.setState({
        user: originalState.user,
        accessToken: originalState.accessToken,
        isAuthenticated: originalState.isAuthenticated,
        isLoading: originalState.isLoading,
        needsSetup: originalState.needsSetup,
        initializationFailed: originalState.initializationFailed,
        isInitialized: originalState.isInitialized,
        authMode: originalState.authMode,
      });
    });
    await act(async () => undefined);
  });

  it("allows its own null-to-user publication and navigates once", async () => {
    const request = deferred({ data: { access_token: "cleanup", user: user(999) } });
    authClient.post.mockImplementation((url: string) => url === "/auth/register" ? request.promise : Promise.resolve({ data: {} }));
    renderRegister();

    await submitValidForm();
    expect(authClient.post).toHaveBeenCalledTimes(1);
    expect(authClient.post).toHaveBeenCalledWith(
      "/auth/register",
      { username: "new-user", password: "GoodPass1", full_name: undefined },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );

    await act(async () => request.resolve({ data: { access_token: "token-a", user: user(10) } }));
    await waitFor(() => expect(screen.getByTestId("home")).toBeInTheDocument());
  });

  it("keeps an ordinary current-owner 409 mapped error visible", async () => {
    authClient.post.mockRejectedValue(Object.assign(new Error("409"), { response: { status: 409 } }));
    renderRegister();
    await submitValidForm();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Username already registered"));
  });

  it("retains the exact three-argument direct action and resolves void", async () => {
    authClient.post.mockResolvedValue({ data: { access_token: "token-direct", user: user(11) } });
    const result = await useAuthStore.getState().register("direct-user", "GoodPass1", "Direct User");
    expect(result).toBeUndefined();
  });

  it("retires a response after owner replacement and does not navigate", async () => {
    const request = deferred({ data: { access_token: "cleanup", user: user(999) } });
    authClient.post.mockImplementation((url: string) => url === "/auth/register" ? request.promise : Promise.resolve({ data: {} }));
    renderRegister();
    await submitValidForm();
    reserveReplacementAuthOwner();
    await act(async () => request.resolve({ data: { access_token: "token-stale", user: user(12) } }));
    await act(async () => undefined);
    expect(screen.queryByTestId("home")).not.toBeInTheDocument();
  });

  it("retires same-owner role changes and null ABA before publication", async () => {
    const request = deferred({ data: { access_token: "cleanup", user: user(999) } });
    authClient.post.mockImplementation((url: string) => url === "/auth/register" ? request.promise : Promise.resolve({ data: {} }));
    const view = renderRegister();
    await submitValidForm();
    useAuthStore.setState({ user: user(13, "admin") });
    useAuthStore.setState({ user: null });
    await act(async () => request.resolve({ data: { access_token: "token-aba", user: user(13) } }));
    await act(async () => undefined);
    expect(view.queryByTestId("home")).not.toBeInTheDocument();
  });

  it("allows fresh B to submit while retained A is held, and ignores retained A", async () => {
    const requestA = deferred({ data: { access_token: "cleanup-a", user: user(999) } });
    const requestB = deferred({ data: { access_token: "cleanup-b", user: user(998) } });
    let registerCalls = 0;
    authClient.post.mockImplementation((url: string) => {
      if (url !== "/auth/register") return Promise.resolve({ data: {} });
      registerCalls += 1;
      return registerCalls === 1 ? requestA.promise : requestB.promise;
    });
    renderRegister();
    await submitValidForm();
    const retainedA = capturedForm();
    reserveReplacementAuthOwner();
    useAuthStore.setState({ user: null });
    await act(async () => undefined);
    const freshB = capturedForm();
    await act(async () => { void invokeCapturedForm(freshB); });
    expect(registerCalls).toBe(1);
    await act(async () => { void invokeCapturedForm(retainedA); });
    expect(registerCalls).toBe(1);
    await act(async () => requestA.resolve({ data: { access_token: "token-b", user: user(17) } }));
    await waitFor(() => expect(registerCalls).toBe(2));
    await act(async () => requestB.resolve({ data: { access_token: "token-b", user: user(18) } }));
    await waitFor(() => expect(screen.getByTestId("home")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not publish after a re-entrant JWT/store replacement", async () => {
    const request = deferred({ data: { access_token: "cleanup", user: user(999) } });
    authClient.post.mockImplementation((url: string) => url === "/auth/register" ? request.promise : Promise.resolve({ data: {} }));
    const unsubscribe = useAuthStore.subscribe((state) => {
      if (state.accessToken === "token-reentrant") reserveReplacementAuthOwner();
    });
    testUnsubscribers.add(unsubscribe);
    renderRegister();
    await submitValidForm();
    await act(async () => request.resolve({ data: { access_token: "token-reentrant", user: user(14) } }));
    unsubscribe();
    await act(async () => undefined);
    expect(screen.queryByTestId("home")).not.toBeInTheDocument();
  });

  it("rejects a retained A scope while a fresh B scope remains admissible", () => {
    const scopeA = captureRegisterPublicationScope();
    reserveReplacementAuthOwner();
    expect(isRegisterPublicationAdmissionCurrent(scopeA)).toBe(false);
    const scopeB = captureRegisterPublicationScope();
    expect(isRegisterPublicationAdmissionCurrent(scopeB)).toBe(true);
  });

  it("ignores an unmounted completion", async () => {
    const request = deferred({ data: { access_token: "cleanup", user: user(999) } });
    authClient.post.mockImplementation((url: string) => url === "/auth/register" ? request.promise : Promise.resolve({ data: {} }));
    const view = renderRegister();
    await submitValidForm();
    view.unmount();
    await act(async () => request.resolve({ data: { access_token: "token-unmounted", user: user(15) } }));
    expect(screen.queryByTestId("home")).not.toBeInTheDocument();
  });

  it("does not dispatch a synchronous duplicate form callback", async () => {
    const request = deferred({ data: { access_token: "cleanup", user: user(999) } });
    authClient.post.mockImplementation((url: string) => url === "/auth/register" ? request.promise : Promise.resolve({ data: {} }));
    renderRegister();
    await submitValidForm();
    const form = screen.getByRole("button", { name: /creating account/i }).closest("form")!;
    fireEvent.submit(form);
    expect(authClient.post).toHaveBeenCalledTimes(1);
    await act(async () => request.resolve({ data: { access_token: "token-once", user: user(16) } }));
  });
});

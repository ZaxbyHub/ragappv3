import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { axiosPostMock, axiosGetMock, axiosPatchMock, axiosPutMock, axiosDeleteMock, navigateMock } =
  vi.hoisted(() => ({
    axiosPostMock: vi.fn(),
    axiosGetMock: vi.fn(),
    axiosPatchMock: vi.fn(),
    axiosPutMock: vi.fn(),
    axiosDeleteMock: vi.fn(),
    navigateMock: vi.fn(),
  }));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: axiosGetMock,
      post: axiosPostMock,
      patch: axiosPatchMock,
      put: axiosPutMock,
      delete: axiosDeleteMock,
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    })),
  },
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: { getState: () => ({ fetchVaults: vi.fn() }) },
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigateMock,
}));

import { MobileBottomNav } from "./MobileBottomNav";
import { resetCsrfToken, setJwtAccessToken } from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function jsonResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

function renderNav(count = 1) {
  return render(
    <MemoryRouter>
      {Array.from({ length: count }, (_, index) => (
        <MobileBottomNav key={index} activeItem="chat" onItemSelect={vi.fn()} />
      ))}
    </MemoryRouter>,
  );
}

describe("public MobileBottomNav transport carry contract for issue #775", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    axiosPostMock.mockReset();
    axiosGetMock.mockReset();
    axiosPatchMock.mockReset();
    axiosPutMock.mockReset();
    axiosDeleteMock.mockReset();
    navigateMock.mockReset();
    resetCsrfToken();
    setJwtAccessToken("current-token");
    useAuthStore.setState({
      user: {
        id: 1,
        username: "mobile-user",
        full_name: "Mobile User",
        role: "admin",
        is_active: true,
      },
      accessToken: "current-token",
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      authMode: "jwt",
    });
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    resetCsrfToken();
    setJwtAccessToken(null);
  });

  it("uses the real store/core public transport so stale logout cannot navigate and the replacement waits for body settlement", async () => {
    const firstLogout = deferred<unknown>();
    const secondLogout = deferred<unknown>();
    let logoutCalls = 0;
    let firstReleased = false;
    let secondReleased = false;
    let refreshCalls = 0;
    const publicOutcomes: Promise<unknown>[] = [];
    const outcomes: Promise<unknown>[] = [];
    const originalLogout = useAuthStore.getState().logout;
    axiosPostMock.mockImplementation((path: string) => {
      expect(path).toBe("/auth/logout");
      logoutCalls += 1;
      return logoutCalls === 1 ? firstLogout.promise : secondLogout.promise;
    });
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/csrf-token")) {
        return Promise.resolve(jsonResponse(200, { csrf_token: "csrf-mobile" }));
      }
      if (url.includes("/auth/refresh")) {
        refreshCalls += 1;
        return Promise.resolve(jsonResponse(200, { access_token: "replacement-token" }));
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    try {
      useAuthStore.setState({
        logout: () => {
          const operation = originalLogout();
          const outcome = operation.then(
            (value) => ({ status: "fulfilled" as const, value }),
            (reason) => ({ status: "rejected" as const, reason }),
          );
          publicOutcomes.push(outcome);
          return operation;
        },
      });
      renderNav(2);
      for (const moreButton of screen.getAllByLabelText("More navigation options")) {
        fireEvent.click(moreButton);
      }
      for (const logoutButton of screen.getAllByLabelText("Log out")) {
        fireEvent.click(logoutButton);
      }

      await waitFor(() => expect(logoutCalls).toBe(1));
      const queuedPreLogoutRefresh = useAuthStore.getState().refreshToken();
      const queuedPreLogoutOutcome = queuedPreLogoutRefresh.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (reason) => ({ status: "rejected" as const, reason }),
      );
      outcomes.push(queuedPreLogoutOutcome);
      expect(refreshCalls).toBe(0);

      firstLogout.resolve({});
      firstReleased = true;
      await Promise.resolve();
      expect(navigateMock).not.toHaveBeenCalled();
      await waitFor(() => expect(logoutCalls).toBe(2));
      expect(refreshCalls).toBe(0);

      secondLogout.resolve({});
      secondReleased = true;
      await waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
      await expect(queuedPreLogoutOutcome).resolves.toMatchObject({
        status: "fulfilled",
        value: null,
      });
      await expect(publicOutcomes[0]).resolves.toMatchObject({ status: "rejected" });
      await expect(publicOutcomes[1]).resolves.toMatchObject({ status: "fulfilled" });
      expect(refreshCalls).toBe(0);

      // Publish a genuinely new principal/session after logout settlement so
      // the next refresh captures a fresh generation and may dispatch.
      useAuthStore.setState({
        user: {
          id: 2,
          username: "replacement-user",
          full_name: "Replacement User",
          role: "admin",
          is_active: true,
        },
        accessToken: "replacement-session-token",
        isAuthenticated: true,
        authMode: "jwt",
      });
      await Promise.resolve();
      const freshRefresh = useAuthStore.getState().refreshToken();
      const freshOutcome = freshRefresh.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (reason) => ({ status: "rejected" as const, reason }),
      );
      outcomes.push(freshOutcome);
      await waitFor(() => expect(refreshCalls).toBe(1));
      await expect(freshOutcome).resolves.toMatchObject({
        status: "fulfilled",
        value: "replacement-token",
      });
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/csrf-token"))).toBe(true);
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/auth/refresh"))).toBe(true);
    } finally {
      if (!firstReleased) {
        firstLogout.resolve({});
      }
      if (!secondReleased) {
        secondLogout.resolve({});
      }
      await Promise.all([...publicOutcomes, ...outcomes]);
      useAuthStore.setState({ logout: originalLogout });
    }
  });
});

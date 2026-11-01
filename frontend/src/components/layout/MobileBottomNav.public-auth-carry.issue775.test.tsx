import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MobileBottomNav } from "./MobileBottomNav";

const { mockLogout, navigateMock } = vi.hoisted(() => ({
  mockLogout: vi.fn(),
  navigateMock: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(
    (selector: (state: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
      selector({ user: { role: "admin" }, logout: mockLogout }),
  ),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigateMock,
}));

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

function captureReactPropHandler(element: Element, prop: string): (...args: unknown[]) => unknown {
  const carrier = element as unknown as Record<string, unknown>;
  const propsKey = Object.keys(carrier).find((key) => key.startsWith("__reactProps$"));
  const props = propsKey ? (carrier[propsKey] as Record<string, unknown>) : undefined;
  const handler = props?.[prop];
  if (typeof handler !== "function") {
    throw new Error(`Missing React prop handler ${prop}`);
  }
  return handler as (...args: unknown[]) => unknown;
}

function renderNav() {
  return render(
    <MemoryRouter>
      <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
    </MemoryRouter>,
  );
}

describe("public MobileBottomNav logout carry contracts for issue #775", () => {
  beforeEach(() => {
    mockLogout.mockReset();
    navigateMock.mockReset();
    mockLogout.mockResolvedValue(undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("calls the public zero-argument logout and navigates once after current success", async () => {
    renderNav();
    fireEvent.click(screen.getByLabelText("More navigation options"));
    fireEvent.click(screen.getByLabelText("Log out"));

    await waitFor(() => expect(mockLogout).toHaveBeenCalledTimes(1));
    expect(mockLogout).toHaveBeenCalledWith();
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/login", { replace: true }));
    expect(navigateMock).toHaveBeenCalledTimes(1);
  });

  it("does not navigate after the public caller unmounts before logout settles", async () => {
    const pendingLogout = deferred<void>();
    mockLogout.mockReturnValue(pendingLogout.promise);
    const { unmount } = renderNav();
    fireEvent.click(screen.getByLabelText("More navigation options"));
    fireEvent.click(screen.getByLabelText("Log out"));
    await waitFor(() => expect(mockLogout).toHaveBeenCalledTimes(1));

    unmount();
    pendingLogout.resolve();
    await Promise.resolve();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("contains a rejected public logout at the caller boundary without navigation", async () => {
    const logoutError = new Error("logout deadline");
    mockLogout.mockImplementationOnce(() => Promise.reject(logoutError));
    renderNav();
    fireEvent.click(screen.getByLabelText("More navigation options"));

    const logoutButton = screen.getByLabelText("Log out");
    const handleLogout = captureReactPropHandler(logoutButton, "onClick");
    let completion!: Promise<{ status: "fulfilled" } | { status: "rejected"; reason: unknown }>;

    await act(async () => {
      completion = Promise.resolve(handleLogout()).then(
        () => ({ status: "fulfilled" as const }),
        (reason) => ({ status: "rejected" as const, reason }),
      );
      await completion;
    });

    await expect(completion).resolves.toEqual({ status: "fulfilled" });
    expect(mockLogout).toHaveBeenCalledWith();
    expect(navigateMock).not.toHaveBeenCalled();
  });
});

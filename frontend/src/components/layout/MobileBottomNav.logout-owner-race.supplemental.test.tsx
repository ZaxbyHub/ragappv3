import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MobileBottomNav } from "./MobileBottomNav";

const harness = vi.hoisted(() => ({
  logout: vi.fn<() => Promise<void>>(),
  navigate: vi.fn(),
  owner: {} as object,
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (state: { user: { role: string }; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: harness.logout }),
  ),
}));

vi.mock("@/lib/api/auth-lifecycle", () => ({
  captureAuthOwner: () => harness.owner,
  isCurrentAuthOwner: (owner: object) => owner === harness.owner,
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => harness.navigate };
});

function deferred() {
  let resolve!: () => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function renderNav() {
  return render(
    <MemoryRouter>
      <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
    </MemoryRouter>,
  );
}

function clickLogout() {
  fireEvent.click(screen.getByLabelText("More navigation options"));
  fireEvent.click(screen.getByLabelText("Log out"));
}

describe("MobileBottomNav logout completion ownership", () => {
  beforeEach(() => {
    harness.logout.mockReset();
    harness.navigate.mockReset();
    harness.owner = {};
  });

  it("does not navigate when an earlier reaction replaces the owner before its completion callback", async () => {
    const logoutOwner = {};
    const replacementOwner = {};
    harness.logout.mockImplementation(() => {
      harness.owner = logoutOwner;
      const operation = Promise.resolve();
      // This reaction is enrolled by logout before the component attaches .then().
      void operation.then(() => { harness.owner = replacementOwner; });
      return operation;
    });

    renderNav();
    clickLogout();

    await waitFor(() => expect(harness.owner).toBe(replacementOwner));
    expect(harness.navigate).not.toHaveBeenCalled();
  });

  it("navigates after a current logout succeeds", async () => {
    const logoutOwner = {};
    harness.logout.mockImplementation(() => {
      harness.owner = logoutOwner;
      return Promise.resolve();
    });

    renderNav();
    clickLogout();

    await waitFor(() => expect(harness.navigate).toHaveBeenCalledWith("/login", { replace: true }));
  });

  it("does not navigate after unmount, even when the operation later succeeds", async () => {
    const operation = deferred();
    harness.logout.mockImplementation(() => operation.promise);
    const view = renderNav();

    clickLogout();
    view.unmount();
    await act(async () => { operation.resolve(); await operation.promise; });

    expect(harness.navigate).not.toHaveBeenCalled();
  });

  it("consumes a rejected logout without navigating", async () => {
    harness.logout.mockReturnValue(Promise.reject(new Error("stale owner")));
    renderNav();

    clickLogout();

    await waitFor(() => expect(harness.logout).toHaveBeenCalledOnce());
    await act(async () => { await Promise.resolve(); });
    expect(harness.navigate).not.toHaveBeenCalled();
  });
});

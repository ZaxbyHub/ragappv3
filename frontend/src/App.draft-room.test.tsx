import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Mirrors the mocking convention in App.subpath.test.tsx: shell-level
// dependencies and every routed page are stubbed to a trivial marker so this
// file tests ONLY the routing wiring in App.tsx (route registration, active
// item resolution), not any page's internal behavior.

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: { init: () => Promise<void> }) => unknown) =>
    selector({ init: vi.fn().mockResolvedValue(undefined) }),
}));

vi.mock("@/components/auth/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({ backend: true, embeddings: true, chat: true, loading: false }),
}));

// The routing decision (render vs redirect) must live entirely in App.tsx, not
// in a capability read. Stubbing the hook to report "disabled" here means: if
// a future change ever wraps the /draft-room route in a
// `useDraftRoomVisible() ? <DraftRoomPage/> : <Navigate .../>` gate, this file
// starts failing because the mocked page would stop rendering.
vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => false,
}));

// Capture the activeItem PageShell receives so route->nav-highlight mapping
// (getActiveItemFromPath in App.tsx) is verifiable without rendering the real
// NavigationRail/MobileBottomNav (covered by their own dedicated test files).
vi.mock("@/components/layout/PageShell", () => ({
  PageShell: ({
    children,
    activeItem,
  }: {
    children: React.ReactNode;
    activeItem: string;
  }) => (
    <div>
      <div data-testid="active-item">{activeItem}</div>
      {children}
    </div>
  ),
}));

vi.mock("@/pages/ChatShell", () => ({ default: () => <div>Chat Page</div> }));
vi.mock("@/pages/DocumentsPage", () => ({ default: () => <div>Documents Page</div> }));
vi.mock("@/pages/DocumentDetailPage", () => ({ default: () => <div>Document Detail Page</div> }));
vi.mock("@/pages/MemoryPage", () => ({ default: () => <div>Memory Page</div> }));
vi.mock("@/pages/VaultsPage", () => ({ default: () => <div>Vaults Page</div> }));
vi.mock("@/pages/SettingsPage", () => ({ default: () => <div>Settings Page</div> }));
vi.mock("@/pages/LoginPage", () => ({ default: () => <div>Login Page</div> }));
vi.mock("@/pages/SetupPage", () => ({ default: () => <div>Setup Page</div> }));
vi.mock("@/pages/RegisterPage", () => ({ default: () => <div>Register Page</div> }));
vi.mock("@/pages/AdminUsersPage", () => ({ default: () => <div>Admin Users Page</div> }));
vi.mock("@/pages/AdminGroupsPage", () => ({ default: () => <div>Admin Groups Page</div> }));
vi.mock("@/pages/OrgsPage", () => ({ default: () => <div>Organizations Page</div> }));
vi.mock("@/pages/ProfilePage", () => ({ default: () => <div>Profile Page</div> }));
vi.mock("@/pages/ChangePasswordRequiredPage", () => ({ default: () => <div>Change Password Page</div> }));
vi.mock("@/pages/NotFoundPage", () => ({ default: () => <div>Not Found Page</div> }));
vi.mock("@/pages/WikiPage", () => ({ default: () => <div>Wiki Page</div> }));
vi.mock("@/pages/KMSPage", () => ({ default: () => <div>KMS Page</div> }));
vi.mock("@/pages/KMSDetailPage", () => ({ default: () => <div>KMS Detail Page</div> }));
vi.mock("@/pages/DraftRoomPage", () => ({ default: () => <div>Draft Room Page</div> }));
vi.mock("@/pages/DraftRoomDetailPage", () => ({ default: () => <div>Draft Room Detail Page</div> }));

async function renderAppAt(path: string) {
  window.history.pushState({}, "", path);
  const { default: App } = await import("./App");
  return render(<App />);
}

describe("App draft room routing", () => {
  it("resolves /draft-room to DraftRoomPage, not NotFound", async () => {
    await renderAppAt("/draft-room");

    expect(await screen.findByText("Draft Room Page")).toBeInTheDocument();
    expect(screen.queryByText("Not Found Page")).not.toBeInTheDocument();
  });

  it("resolves /draft-room/:draftId to DraftRoomDetailPage, not NotFound", async () => {
    await renderAppAt("/draft-room/123");

    expect(await screen.findByText("Draft Room Detail Page")).toBeInTheDocument();
    expect(screen.queryByText("Not Found Page")).not.toBeInTheDocument();
  });

  it("renders /draft-room directly (not a redirect) even while the capability hook reports disabled", async () => {
    // useDraftRoomVisible() is mocked to return false for this whole file
    // (see top-of-file mock). The route must still render the page itself —
    // never redirect to /documents and never 404.
    await renderAppAt("/draft-room");

    expect(await screen.findByText("Draft Room Page")).toBeInTheDocument();
    expect(screen.queryByText("Documents Page")).not.toBeInTheDocument();
    expect(screen.queryByText("Not Found Page")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/draft-room");
  });

  it("resolves the active nav item to draftRoom for /draft-room", async () => {
    await renderAppAt("/draft-room");

    await screen.findByText("Draft Room Page");
    expect(screen.getByTestId("active-item")).toHaveTextContent("draftRoom");
  });

  it("resolves the active nav item to draftRoom for /draft-room/:draftId", async () => {
    await renderAppAt("/draft-room/123");

    await screen.findByText("Draft Room Detail Page");
    expect(screen.getByTestId("active-item")).toHaveTextContent("draftRoom");
  });

  it("still resolves existing routes unaffected by the new registration", async () => {
    await renderAppAt("/documents");

    expect(await screen.findByText("Documents Page")).toBeInTheDocument();
    expect(screen.getByTestId("active-item")).toHaveTextContent("documents");
  });
});

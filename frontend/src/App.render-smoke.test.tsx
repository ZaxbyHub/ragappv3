import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Issue #657 route-resolution smoke: proves the route table maps /draft-room,
// /documents, and an unknown path to the right pages WITHOUT exercising the
// heavy full-App render path that the integration specs
// (App.subpath.test.tsx, App.draft-room.test.tsx) own. Every routed page and
// every shell dependency is stubbed to a trivial marker, so a routing
// regression fails this file in well under a second at any host speed instead
// of surfacing as a load-sensitive timeout.

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: { init: () => Promise<void> }) => unknown) =>
    selector({ init: vi.fn().mockResolvedValue(undefined) }),
}));

vi.mock("@/components/auth/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/components/layout/PageShell", () => ({
  PageShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("@/components/shared/CommandPalette", () => ({ CommandPalette: () => null }));
vi.mock("@/components/ReconnectingBanner", () => ({ default: () => null }));

vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({ status: "healthy" }),
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
vi.mock("@/pages/SearchPage", () => ({ default: () => <div>Search Page</div> }));
vi.mock("@/pages/DraftRoomPage", () => ({ default: () => <div>Draft Room Page</div> }));
vi.mock("@/pages/DraftRoomDetailPage", () => ({ default: () => <div>Draft Room Detail Page</div> }));
vi.mock("@/components/canvas/CanvasPage", () => ({ default: () => <div>Canvas Page</div> }));

async function renderAppAt(path: string) {
  window.history.pushState({}, "", path);
  const { default: App } = await import("./App");
  return render(<App />);
}

describe("App route resolution smoke", { timeout: 30_000 }, () => {
  it("maps /draft-room to DraftRoomPage", async () => {
    await renderAppAt("/draft-room");

    expect(await screen.findByText("Draft Room Page")).toBeInTheDocument();
    expect(screen.queryByText("Not Found Page")).not.toBeInTheDocument();
  });

  it("maps /documents to DocumentsPage", async () => {
    await renderAppAt("/documents");

    expect(await screen.findByText("Documents Page")).toBeInTheDocument();
    expect(screen.queryByText("Not Found Page")).not.toBeInTheDocument();
  });

  it("maps an unknown path to NotFoundPage (never a silent documents fallback)", async () => {
    await renderAppAt("/definitely-not-a-route");

    expect(await screen.findByText("Not Found Page")).toBeInTheDocument();
    expect(screen.queryByText("Documents Page")).not.toBeInTheDocument();
  });
});

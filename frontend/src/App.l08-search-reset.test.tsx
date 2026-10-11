import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Issue #779 feedback round (F-7/PRR-011): POSITIVE CONTROL for the
// /search active-item contract — the frozen AC1 check only asserts
// "not documents"; this pins the actual null (an empty capture), so a
// mapper returning any other nav id fails. (The query-only RESET test
// lives in App.l08-query-reset.test.tsx — this file's PageShell capture
// mock has no route boundary, so recovery cannot be exercised here.)

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

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => false,
}));

vi.mock("@/components/layout/PageShell", () => ({
  PageShell: ({
    children,
    activeItem,
  }: {
    children: React.ReactNode;
    activeItem: string | null;
  }) => (
    <div>
      <div data-testid="active-item">{activeItem === null ? "" : activeItem}</div>
      {children}
    </div>
  ),
}));

vi.mock("@/pages/SearchPage", () => ({
  default: () => <div>Search Page</div>,
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
vi.mock("@/components/canvas/CanvasPage", () => ({ default: () => <div>Canvas Page</div> }));

async function renderAppAt(path: string) {
  window.history.pushState({}, "", path);
  const { default: App } = await import("./App");
  return render(<App />);
}

describe("App /search active-item positive control (issue #779 feedback)", { timeout: 30_000 }, () => {
  it("search route passes a NEUTRAL active item (empty capture, not just not-documents)", async () => {
    await renderAppAt("/search?q=probe");

    expect(await screen.findByText("Search Page")).toBeInTheDocument();
    expect(screen.getByTestId("active-item")).toHaveTextContent("");
  });
});

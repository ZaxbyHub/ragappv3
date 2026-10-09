// Issue #775 AC4–AC5/AC7: exercise the real app shell and CommandPalette
// navigation/search wiring. Routed pages are marker stubs only for these
// navigation checks; the real palette and router remain mounted.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within, cleanup } from "@testing-library/react";

const paletteHarness = vi.hoisted(() => ({
  role: "admin",
  draftRoomVisible: true,
  unifiedSearch: vi.fn(),
}));

vi.mock("@/lib/api/onboarding", () => ({
  getOnboardingMilestones: vi.fn().mockResolvedValue({
    vault_created: true,
    upload_indexed: true,
    first_question_asked: true,
    first_citation_opened: true,
    show_checklist: false,
  }),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      user: { id: 1, username: "alice", role: paletteHarness.role },
      logout: vi.fn(),
      init: vi.fn().mockResolvedValue(undefined),
    }),
}));

vi.mock("@/components/auth/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({ backend: true, embeddings: true, chat: true, loading: false }),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => paletteHarness.draftRoomVisible,
}));

vi.mock("@/lib/api/search", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/search")>();
  return { ...actual, unifiedSearch: paletteHarness.unifiedSearch };
});

// Keep the shell and router real while isolating unrelated page internals.
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
vi.mock("@/pages/SearchPage", () => ({ default: () => <div>Search Page</div> }));

async function openPalette() {
  fireEvent.keyDown(document.body, { key: "k", code: "KeyK", ctrlKey: true });
  return waitFor(() => {
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    expect(within(dialog).getByRole("textbox", { name: "Search commands" })).toBeVisible();
    return dialog;
  });
}

describe("#775 command palette", { timeout: 15_000 }, () => {
  beforeAll(async () => {
    await import("../App");
  }, 15_000);

  beforeEach(() => {
    vi.clearAllMocks();
    paletteHarness.role = "admin";
    paletteHarness.draftRoomVisible = true;
    paletteHarness.unifiedSearch.mockResolvedValue({ results: [] });
    window.history.pushState({}, "", "/vaults");
  });

  afterEach(() => {
    cleanup();
  });

  it("admin commands reach every NavigationRail destination", async () => {
    const { default: App } = await import("../App");
    render(<App />);
    expect(await screen.findByText("Vaults Page")).toBeInTheDocument();

    const destinations = [
      ["Chat", "/chat"],
      ["Documents", "/documents"],
      ["Memory", "/memory"],
      ["Wiki", "/wiki"],
      ["KMS", "/kms"],
      ["Draft Room", "/draft-room"],
      ["Vaults", "/vaults"],
      ["Groups", "/admin/groups"],
      ["Users", "/admin/users"],
      ["Organizations", "/admin/organizations"],
      ["Settings", "/settings"],
      ["Profile", "/profile"],
    ] as const;

    for (const [label, path] of destinations) {
      const palette = await openPalette();
      fireEvent.click(within(palette).getByRole("button", { name: `Go to ${label}` }));
      await waitFor(() => expect(window.location.pathname).toBe(path));
      await waitFor(() => expect(screen.queryByRole("textbox", { name: "Search commands" })).not.toBeInTheDocument());
      const destinationMarker = label === "Groups" ? "Admin Groups Page" : label === "Users" ? "Admin Users Page" : `${label} Page`;
      await waitFor(() => expect(screen.getByText(destinationMarker)).toBeVisible());
    }
  });

  it("member commands expose exactly the nine non-admin destinations", async () => {
    paletteHarness.role = "member";
    const { default: App } = await import("../App");
    render(<App />);
    expect(await screen.findByText("Vaults Page")).toBeInTheDocument();

    const palette = await openPalette();
    const navigationLabels = [
      "Chat",
      "Documents",
      "Memory",
      "Wiki",
      "KMS",
      "Draft Room",
      "Vaults",
      "Settings",
      "Profile",
    ];
    for (const label of navigationLabels) {
      expect(within(palette).getByRole("button", { name: `Go to ${label}` })).toBeInTheDocument();
    }
    for (const label of ["Groups", "Users", "Organizations"]) {
      expect(within(palette).queryByRole("button", { name: `Go to ${label}` })).toBeNull();
    }
    expect(within(palette).getAllByRole("button", { name: /^Go to / })).toHaveLength(9);
  });

  it("capability-off members lose only Draft Room, while superadmins retain admin controls", async () => {
    paletteHarness.role = "member";
    paletteHarness.draftRoomVisible = false;
    const { default: App } = await import("../App");
    render(<App />);
    expect(await screen.findByText("Vaults Page")).toBeInTheDocument();
    const memberPalette = await openPalette();
    expect(within(memberPalette).queryByRole("button", { name: "Go to Draft Room" })).toBeNull();
    expect(within(memberPalette).getAllByRole("button", { name: /^Go to / })).toHaveLength(8);
    cleanup();

    paletteHarness.role = "superadmin";
    paletteHarness.draftRoomVisible = true;
    window.history.pushState({}, "", "/vaults");
    render(<App />);
    expect(await screen.findByText("Vaults Page")).toBeInTheDocument();
    const superadminPalette = await openPalette();
    expect(within(superadminPalette).getAllByRole("button", { name: /^Go to / })).toHaveLength(12);
    for (const label of ["Groups", "Users", "Organizations"]) {
      expect(within(superadminPalette).getByRole("button", { name: `Go to ${label}` })).toBeInTheDocument();
    }
  });

  it("queries the global search API for entity hits", async () => {
    paletteHarness.unifiedSearch.mockResolvedValue({
      results: [
        {
          type: "document",
          id: 42,
          title: "Alpha handbook",
          snippet: "Searchable entity result",
          vault_id: 1,
          url_hint: "/documents/42",
          score: 1,
        },
      ],
    });
    const { default: App } = await import("../App");
    render(<App />);
    expect(await screen.findByText("Vaults Page")).toBeInTheDocument();

    const palette = await openPalette();
    fireEvent.change(within(palette).getByRole("textbox", { name: "Search commands" }), {
      target: { value: "alpha" },
    });
    await vi.waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "alpha" }))
    );
    const result = await within(palette).findByText("Alpha handbook");
    fireEvent.click(result);
    await waitFor(() => expect(window.location.pathname).toBe("/documents/42"));
  });
});

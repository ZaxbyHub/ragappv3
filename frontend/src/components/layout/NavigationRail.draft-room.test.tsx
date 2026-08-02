import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter, BrowserRouter } from "react-router-dom";
import { NavigationRail } from "./NavigationRail";
import { useAuthStore } from "@/stores/useAuthStore";

// Draft Room nav-gating tests, split out from NavigationRail.test.tsx so the
// capability mock (module boundary: @/hooks/useDraftRoomCapabilities) does
// not have to coexist with that file's setup.
//
// NavigationRail only ever consumes the derived `useDraftRoomVisible()`
// boolean — it never reads capability fields itself. The derivation from
// loading/error/`enabled`/`editorial_gates_installed`/`ready_available` into
// that boolean is unit-tested in useDraftRoomCapabilities.test.tsx (owned by
// W-HOOKS). Each scenario below sets the mock to the value
// `useDraftRoomVisible` would actually produce for that state, so this file
// proves NavigationRail's own wiring reacts correctly to each one.

const mockLogout = vi.hoisted(() => vi.fn());
const mockUseDraftRoomVisible = vi.hoisted(() => vi.fn());

vi.mock("@/stores/useThemeStore", () => ({
  useThemeStore: vi.fn(() => ({
    theme: "dark",
    setTheme: vi.fn(),
  })),
  applyTheme: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: mockUseDraftRoomVisible,
}));

const mockHealthStatus = {
  backend: true,
  embeddings: true,
  chat: true,
  loading: false,
  lastChecked: Date.now(),
};

describe("NavigationRail Draft Room gating", () => {
  beforeEach(() => {
    mockLogout.mockResolvedValue(undefined);
    mockLogout.mockClear();
    mockUseDraftRoomVisible.mockReset();
    vi.mocked(useAuthStore).mockImplementation(
      (selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
        selector({ user: { role: "admin" }, logout: mockLogout })
    );
  });

  it("is absent when the capability reports disabled (enabled: false)", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is absent while the capabilities query is loading", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is absent on a capabilities query error", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is absent when editorial_gates_installed is false even though enabled is true", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is present when enabled, editorial_gates_installed and ready_available all hold", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.getByLabelText("Draft Room")).toBeInTheDocument();
  });

  it("navigates to /draft-room when clicked", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    render(
      <BrowserRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </BrowserRouter>
    );
    expect(screen.getByLabelText("Draft Room")).toHaveAttribute("href", "/draft-room");
  });

  it("highlights the Draft Room item as active on /draft-room", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    render(
      <MemoryRouter initialEntries={["/draft-room"]}>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.getByLabelText("Draft Room")).toHaveClass("bg-accent");
  });

  it("highlights the Draft Room item as active on /draft-room/:draftId", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    render(
      <MemoryRouter initialEntries={["/draft-room/123"]}>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.getByLabelText("Draft Room")).toHaveClass("bg-accent");
  });

  it("does not affect other nav items when the capability is disabled", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.getByLabelText("Chat")).toBeInTheDocument();
    expect(screen.getByLabelText("Documents")).toBeInTheDocument();
    expect(screen.getByLabelText("KMS")).toBeInTheDocument();
    expect(screen.getByLabelText("Vaults")).toBeInTheDocument();
  });

  it("still hides adminOnly items for a non-admin user, independent of the capability gate", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    // mockImplementation (not -Once): the component reads useAuthStore twice
    // per render (role, then logout) and re-renders across interactions, so a
    // single-shot override would silently revert to "admin" after the first call.
    vi.mocked(useAuthStore).mockImplementation(
      (selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
        selector({ user: { role: "user" }, logout: mockLogout })
    );
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    expect(screen.getByLabelText("Draft Room")).toBeInTheDocument();
    expect(screen.queryByLabelText("Groups")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Users")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Organizations")).not.toBeInTheDocument();
  });

  it("logout still works with the Draft Room item present", async () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    render(
      <MemoryRouter>
        <NavigationRail healthStatus={mockHealthStatus} />
      </MemoryRouter>
    );
    fireEvent.click(screen.getByLabelText("Log out"));
    await waitFor(() => expect(mockLogout).toHaveBeenCalledTimes(1));
  });
});

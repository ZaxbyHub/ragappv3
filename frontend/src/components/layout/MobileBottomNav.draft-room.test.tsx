import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MobileBottomNav } from "./MobileBottomNav";
import { useAuthStore } from "@/stores/useAuthStore";

// Draft Room nav-gating tests for the mobile "More" sheet, split out from
// MobileBottomNav.test.tsx. See NavigationRail.draft-room.test.tsx for why the
// derived `useDraftRoomVisible()` boolean (not the raw capability shape) is
// the correct mock boundary for a nav-consumer test.

const mockLogout = vi.hoisted(() => vi.fn());
const mockUseDraftRoomVisible = vi.hoisted(() => vi.fn());

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: mockUseDraftRoomVisible,
}));

function openMoreSheet() {
  fireEvent.click(screen.getByLabelText("More navigation options"));
}

describe("MobileBottomNav Draft Room gating", () => {
  beforeEach(() => {
    mockLogout.mockResolvedValue(undefined);
    mockLogout.mockClear();
    mockUseDraftRoomVisible.mockReset();
    vi.mocked(useAuthStore).mockImplementation(
      (selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
        selector({ user: { role: "admin" }, logout: mockLogout })
    );
  });

  it("is absent from the More sheet when the capability reports disabled", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is absent while the capabilities query is loading", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is absent on a capabilities query error", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is absent when editorial_gates_installed is false even though enabled is true", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    expect(screen.queryByLabelText("Draft Room")).not.toBeInTheDocument();
  });

  it("is present in the More sheet when all capability conditions hold", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    expect(screen.getByLabelText("Draft Room")).toBeInTheDocument();
  });

  it("calls onItemSelect(draftRoom) and closes the sheet when clicked", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    const onItemSelect = vi.fn();
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={onItemSelect} />
      </MemoryRouter>
    );
    openMoreSheet();
    fireEvent.click(screen.getByLabelText("Draft Room"));
    expect(onItemSelect).toHaveBeenCalledWith("draftRoom");
  });

  it("does not affect other More-sheet items when the capability is disabled", () => {
    mockUseDraftRoomVisible.mockReturnValue(false);
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    expect(screen.getByLabelText("Wiki")).toBeInTheDocument();
    expect(screen.getByLabelText("KMS")).toBeInTheDocument();
    expect(screen.getByLabelText("Vaults")).toBeInTheDocument();
    expect(screen.getByLabelText("Settings")).toBeInTheDocument();
  });

  it("still hides adminOnly items for a non-admin user, independent of the capability gate", () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    vi.mocked(useAuthStore).mockImplementation(
      (selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
        selector({ user: { role: "user" }, logout: mockLogout })
    );
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    expect(screen.getByLabelText("Draft Room")).toBeInTheDocument();
    expect(screen.queryByLabelText("Groups")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Users")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Orgs")).not.toBeInTheDocument();
  });

  it("logout still works in the More sheet with Draft Room present", async () => {
    mockUseDraftRoomVisible.mockReturnValue(true);
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );
    openMoreSheet();
    fireEvent.click(screen.getByLabelText("Log out"));
    expect(mockLogout).toHaveBeenCalled();
  });
});

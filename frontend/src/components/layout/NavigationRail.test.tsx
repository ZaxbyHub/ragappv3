import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter, BrowserRouter } from "react-router-dom";
import { NavigationRail } from "./NavigationRail";
import type { NavItemId } from "./navigationTypes";

const mockLogout = vi.hoisted(() => vi.fn());

// Under vitest, the named icons from "@hugeicons/core-free-icons" resolve to
// undefined, so the real HugeiconsIcon throws "currentIcon is not iterable" and
// crashes every render. Mock the renderer to a harmless stub so the rebranded
// component mounts. Identity of the SVG is not assertable; tests assert on the
// stable aria-labels instead.
vi.mock("@hugeicons/react", () => ({
  HugeiconsIcon: () => null,
  IconSvgElement: undefined,
}));

// Mock useThemeStore before importing NavigationRail (NavigationRail imports useThemeStore)
vi.mock("@/stores/useThemeStore", () => ({
  useThemeStore: vi.fn(() => ({
    theme: "dark",
    setTheme: vi.fn(),
  })),
  applyTheme: vi.fn(),
}));

// Mock useAuthStore — default to admin so all nav items are visible
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

// Mock health status
const mockHealthStatus = {
  backend: true,
  embeddings: true,
  chat: true,
  loading: false,
  lastChecked: Date.now(),
};

describe("NavigationRail", () => {
  beforeEach(() => {
    mockLogout.mockResolvedValue(undefined);
    mockLogout.mockClear();
  });

  describe("Navigation Items", () => {
    it("renders all navigation items", () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      // Check for all expected nav items visible to an admin user.
      expect(screen.getByLabelText("Chat")).toBeInTheDocument();
      expect(screen.getByLabelText("Documents")).toBeInTheDocument();
      expect(screen.getByLabelText("Memory")).toBeInTheDocument();
      expect(screen.getByLabelText("Wiki")).toBeInTheDocument();
      expect(screen.getByLabelText("KMS")).toBeInTheDocument();
      expect(screen.getByLabelText("Vaults")).toBeInTheDocument();
      expect(screen.getByLabelText("Settings")).toBeInTheDocument();
      expect(screen.getByLabelText("Groups")).toBeInTheDocument();
      expect(screen.getByLabelText("Users")).toBeInTheDocument();
      expect(screen.getByLabelText("Organizations")).toBeInTheDocument();
      expect(screen.getByLabelText("Profile")).toBeInTheDocument();
    });

    it("does not render chatNew item", () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      // chatNew should not exist
      expect(screen.queryByLabelText("Chat (New)")).not.toBeInTheDocument();
      expect(screen.queryByText("NEW")).not.toBeInTheDocument();
    });
  });

  describe("Active State", () => {
    it("highlights active item with primary background", () => {
      render(
        <MemoryRouter initialEntries={["/chat"]}>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      const chatButton = screen.getByLabelText("Chat");
      expect(chatButton).toHaveClass("bg-accent");
    });

    it("shows active indicator for active item", () => {
      render(
        <MemoryRouter initialEntries={["/documents"]}>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      const documentsButton = screen.getByLabelText("Documents");
      const activeIndicator = documentsButton.querySelector("span");
      expect(activeIndicator).toBeInTheDocument();
    });
  });

  describe("Interactions", () => {
    it("navigates to correct route when clicking navigation items", () => {
      render(
        <BrowserRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </BrowserRouter>
      );

      const documentsButton = screen.getByLabelText("Documents");
      expect(documentsButton).toHaveAttribute("href", "/documents");
    });

    it("navigates to /admin/users for users item", () => {
      render(
        <BrowserRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </BrowserRouter>
      );

      const usersButton = screen.getByLabelText("Users");
      expect(usersButton).toHaveAttribute("href", "/admin/users");
    });

    it("logs out from the rail action", async () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      fireEvent.click(screen.getByLabelText("Log out"));

      await waitFor(() => expect(mockLogout).toHaveBeenCalledTimes(1));
    });
  });

  describe("Health Status Footer", () => {
    it("renders health status indicators", () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      // Check for health status labels in the footer
      expect(screen.getByText("API")).toBeInTheDocument();
      expect(screen.getByText("Embeddings")).toBeInTheDocument();
      // Use getAllByText since "Chat" appears in both nav and health status
      const chatLabels = screen.getAllByText("Chat");
      expect(chatLabels.length).toBeGreaterThan(1);

      // Check green indicators (healthy) - uses bg-success class
      const footerContainer = screen.getByText("API").closest("div");
      const greenIndicators = footerContainer?.querySelectorAll(".bg-success");
      expect(greenIndicators?.length).toBeGreaterThan(0);
    });

    it("shows loading state when health check is in progress", () => {
      const loadingHealthStatus = {
        backend: false,
        embeddings: false,
        chat: false,
        loading: true,
      };

      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={loadingHealthStatus}
          />
        </MemoryRouter>
      );

      // Check for "Checking" text when loading (appears 3 times for each service)
      const checkingElements = screen.getAllByText("Checking");
      expect(checkingElements.length).toBe(3);
    });
  });

  describe("Theme Toggle", () => {
    it("renders theme toggle button", () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      expect(screen.getByLabelText(/switch to .* mode/i)).toBeInTheDocument();
    });

    it("shows sun icon in dark mode", () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      // Default theme is dark, should show sun icon with aria-label "Switch to light mode"
      const sunIcon = screen.getByLabelText("Switch to light mode");
      expect(sunIcon).toBeInTheDocument();
    });
  });
});

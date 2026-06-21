import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter, BrowserRouter } from "react-router-dom";
import { NavigationRail } from "./NavigationRail";
import { useThemeStore } from "@/stores/useThemeStore";
import type { NavItemId } from "./navigationTypes";

const mockLogout = vi.hoisted(() => vi.fn());

// NOTE: We intentionally do NOT mock "@hugeicons/react". The real HugeiconsIcon
// spreads its `icon` prop (`[...icon]`), so it throws "currentIcon is not
// iterable" if a non-array (e.g. a lucide forwardRef component such as the KMS
// "Library" icon) is ever routed to it. Rendering the real component here is a
// regression guard for that production crash (every authenticated page mounts
// this rail). See the Array.isArray discriminator in NavRow.

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

    // Regression: the KMS nav item uses a lucide icon (`Library`), which is a
    // forwardRef object, not an array. With the real HugeiconsIcon mounted,
    // routing it through HugeiconsIcon would throw "currentIcon is not iterable"
    // and crash every authenticated page. The KMS row must render a real icon.
    it("renders the lucide-iconed KMS item without crashing (regression)", () => {
      render(
        <MemoryRouter>
          <NavigationRail healthStatus={mockHealthStatus} />
        </MemoryRouter>
      );

      const kmsLink = screen.getByLabelText("KMS");
      expect(kmsLink).toBeInTheDocument();
      // The lucide icon renders as an inline <svg>, proving it was NOT misrouted
      // into HugeiconsIcon (which would have thrown before rendering anything).
      expect(kmsLink.querySelector("svg")).toBeInTheDocument();
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

  describe("Theme Selector", () => {
    // Behavior change: the binary light/dark toggle (aria-label "Switch to … mode")
    // was replaced by a 4-option theme dropdown (Light / Dark / System / High
    // contrast) so high-contrast is selectable. The trigger now carries a stable
    // aria-label "Select theme" instead of describing the next toggle target.
    it("renders the theme selector trigger", () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      expect(screen.getByLabelText("Select theme")).toBeInTheDocument();
    });

    it("renders an icon in the trigger reflecting the active theme", () => {
      render(
        <MemoryRouter>
          <NavigationRail
            healthStatus={mockHealthStatus}
          />
        </MemoryRouter>
      );

      // Mocked theme is "dark"; the trigger renders an inline icon svg.
      const trigger = screen.getByLabelText("Select theme");
      expect(trigger.querySelector("svg")).toBeInTheDocument();
    });

    // Regression (F-003): in "system" mode the trigger must reflect the RESOLVED
    // appearance. With the OS preferring dark, the rail previously kept the light
    // (Sun) icon because it only checked `theme === "dark"`.
    it("reflects the dark OS preference in system mode (F-003)", () => {
      vi.mocked(useThemeStore).mockReturnValueOnce({
        theme: "system",
        setTheme: vi.fn(),
      });
      const originalMatchMedia = window.matchMedia;
      window.matchMedia = vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }) as unknown as typeof window.matchMedia;

      try {
        render(
          <MemoryRouter>
            <NavigationRail healthStatus={mockHealthStatus} />
          </MemoryRouter>
        );
        const trigger = screen.getByLabelText("Select theme");
        expect(trigger).toHaveAttribute("data-resolved-appearance", "dark");
      } finally {
        window.matchMedia = originalMatchMedia;
      }
    });
  });
});

// frontend/src/components/layout/NavigationRail.a11y-toggle-name.test.tsx
// Issue #1159 AC2 (frozen acceptance check C2) — the sidebar expand/collapse
// toggle's accessible name names the action it performs in its CURRENT state:
// when the rail is expanded, the control announces a collapse action.
//
// At base the button carried a static aria-label="Expand sidebar" while the
// icon flipped with expansion state.
//
// Harness: the NavigationRail.l05.test.tsx mock set (useThemeStore,
// useAuthStore admin selector, useDraftRoomCapabilities stub, capturing
// ScrollArea stub, matchMedia stub, MemoryRouter). Real clicks drive the
// component's own state machine; assertions read the rendered accessible
// name and aria-expanded.

import type { ReactNode } from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { NavigationRail } from "./NavigationRail";

const { mockLogout } = vi.hoisted(() => ({ mockLogout: vi.fn() }));

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: (props: { children?: ReactNode }) => <div>{props.children}</div>,
}));

vi.mock("@/stores/useThemeStore", () => ({
  useThemeStore: vi.fn(() => ({ theme: "dark", setTheme: vi.fn() })),
  applyTheme: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

const mockHealthStatus = {
  backend: true,
  embeddings: true,
  chat: true,
  loading: false,
  lastChecked: Date.now(),
};

function stubMatchMedia() {
  const originalMatchMedia = window.matchMedia;
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }) as unknown as typeof window.matchMedia;
  return () => {
    window.matchMedia = originalMatchMedia;
  };
}

function toggleButton(): HTMLButtonElement {
  // The rail's disclosure toggle is the button whose name is one of the two
  // action names; query both arms without assuming the default state.
  const expand = screen.queryByRole("button", { name: "Expand sidebar" });
  const collapse = screen.queryByRole("button", { name: "Collapse sidebar" });
  const el = expand ?? collapse;
  if (!el) throw new Error("rail toggle not found under either action name");
  return el as HTMLButtonElement;
}

describe("NavigationRail a11y toggle name (#1159 AC2)", () => {
  beforeEach(() => {
    mockLogout.mockResolvedValue(undefined);
    mockLogout.mockClear();
  });

  it("names the action for the current state and flips on real clicks", () => {
    const restore = stubMatchMedia();
    try {
      render(
        <MemoryRouter>
          <NavigationRail healthStatus={mockHealthStatus} />
        </MemoryRouter>
      );

      let button = toggleButton();
      const initialName = button.getAttribute("aria-label") ?? "";
      const initialExpanded = button.getAttribute("aria-expanded");
      expect(["Expand sidebar", "Collapse sidebar"]).toContain(initialName);
      // The name must agree with the state it reports.
      expect(initialName).toBe(initialExpanded === "true" ? "Collapse sidebar" : "Expand sidebar");

      // Real click drives the component's own state machine.
      fireEvent.click(button);
      button = toggleButton();
      const nextName = button.getAttribute("aria-label") ?? "";
      const nextExpanded = button.getAttribute("aria-expanded");
      expect(nextName).toBe(initialName === "Expand sidebar" ? "Collapse sidebar" : "Expand sidebar");
      expect(nextExpanded).toBe(initialExpanded === "true" ? "false" : "true");

      // And back — the toggle is stable across a full cycle.
      fireEvent.click(button);
      button = toggleButton();
      expect(button.getAttribute("aria-label")).toBe(initialName);
      expect(button.getAttribute("aria-expanded")).toBe(initialExpanded);
    } finally {
      restore();
    }
  });

  it("the collapsed rail's toggle announces expand; after one click it announces collapse", () => {
    const restore = stubMatchMedia();
    try {
      render(
        <MemoryRouter>
          <NavigationRail healthStatus={mockHealthStatus} />
        </MemoryRouter>
      );

      let button = toggleButton();
      // Drive to the collapsed state first (AC2's stated condition is the
      // EXPANDED arm naming collapse; cover both arms deterministically).
      if (button.getAttribute("aria-expanded") === "true") {
        fireEvent.click(button);
        button = toggleButton();
      }
      expect(button.getAttribute("aria-label")).toBe("Expand sidebar");
      expect(button.getAttribute("aria-expanded")).toBe("false");

      fireEvent.click(button);
      button = toggleButton();
      expect(button.getAttribute("aria-label")).toBe("Collapse sidebar");
      expect(button.getAttribute("aria-expanded")).toBe("true");
    } finally {
      restore();
    }
  });
});

// frontend/src/components/layout/NavigationRail.l05.test.tsx
// Issue #776 L05 / AC6 (frozen acceptance check) — the navigation rail's
// ScrollArea must not hide its scrollbar until hover: the Radix ScrollArea
// type prop must be one of "auto" | "always" | "scroll".
//
// At master NavigationRail.tsx:274 renders <ScrollArea className=...> with
// no `type` prop, so the browser default ("hover" — scrollbar vanishes until
// the pointer enters) applies.
//
// Expected RED at master: "expected false to be true".
//
// Harness: the mock set from NavigationRail.test.tsx (useThemeStore,
// useAuthStore admin selector, useDraftRoomCapabilities stub, real
// @hugeicons/react as a regression guard, MemoryRouter) plus a capturing
// stub for @/components/ui/scroll-area (the ManageMembersSheet.l01.test.tsx
// mock idiom, extended to record each instance's props). matchMedia is
// stubbed explicitly (NavigationRail reads prefers-color-scheme on mount),
// mirroring the F-003 stub shape and the global src/test/setup.ts stub.

import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { NavigationRail } from "./NavigationRail";

const { mockLogout, scrollAreaCalls } = vi.hoisted(() => ({
  mockLogout: vi.fn(),
  scrollAreaCalls: [] as Array<Record<string, unknown>>,
}));

// Capturing ScrollArea stub — records the props of each instance so the
// check can assert on the `type` prop without jsdom layout.
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: (props: { children?: ReactNode }) => {
    scrollAreaCalls.push(props as Record<string, unknown>);
    return <div>{props.children}</div>;
  },
}));

// Mock useThemeStore before importing NavigationRail.
vi.mock("@/stores/useThemeStore", () => ({
  useThemeStore: vi.fn(() => ({
    theme: "dark",
    setTheme: vi.fn(),
  })),
  applyTheme: vi.fn(),
}));

// Mock useAuthStore — default to admin so all nav items are visible.
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

// Unrelated to Draft Room gating — stub the capability hook.
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

describe("NavigationRail L05 (issue #776)", () => {
  beforeEach(() => {
    mockLogout.mockResolvedValue(undefined);
    mockLogout.mockClear();
    scrollAreaCalls.length = 0;
  });

  it("nav list scrollbar is not hover-only", () => {
    // Explicit matchMedia stub (NavigationRail reads prefers-color-scheme at
    // mount); restored afterwards like the F-003 harness test.
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia;

    try {
      render(
        <MemoryRouter>
          <NavigationRail healthStatus={mockHealthStatus} />
        </MemoryRouter>
      );

      expect(scrollAreaCalls.length).toBeGreaterThan(0);
      const captured = scrollAreaCalls[0] as { type?: unknown };
      expect(["auto", "always", "scroll"].includes(captured.type as string)).toBe(true);
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });
});

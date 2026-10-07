// frontend/src/components/layout/MobileBottomNav.l05.test.tsx
// Issue #776 L05 (frozen acceptance checks) — the mobile "More" sheet must
// scroll its content and expose exactly one close button.
//
// At master the SheetContent is "h-[50vh] rounded-t-2xl"
// (MobileBottomNav.tsx:124) with no overflow-y-* token, and there are TWO
// close controls: the sheet.tsx built-in SheetPrimitive.Close (sr-only
// "Close", sheet.tsx:73-76) plus the custom "Close" button
// (MobileBottomNav.tsx:128-134).
//
// Expected RED at master:
//   "More sheet content scrolls"            — expected false to be true
//   "More sheet has exactly one close button" — expected 2 to be 1
//
// Harness: the mock set from MobileBottomNav.test.tsx (useAuthStore admin
// selector mock, useDraftRoomCapabilities stub, MemoryRouter). jsdom has no
// layout engine — all assertions are class-token / DOM-shape only.

import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MobileBottomNav } from "./MobileBottomNav";

const mockLogout = vi.hoisted(() => vi.fn());

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

// Unrelated to Draft Room gating — stub the capability hook so it doesn't
// require a QueryClientProvider (same note as MobileBottomNav.test.tsx).
vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

describe("MobileBottomNav L05 (issue #776)", () => {
  beforeEach(() => {
    mockLogout.mockResolvedValue(undefined);
    mockLogout.mockClear();
  });

  it("More sheet content scrolls", () => {
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );

    fireEvent.click(screen.getByRole("button", { name: "More navigation options" }));
    const dialog = screen.getByRole("dialog");

    expect(/overflow-y-(auto|scroll)/.test(dialog.className)).toBe(true);
  });

  it("More sheet has exactly one close button", () => {
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem="chat" onItemSelect={vi.fn()} />
      </MemoryRouter>
    );

    fireEvent.click(screen.getByRole("button", { name: "More navigation options" }));
    const dialog = screen.getByRole("dialog");

    const closeButtons = within(dialog).getAllByRole("button", { name: /close/i });
    expect(closeButtons.length).toBe(1);
  });
});

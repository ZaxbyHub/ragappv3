// Issue #778 (L07) — active-state label contrast pins for MobileBottomNav.
//
// The a11y route-x-viewport gate found the active bottom-nav label rendering
// text-primary at text-xs on the bg-primary/10 pill: ~4.4:1 in light theme,
// under the 4.5:1 AA floor (the same wrong-surface class as #777 — the token
// guardrail measured pairs on reference frames, not this rendered pill).
// The fix settles ALL THREE active-label sites on the #862 selected-tab cue
// contract: text-foreground label + ring-2 ring-inset ring-foreground (the
// pill alone measured ~1.1:1, so the ring restores a >=3:1 non-text cue).
// The real contrast proof is the e2e axe matrix (a11y-matrix.spec.ts); these
// pins guard the class contract against silent regression.
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MobileBottomNav } from "./MobileBottomNav";

const mockLogout = vi.hoisted(() => vi.fn());

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(
    (selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
      selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

function labelClasses(button: HTMLElement): string {
  return button.querySelector("span")?.className ?? "";
}

function renderNav(activeItem: string) {
  render(
    <MemoryRouter>
      <MobileBottomNav activeItem={activeItem} onItemSelect={vi.fn()} />
    </MemoryRouter>
  );
}

describe("MobileBottomNav active-state label cues (issue #778)", () => {
  beforeEach(() => {
    mockLogout.mockResolvedValue(undefined);
    mockLogout.mockClear();
  });

  it("active primary tab: foreground label + inset ring cue, never text-primary", () => {
    renderNav("chat");

    const active = screen.getByRole("button", { name: "Chat" });
    expect(active.getAttribute("aria-current")).toBe("page");
    expect(active.className, "active cue carries the inset ring").toContain("ring-inset");
    expect(active.className, "active cue carries the ring color").toContain("ring-foreground");
    expect(active.className, "active cue carries the ring width").toContain("ring-2");
    expect(labelClasses(active), "active label is foreground").toContain("text-foreground");
    expect(labelClasses(active), "active label must not be text-primary").not.toContain("text-primary");

    const inactive = screen.getByRole("button", { name: /documents/i });
    expect(labelClasses(inactive), "inactive label stays muted").toContain("text-muted-foreground");
    expect(inactive.className, "inactive tab has no ring cue").not.toContain("ring-inset");
  });

  it("open More trigger: foreground label + inset ring cue, never text-primary", () => {
    renderNav("chat");

    const more = screen.getByRole("button", { name: "More navigation options" });
    fireEvent.click(more);

    expect(more.getAttribute("aria-expanded")).toBe("true");
    expect(more.className, "open state carries the inset ring").toContain("ring-inset");
    expect(more.className, "open state carries the ring width").toContain("ring-2");
    expect(labelClasses(more), "open label is foreground").toContain("text-foreground");
    expect(labelClasses(more), "open label must not be text-primary").not.toContain("text-primary");
  });

  it("More-sheet active tile: foreground label + inset ring cue, never text-primary", () => {
    renderNav("settings");

    fireEvent.click(screen.getByRole("button", { name: "More navigation options" }));

    const tile = screen.getByRole("button", { name: "Settings" });
    expect(tile.className, "active tile carries the inset ring").toContain("ring-inset");
    expect(tile.className, "active tile carries the ring width").toContain("ring-2");
    expect(labelClasses(tile), "tile label is foreground").toContain("text-foreground");
    expect(labelClasses(tile), "tile label must not be text-primary").not.toContain("text-primary");
  });
});

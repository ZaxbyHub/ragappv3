import { render, screen } from "@testing-library/react";
import { readFileSync } from "fs";
import { resolve } from "path";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

// Issue #779 recurrence guardrail (class C17: a shell-level primitive
// misreports state or fails to contain a fault). The black-box App feedback
// tests cover the actual bootstrap behavior; this file pins the structural
// invariants checked here:
//  1. TEST_MODE remains DEV-only, and ProtectedRoute owns no store seed;
//     router-local bootstrap ordering and render-phase safety are exercised
//     by the black-box App feedback tests;
//  2. the mobile nav must accept a NEUTRAL active item (null) and mark
//     nothing aria-current for it (the UI-R1-08 defect class: a total
//     route->item function with a lying non-null default).
// Source-inspection guards are sanctioned here for structural invariants
// that are disproportionately expensive to exercise behaviorally (same
// precedent as LoginPage.test.tsx's DEV-gate pins).

const protectedRouteSource = readFileSync(
  resolve(__dirname, "./components/auth/ProtectedRoute.tsx"),
  "utf-8",
);
const appSource = readFileSync(resolve(__dirname, "./App.tsx"), "utf-8");

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
    selector({ user: { role: "admin" }, logout: vi.fn().mockResolvedValue(undefined) })
  ),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

describe("shell recurrence guardrail (issue #779, class C17)", () => {
  it("keeps the DEV-only test-mode gate and no guard-owned demo seed", () => {
    expect(appSource).toContain(
      'const TEST_MODE = import.meta.env.DEV && import.meta.env.VITE_TEST_MODE === "true";',
    );
    expect(protectedRouteSource).not.toContain("useAuthStore.setState");
  });

  it("MobileBottomNav marks nothing active for a neutral (null) activeItem", async () => {
    const { MobileBottomNav } = await import("./components/layout/MobileBottomNav");
    const onItemSelect = vi.fn();
    render(
      <MemoryRouter>
        <MobileBottomNav activeItem={null} onItemSelect={onItemSelect} />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Mobile navigation" });
    expect(nav.querySelectorAll('[aria-current="page"]').length).toBe(0);
  });
});

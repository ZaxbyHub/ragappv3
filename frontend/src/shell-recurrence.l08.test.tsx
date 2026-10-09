import { render, screen } from "@testing-library/react";
import { readFileSync } from "fs";
import { resolve } from "path";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

// Issue #779 recurrence guardrail (class C17: a shell-level primitive
// misreports state or fails to contain a fault). The behavioral regression
// tests are the frozen l08 acceptance files; this file pins the CLASS
// invariants those files cannot express structurally:
//  1. the demo-session seed must stay SYNCHRONOUS in App's render path —
//     it must never move back into a useEffect (the UI-R3-08 defect class:
//     a guard reading state one commit before the effect that seeds it);
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
  it("keeps the demo-session seed synchronous in App and out of ProtectedRoute effects", () => {
    // The seed must live in App's render path, gated on TEST_MODE.
    expect(appSource).toMatch(
      /if \(TEST_MODE && !isAuthenticated\) \{[\s\S]*?setState\?\.\(\{[\s\S]*?needsSetup: false/,
    );

    // ProtectedRoute must not own a store write anymore: any
    // useAuthStore.setState inside a useEffect there is the recurrence
    // shape (state written one commit after the guards already read it).
    const effectBuriedSeed = /useEffect\([\s\S]*?useAuthStore\.setState/.test(
      protectedRouteSource,
    );
    expect(effectBuriedSeed).toBe(false);
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

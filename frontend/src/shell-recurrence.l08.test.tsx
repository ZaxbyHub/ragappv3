import { render, screen } from "@testing-library/react";
import { readFileSync } from "fs";
import { resolve } from "path";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

// Issue #779 recurrence guardrail (class C17: a shell-level primitive
// misreports state or fails to contain a fault). The behavioral regression
// tests are the frozen l08 acceptance files plus App.l08-feedback.test.tsx;
// this file pins the CLASS invariants those files cannot express
// structurally:
//  1. the demo-session seed must stay SYNCHRONOUS in App's render path —
//     the first-render arm must exist as the ref-guarded render block and
//     must precede every effect registration (the UI-R3-08 defect class:
//     a guard reading state one commit before the effect that seeds it);
//  2. the raw store write must never appear inside an effect body (the
//     sanctioned effect arm goes through the named seedDemoSession helper);
//  3. ProtectedRoute must not seed at all (the seed lives in App);
//  4. the mobile nav must accept a NEUTRAL active item (null) and mark
//     nothing aria-current for it (the UI-R1-08 defect class: a total
//     route->item function with a lying non-null default).
// Scan mechanics (issue #779 feedback, PRR-010/F-6): the source scans run
// against COMMENT-STRIPPED text with BOUNDED gaps and a brace-matched
// effect-body extractor, so a seed wrapped in a useEffect, a reordered
// condition, or a comment containing the pattern cannot slip through the
// way the original unbounded regexes allowed.

const stripComments = (source: string): string =>
  // Line comments FIRST: a literal like "/admin/*" inside a line comment
  // would otherwise open a phantom block comment and the block pass would
  // eat everything up to the next unrelated closer. Block comments second.
  source
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1")
    .replace(/\/\*[\s\S]*?\*\//g, " ");

// Extract the balanced-paren body of every `useEffect(` call occurrence.
const effectBodies = (source: string): string[] => {
  const bodies: string[] = [];
  let from = source.indexOf("useEffect(");
  while (from !== -1) {
    let depth = 0;
    let end = -1;
    for (let i = from + "useEffect".length; i < source.length; i++) {
      const ch = source[i];
      if (ch === "(") depth++;
      else if (ch === ")") {
        depth--;
        if (depth === 0) {
          end = i;
          break;
        }
      }
    }
    if (end === -1) break;
    bodies.push(source.slice(from, end + 1));
    from = source.indexOf("useEffect(", end + 1);
  }
  return bodies;
};

const appSource = stripComments(
  readFileSync(resolve(__dirname, "./App.tsx"), "utf-8"),
);
const protectedRouteSource = stripComments(
  readFileSync(resolve(__dirname, "./components/auth/ProtectedRoute.tsx"), "utf-8"),
);

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
  it("keeps the first-render demo seed synchronous in App's render body", () => {
    // The sync arm: ref-guarded, runs before any effect registration, and
    // calls the seed helper DIRECTLY in the render body. The bounded gaps
    // keep the pin structural — a seed moved into an effect loses this
    // exact block and behavioral checks C5/C6 catch the bounce.
    const syncSeed = appSource.match(
      /if \(TEST_MODE && !isAuthenticated && !demoSeededRef\.current\) \{[^]{0,200}?demoSeededRef\.current = true;[^]{0,200}?seedDemoSession\(\);/,
    );
    expect(syncSeed).not.toBeNull();

    // Source-order pin, scoped to App's own region (earlier components in
    // this file register effects above App): the synchronous block must
    // precede App's first effect registration (render body executes before
    // effects).
    const appBodyStart = appSource.indexOf("function App(");
    const blockIndex = appSource.indexOf("demoSeededRef.current = true", appBodyStart);
    const firstEffectInApp = appSource.indexOf("useEffect(", appBodyStart);
    expect(blockIndex).toBeGreaterThan(-1);
    expect(firstEffectInApp).toBeGreaterThan(blockIndex);
  });

  it("keeps raw store writes out of every effect body in App and ProtectedRoute", () => {
    // The sanctioned effect arm calls the seedDemoSession helper; a RAW
    // useAuthStore.setState inside any effect body is the recurrence shape
    // (state written one commit after the guards already read it).
    for (const [name, source] of [
      ["App.tsx", appSource],
      ["ProtectedRoute.tsx", protectedRouteSource],
    ] as const) {
      for (const body of effectBodies(source)) {
        expect(`effect body in ${name} contains a raw store write: ${body.slice(0, 120)}`).not.toMatch(
          /useAuthStore\.setState/,
        );
      }
    }
  });

  it("keeps ProtectedRoute seed-free entirely", () => {
    expect(protectedRouteSource).not.toMatch(/useAuthStore\.setState/);
    expect(protectedRouteSource).not.toMatch(/seedDemoSession/);
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

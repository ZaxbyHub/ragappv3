// frontend/src/pages/SetupPage.l05.test.tsx
// Issue #776 L05 / AC9 (frozen acceptance check) — the setup form must state
// the full password rule in visible text: uppercase requirement, digit/number
// requirement, and the minimum length (8).
//
// At master the only statement of the rule is the password field's
// placeholder "Password (min 8 characters)" (SetupPage.tsx:224) —
// placeholders are not part of document.body.textContent — and no visible
// text mentions uppercase or a digit, so a screen-reader user (or anyone
// with autofill) never sees the actual policy.
//
// Expected RED at master: "expected false to be true".
//
// Harness: the mock set from SetupPage.test.tsx (useAuthStore spy with
// needsSetup=true, react-router-dom partial mock for useNavigate,
// BrowserRouter wrapper).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import SetupPage from "./SetupPage";
import * as useAuthStoreModule from "@/stores/useAuthStore";
import { BrowserRouter } from "react-router-dom";

// Mock the dependencies
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return {
    ...actual,
    useNavigate: vi.fn(() => vi.fn()),
  };
});

// Helper to render with router (same as SetupPage.test.tsx).
const renderSetupPage = (needsSetup: boolean | null = true) => {
  vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
    register: vi.fn().mockResolvedValue({ success: true }),
    needsSetup,
    isLoading: false,
  } as any);

  return render(
    <BrowserRouter>
      <SetupPage />
    </BrowserRouter>
  );
};

describe("SetupPage L05 (issue #776)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("states the full password rule in visible text", () => {
    renderSetupPage();

    // Visible text only — placeholders are deliberately NOT in textContent.
    const text = document.body.textContent ?? "";
    expect(/uppercase/i.test(text) && /(digit|number)/i.test(text) && /8/.test(text)).toBe(true);
  });
});

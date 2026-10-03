// frontend/src/components/vault/VaultGate.m01.test.tsx
// Issue-trace 781-vaultgate-first-run-baseline — acceptance check C5 (AC1).
//
// NEW-SURFACE check: a shared VaultGate component does not exist at base —
// at base this file fails during module resolution ("Failed to resolve
// import" / "Cannot find module"), which is the pre-fix signature.
//
// Post-fix contract under test: VaultGate renders the vault selector (the
// component the null-vault guard copy points users at) AND an action that
// navigates to "/vaults" so a first-run user with no vault can create one.
//
// Conventions: router context via MemoryRouter (DocumentsPage.test.tsx
// pattern); react-router-dom partially mocked so useNavigate is observable
// (SetupPage.test.tsx pattern — importActual spread with useNavigate
// overridden).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";
import { VaultGate } from "@/components/vault/VaultGate";

const mockNavigate = vi.fn();

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

// Same minimal typed stub as MemoryPage.m01.test.tsx — the gate must render
// exactly one selector surface.
vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

describe("VaultGate m01 (issue-trace 781-vaultgate-first-run-baseline)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockNavigate.mockReset();
  });

  it("VaultGate renders selector and vaults action", async () => {
    await act(async () => {
      render(
        <MemoryRouter>
          <VaultGate />
        </MemoryRouter>
      );
    });

    // Exactly one vault selector on the gate surface.
    expect(screen.queryAllByTestId("vault-selector").length).toBe(1);

    // The open/create-vaults action navigates to /vaults. The VaultSelector
    // stub renders a div (no button), so any button whose accessible name
    // mentions "vault" is VaultGate's own action.
    const vaultActions = screen.queryAllByRole("button", { name: /vault/i });
    expect(vaultActions.length).toBeGreaterThan(0);
    await act(async () => {
      fireEvent.click(vaultActions[0]);
    });
    expect(mockNavigate).toHaveBeenCalledWith("/vaults");
  });
});

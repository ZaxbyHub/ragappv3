// frontend/src/components/vault/VaultGate.m02.test.tsx
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki (unfrozen pin).
//
// Uses the REAL react-router-dom (no module mock): the #781 component called
// useNavigate() unconditionally, which throws outside a Router — the exact
// class that broke Wiki's bare-render acceptance check until the
// useOptionalNavigate tolerance landed. On the pre-fix tree this file's bare
// renders throw, so the coverage is genuinely discriminating.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import React from "react";
import { MemoryRouter, useLocation } from "react-router-dom";

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

import { VaultGate } from "@/components/vault/VaultGate";

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

describe("VaultGate m02 (router tolerance, issue #782)", () => {
  it("renders the selector and Open Vaults action outside a Router without throwing", () => {
    // Bare render — the pre-fix component threw here (useNavigate invariant).
    render(<VaultGate />);

    expect(screen.getByTestId("vault-selector")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /open vaults/i }),
    ).toBeInTheDocument();
  });

  it("renders the optional reason line", () => {
    render(<VaultGate reason="Knowledge entries are scoped to a single vault." />);
    expect(
      screen.getByText("Knowledge entries are scoped to a single vault."),
    ).toBeInTheDocument();
  });

  it("navigates to /vaults when Open Vaults is clicked inside a Router", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/kms"]}>
        <LocationProbe />
        <VaultGate />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/kms");
    await user.click(screen.getByRole("button", { name: /open vaults/i }));
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/vaults");
  });
});

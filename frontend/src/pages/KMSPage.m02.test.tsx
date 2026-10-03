// frontend/src/pages/KMSPage.m02.test.tsx
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki — acceptance check C1 (AC1).
//
// The KMS no-selection branch (KMSPage.tsx:211-216) is text-only: a raw <p>
// telling the user to "Select a vault to view its knowledge entries." with no
// selector from where they stand. The header row already renders one
// VaultSelector (KMSPage.tsx:160), so a page-level count would mask the gap —
// this check counts selectors WITHIN the container that holds the
// no-selection copy only.
//
// Expected pre-fix (base) verdict: RED with "expected +0 to be 1" (the
// container holds only the copy; the header selector lives outside it).
//
// Mock idioms reused (not invented):
// - @/lib/api factory mock set from frontend/src/tests/issue515-kms.test.tsx
//   (only the members KMSPage imports).
// - useVaultStore as vi.fn() + per-test mockReturnValue seeding, and the
//   VaultSelector data-testid stub, from DocumentsPage.m01.test.tsx /
//   MemoryPage.m01.test.tsx (issue-trace 781 m01 fixtures).
// - MemoryRouter wrapper: KMSPage calls useNavigate.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, act, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

import KMSPage from "@/pages/KMSPage";
import { useVaultStore } from "@/stores/useVaultStore";

// Minimal @/lib/api surface KMSPage imports (issue515-kms.test.tsx pattern).
vi.mock("@/lib/api", () => ({
  listKMSEntries: vi.fn().mockResolvedValue({ entries: [], total: 0, page: 1, per_page: 200 }),
  createKMSEntry: vi.fn(),
  recompileVaultKMS: vi.fn().mockResolvedValue({ job_id: 1, status: "pending" }),
}));

// useVaultStore as a mockable fn so the test can seed the first-run scenario
// (accessible vaults exist, none selected).
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: null,
    vaults: [],
  })),
}));

// Same data-testid stub as the m01 fixtures — counts the selector wherever it
// renders (header row AND, post-fix, inside the no-selection state).
vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

describe("KMSPage m02 (issue-trace 782-firstrun-checklist-vaultgate-kms-wiki)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("no-selection state uses the VaultGate selector", async () => {
    // First-run scenario: an accessible vault exists, none is selected —
    // the state where the user must be able to pick a vault from where
    // they stand.
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: null,
      vaults: [{ id: 2, name: "Team Vault", current_user_permission: "write" }],
    } as ReturnType<typeof useVaultStore>);

    await act(async () => {
      render(<KMSPage />);
    });

    // The no-selection copy is rendered.
    const copy = screen.getByText(/Select a vault to view its knowledge entries/);
    expect(copy).toBeInTheDocument();

    // The container that holds the no-selection state (the body pane the
    // copy lives in). The header VaultSelector is OUTSIDE this container,
    // so it cannot satisfy the count.
    const container = copy.closest("div");
    expect(container).not.toBeNull();

    // Exactly one selector must be offered WITHIN the no-selection state.
    expect(within(container as HTMLElement).queryAllByTestId("vault-selector").length).toBe(1);
  });
});

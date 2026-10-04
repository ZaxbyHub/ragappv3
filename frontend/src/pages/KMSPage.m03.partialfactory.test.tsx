// frontend/src/pages/KMSPage.m03.partialfactory.test.tsx
// Issue-trace 783-kms-jobhandle-ingest-cancel — the lazy-closure pin: the
// page renders and loads entries under a PARTIAL @/lib/api factory whose
// KMS members lack the poll client entirely (exactly what the m02 and
// vaultGate.781 suites use). The poll-time fetcher is the only place the
// client is dereferenced, so an absent factory member can never throw at
// render time.

import { describe, it, expect, vi } from "vitest";
import { render as rtlRender, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/api", () => ({
  // Deliberately partial: no listKMSJobs member.
  listKMSEntries: vi
    .fn()
    .mockResolvedValue({ entries: [], total: 0, page: 1, per_page: 200 }),
  createKMSEntry: vi.fn(),
  recompileVaultKMS: vi.fn().mockResolvedValue({ job_id: 7, status: "pending" }),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import KMSPage from "@/pages/KMSPage";
import { listKMSEntries } from "@/lib/api";

describe("KMSPage m03 partial-factory render pin (issue-trace 783)", () => {
  it("renders and loads entries without dereferencing the poll client", async () => {
    rtlRender(<KMSPage />, { wrapper: MemoryRouter });
    const button = await screen.findByRole("button", { name: "Recompile" });
    expect(button).toBeEnabled();
    expect(listKMSEntries).toHaveBeenCalled();
    await act(async () => {});
  });
});

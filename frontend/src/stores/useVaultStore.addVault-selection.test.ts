// frontend/src/stores/useVaultStore.addVault-selection.test.ts
// Trace meridian-canvas-401-vault-zero — acceptance check C2 (AC3, RC2).
//
// FROZEN CONTRACT (frozen at Phase 2.5; the implementer cannot weaken it):
//
//   useVaultStore.addVault(request), after a successful createVault call,
//   must SELECT the newly created vault:
//     - state.activeVaultId becomes the new vault's id, and
//     - localStorage 'kv_active_vault_id' is updated to that id.
//
//   Without this, the previously-active vault stays selected (persisted via
//   kv_active_vault_id), so the next upload silently targets the OLD vault:
//   the new vault then truthfully shows 0 documents and chat scoped to it
//   finds nothing (04-root-cause.md RC2 — the reported zero-documents flow).
//
// Base behavior (94c0b925): addVault only appends the vault
// (useVaultStore.ts:76-80) and never selects it, so this check is RED at
// base for exactly that reason.
//
// The API layer is mocked at the store's import boundary (@/lib/api), the
// same pattern as useUploadStore.test.ts; localStorage gets the functional
// Map-backed mock the repo's setup.ts vi.fn() stubs need to actually store
// values (Composer.draft.test.tsx pattern).

import { describe, it, expect, vi, beforeEach } from "vitest";
import type { Vault } from "@/lib/api";

const { createVaultMock } = vi.hoisted(() => ({
  createVaultMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createVault: createVaultMock,
}));

import { useVaultStore } from "./useVaultStore";

function makeVault(id: number, name: string): Vault {
  return {
    id,
    name,
    description: "",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    effective_enrichment_enabled: true,
    effective_multimodal_enabled: true,
  };
}

describe("useVaultStore.addVault selects the newly created vault (trace meridian-canvas-401-vault-zero)", () => {
  const storage = new Map<string, string>();

  beforeEach(() => {
    vi.clearAllMocks();
    storage.clear();
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => storage.get(key) ?? null);
    vi.mocked(localStorage.setItem).mockImplementation((key: string, value: string) => {
      storage.set(key, value);
    });
    vi.mocked(localStorage.removeItem).mockImplementation((key: string) => {
      storage.delete(key);
    });
    useVaultStore.setState({ vaults: [], activeVaultId: null, loading: false, error: null });
  });

  it("addVault makes the new vault active and persists the selection to kv_active_vault_id", async () => {
    const existing = makeVault(1, "Research");
    const created = makeVault(2, "Meridian notes");
    // The reporter's starting point: vault 1 is active and persisted.
    useVaultStore.setState({ vaults: [existing], activeVaultId: 1 });
    localStorage.setItem("kv_active_vault_id", "1");
    createVaultMock.mockResolvedValue(created);

    const returned = await useVaultStore.getState().addVault({ name: "Meridian notes" });

    // The vault list itself is appended either way.
    expect(returned).toBe(created);
    expect(useVaultStore.getState().vaults.map((v) => v.id)).toEqual([1, 2]);

    expect(
      useVaultStore.getState().activeVaultId,
      "addVault must select the newly created vault: activeVaultId should become the new vault's id (the stale selection silently retargets the next upload into the old vault — trace meridian-canvas-401-vault-zero RC2)"
    ).toBe(2);

    expect(
      localStorage.getItem("kv_active_vault_id"),
      "addVault must persist the new selection to localStorage key kv_active_vault_id"
    ).toBe("2");
  });
});

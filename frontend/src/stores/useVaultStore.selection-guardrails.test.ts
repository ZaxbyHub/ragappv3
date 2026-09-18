// frontend/src/stores/useVaultStore.selection-guardrails.test.ts
// Trace meridian-canvas-401-vault-zero — acceptance check C3 (AC6).
//
// PRESERVING guardrails around the C2 fix (addVault selecting the newly
// created vault). The create-path change must not disturb the existing
// selection invariants in useVaultStore — the delete-path asymmetry 04-root-
// cause.md RC2 points at (removeVault handles the active-vault case that
// addVault was missing):
//
//   (a) removeVault of the ACTIVE vault clears activeVaultId and removes the
//       persisted localStorage key 'kv_active_vault_id';
//   (b) removeVault of a NON-active vault leaves activeVaultId and the
//       persisted key untouched;
//   (c) fetchVaults replaces an invalid (no longer accessible) non-null
//       activeVaultId with the first fetched vault and persists that choice.
//
// All three pass at base (94c0b925); they must stay GREEN after the fix.
//
// API mocked at the store's import boundary (@/lib/api) per the
// useUploadStore.test.ts pattern; localStorage gets the functional
// Map-backed mock (Composer.draft.test.tsx pattern).

import { describe, it, expect, vi, beforeEach } from "vitest";
import type { Vault } from "@/lib/api";

const { listAccessibleVaultsMock, deleteVaultMock } = vi.hoisted(() => ({
  listAccessibleVaultsMock: vi.fn(),
  deleteVaultMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listAccessibleVaults: listAccessibleVaultsMock,
  deleteVault: deleteVaultMock,
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

describe("useVaultStore selection guardrails (trace meridian-canvas-401-vault-zero AC6)", () => {
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
    deleteVaultMock.mockResolvedValue(undefined);
  });

  it("removeVault of the ACTIVE vault clears activeVaultId and removes the persisted key", async () => {
    const first = makeVault(1, "Research");
    const second = makeVault(2, "Meridian notes");
    useVaultStore.setState({ vaults: [first, second], activeVaultId: 1 });
    localStorage.setItem("kv_active_vault_id", "1");

    await useVaultStore.getState().removeVault(1);

    expect(useVaultStore.getState().vaults.map((v) => v.id)).toEqual([2]);
    expect(useVaultStore.getState().activeVaultId).toBeNull();
    expect(localStorage.getItem("kv_active_vault_id")).toBeNull();
  });

  it("removeVault of a NON-active vault leaves activeVaultId untouched", async () => {
    const first = makeVault(1, "Research");
    const second = makeVault(2, "Meridian notes");
    useVaultStore.setState({ vaults: [first, second], activeVaultId: 2 });
    localStorage.setItem("kv_active_vault_id", "2");

    await useVaultStore.getState().removeVault(1);

    expect(useVaultStore.getState().vaults.map((v) => v.id)).toEqual([2]);
    expect(useVaultStore.getState().activeVaultId).toBe(2);
    expect(localStorage.getItem("kv_active_vault_id")).toBe("2");
  });

  it("fetchVaults replaces an invalid persisted activeVaultId with the first fetched vault", async () => {
    // 999 is no longer in the accessible list (e.g. deleted elsewhere).
    useVaultStore.setState({ vaults: [], activeVaultId: 999 });
    const alpha = makeVault(101, "Alpha");
    const beta = makeVault(202, "Beta");
    listAccessibleVaultsMock.mockResolvedValue({ vaults: [alpha, beta] });

    await useVaultStore.getState().fetchVaults();

    expect(useVaultStore.getState().vaults.map((v) => v.id)).toEqual([101, 202]);
    expect(useVaultStore.getState().activeVaultId).toBe(101);
    expect(localStorage.getItem("kv_active_vault_id")).toBe("101");
    expect(useVaultStore.getState().loading).toBe(false);
  });
});

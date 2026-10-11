// frontend/src/stores/useVaultStore.l09-feedback.test.ts
// Issue #780 L09 feedback round (PR #880 review, PRR-007 + OOB items 9/10 +
// Copilot's useVaultStore.ts:121 comment) — new non-frozen file per the
// frozen-file policy. Pins what useVaultStore.l09.test.ts cannot:
//   - fetchVaults corrects the in-memory activeVaultId even when persisting
//     the correction throws (a mutant that drops the in-memory correction
//     must fail here);
//   - removeVault still resolves when the post-delete selection persist
//     throws, filters the vault, and clears the in-memory selection.
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Vault } from "@/lib/api";
import { useVaultStore } from "./useVaultStore";

const fetched: Vault[] = [
  { id: 1, name: "Alpha", description: "", file_count: 0, memory_count: 0, session_count: 0 },
  { id: 2, name: "Beta", description: "", file_count: 0, memory_count: 0, session_count: 0 },
] as Vault[];

const mockDeleteVault = vi.fn();

vi.mock("@/lib/api", () => ({
  listAccessibleVaults: vi.fn(async () => ({ vaults: fetched })),
  createVault: vi.fn(),
  updateVault: vi.fn(),
  deleteVault: (...args: unknown[]) => mockDeleteVault(...args),
}));

// Map-backed localStorage whose kv_active_vault_id persistence ALWAYS throws
// (quota-exceeded shape) while reads keep working.
const store = new Map<string, string>();
const localStorageMock = {
  getItem: vi.fn((key: string) => (store.has(key) ? store.get(key)! : null)),
  setItem: vi.fn((key: string, value: string) => {
    if (key === "kv_active_vault_id") {
      throw new DOMException("QuotaExceededError");
    }
    store.set(key, value);
  }),
  removeItem: vi.fn((key: string) => {
    if (key === "kv_active_vault_id") {
      throw new DOMException("QuotaExceededError");
    }
    store.delete(key);
  }),
  clear: vi.fn(() => store.clear()),
};

describe("vault-store storage-failure hardening, feedback pins (issue #780 L09 review)", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", localStorageMock);
    useVaultStore.setState({ vaults: [], activeVaultId: 999, loading: false, error: null });
    store.clear();
    store.set("kv_active_vault_id", "999");
  });

  it("fetchVaults corrects the in-memory selection even when persisting it throws", async () => {
    await useVaultStore.getState().fetchVaults();

    const state = useVaultStore.getState();
    // The fetched list stays…
    expect(state.vaults.length).toBe(2);
    expect(state.error).toBeNull();
    // …AND the in-memory selection is corrected to the first fetched vault
    // (999 is gone) even though persisting that correction threw.
    expect(state.activeVaultId).toBe(1);
    // The persisted key still holds the stale id: the best-effort write
    // failed and was contained, not silently half-applied.
    expect(store.get("kv_active_vault_id")).toBe("999");
  });

  it("removeVault resolves and filters when the selection persist throws", async () => {
    useVaultStore.setState({ vaults: fetched, activeVaultId: 1, loading: false, error: null });
    store.set("kv_active_vault_id", "1");
    mockDeleteVault.mockResolvedValueOnce(undefined);

    // Deleting the ACTIVE vault: the post-delete persist (removeItem) throws,
    // but the vault is already gone server-side so the removal must resolve.
    await expect(useVaultStore.getState().removeVault(1)).resolves.toBeUndefined();

    const state = useVaultStore.getState();
    expect(state.vaults.map((v) => v.id)).toEqual([2]);
    expect(state.activeVaultId).toBeNull();
  });
});

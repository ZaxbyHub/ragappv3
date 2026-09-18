// frontend/src/stores/useVaultStore.addVault-storage-failure.test.ts
// PR #626 review round (Multi-Stage review finding 5/11): selection is
// best-effort — a localStorage failure during addVault must not fail the
// create (the vault itself already exists server-side), and must not leave
// the store in a state where the created vault is missing.
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Vault } from "@/lib/api";
import { useVaultStore } from "./useVaultStore";

const mockCreateVault = vi.fn();

vi.mock("@/lib/api", () => ({
  listAccessibleVaults: vi.fn(async () => ({ vaults: [] })),
  createVault: (...args: unknown[]) => mockCreateVault(...args),
  updateVault: vi.fn(),
  deleteVault: vi.fn(),
}));

// Functional Map-backed localStorage (the repo's established pattern) that we
// can make throw on demand.
const store = new Map<string, string>();
const localStorageMock = {
  getItem: vi.fn((key: string) => (store.has(key) ? store.get(key)! : null)),
  setItem: vi.fn((key: string, value: string) => {
    store.set(key, value);
  }),
  removeItem: vi.fn((key: string) => {
    store.delete(key);
  }),
  clear: vi.fn(() => store.clear()),
};

function freshStore() {
  useVaultStore.setState({ vaults: [], activeVaultId: 7, loading: false, error: null });
  store.clear();
  vi.mocked(localStorageMock.setItem).mockClear();
}

describe("addVault storage-failure hardening (PR #626 review round)", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", localStorageMock);
    freshStore();
  });

  it("still resolves and keeps the created vault when persisting the selection throws", async () => {
    const created: Vault = {
      id: 42,
      name: "New Vault",
      description: "",
      file_count: 0,
      memory_count: 0,
      session_count: 0,
      effective_enrichment_enabled: false,
      effective_multimodal_enabled: false,
    } as Vault;
    mockCreateVault.mockResolvedValueOnce(created);

    // Fail ONLY the selection persistence (the kv_active_vault_id write).
    vi.mocked(localStorageMock.setItem).mockImplementation((key: string, value: string) => {
      if (key === "kv_active_vault_id") {
        throw new DOMException("QuotaExceededError");
      }
      store.set(key, value);
    });

    const vault = await useVaultStore.getState().addVault({ name: "New Vault" });

    // The create succeeded and the vault is in the list…
    expect(vault.id).toBe(42);
    expect(useVaultStore.getState().vaults.map((v) => v.id)).toContain(42);
    // …and the failure was contained: the previous selection is untouched
    // (7), not silently half-migrated to the new vault.
    expect(useVaultStore.getState().activeVaultId).toBe(7);
  });
});

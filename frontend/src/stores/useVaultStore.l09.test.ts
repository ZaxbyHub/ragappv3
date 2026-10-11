// frontend/src/stores/useVaultStore.l09.test.ts
// Issue #780 L09 / AC6 — a localStorage failure must not discard a
// successful fetchVaults.
//
// fetchVaults persists the corrected vault selection (kv_active_vault_id)
// inside its set() updater. When that write throws (QuotaExceededError), the
// updater aborts before returning the fetched list, the outer catch records
// the storage error as a fetch failure, and the successful fetch is
// discarded. Mirrors useVaultStore.addVault-storage-failure.test.ts (the
// sibling best-effort pattern from 59f387b3): the fetched vault list must
// stay in the store with error null.
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Vault } from "@/lib/api";
import { useVaultStore } from "./useVaultStore";

const fetched: Vault[] = [
  {
    id: 1,
    name: "Alpha",
    description: "",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    effective_enrichment_enabled: false,
    effective_multimodal_enabled: false,
  },
  {
    id: 2,
    name: "Beta",
    description: "",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    effective_enrichment_enabled: false,
    effective_multimodal_enabled: false,
  },
] as Vault[];

vi.mock("@/lib/api", () => ({
  listAccessibleVaults: vi.fn(async () => ({ vaults: fetched })),
  createVault: vi.fn(),
  updateVault: vi.fn(),
  deleteVault: vi.fn(),
}));

// Functional Map-backed localStorage (the repo's established pattern). The
// persisted selection (999) is not among the fetched vault ids, so the
// correction branch in fetchVaults runs — and persisting the corrected
// selection throws (QuotaExceededError shape).
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
    store.delete(key);
  }),
  clear: vi.fn(() => store.clear()),
};

function freshStore() {
  useVaultStore.setState({ vaults: [], activeVaultId: 999, loading: false, error: null });
  store.clear();
  store.set("kv_active_vault_id", "999");
}

describe("fetchVaults storage-failure hardening (issue #780 L09)", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", localStorageMock);
    freshStore();
  });

  it("a storage failure does not discard a successful vault fetch", async () => {
    await useVaultStore.getState().fetchVaults();

    // The fetch succeeded — the two fetched vaults must be in the store…
    expect(useVaultStore.getState().vaults.length).toBe(2);
    // …and the storage failure must be contained: no error surfaced, and the
    // store is not left busy.
    expect(useVaultStore.getState().error).toBeNull();
    expect(useVaultStore.getState().loading).toBe(false);
  });
});

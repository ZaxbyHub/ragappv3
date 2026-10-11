import { create } from "zustand";
import type { Vault } from "@/lib/api";
import { listAccessibleVaults, createVault, updateVault, deleteVault } from "@/lib/api";
import type { VaultCreateRequest, VaultUpdateRequest } from "@/lib/api";

// Defer localStorage access to avoid errors in test environments where setup hasn't run yet
const getInitialVaultId = () => {
  if (typeof window === 'undefined') return null;
  const storedVaultId = localStorage.getItem("kv_active_vault_id");
  const parsed = storedVaultId ? parseInt(storedVaultId, 10) : NaN;
  return Number.isNaN(parsed) ? null : parsed;
};

export interface VaultState {
  // State
  vaults: Vault[];
  activeVaultId: number | null;
  loading: boolean;
  error: string | null;
  // Actions
  fetchVaults: () => Promise<void>;
  setActiveVault: (id: number | null) => void;
  addVault: (request: VaultCreateRequest) => Promise<Vault>;
  editVault: (id: number, request: VaultUpdateRequest) => Promise<Vault>;
  removeVault: (id: number) => Promise<void>;
  getActiveVault: () => Vault | undefined;
}

export const useVaultStore = create<VaultState>((set, get) => ({
  // Initial state
  vaults: [],
  activeVaultId: getInitialVaultId(),
  loading: false,
  error: null,

  // Actions
  fetchVaults: async () => {
    set({ loading: true, error: null });
    try {
      const data = await listAccessibleVaults();
      const vaults = data.vaults ?? [];
      // Validate that the persisted activeVaultId is still accessible.
      // If not, auto-select the first vault or clear to null.
      const currentId = get().activeVaultId;
      // null is a valid "All Vaults" selection — only replace a non-null ID that
      // no longer exists in the fetched vault list (e.g. vault was deleted).
      const isValid = currentId === null || vaults.some((v) => v.id === currentId);
      // Commit the state slice first, pure of side effects (#780): persisting
      // the corrected selection below is best-effort, so a storage failure
      // (e.g. QuotaExceededError) must not discard an already-successful fetch.
      set(
        isValid
          ? { vaults, loading: false, error: null }
          : { vaults, activeVaultId: vaults[0]?.id ?? null, loading: false, error: null },
      );
      if (!isValid) {
        // Same pattern addVault uses (59f387b3): the state above already
        // reflects the correction; the persisted selection is secondary.
        try {
          get().setActiveVault(vaults[0]?.id ?? null);
        } catch {
          // localStorage unavailable (quota/private mode): keep the corrected
          // in-memory selection.
        }
      }
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to fetch vaults";
      set({ error: errorMessage, loading: false });
    }
  },

  setActiveVault: (id: number | null) => {
    // Storage write happens BEFORE the state set, so a throwing caller gets
    // NEITHER the persistence NOR the in-memory change. Callers that wrap
    // this in a best-effort try/catch (fetchVaults/removeVault below,
    // addVault) must therefore commit the corrected state slice FIRST — the
    // guard only contains the persistence failure, never the state change.
    if (id != null) {
      localStorage.setItem("kv_active_vault_id", String(id));
    } else {
      localStorage.removeItem("kv_active_vault_id");
    }
    set({ activeVaultId: id });
  },

  addVault: async (request: VaultCreateRequest) => {
    const newVault = await createVault(request);
    set((state) => ({ vaults: [...state.vaults, newVault] }));
    // Select the vault we just created: uploads and chat snapshot activeVaultId,
    // so leaving the previous selection active silently targets the wrong vault
    // for everything the user does right after creating a new one. Selection is
    // best-effort — the vault itself is already created server-side, so a
    // storage failure here must not fail the create.
    try {
      get().setActiveVault(newVault.id);
    } catch {
      // localStorage unavailable (quota/private mode): keep the vault, leave
      // the previous selection active.
    }
    return newVault;
  },

  editVault: async (id: number, request: VaultUpdateRequest) => {
    const updatedVault = await updateVault(id, request);
    set((state) => ({ vaults: state.vaults.map((v) => (v.id === id ? updatedVault : v)) }));
    return updatedVault;
  },

  removeVault: async (id: number) => {
    await deleteVault(id);
    // The vault is already deleted server-side, so the state update is pure
    // and the persisted-selection clear below is best-effort (#780): a
    // storage failure must not reject the removal after the delete succeeded.
    const { vaults, activeVaultId } = get();
    const isActiveVault = activeVaultId === id;
    set({
      vaults: vaults.filter((v) => v.id !== id),
      ...(isActiveVault && { activeVaultId: null }),
    });
    if (isActiveVault) {
      try {
        get().setActiveVault(null);
      } catch {
        // localStorage unavailable: the in-memory selection is already cleared.
      }
    }
  },

  getActiveVault: () => {
    const { vaults, activeVaultId } = get();
    if (!vaults || !Array.isArray(vaults)) return undefined;
    return vaults.find((v) => v.id === activeVaultId);
  },
}));

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
      set((state) => {
        // Validate that the persisted activeVaultId is still accessible.
        // If not, auto-select the first vault or clear to null.
        const currentId = state.activeVaultId;
        // null is a valid "All Vaults" selection — only replace a non-null ID that
        // no longer exists in the fetched vault list (e.g. vault was deleted).
        const isValid = currentId === null || vaults.some((v) => v.id === currentId);
        if (!isValid) {
          const first = vaults[0] ?? null;
          const newId = first ? first.id : null;
          if (newId != null) {
            localStorage.setItem("kv_active_vault_id", String(newId));
          } else {
            localStorage.removeItem("kv_active_vault_id");
          }
          return { vaults, activeVaultId: newId, loading: false, error: null };
        }
        return { vaults, loading: false, error: null };
      });
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to fetch vaults";
      set({ error: errorMessage, loading: false });
    }
  },

  setActiveVault: (id: number | null) => {
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
    set((state) => {
      const isActiveVault = state.activeVaultId === id;
      if (isActiveVault) {
        localStorage.removeItem("kv_active_vault_id");
      }
      return {
        vaults: state.vaults.filter((v) => v.id !== id),
        ...(isActiveVault && { activeVaultId: null }),
      };
    });
  },

  getActiveVault: () => {
    const { vaults, activeVaultId } = get();
    if (!vaults || !Array.isArray(vaults)) return undefined;
    return vaults.find((v) => v.id === activeVaultId);
  },
}));

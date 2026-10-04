import { useState, useEffect, useCallback, useRef } from "react";
import type { JSX } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, FolderOpen, Search, Shield } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  type Group,
  type Vault,
  type VaultAccessItem,
  getGroupVaults,
  listVaults,
} from "@/lib/api";

interface ManageVaultsSheetProps {
  group: Group | null;
  open: boolean;
  editorToken?: number;
  onOpenChange: (open: boolean) => void;
  onSave: (vaultAccess: VaultAccessItem[]) => Promise<void>;
}

const PERMISSIONS = [
  { value: "read", label: "Read" },
  { value: "write", label: "Write" },
  { value: "admin", label: "Admin" },
] as const;

export function ManageVaultsSheet({
  group,
  open,
  editorToken,
  onOpenChange,
  onSave,
}: ManageVaultsSheetProps): JSX.Element {
  // Map of vault_id → permission for selected vaults
  const [accessMap, setAccessMap] = useState<Map<number, string>>(new Map());
  const [searchQuery, setSearchQuery] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [activeOpeningKey, setActiveOpeningKey] = useState<string | null>(null);
  const [initializedOpeningKey, setInitializedOpeningKey] = useState<string | null>(null);
  const openingKey = open && group ? `${group.id}:${editorToken ?? 0}` : null;
  const currentOpeningKeyRef = useRef<string | null>(openingKey);
  currentOpeningKeyRef.current = openingKey;

  const {
    data: allVaults,
    isLoading: isLoadingVaults,
    fetchStatus: vaultCatalogFetchStatus,
    status: vaultCatalogStatus,
    isSuccess: isVaultCatalogReadSuccessful,
    isError: isVaultCatalogReadError,
    refetch: refetchVaultCatalog,
  } = useQuery<Vault[]>({
    queryKey: ["vaults"],
    queryFn: async () => {
      const resp = await listVaults();
      return resp.vaults;
    },
    enabled: open,
  });

  const {
    data: groupVaults,
    isLoading: isLoadingAccess,
    fetchStatus: groupAccessFetchStatus,
    status: groupAccessStatus,
    isSuccess: isGroupAccessReadSuccessful,
    isError: isGroupAccessReadError,
    refetch: refetchGroupAccess,
  } = useQuery({
    queryKey: ["groups", group?.id, "vaults"],
    queryFn: () => getGroupVaults(group!.id),
    enabled: open && !!group,
  });

  const hasSuccessfulReads =
    vaultCatalogStatus === "success" &&
    groupAccessStatus === "success" &&
    vaultCatalogFetchStatus === "idle" &&
    groupAccessFetchStatus === "idle" &&
    isVaultCatalogReadSuccessful &&
    isGroupAccessReadSuccessful &&
    !isVaultCatalogReadError &&
    !isGroupAccessReadError;

  // Reset all editor state whenever a new group/opening becomes active. The
  // opening token also changes when the same group is reopened.
  useEffect(() => {
    if (activeOpeningKey === openingKey) {
      return;
    }
    setActiveOpeningKey(openingKey);
    setInitializedOpeningKey(null);
    setAccessMap(new Map());
    setIsSaving(false);
  }, [activeOpeningKey, openingKey]);

  // Initialize exactly once from successful reads for this opening. A
  // background refetch must not overwrite local edits, and a successful empty
  // response is still a valid empty snapshot.
  useEffect(() => {
    if (
      !openingKey ||
      activeOpeningKey !== openingKey ||
      initializedOpeningKey === openingKey ||
      !hasSuccessfulReads ||
      !allVaults ||
      !groupVaults
    ) {
      return;
    }
    const map = new Map<number, string>();
    for (const gv of groupVaults) {
      map.set(gv.id, gv.permission ?? "read");
    }
    setAccessMap(map);
    setInitializedOpeningKey(openingKey);
  }, [
    activeOpeningKey,
    allVaults,
    groupVaults,
    hasSuccessfulReads,
    initializedOpeningKey,
    openingKey,
  ]);

  useEffect(() => {
    if (!open) setSearchQuery("");
  }, [open]);

  const toggleVault = useCallback((vaultId: number) => {
    setAccessMap((prev) => {
      const next = new Map(prev);
      if (next.has(vaultId)) {
        next.delete(vaultId);
      } else {
        next.set(vaultId, "read");
      }
      return next;
    });
  }, []);

  const setPermission = useCallback((vaultId: number, permission: string) => {
    setAccessMap((prev) => {
      const next = new Map(prev);
      next.set(vaultId, permission);
      return next;
    });
  }, []);

  const isLoading = isLoadingVaults || isLoadingAccess;
  const hasReadError = isVaultCatalogReadError || isGroupAccessReadError;
  const hasInitializedOpening =
    openingKey !== null &&
    activeOpeningKey === openingKey &&
    initializedOpeningKey === openingKey;
  const hasInitialReadError = !hasInitializedOpening && hasReadError;
  const hasInitialReadPaused =
    !hasInitializedOpening &&
    (vaultCatalogFetchStatus === "paused" || groupAccessFetchStatus === "paused");
  const hasBackgroundReadError = hasInitializedOpening && hasReadError;
  const isReadRetrying =
    vaultCatalogFetchStatus === "fetching" || groupAccessFetchStatus === "fetching";
  const canSave = hasInitializedOpening && !isSaving;

  const retryReads = useCallback(() => {
    void Promise.all([refetchVaultCatalog(), refetchGroupAccess()]).catch(() => {
      // Query state renders retry failures. This callback does not update
      // editor state, so it cannot overwrite a newer opening.
    });
  }, [refetchGroupAccess, refetchVaultCatalog]);

  const handleOpenChange = useCallback((nextOpen: boolean) => {
    if (!nextOpen && isSaving) {
      return;
    }
    onOpenChange(nextOpen);
  }, [isSaving, onOpenChange]);

  const handleSave = useCallback(async () => {
    const submittedOpeningKey = openingKey;
    if (!canSave || !submittedOpeningKey) {
      return;
    }
    setIsSaving(true);
    try {
      const vaultAccess: VaultAccessItem[] = Array.from(accessMap.entries()).map(
        ([vault_id, permission]) => ({ vault_id, permission })
      );
      await onSave(vaultAccess);
    } catch {
      // The owning mutation reports the failure. Do not leak a rejected
      // promise from this React event handler.
    } finally {
      if (currentOpeningKeyRef.current === submittedOpeningKey) {
        setIsSaving(false);
      }
    }
  }, [accessMap, canSave, onSave, openingKey]);

  const filteredVaults = (allVaults ?? []).filter((vault) => {
    // Only show vaults belonging to the same org as the group (or global vaults)
    if (group?.org_id != null && vault.org_id != null && vault.org_id !== group.org_id) {
      return false;
    }
    const q = searchQuery.toLowerCase();
    return (
      vault.name.toLowerCase().includes(q) ||
      (vault.description && vault.description.toLowerCase().includes(q))
    );
  });

  const selectedCount = accessMap.size;

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        className="sm:max-w-[480px] flex flex-col"
        aria-labelledby="vaults-title"
        aria-describedby="vaults-desc"
      >
        <SheetHeader>
          <SheetTitle id="vaults-title" className="flex items-center gap-2">
            <FolderOpen className="h-5 w-5" aria-hidden="true" />
            Manage Vault Access
          </SheetTitle>
          <SheetDescription id="vaults-desc">
            Configure vault access permissions for <strong>{group?.name}</strong>.
            Select a vault and choose the permission level (read / write / admin).
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 flex flex-col py-4 min-h-0">
          {hasBackgroundReadError && (
            <div
              className="mb-4 flex items-center justify-between gap-3 rounded-sm border border-amber-500/50 bg-amber-500/10 p-3 text-sm"
              role="status"
              aria-live="polite"
            >
              <span>The latest vault-access refresh failed. Showing the last loaded access.</span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={retryReads}
                disabled={isSaving || isReadRetrying}
              >
                Retry
              </Button>
            </div>
          )}
          <div className="relative mb-4">
            <Search
              className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              placeholder="Search vaults..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10"
              aria-label="Search vaults"
              disabled={isLoading}
            />
          </div>

          <ScrollArea className="flex-1 -mx-6 px-6">
            {hasInitialReadError ? (
              <div
                className="text-center py-8 text-muted-foreground"
                role="status"
                aria-live="polite"
              >
                <p>Unable to load vault access. Please try again.</p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={retryReads}
                  disabled={isSaving || isReadRetrying}
                >
                  Retry
                </Button>
              </div>
            ) : hasInitialReadPaused ? (
              <div
                className="text-center py-8 text-muted-foreground"
                role="status"
                aria-live="polite"
              >
                You are offline. Reconnect to load vault access.
              </div>
            ) : !hasInitializedOpening && isLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="p-3 rounded-sm border space-y-2">
                    <Skeleton className="h-4 w-32" />
                    <Skeleton className="h-3 w-full" />
                    <Skeleton className="h-9 w-32" />
                  </div>
                ))}
              </div>
            ) : filteredVaults.length === 0 ? (
              <div
                className="text-center py-8 text-muted-foreground"
                role="status"
                aria-live="polite"
              >
                {searchQuery ? "No vaults match your search" : "No vaults available"}
              </div>
            ) : (
              <div className="space-y-2 pr-4">
                {filteredVaults.map((vault) => {
                  const hasAccess = accessMap.has(vault.id);
                  const permission = accessMap.get(vault.id) ?? "read";

                  return (
                    <div
                      key={vault.id}
                      className={`flex items-start space-x-3 rounded-sm border p-3 transition-colors ${
                        hasAccess
                          ? "border-primary/50 bg-primary/5"
                          : "hover:bg-muted/50"
                      }`}
                    >
                      <Checkbox
                        id={`vault-${vault.id}`}
                        checked={hasAccess}
                        onCheckedChange={() => toggleVault(vault.id)}
                        aria-label={`Grant access to ${vault.name}`}
                        disabled={isSaving || !hasInitializedOpening}
                        className="mt-0.5"
                      />
                      <div className="flex-1 min-w-0">
                        <Label
                          htmlFor={`vault-${vault.id}`}
                          className="flex items-center gap-2 cursor-pointer"
                        >
                          <FolderOpen
                            className="h-4 w-4 text-muted-foreground shrink-0"
                            aria-hidden="true"
                          />
                          <span className="font-medium truncate">{vault.name}</span>
                          {hasAccess && (
                            <Shield
                              className="h-3 w-3 text-primary shrink-0"
                              aria-hidden="true"
                            />
                          )}
                        </Label>
                        {vault.description && (
                          <p className="text-sm text-muted-foreground line-clamp-1 mt-0.5 ml-6">
                            {vault.description}
                          </p>
                        )}
                        {/* Permission select — only enabled when access is granted */}
                        <div className="mt-2 ml-6">
                          <Select
                            value={permission}
                            onValueChange={(v) => setPermission(vault.id, v)}
                            disabled={!hasAccess || isSaving || !hasInitializedOpening}
                          >
                            <SelectTrigger
                              className="h-7 w-28 text-xs"
                              aria-label={`Permission level for ${vault.name}`}
                            >
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {PERMISSIONS.map((p) => (
                                <SelectItem key={p.value} value={p.value} className="text-xs">
                                  {p.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </ScrollArea>

          <div className="mt-4 text-sm text-muted-foreground">
            {selectedCount} vault{selectedCount !== 1 ? "s" : ""} with access
          </div>
        </div>

        <SheetFooter className="flex-col gap-2 sm:flex-row border-t pt-4">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isSaving}
            className="w-full sm:w-auto"
          >
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            disabled={!canSave}
            className="w-full sm:w-auto"
            aria-label="Save vault access changes"
          >
            {isSaving ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                Saving...
              </>
            ) : (
              "Save Changes"
            )}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

export default ManageVaultsSheet;

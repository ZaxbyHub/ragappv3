import { useEffect } from "react";
import { Database, ChevronDown, Globe } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useVaultStore } from "@/stores/useVaultStore";
import { cn } from "@/lib/utils";

interface VaultSelectorProps {
  className?: string;
}

export function VaultSelector({ className }: VaultSelectorProps) {
  const { vaults, activeVaultId, setActiveVault, fetchVaults, getActiveVault } = useVaultStore();
  const activeVault = getActiveVault();
  const totalFiles = (vaults ?? []).reduce((sum, v) => sum + (v.file_count ?? 0), 0);

  useEffect(() => {
    if (!vaults || vaults.length === 0) {
      fetchVaults();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vaults?.length, fetchVaults]);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          className={cn("gap-2", className)}
          aria-label={
            activeVault
              ? `Active vault: ${activeVault.name} (${activeVault.file_count} files)`
              : `All vaults (${totalFiles} files total)`
          }
        >
          <Database className="h-4 w-4" aria-hidden="true" />
          <span className="truncate max-w-[150px]">{activeVault?.name ?? "All Vaults"}</span>
          {/* Inline file-count badge so users can gauge corpus size at a glance
              without opening the dropdown. */}
          <Badge variant="secondary" className="px-1.5 py-0 text-[10px] font-normal">
            {activeVault ? activeVault.file_count : totalFiles}
          </Badge>
          <ChevronDown className="h-3 w-3 opacity-50" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        <DropdownMenuLabel>Select Vault</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => setActiveVault(null)}
          className={cn(activeVaultId === null && "font-semibold bg-accent")}
        >
          <Globe className="mr-2 h-4 w-4" aria-hidden="true" />
          <div className="flex flex-1 items-center justify-between">
            <span>All Vaults</span>
            <span className="text-xs text-muted-foreground">{totalFiles} files</span>
          </div>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {vaults?.map((vault) => (
          <DropdownMenuItem
            key={vault.id}
            onClick={() => setActiveVault(vault.id)}
            className={cn(vault.id === activeVaultId && "font-semibold bg-accent")}
          >
            <Database className="mr-2 h-4 w-4" aria-hidden="true" />
            <div className="flex flex-1 items-center justify-between min-w-0">
              <div className="flex items-center gap-2 truncate">
                <span className="truncate">{vault.name}</span>
                {vault.current_user_permission && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full border text-muted-foreground">
                    {vault.current_user_permission}
                  </span>
                )}
              </div>
              <span className="text-xs text-muted-foreground ml-2 flex-shrink-0">
                {vault.file_count} {vault.file_count === 1 ? "file" : "files"}
              </span>
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

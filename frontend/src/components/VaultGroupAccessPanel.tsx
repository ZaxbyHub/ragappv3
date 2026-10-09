import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";
import apiClient from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NATIVE_SELECT_CLASS_NAME } from "@/lib/utils";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Users, UserX, Loader2, Building2 } from "lucide-react";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import {
  captureAuthOwner,
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
} from "@/lib/api/auth-lifecycle";

type VaultPermission = "read" | "write" | "admin";

interface GroupAccess {
  group_id: number;
  group_name: string;
  org_name: string;
  permission: VaultPermission;
  granted_at: string;
  granted_by: string;
}

const PERMISSION_OPTIONS: { value: VaultPermission; label: string }[] = [
  { value: "read", label: "Read" },
  { value: "write", label: "Write" },
  { value: "admin", label: "Admin" },
];

interface VaultGroupAccessPanelProps {
  vaultId: number;
}

type AuthOwnerLease = ReturnType<typeof useAuthOwner>;

interface ReadRequest {
  generation: number;
  vaultId: number;
  owner: AuthOwnerLease;
  principalGeneration: number;
  controller: AbortController;
}

interface MutationToken {
  generation: number;
  vaultId: number;
  ownerId: string | number;
  principalGeneration: number;
  controller: AbortController;
}

interface SelectionContext {
  generation: number;
  rowId: number;
  vaultId: number;
  ownerId: string | number;
  principalGeneration: number;
}

type ReadStatus = "loading" | "success" | "error";

function VaultGroupAccessPanelState({
  vaultId,
  owner,
  principalGeneration,
}: VaultGroupAccessPanelProps & { owner: AuthOwnerLease; principalGeneration: number }) {
  const [groupAccessList, setGroupAccessList] = useState<GroupAccess[]>([]);
  const [readStatus, setReadStatus] = useState<ReadStatus>("loading");
  const [readError, setReadError] = useState<string | null>(null);
  const [addingGroup, setAddingGroup] = useState(false);
  const [newGroupId, setNewGroupId] = useState("");
  const [newGroupPermission, setNewGroupPermission] = useState<VaultPermission>("read");
  const [removeDialogOpen, setRemoveDialogOpen] = useState(false);
  const [groupToRemove, setGroupToRemove] = useState<GroupAccess | null>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const mountedRef = useRef(false);
  const readStatusRef = useRef<ReadStatus>("loading");
  const readGenerationRef = useRef(0);
  const activeReadRef = useRef<ReadRequest | null>(null);
  const mutationGenerationRef = useRef(0);
  const activeMutationRef = useRef<MutationToken | null>(null);
  const selectionGenerationRef = useRef(0);
  const selectionRef = useRef<SelectionContext | null>(null);
  const pendingRemovalRef = useRef<{ selection: SelectionContext; token: MutationToken } | null>(null);

  const isCurrentContext = useCallback(() => {
    const currentOwner = captureAuthOwner();
    return (
      mountedRef.current &&
      currentOwner.id === owner.id &&
      isCurrentAuthOwner(owner) &&
      !owner.signal.aborted &&
      captureAuthPrincipalGeneration() === principalGeneration
    );
  }, [owner, principalGeneration]);

  const cancelRead = useCallback(() => {
    readGenerationRef.current += 1;
    activeReadRef.current?.controller.abort();
    activeReadRef.current = null;
  }, []);

  const refreshRows = useCallback(
    async (preserveRows: boolean): Promise<boolean> => {
      if (!isCurrentContext()) return false;
      cancelRead();
      const request: ReadRequest = {
        generation: readGenerationRef.current,
        vaultId,
        owner,
        principalGeneration,
        controller: new AbortController(),
      };
      activeReadRef.current = request;
      if (!preserveRows) setGroupAccessList([]);
      setReadError(null);
      readStatusRef.current = "loading";
      setReadStatus("loading");
      try {
        const response = await apiClient.get<{ group_access: GroupAccess[]; total: number }>(
          `/vaults/${vaultId}/group-access`,
          { signal: request.controller.signal }
        );
        if (
          activeReadRef.current !== request ||
          request.generation !== readGenerationRef.current ||
          !isCurrentContext()
        ) return false;
        const nextRows = response.data?.group_access;
        if (!Array.isArray(nextRows)) throw new Error("Invalid group access response");
        setGroupAccessList(nextRows);
        readStatusRef.current = "success";
        setReadStatus("success");
        return true;
      } catch (error) {
        if (
          request.controller.signal.aborted ||
          activeReadRef.current !== request ||
          request.generation !== readGenerationRef.current ||
          !isCurrentContext()
        ) return false;
        toast.error("Failed to load group access");
        setReadError(error instanceof Error ? error.message : "Failed to load group access");
        readStatusRef.current = "error";
        setReadStatus("error");
        return false;
      } finally {
        if (activeReadRef.current === request && request.generation === readGenerationRef.current) {
          activeReadRef.current = null;
        }
      }
    },
    [cancelRead, isCurrentContext, owner, principalGeneration, vaultId]
  );

  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelRead();
      mutationGenerationRef.current += 1;
      activeMutationRef.current?.controller.abort();
      activeMutationRef.current = null;
      selectionRef.current = null;
    };
  }, [cancelRead]);

  useEffect(() => {
    void refreshRows(false);
    return cancelRead;
  }, [cancelRead, refreshRows]);

  const isCurrentMutation = useCallback(
    (token: MutationToken) =>
      activeMutationRef.current === token &&
      token.generation === mutationGenerationRef.current &&
      token.vaultId === vaultId &&
      token.ownerId === owner.id &&
      token.principalGeneration === principalGeneration &&
      isCurrentContext(),
    [isCurrentContext, owner.id, principalGeneration, vaultId]
  );

  const beginMutation = useCallback(() => {
    if (!isCurrentContext() || readStatusRef.current !== "success" || activeMutationRef.current) return null;
    const token: MutationToken = {
      generation: ++mutationGenerationRef.current,
      vaultId,
      ownerId: owner.id,
      principalGeneration,
      controller: new AbortController(),
    };
    activeMutationRef.current = token;
    setMutationBusy(true);
    return token;
  }, [isCurrentContext, owner.id, principalGeneration, vaultId]);

  const finishMutation = useCallback((token: MutationToken) => {
    if (isCurrentMutation(token)) {
      activeMutationRef.current = null;
      setMutationBusy(false);
    }
  }, [isCurrentMutation]);

  const handleAddGroup = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!isCurrentContext() || readStatusRef.current !== "success" || !newGroupId.trim()) return;
    const groupId = parseInt(newGroupId, 10);
    if (isNaN(groupId)) {
      toast.error("Please enter a valid group ID");
      return;
    }
    const token = beginMutation();
    if (!token) return;
    setAddingGroup(true);
    try {
      await apiClient.post(
        `/vaults/${vaultId}/group-access`,
        { group_id: groupId, permission: newGroupPermission },
        { signal: token.controller.signal }
      );
      if (!isCurrentMutation(token)) return;
      toast.success("Group access granted");
      if (!isCurrentMutation(token)) return;
      setNewGroupId("");
      setNewGroupPermission("read");
      await refreshRows(true);
    } catch (error) {
      if (!token.controller.signal.aborted && isCurrentMutation(token)) toast.error("Failed to grant group access");
    } finally {
      if (isCurrentMutation(token)) setAddingGroup(false);
      finishMutation(token);
    }
  };

  const handlePermissionChange = async (groupId: number, newPermission: VaultPermission) => {
    const token = beginMutation();
    if (!token) return;
    try {
      await apiClient.patch(
        `/vaults/${vaultId}/group-access/${groupId}`,
        { permission: newPermission },
        { signal: token.controller.signal }
      );
      if (!isCurrentMutation(token)) return;
      toast.success("Permission updated");
      setGroupAccessList((current) => isCurrentContext() ? current.map((row) => row.group_id === groupId ? { ...row, permission: newPermission } : row) : current);
    } catch (error) {
      if (!token.controller.signal.aborted && isCurrentMutation(token)) toast.error("Failed to update permission");
    } finally {
      finishMutation(token);
    }
  };

  const openRemoveDialog = (group: GroupAccess) => {
    if (!isCurrentContext() || readStatusRef.current !== "success" || activeMutationRef.current) return;
    const selection: SelectionContext = {
      generation: ++selectionGenerationRef.current,
      rowId: group.group_id,
      vaultId,
      ownerId: owner.id,
      principalGeneration,
    };
    selectionRef.current = selection;
    setGroupToRemove(group);
    setRemoveDialogOpen(true);
  };

  const isCurrentSelection = (selection: SelectionContext) =>
    selectionRef.current === selection &&
    selection.generation === selectionGenerationRef.current &&
    selection.rowId === groupToRemove?.group_id &&
    selection.vaultId === vaultId &&
    selection.ownerId === owner.id &&
    selection.principalGeneration === principalGeneration &&
    isCurrentContext();

  const handleRemove = async (selection: SelectionContext | null) => {
    if (!selection || !groupToRemove || !isCurrentSelection(selection)) return;
    const token = beginMutation();
    if (!token) return;
    pendingRemovalRef.current = { selection, token };
    try {
      await apiClient.delete(`/vaults/${vaultId}/group-access/${groupToRemove.group_id}`, {
        signal: token.controller.signal,
      });
      if (!isCurrentMutation(token) || !isCurrentSelection(selection)) return;
      toast.success("Group access revoked");
      if (!isCurrentMutation(token) || !isCurrentSelection(selection)) return;
      selectionRef.current = null;
      setRemoveDialogOpen(false);
      setGroupToRemove(null);
      setGroupAccessList((current) => isCurrentContext() ? current.filter((row) => row.group_id !== selection.rowId) : current);
    } catch (error) {
      if (!token.controller.signal.aborted && isCurrentMutation(token)) toast.error("Failed to revoke group access");
    } finally {
      if (pendingRemovalRef.current?.token === token) pendingRemovalRef.current = null;
      finishMutation(token);
    }
  };

  const renderedSelection = selectionRef.current;
  const closeSelection = (selection: SelectionContext | null) => {
    const pendingRemoval = pendingRemovalRef.current;
    if (
      pendingRemoval?.selection === selection &&
      activeMutationRef.current === pendingRemoval.token
    ) return;
    if (!selection || !isCurrentSelection(selection)) return;
    selectionRef.current = null;
    setRemoveDialogOpen(false);
    setGroupToRemove(null);
  };
  const canInteract = readStatus === "success" && isCurrentContext() && !mutationBusy;
  const formatDate = (dateStr: string) => new Date(dateStr).toLocaleDateString();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Building2 className="w-5 h-5" />Group Access</CardTitle>
        <CardDescription>Manage organization group access to this vault</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <form onSubmit={handleAddGroup} className="flex gap-2 items-end">
          <div className="flex-1 space-y-2">
            <Label htmlFor={`group-id-${vaultId}`}>Group ID</Label>
            <Input id={`group-id-${vaultId}`} placeholder="Enter group ID..." value={newGroupId} onChange={(e) => { if (isCurrentContext()) setNewGroupId(e.target.value); }} disabled={!canInteract || addingGroup} aria-label="Group ID to grant vault access" />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`group-perm-${vaultId}`}>Permission</Label>
            <Select
              value={newGroupPermission}
              onValueChange={(v) => { if (isCurrentContext()) setNewGroupPermission(v as VaultPermission); }}
              disabled={!canInteract || addingGroup}
            >
              <SelectTrigger id={`group-perm-${vaultId}`} aria-label="Permission level for group access">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PERMISSION_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" disabled={!canInteract || addingGroup || !newGroupId.trim()}>
            {addingGroup ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Users className="w-4 h-4 mr-2" />}
            Grant
          </Button>
        </form>
        {readStatus === "loading" && <div role="status" aria-live="polite">Loading group access</div>}
        {readStatus === "error" && <div role="alert"><p>{readError || "Failed to load group access"}</p><Button type="button" variant="outline" onClick={() => void refreshRows(true)}>Retry</Button></div>}
        <Table>
          <TableCaption className="sr-only">Group Access List</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead className="text-left py-2">Group</TableHead>
              <TableHead className="text-left py-2">Organization</TableHead>
              <TableHead className="text-left py-2">Permission</TableHead>
              <TableHead className="text-left py-2">Granted</TableHead>
              <TableHead className="text-right py-2">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {readStatus !== "success" && groupAccessList.length === 0 ? null : groupAccessList.length === 0 ? (
              <TableRow><TableCell colSpan={5} className="py-8 text-center text-muted-foreground" role="status" aria-live="polite">No groups have access yet. Grant access to organization groups to give their members vault permissions.</TableCell></TableRow>
            ) : (
              groupAccessList.map((group) => (
                <TableRow key={group.group_id}>
                  <TableCell className="py-3"><div className="font-medium">{group.group_name}</div></TableCell>
                  <TableCell className="py-3 text-muted-foreground text-sm">{group.org_name}</TableCell>
                  <TableCell className="py-3">
                     <select value={group.permission} onChange={(e) => handlePermissionChange(group.group_id, e.target.value as VaultPermission)} disabled={!canInteract} aria-label={`Change permission for ${group.group_name}`} className={NATIVE_SELECT_CLASS_NAME}>
                      {PERMISSION_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{opt.label}</option>))}
                    </select>
                  </TableCell>
                  <TableCell className="py-3 text-muted-foreground text-sm">{formatDate(group.granted_at)}</TableCell>
                  <TableCell className="py-3 text-right">
                    <Button variant="destructive" size="icon" onClick={() => openRemoveDialog(group)} disabled={!canInteract} aria-label={`Revoke access for ${group.group_name}`}><UserX className="w-4 h-4" /></Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </CardContent>
      <Dialog open={removeDialogOpen} onOpenChange={(nextOpen) => { if (!nextOpen) closeSelection(renderedSelection); }}>
        <DialogContent aria-labelledby="revoke-group-title" aria-describedby="revoke-group-desc">
          <DialogHeader>
            <DialogTitle id="revoke-group-title" className="flex items-center gap-2"><UserX className="w-5 h-5 text-destructive" />Revoke Group Access</DialogTitle>
            <DialogDescription id="revoke-group-desc">Are you sure you want to revoke vault access for <strong>{groupToRemove?.group_name}</strong> ({groupToRemove?.org_name})?</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => closeSelection(renderedSelection)} disabled={pendingRemovalRef.current?.selection === renderedSelection}>Cancel</Button>
            <Button variant="destructive" onClick={() => void handleRemove(renderedSelection)} disabled={!canInteract}>Revoke</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function VaultGroupAccessPanel({ vaultId }: VaultGroupAccessPanelProps) {
  const owner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration);
  return <VaultGroupAccessPanelState key={`${vaultId}:${owner.id}:${principalGeneration}`} vaultId={vaultId} owner={owner} principalGeneration={principalGeneration} />;
}

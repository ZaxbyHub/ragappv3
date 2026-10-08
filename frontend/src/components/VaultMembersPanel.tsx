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
import { UserPlus, UserX, Loader2, Users } from "lucide-react";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import {
  captureAuthOwner,
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
} from "@/lib/api/auth-lifecycle";

type VaultPermission = "read" | "write" | "admin";

interface VaultMember {
  user_id: number;
  username: string;
  full_name: string;
  permission: VaultPermission;
  granted_at: string;
}

const PERMISSION_OPTIONS: { value: VaultPermission; label: string }[] = [
  { value: "read", label: "Read" },
  { value: "write", label: "Write" },
  { value: "admin", label: "Admin" },
];

interface VaultMembersPanelProps {
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

function VaultMembersPanelState({
  vaultId,
  owner,
  principalGeneration,
}: VaultMembersPanelProps & { owner: AuthOwnerLease; principalGeneration: number }) {
  const [members, setMembers] = useState<VaultMember[]>([]);
  const [readStatus, setReadStatus] = useState<ReadStatus>("loading");
  const [readError, setReadError] = useState<string | null>(null);
  const [addingMember, setAddingMember] = useState(false);
  const [newMemberUserId, setNewMemberUserId] = useState("");
  const [newMemberPermission, setNewMemberPermission] = useState<VaultPermission>("read");
  const [removeDialogOpen, setRemoveDialogOpen] = useState(false);
  const [memberToRemove, setMemberToRemove] = useState<VaultMember | null>(null);
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
      if (!preserveRows) setMembers([]);
      setReadError(null);
      readStatusRef.current = "loading";
      setReadStatus("loading");
      try {
        const response = await apiClient.get<{ members: VaultMember[]; total: number }>(
          `/vaults/${vaultId}/members`,
          { signal: request.controller.signal }
        );
        if (
          activeReadRef.current !== request ||
          request.generation !== readGenerationRef.current ||
          !isCurrentContext()
        ) return false;
        const nextRows = response.data?.members;
        if (!Array.isArray(nextRows)) throw new Error("Invalid member access response");
        setMembers(nextRows);
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
        toast.error("Failed to load vault members");
        setReadError(error instanceof Error ? error.message : "Failed to load member access");
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

  const handleAddMember = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!isCurrentContext() || readStatusRef.current !== "success" || !newMemberUserId.trim()) return;
    const userId = parseInt(newMemberUserId, 10);
    if (isNaN(userId)) {
      toast.error("Please enter a valid user ID");
      return;
    }
    const token = beginMutation();
    if (!token) return;
    setAddingMember(true);
    try {
      await apiClient.post(
        `/vaults/${vaultId}/members`,
        { member_user_id: userId, permission: newMemberPermission },
        { signal: token.controller.signal }
      );
      if (!isCurrentMutation(token)) return;
      toast.success("Member added to vault");
      if (!isCurrentMutation(token)) return;
      setNewMemberUserId("");
      setNewMemberPermission("read");
      await refreshRows(true);
    } catch (error) {
      if (!token.controller.signal.aborted && isCurrentMutation(token)) toast.error("Failed to add member");
    } finally {
      if (isCurrentMutation(token)) setAddingMember(false);
      finishMutation(token);
    }
  };

  const handlePermissionChange = async (userId: number, newPermission: VaultPermission) => {
    const token = beginMutation();
    if (!token) return;
    try {
      await apiClient.patch(
        `/vaults/${vaultId}/members/${userId}`,
        { permission: newPermission },
        { signal: token.controller.signal }
      );
      if (!isCurrentMutation(token)) return;
      toast.success("Permission updated");
      setMembers((current) => isCurrentContext() ? current.map((row) => row.user_id === userId ? { ...row, permission: newPermission } : row) : current);
    } catch (error) {
      if (!token.controller.signal.aborted && isCurrentMutation(token)) toast.error("Failed to update permission");
    } finally {
      finishMutation(token);
    }
  };

  const openRemoveDialog = (member: VaultMember) => {
    if (!isCurrentContext() || readStatusRef.current !== "success" || activeMutationRef.current) return;
    const selection: SelectionContext = {
      generation: ++selectionGenerationRef.current,
      rowId: member.user_id,
      vaultId,
      ownerId: owner.id,
      principalGeneration,
    };
    selectionRef.current = selection;
    setMemberToRemove(member);
    setRemoveDialogOpen(true);
  };

  const isCurrentSelection = (selection: SelectionContext) =>
    selectionRef.current === selection &&
    selection.generation === selectionGenerationRef.current &&
    selection.rowId === memberToRemove?.user_id &&
    selection.vaultId === vaultId &&
    selection.ownerId === owner.id &&
    selection.principalGeneration === principalGeneration &&
    isCurrentContext();

  const handleRemove = async (selection: SelectionContext | null) => {
    if (!selection || !memberToRemove || !isCurrentSelection(selection)) return;
    const token = beginMutation();
    if (!token) return;
    pendingRemovalRef.current = { selection, token };
    try {
      await apiClient.delete(`/vaults/${vaultId}/members/${memberToRemove.user_id}`, {
        signal: token.controller.signal,
      });
      if (!isCurrentMutation(token) || !isCurrentSelection(selection)) return;
      toast.success("Member removed from vault");
      if (!isCurrentMutation(token) || !isCurrentSelection(selection)) return;
      selectionRef.current = null;
      setRemoveDialogOpen(false);
      setMemberToRemove(null);
      setMembers((current) => isCurrentContext() ? current.filter((row) => row.user_id !== selection.rowId) : current);
    } catch (error) {
      if (!token.controller.signal.aborted && isCurrentMutation(token)) toast.error("Failed to remove member");
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
    setMemberToRemove(null);
  };
  const canInteract = readStatus === "success" && isCurrentContext() && !mutationBusy;
  const formatDate = (dateStr: string) => new Date(dateStr).toLocaleDateString();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Users className="w-5 h-5" />Vault Members</CardTitle>
        <CardDescription>Manage who has access to this vault</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <form onSubmit={handleAddMember} className="flex gap-2 items-end">
          <div className="flex-1 space-y-2">
            <Label htmlFor={`member-userid-${vaultId}`}>User ID</Label>
            <Input id={`member-userid-${vaultId}`} placeholder="Enter user ID..." value={newMemberUserId} onChange={(e) => { if (isCurrentContext()) setNewMemberUserId(e.target.value); }} disabled={!canInteract || addingMember} aria-label="User ID to add as vault member" />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`member-perm-${vaultId}`}>Permission</Label>
            <Select
              value={newMemberPermission}
              onValueChange={(v) => { if (isCurrentContext()) setNewMemberPermission(v as VaultPermission); }}
              disabled={!canInteract || addingMember}
            >
              <SelectTrigger id={`member-perm-${vaultId}`} aria-label="Permission level for new member">
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
          <Button type="submit" disabled={!canInteract || addingMember || !newMemberUserId.trim()}>
            {addingMember ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <UserPlus className="w-4 h-4 mr-2" />}
            Add
          </Button>
        </form>
        {readStatus === "loading" && <div role="status" aria-live="polite">Loading member access</div>}
        {readStatus === "error" && <div role="alert"><p>{readError || "Failed to load member access"}</p><Button type="button" variant="outline" onClick={() => void refreshRows(true)}>Retry</Button></div>}
        <Table>
          <TableCaption className="sr-only">Vault Members</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead className="text-left py-2">User</TableHead>
              <TableHead className="text-left py-2">Permission</TableHead>
              <TableHead className="text-left py-2">Added</TableHead>
              <TableHead className="text-right py-2">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {readStatus !== "success" && members.length === 0 ? null : members.length === 0 ? (
              <TableRow><TableCell colSpan={4} className="py-8 text-center text-muted-foreground" role="status" aria-live="polite">No members yet. Add users to give them access to this vault.</TableCell></TableRow>
            ) : (
              members.map((member) => (
                <TableRow key={member.user_id}>
                  <TableCell className="py-3"><div><div className="font-medium">{member.full_name || member.username}</div><div className="text-sm text-muted-foreground">@{member.username}</div></div></TableCell>
                  <TableCell className="py-3">
                     <select value={member.permission} onChange={(e) => handlePermissionChange(member.user_id, e.target.value as VaultPermission)} disabled={!canInteract} aria-label={`Change permission for ${member.username}`} className={NATIVE_SELECT_CLASS_NAME}>
                      {PERMISSION_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{opt.label}</option>))}
                    </select>
                  </TableCell>
                  <TableCell className="py-3 text-muted-foreground text-sm">{formatDate(member.granted_at)}</TableCell>
                  <TableCell className="py-3 text-right">
                    <Button variant="destructive" size="icon" onClick={() => openRemoveDialog(member)} disabled={!canInteract} aria-label={`Remove ${member.username} from vault`}><UserX className="w-4 h-4" /></Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </CardContent>
      <Dialog open={removeDialogOpen} onOpenChange={(nextOpen) => { if (!nextOpen) closeSelection(renderedSelection); }}>
        <DialogContent aria-labelledby="remove-member-title" aria-describedby="remove-member-desc">
          <DialogHeader>
            <DialogTitle id="remove-member-title" className="flex items-center gap-2"><UserX className="w-5 h-5 text-destructive" />Remove Member</DialogTitle>
            <DialogDescription id="remove-member-desc">Are you sure you want to remove <strong>{memberToRemove?.full_name || memberToRemove?.username}</strong> from this vault?</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => closeSelection(renderedSelection)} disabled={pendingRemovalRef.current?.selection === renderedSelection}>Cancel</Button>
            <Button variant="destructive" onClick={() => void handleRemove(renderedSelection)} disabled={!canInteract}>Remove</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function VaultMembersPanel({ vaultId }: VaultMembersPanelProps) {
  const owner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration);
  return <VaultMembersPanelState key={`${vaultId}:${owner.id}:${principalGeneration}`} vaultId={vaultId} owner={owner} principalGeneration={principalGeneration} />;
}

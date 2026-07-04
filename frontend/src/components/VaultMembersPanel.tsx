import { useState, useEffect, useCallback } from "react";
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

export function VaultMembersPanel({ vaultId }: VaultMembersPanelProps) {
  const [members, setMembers] = useState<VaultMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [addingMember, setAddingMember] = useState(false);
  const [newMemberUserId, setNewMemberUserId] = useState("");
  const [newMemberPermission, setNewMemberPermission] = useState<VaultPermission>("read");
  const [removeDialogOpen, setRemoveDialogOpen] = useState(false);
  const [memberToRemove, setMemberToRemove] = useState<VaultMember | null>(null);
  const [updatingMemberId, setUpdatingMemberId] = useState<number | null>(null);

  const fetchMembers = useCallback(async () => {
    setLoading(true);
    try {
      const response = await apiClient.get<{ members: VaultMember[]; total: number }>(`/vaults/${vaultId}/members`);
      setMembers(response.data.members);
    } catch (err) {
      console.error("Failed to fetch members:", err);
      toast.error("Failed to load vault members");
    } finally { setLoading(false); }
  }, [vaultId]);

  useEffect(() => { fetchMembers(); }, [fetchMembers]);

  const handleAddMember = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newMemberUserId.trim()) return;
    const userId = parseInt(newMemberUserId, 10);
    if (isNaN(userId)) { toast.error("Please enter a valid user ID"); return; }
    setAddingMember(true);
    try {
      await apiClient.post(`/vaults/${vaultId}/members`, { member_user_id: userId, permission: newMemberPermission });
      toast.success("Member added to vault");
      setNewMemberUserId("");
      setNewMemberPermission("read");
      fetchMembers();
    } catch (err) { toast.error("Failed to add member"); }
    finally { setAddingMember(false); }
  };

  const handlePermissionChange = async (userId: number, newPermission: VaultPermission) => {
    setUpdatingMemberId(userId);
    try {
      await apiClient.patch(`/vaults/${vaultId}/members/${userId}`, { permission: newPermission });
      setMembers((prev) => prev.map((m) => (m.user_id === userId ? { ...m, permission: newPermission } : m)));
      toast.success("Permission updated");
    } catch (err) { toast.error("Failed to update permission"); }
    finally { setUpdatingMemberId(null); }
  };

  const handleRemove = async () => {
    if (!memberToRemove) return;
    try {
      await apiClient.delete(`/vaults/${vaultId}/members/${memberToRemove.user_id}`);
      setMembers((prev) => prev.filter((m) => m.user_id !== memberToRemove.user_id));
      toast.success("Member removed from vault");
      setRemoveDialogOpen(false);
      setMemberToRemove(null);
    } catch (err) { toast.error("Failed to remove member"); }
  };

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
            <Input id={`member-userid-${vaultId}`} placeholder="Enter user ID..." value={newMemberUserId} onChange={(e) => setNewMemberUserId(e.target.value)} disabled={addingMember} aria-label="User ID to add as vault member" />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`member-perm-${vaultId}`}>Permission</Label>
            <Select
              value={newMemberPermission}
              onValueChange={(v) => setNewMemberPermission(v as VaultPermission)}
              disabled={addingMember}
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
          <Button type="submit" disabled={addingMember || !newMemberUserId.trim()}>
            {addingMember ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <UserPlus className="w-4 h-4 mr-2" />}
            Add
          </Button>
        </form>
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
            {loading ? (
              <TableRow><TableCell colSpan={4} className="py-8 text-center"><Loader2 className="w-6 h-6 animate-spin mx-auto text-muted-foreground" role="status" aria-live="polite" /><span className="sr-only">Loading members</span></TableCell></TableRow>
            ) : members.length === 0 ? (
              <TableRow><TableCell colSpan={4} className="py-8 text-center text-muted-foreground" role="status" aria-live="polite">No members yet. Add users to give them access to this vault.</TableCell></TableRow>
            ) : (
              members.map((member) => (
                <TableRow key={member.user_id}>
                  <TableCell className="py-3"><div><div className="font-medium">{member.full_name || member.username}</div><div className="text-sm text-muted-foreground">@{member.username}</div></div></TableCell>
                  <TableCell className="py-3">
                     <select value={member.permission} onChange={(e) => handlePermissionChange(member.user_id, e.target.value as VaultPermission)} disabled={updatingMemberId === member.user_id} aria-label={`Change permission for ${member.username}`} className={NATIVE_SELECT_CLASS_NAME}>
                      {PERMISSION_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{opt.label}</option>))}
                    </select>
                  </TableCell>
                  <TableCell className="py-3 text-muted-foreground text-sm">{formatDate(member.granted_at)}</TableCell>
                  <TableCell className="py-3 text-right">
                    <Button variant="destructive" size="icon" onClick={() => { setMemberToRemove(member); setRemoveDialogOpen(true); }} aria-label={`Remove ${member.username} from vault`}><UserX className="w-4 h-4" /></Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </CardContent>
      <Dialog open={removeDialogOpen} onOpenChange={setRemoveDialogOpen}>
        <DialogContent aria-labelledby="remove-member-title" aria-describedby="remove-member-desc">
          <DialogHeader>
            <DialogTitle id="remove-member-title" className="flex items-center gap-2"><UserX className="w-5 h-5 text-destructive" />Remove Member</DialogTitle>
            <DialogDescription id="remove-member-desc">Are you sure you want to remove <strong>{memberToRemove?.full_name || memberToRemove?.username}</strong> from this vault?</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRemoveDialogOpen(false)}>Cancel</Button>
            <Button variant="destructive" onClick={handleRemove}>Remove</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

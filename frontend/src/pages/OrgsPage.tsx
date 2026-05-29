import { useState, useEffect, useCallback, useRef } from "react";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/useAuthStore";
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
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { RoleGuard } from "@/components/auth/RoleGuard";
import { Building2, Plus, Trash2, Users, Vault, ChevronDown, ChevronUp, Loader2, UserPlus, UserX, Search } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { LoadingSpinner } from "@/components/LoadingSpinner";
import { useTestMode } from "@/fixtures/TestModeContext";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type OrgRole = "owner" | "admin" | "member";

interface OrgMember {
  user_id: number;
  username: string;
  full_name: string;
  role: OrgRole;
  joined_at: string;
}

interface UserResult {
  id: number;
  username: string;
  full_name: string;
  role: string;
  is_active: boolean;
}

interface Organization {
  id: number;
  name: string;
  description: string | null;
  member_count: number;
  vault_count: number;
  created_at: string;
  members?: OrgMember[];
}

const ROLE_OPTIONS: { value: OrgRole; label: string }[] = [
  { value: "admin", label: "Admin" },
  { value: "member", label: "Member" },
];

const CHANGEABLE_ROLE_OPTIONS = ROLE_OPTIONS.filter((r) => r.value !== "owner");

// Dev-only mock data; gated on import.meta.env.DEV so it folds away (and is
// dead-code eliminated) in production builds. See finding F-004.
const MOCK_ORGS: Organization[] = import.meta.env.DEV ? [
  { id: 1, name: "Acme Corporation", description: "Primary engineering organization", member_count: 12, vault_count: 4, created_at: "2023-09-01T08:00:00Z" },
  { id: 2, name: "Beta Labs", description: "Research and development division", member_count: 8, vault_count: 2, created_at: "2023-10-15T10:30:00Z" },
  { id: 3, name: "Gamma Solutions", description: "Client-facing support and documentation", member_count: 24, vault_count: 6, created_at: "2023-11-20T14:00:00Z" },
  { id: 4, name: "Delta Systems", description: "Infrastructure and DevOps team", member_count: 6, vault_count: 3, created_at: "2024-01-05T09:15:00Z" },
] : [];

function OrgsPageContent() {
  const testMode = useTestMode();
  const [orgs, setOrgs] = useState<Organization[]>(testMode ? MOCK_ORGS : []);
  const [loading, setLoading] = useState(!testMode);
  const [expandedOrgId, setExpandedOrgId] = useState<number | null>(null);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [orgToDelete, setOrgToDelete] = useState<Organization | null>(null);
  const [creatingOrg, setCreatingOrg] = useState(false);
  const [newOrgName, setNewOrgName] = useState("");
  const [newOrgDescription, setNewOrgDescription] = useState("");
  const [addingMember, setAddingMember] = useState(false);
  const [selectedUser, setSelectedUser] = useState<UserResult | null>(null);
  const [userSearchQuery, setUserSearchQuery] = useState("");
  const [userSearchResults, setUserSearchResults] = useState<UserResult[]>([]);
  const [showUserDropdown, setShowUserDropdown] = useState(false);
  const [searchingUsers, setSearchingUsers] = useState(false);
  const userSearchRef = useRef<HTMLDivElement>(null);
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout>>();
  const [newMemberRole, setNewMemberRole] = useState<OrgRole>("member");
  const [updatingMemberId, setUpdatingMemberId] = useState<number | null>(null);
  const [removeMemberDialogOpen, setRemoveMemberDialogOpen] = useState(false);
  const [memberToRemove, setMemberToRemove] = useState<OrgMember | null>(null);
  const [orgForMemberAction, setOrgForMemberAction] = useState<number | null>(null);

  const [transferDialogOpen, setTransferDialogOpen] = useState(false);
  const [transferOrgId, setTransferOrgId] = useState<number | null>(null);
  const [transferTargetId, setTransferTargetId] = useState<number | null>(null);
  const [transferring, setTransferring] = useState(false);

  const currentUser = useAuthStore((state) => state.user);

  // Close user dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (userSearchRef.current && !userSearchRef.current.contains(e.target as Node)) {
        setShowUserDropdown(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Clean up search timeout on unmount
  useEffect(() => {
    return () => {
      if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    };
  }, []);

  // Debounced user search
  const searchUsers = useCallback(async (query: string) => {
    if (!query.trim()) {
      setUserSearchResults([]);
      setShowUserDropdown(false);
      return;
    }
    setSearchingUsers(true);
    try {
      const response = await apiClient.get<{ users: UserResult[] }>("/users/", { params: { q: query, limit: 10 } });
      setUserSearchResults(response.data.users.filter((u) => u.is_active));
      setShowUserDropdown(true);
    } catch {
      setUserSearchResults([]);
    } finally {
      setSearchingUsers(false);
    }
  }, []);

  const handleUserSearchChange = (value: string) => {
    setUserSearchQuery(value);
    setSelectedUser(null);
    if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    searchTimeoutRef.current = setTimeout(() => searchUsers(value), 300);
  };

  const selectUser = (user: UserResult) => {
    setSelectedUser(user);
    setUserSearchQuery(`${user.full_name || user.username} (${user.username})`);
    setShowUserDropdown(false);
  };
  const isSuperAdmin = currentUser?.role === "superadmin";
  const isMember = currentUser?.role === "member";

  const fetchOrgs = useCallback(async () => {
    if (testMode) {
      setOrgs(MOCK_ORGS);
      return;
    }
    setLoading(true);
    try {
      const response = await apiClient.get<{ organizations: Organization[]; total: number }>("/organizations/");
      setOrgs(Array.isArray(response.data) ? response.data : response.data.organizations ?? []);
    } catch (err: any) {
      console.error("Failed to fetch organizations:", err);
      toast.error(err?.response?.data?.detail || "Failed to load organizations");
    } finally {
      setLoading(false);
    }
  }, [testMode]);

  useEffect(() => { if (!testMode) fetchOrgs(); }, [fetchOrgs, testMode]);

  const fetchOrgMembers = useCallback(async (orgId: number) => {
    try {
      const response = await apiClient.get<{ members: OrgMember[] }>(`/organizations/${orgId}/members`);
      setOrgs((prev) => prev.map((o) => o.id === orgId ? { ...o, members: response.data.members } : o));
    } catch (err: any) {
      console.error("Failed to fetch members:", err);
      toast.error(err?.response?.data?.detail || "Failed to load members");
    }
  }, []);

  const toggleExpand = (orgId: number) => {
    if (expandedOrgId === orgId) {
      setExpandedOrgId(null);
    } else {
      setExpandedOrgId(orgId);
      const org = orgs.find((o) => o.id === orgId);
      if (!org?.members) {
        fetchOrgMembers(orgId);
      }
    }
  };

  const handleCreateOrg = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newOrgName.trim()) return;
    setCreatingOrg(true);
    try {
      const response = await apiClient.post<Organization>("/organizations/", {
        name: newOrgName.trim(),
        description: newOrgDescription.trim() || "",
      });
      setOrgs((prev) => [...prev, response.data]);
      toast.success("Organization created successfully");
      setCreateDialogOpen(false);
      setNewOrgName("");
      setNewOrgDescription("");
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to create organization");
    } finally {
      setCreatingOrg(false);
    }
  };

  const handleDeleteOrg = async () => {
    if (!orgToDelete) return;
    try {
      await apiClient.delete(`/organizations/${orgToDelete.id}`);
      setOrgs((prev) => prev.filter((o) => o.id !== orgToDelete.id));
      toast.success("Organization deleted successfully");
      setDeleteDialogOpen(false);
      setOrgToDelete(null);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to delete organization");
    }
  };

  const handleAddMember = async (e: React.FormEvent, orgId: number) => {
    e.preventDefault();
    if (!selectedUser) { toast.error("Please search and select a user"); return; }
    setAddingMember(true);
    setOrgForMemberAction(orgId);
    try {
      await apiClient.post(`/organizations/${orgId}/members`, { user_id: selectedUser.id, role: newMemberRole });
      toast.success(`${selectedUser.full_name || selectedUser.username} added to organization`);
      setSelectedUser(null);
      setUserSearchQuery("");
      setUserSearchResults([]);
      setNewMemberRole("member");
      fetchOrgMembers(orgId);
      setOrgs((prev) => prev.map((o) => o.id === orgId ? { ...o, member_count: o.member_count + 1 } : o));
    } catch (err: any) {
      const detail = err?.response?.data?.detail || "Failed to add member";
      toast.error(detail);
    } finally {
      setAddingMember(false);
      setOrgForMemberAction(null);
    }
  };

  const handleRoleChange = async (orgId: number, userId: number, newRole: OrgRole) => {
    setUpdatingMemberId(userId);
    try {
      await apiClient.patch(`/organizations/${orgId}/members/${userId}`, { role: newRole });
      setOrgs((prev) => prev.map((o) => {
        if (o.id !== orgId || !o.members) return o;
        return {
          ...o,
          members: o.members.map((m) => m.user_id === userId ? { ...m, role: newRole } : m),
        };
      }));
      toast.success("Role updated successfully");
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to update role");
    } finally {
      setUpdatingMemberId(null);
    }
  };

  const handleRemoveMember = async () => {
    if (!memberToRemove || !orgForMemberAction) return;
    try {
      await apiClient.delete(`/organizations/${orgForMemberAction}/members/${memberToRemove.user_id}`);
      setOrgs((prev) => prev.map((o) => {
        if (o.id !== orgForMemberAction || !o.members) return o;
        return {
          ...o,
          members: o.members.filter((m) => m.user_id !== memberToRemove.user_id),
          member_count: o.member_count - 1,
        };
      }));
      toast.success("Member removed from organization");
      setRemoveMemberDialogOpen(false);
      setMemberToRemove(null);
      setOrgForMemberAction(null);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to remove member");
    }
  };

  const handleTransferOwnership = async () => {
    if (!transferOrgId || !transferTargetId) return;
    setTransferring(true);
    try {
      await apiClient.post(`/organizations/${transferOrgId}/transfer-ownership`, { new_owner_user_id: transferTargetId });
      toast.success("Ownership transferred successfully");
      setTransferDialogOpen(false);
      setTransferOrgId(null);
      setTransferTargetId(null);
      fetchOrgMembers(transferOrgId);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to transfer ownership");
    } finally {
      setTransferring(false);
    }
  };

  const formatDate = (dateStr: string) => new Date(dateStr).toLocaleDateString();

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="Organizations"
          description="Manage organizations and their members"
        />
        {!isMember && (
          <Button onClick={() => setCreateDialogOpen(true)}>
            <Plus className="w-4 h-4 mr-2" />Create Organization
          </Button>
        )}
      </div>

      {loading ? (
        <LoadingSpinner label="Loading organizations…" />
      ) : orgs.length === 0 ? (
        <EmptyState
          icon={Building2}
          title="No organizations found"
          description="Create one to get started."
        />
      ) : (
        <div className="space-y-4">
          {orgs.map((org) => (
            <Card key={org.id} className="overflow-hidden">
              <CardHeader className="pb-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-sm bg-primary/10 flex items-center justify-center">
                      <Building2 className="w-5 h-5 text-primary" />
                    </div>
                    <div>
                      <CardTitle className="text-lg">{org.name}</CardTitle>
                      {org.description && (
                        <CardDescription>{org.description}</CardDescription>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary" className="flex items-center gap-1">
                      <Users className="w-3 h-3" />{org.member_count}
                    </Badge>
                    <Badge variant="secondary" className="flex items-center gap-1">
                      <Vault className="w-3 h-3" />{org.vault_count}
                    </Badge>
                    <Button variant="ghost" size="icon" onClick={() => toggleExpand(org.id)} aria-label={expandedOrgId === org.id ? "Collapse" : "Expand"}>
                      {expandedOrgId === org.id ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                    </Button>
                    {!isMember && (
                      <Button variant="destructive" size="icon" onClick={() => { setOrgToDelete(org); setDeleteDialogOpen(true); }} aria-label={`Delete organization ${org.name}`}>
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    )}
                  </div>
                </div>
              </CardHeader>
              {expandedOrgId === org.id && (
                <CardContent className="border-t pt-4">
                  {!isMember && (
                  <form onSubmit={(e) => handleAddMember(e, org.id)} className="flex gap-2 items-end mb-4">
                    <div className="flex-1 space-y-2 relative w-2/3" ref={userSearchRef}>
                      <Label htmlFor={`org-member-search-${org.id}`}>Search User</Label>
                      <div className="relative">
                        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                        <Input
                          id={`org-member-search-${org.id}`}
                          placeholder="Search by name or username..."
                          value={userSearchQuery}
                          onChange={(e) => handleUserSearchChange(e.target.value)}
                          onFocus={() => { if (userSearchResults.length > 0) setShowUserDropdown(true); }}
                          disabled={addingMember && orgForMemberAction === org.id}
                          aria-label="Search for user to add as organization member"
                          className="pl-10 w-full"
                          autoComplete="off"
                        />
                        {searchingUsers && (
                          <div className="absolute right-3 top-1/2 -translate-y-1/2">
                            <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
                          </div>
                        )}
                      </div>
                      {showUserDropdown && userSearchResults.length > 0 && (
                        <div className="absolute z-50 top-full left-0 right-0 mt-1 bg-popover border rounded-sm shadow-md max-h-48 overflow-y-auto">
                          {userSearchResults.map((u) => {
                            const alreadyMember = org.members?.some((m) => m.user_id === u.id);
                            return (
                              <button
                                key={u.id}
                                type="button"
                                className={`w-full text-left px-3 py-2 text-sm hover:bg-accent hover:text-accent-foreground flex items-center justify-between ${alreadyMember ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}
                                onClick={() => { if (!alreadyMember) selectUser(u); }}
                                disabled={alreadyMember}
                              >
                                <div>
                                  <span className="font-medium">{u.full_name || u.username}</span>
                                  <span className="text-muted-foreground ml-2">@{u.username}</span>
                                </div>
                                {alreadyMember && <Badge variant="secondary" className="text-xs">Already member</Badge>}
                              </button>
                            );
                          })}
                        </div>
                      )}
                      {showUserDropdown && userSearchQuery.trim() && !searchingUsers && userSearchResults.length === 0 && (
                        <div className="absolute z-50 top-full left-0 right-0 mt-1 bg-popover border rounded-sm shadow-md px-3 py-2 text-sm text-muted-foreground">
                          No users found
                        </div>
                      )}
                    </div>
                    <div className="space-y-2 w-1/3">
                      <Label htmlFor={`org-member-role-${org.id}`}>Role</Label>
                      <Select
                        value={newMemberRole}
                        onValueChange={(v) => setNewMemberRole(v as OrgRole)}
                        disabled={addingMember && orgForMemberAction === org.id}
                      >
                        <SelectTrigger id={`org-member-role-${org.id}`} aria-label="Role for new member">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {ROLE_OPTIONS.map((opt) => (
                            <SelectItem key={opt.value} value={opt.value}>
                              {opt.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <Button type="submit" disabled={(addingMember && orgForMemberAction === org.id) || !selectedUser}>
                      {addingMember && orgForMemberAction === org.id ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <UserPlus className="w-4 h-4 mr-2" />}
                      Add
                    </Button>
                  </form>
                  )}

                  <Table>
                    <TableCaption className="sr-only">Organization Members</TableCaption>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="text-left py-2">User</TableHead>
                        <TableHead className="text-left py-2">Role</TableHead>
                        <TableHead className="text-left py-2">Joined</TableHead>
                        <TableHead className="text-right py-2">Actions</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {!org.members ? (
                        <TableRow><TableCell colSpan={4} className="py-8 text-center"><Loader2 className="w-6 h-6 animate-spin mx-auto text-muted-foreground" role="status" aria-live="polite" /><span className="sr-only">Loading members</span></TableCell></TableRow>
                      ) : org.members.length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={4} className="py-8">
                            <EmptyState
                              title="No members yet"
                              className="py-0"
                            />
                          </TableCell>
                        </TableRow>
                      ) : (
                        org.members.map((member) => (
                          <TableRow key={member.user_id}>
                            <TableCell className="py-3">
                              <div>
                                <div className="font-medium">{member.full_name || member.username}</div>
                                <div className="text-sm text-muted-foreground">@{member.username}</div>
                              </div>
                            </TableCell>
                            <TableCell className="py-3">
                              {member.role === "owner" ? (
                                <Badge variant="default" className="text-xs">Owner</Badge>
                              ) : (
                                <select value={member.role} onChange={(e) => handleRoleChange(org.id, member.user_id, e.target.value as OrgRole)} disabled={updatingMemberId === member.user_id} aria-label={`Change role for ${member.username}`} className="h-8 rounded-sm border border-input bg-background px-2 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50">
                                  {CHANGEABLE_ROLE_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{opt.label}</option>))}
                                </select>
                              )}
                            </TableCell>
                            <TableCell className="py-3 text-muted-foreground text-sm">{formatDate(member.joined_at)}</TableCell>
                            <TableCell className="py-3 text-right flex items-center justify-end gap-1">
                              {member.role !== "owner" && (isSuperAdmin || org.members?.some((m) => m.user_id === currentUser?.id && m.role === "owner")) && (
                                <Button variant="ghost" size="sm" className="text-xs h-7 px-2" onClick={() => { setTransferOrgId(org.id); setTransferTargetId(member.user_id); setTransferDialogOpen(true); }} aria-label={`Transfer ownership to ${member.username}`} title="Transfer Ownership">
                                  Transfer
                                </Button>
                              )}
                              <Button variant="destructive" size="icon" onClick={() => { setMemberToRemove(member); setOrgForMemberAction(org.id); setRemoveMemberDialogOpen(true); }} aria-label={`Remove ${member.username} from organization`} disabled={member.role === "owner"} title={member.role === "owner" ? "Cannot remove owner — transfer ownership first" : "Remove member"}>
                                <UserX className="w-4 h-4" />
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))
                      )}
                    </TableBody>
                  </Table>
                </CardContent>
              )}
            </Card>
          ))}
        </div>
      )}

      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent aria-labelledby="create-org-title" aria-describedby="create-org-desc">
          <form onSubmit={handleCreateOrg}>
            <DialogHeader>
              <DialogTitle id="create-org-title" className="flex items-center gap-2">
                <Building2 className="w-5 h-5" />Create Organization
              </DialogTitle>
              <DialogDescription id="create-org-desc">Create a new organization to group users and vaults.</DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label htmlFor="org-name">Name</Label>
                <Input id="org-name" placeholder="Organization name..." value={newOrgName} onChange={(e) => setNewOrgName(e.target.value)} disabled={creatingOrg} required aria-label="Organization name" />
              </div>
              <div className="space-y-2">
                <Label htmlFor="org-description">Description</Label>
                <Input id="org-description" placeholder="Description (optional)..." value={newOrgDescription} onChange={(e) => setNewOrgDescription(e.target.value)} disabled={creatingOrg} aria-label="Organization description" />
              </div>
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setCreateDialogOpen(false)} disabled={creatingOrg}>Cancel</Button>
              <Button type="submit" disabled={creatingOrg || !newOrgName.trim()}>
                {creatingOrg && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
                Create
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent aria-labelledby="delete-org-title" aria-describedby="delete-org-desc">
          <DialogHeader>
            <DialogTitle id="delete-org-title" className="flex items-center gap-2">
              <Trash2 className="w-5 h-5 text-destructive" />Delete Organization
            </DialogTitle>
            <DialogDescription id="delete-org-desc">Are you sure you want to delete <strong>{orgToDelete?.name}</strong>? This action cannot be undone.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>Cancel</Button>
            <Button variant="destructive" onClick={handleDeleteOrg}>Delete Organization</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={removeMemberDialogOpen} onOpenChange={setRemoveMemberDialogOpen}>
        <DialogContent aria-labelledby="remove-member-title" aria-describedby="remove-member-desc">
          <DialogHeader>
            <DialogTitle id="remove-member-title" className="flex items-center gap-2">
              <UserX className="w-5 h-5 text-destructive" />Remove Member
            </DialogTitle>
            <DialogDescription id="remove-member-desc">Are you sure you want to remove <strong>{memberToRemove?.full_name || memberToRemove?.username}</strong> from this organization?</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => { setRemoveMemberDialogOpen(false); setMemberToRemove(null); setOrgForMemberAction(null); }}>Cancel</Button>
            <Button variant="destructive" onClick={handleRemoveMember}>Remove</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={transferDialogOpen} onOpenChange={setTransferDialogOpen}>
        <DialogContent aria-labelledby="transfer-title" aria-describedby="transfer-desc">
          <DialogHeader>
            <DialogTitle id="transfer-title">Transfer Ownership</DialogTitle>
            <DialogDescription id="transfer-desc">
              Transfer organization ownership to the selected member. You will become an admin after the transfer.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => { setTransferDialogOpen(false); setTransferOrgId(null); setTransferTargetId(null); }} disabled={transferring}>Cancel</Button>
            <Button onClick={handleTransferOwnership} disabled={transferring}>
              {transferring && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
              Transfer Ownership
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function OrgsPage() {
  return (<RoleGuard allowedRoles={["member", "admin", "superadmin"]}><OrgsPageContent /></RoleGuard>);
}

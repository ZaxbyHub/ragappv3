import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import { AdminGuard } from "@/components/auth/RoleGuard";
import { useAuthStore } from "@/stores/useAuthStore";
import apiClient from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockAdminUsers } from "@/fixtures/users";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Search,
  Trash2,
  Loader2,
  UserX,
  Users,
  Pencil,
  KeyRound,
  Plus,
  Building2,
  ChevronUp,
  ChevronDown,
} from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { LoadingSpinner } from "@/components/LoadingSpinner";

type UserRole = "superadmin" | "admin" | "member" | "viewer";

interface User {
  id: number;
  username: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
  created_at: string;
}

interface Group {
  id: number;
  name: string;
  description: string | null;
}

interface OrgItem {
  id: number;
  name: string;
  description: string;
  role?: string;
  joined_at?: string;
}

const ROLE_OPTIONS: { value: UserRole; label: string }[] = [
  { value: "superadmin", label: "Super Admin" },
  { value: "admin", label: "Admin" },
  { value: "member", label: "Member" },
  { value: "viewer", label: "Viewer" },
];

function AdminUsersPageContent() {
  const testMode = useTestMode();
  const [users, setUsers] = useState<User[]>(testMode ? mockAdminUsers : []);
  const [loading, setLoading] = useState(!testMode);
  const [searchQuery, setSearchQuery] = useState("");
  const [updatingUserId, setUpdatingUserId] = useState<number | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [userToDelete, setUserToDelete] = useState<User | null>(null);

  // Edit User Dialog State
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [userToEdit, setUserToEdit] = useState<User | null>(null);
  const [editFullName, setEditFullName] = useState("");
  const [editRole, setEditRole] = useState<UserRole>("member");
  const [isSavingEdit, setIsSavingEdit] = useState(false);

  // Password Reset Dialog State
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [userToResetPassword, setUserToResetPassword] = useState<User | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isResettingPassword, setIsResettingPassword] = useState(false);
  const [mustChangePassword, setMustChangePassword] = useState(false);

  // Manage Groups Sheet State
  const [groupsSheetOpen, setGroupsSheetOpen] = useState(false);
  const [userForGroups, setUserForGroups] = useState<User | null>(null);
  const [allGroups, setAllGroups] = useState<Group[]>([]);
  const [selectedGroupIds, setSelectedGroupIds] = useState<number[]>([]);
  const [groupsSearchQuery, setGroupsSearchQuery] = useState("");
  const [isLoadingGroups, setIsLoadingGroups] = useState(false);
  const [isSavingGroups, setIsSavingGroups] = useState(false);

  // Manage Organizations Sheet State
  const [orgsSheetOpen, setOrgsSheetOpen] = useState(false);
  const [userForOrgs, setUserForOrgs] = useState<User | null>(null);
  const [allOrgs, setAllOrgs] = useState<OrgItem[]>([]);
  const [orgMemberships, setOrgMemberships] = useState<Map<number, string>>(new Map()); // org_id → role
  const [orgsSearchQuery, setOrgsSearchQuery] = useState("");
  const [isLoadingOrgs, setIsLoadingOrgs] = useState(false);
  const [isSavingOrgs, setIsSavingOrgs] = useState(false);

  // Create User Dialog State
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [createUsername, setCreateUsername] = useState("");
  const [createFullName, setCreateFullName] = useState("");
  const [createPassword, setCreatePassword] = useState("");
  const [createRole, setCreateRole] = useState<UserRole>("member");
  const [isCreatingUser, setIsCreatingUser] = useState(false);

  const currentUser = useAuthStore((state) => state.user);

  const fetchUsers = useCallback(async () => {
    if (testMode) {
      setUsers(mockAdminUsers);
      return;
    }
    setLoading(true);
    try {
      const response = await apiClient.get<{ users: User[]; total: number }>("/users/");
      setUsers(response.data.users);
    } catch (err) {
      console.error("Failed to fetch users:", err);
      toast.error("Failed to load users");
    } finally {
      setLoading(false);
    }
  }, [testMode]);

  useEffect(() => {
    if (!testMode) fetchUsers();
  }, [fetchUsers, testMode]);

  const handleRoleChange = async (userId: number, newRole: UserRole) => {
    setUpdatingUserId(userId);
    try {
      await apiClient.patch(`/users/${userId}/role`, { role: newRole });
      setUsers((prev) => prev.map((u) => (u.id === userId ? { ...u, role: newRole } : u)));
      toast.success("Role updated successfully");
    } catch (err) {
      toast.error("Failed to update role");
    } finally {
      setUpdatingUserId(null);
    }
  };

  const handleActiveToggle = async (userId: number, isActive: boolean) => {
    setUpdatingUserId(userId);
    try {
      await apiClient.patch(`/users/${userId}/active`, { is_active: isActive });
      setUsers((prev) => prev.map((u) => (u.id === userId ? { ...u, is_active: isActive } : u)));
      toast.success(`User ${isActive ? "activated" : "deactivated"} successfully`);
    } catch (err) {
      toast.error("Failed to update user status");
    } finally {
      setUpdatingUserId(null);
    }
  };

  const handleDelete = async () => {
    if (!userToDelete) return;
    try {
      await apiClient.delete(`/users/${userToDelete.id}`);
      setUsers((prev) => prev.filter((u) => u.id !== userToDelete.id));
      toast.success("User deleted successfully");
      setDeleteDialogOpen(false);
      setUserToDelete(null);
    } catch (err) {
      toast.error("Failed to delete user");
    }
  };

  // Edit User Handlers
  const openEditDialog = (user: User) => {
    setUserToEdit(user);
    setEditFullName(user.full_name);
    setEditRole(user.role);
    setEditDialogOpen(true);
  };

  const closeEditDialog = () => {
    setEditDialogOpen(false);
    setUserToEdit(null);
    setEditFullName("");
    setEditRole("member");
  };

  const handleSaveEdit = async () => {
    if (!userToEdit) return;
    setIsSavingEdit(true);
    try {
      await apiClient.patch(`/users/${userToEdit.id}`, {
        full_name: editFullName,
        role: editRole,
      });
      setUsers((prev) =>
        prev.map((u) =>
          u.id === userToEdit.id ? { ...u, full_name: editFullName, role: editRole } : u
        )
      );
      toast.success("User updated successfully");
      closeEditDialog();
    } catch (err) {
      toast.error("Failed to update user");
    } finally {
      setIsSavingEdit(false);
    }
  };

  // Password Reset Handlers
  const openPasswordDialog = (user: User) => {
    setUserToResetPassword(user);
    setNewPassword("");
    setConfirmPassword("");
    setMustChangePassword(false);
    setPasswordDialogOpen(true);
  };

  const closePasswordDialog = () => {
    setPasswordDialogOpen(false);
    setUserToResetPassword(null);
    setNewPassword("");
    setConfirmPassword("");
    setMustChangePassword(false);
  };

  const handleResetPassword = async () => {
    if (!userToResetPassword) return;
    if (newPassword !== confirmPassword) {
      toast.error("Passwords do not match");
      return;
    }
    if (newPassword.length < 8) {
      toast.error("Password must be at least 8 characters");
      return;
    }
    setIsResettingPassword(true);
    try {
      const response = await apiClient.patch(`/users/${userToResetPassword.id}/password`, {
        new_password: newPassword,
      });
      setMustChangePassword(response.data.must_change_password ?? true);
      toast.success("Password reset successfully");
      closePasswordDialog();
    } catch (err) {
      toast.error("Failed to reset password");
    } finally {
      setIsResettingPassword(false);
    }
  };

  // Manage Groups Handlers
  const fetchAllGroups = async () => {
    try {
      const response = await apiClient.get<{ groups: Group[] }>("/groups");
      setAllGroups(response.data.groups);
    } catch (err) {
      console.error("Failed to fetch groups:", err);
      toast.error("Failed to load groups");
    }
  };

  const fetchUserGroups = async (userId: number) => {
    try {
      const response = await apiClient.get<{ groups: Group[] }>(`/users/${userId}/groups`);
      setSelectedGroupIds(response.data.groups.map((g) => g.id));
    } catch (err) {
      console.error("Failed to fetch user groups:", err);
      toast.error("Failed to load user groups");
    }
  };

  const openGroupsSheet = async (user: User) => {
    setUserForGroups(user);
    setGroupsSheetOpen(true);
    setIsLoadingGroups(true);
    setGroupsSearchQuery("");
    await Promise.all([fetchAllGroups(), fetchUserGroups(user.id)]);
    setIsLoadingGroups(false);
  };

  const closeGroupsSheet = () => {
    setGroupsSheetOpen(false);
    setUserForGroups(null);
    setAllGroups([]);
    setSelectedGroupIds([]);
    setGroupsSearchQuery("");
  };

  const toggleGroup = useCallback((groupId: number) => {
    setSelectedGroupIds((prev) =>
      prev.includes(groupId) ? prev.filter((id) => id !== groupId) : [...prev, groupId]
    );
  }, []);

  const handleSaveGroups = async () => {
    if (!userForGroups) return;
    setIsSavingGroups(true);
    try {
      await apiClient.put(`/users/${userForGroups.id}/groups`, {
        group_ids: selectedGroupIds,
      });
      toast.success("Groups updated successfully");
      closeGroupsSheet();
    } catch (err) {
      toast.error("Failed to update groups");
    } finally {
      setIsSavingGroups(false);
    }
  };

  // Manage Organizations Handlers
  const fetchAllOrgs = async () => {
    try {
      const response = await apiClient.get<{ organizations: OrgItem[]; total: number }>("/organizations/");
      setAllOrgs(Array.isArray(response.data) ? response.data : response.data.organizations ?? []);
    } catch (err) {
      console.error("Failed to fetch organizations:", err);
      toast.error("Failed to load organizations");
    }
  };

  const fetchUserOrgs = async (userId: number) => {
    try {
      const response = await apiClient.get<{ organizations: OrgItem[] }>(`/users/${userId}/organizations`);
      const map = new Map<number, string>();
      for (const o of response.data.organizations) {
        map.set(o.id, o.role || "member");
      }
      setOrgMemberships(map);
    } catch (err) {
      console.error("Failed to fetch user organizations:", err);
      toast.error("Failed to load user organizations");
    }
  };

  const openOrgsSheet = async (user: User) => {
    setUserForOrgs(user);
    setOrgsSheetOpen(true);
    setIsLoadingOrgs(true);
    setOrgsSearchQuery("");
    await Promise.all([fetchAllOrgs(), fetchUserOrgs(user.id)]);
    setIsLoadingOrgs(false);
  };

  const closeOrgsSheet = () => {
    setOrgsSheetOpen(false);
    setUserForOrgs(null);
    setAllOrgs([]);
    setOrgMemberships(new Map());
    setOrgsSearchQuery("");
  };

  const toggleOrg = useCallback((orgId: number) => {
    setOrgMemberships((prev) => {
      const next = new Map(prev);
      if (next.has(orgId)) {
        next.delete(orgId);
      } else {
        next.set(orgId, "member");
      }
      return next;
    });
  }, []);

  const setOrgRole = useCallback((orgId: number, role: string) => {
    setOrgMemberships((prev) => {
      const next = new Map(prev);
      next.set(orgId, role);
      return next;
    });
  }, []);

  const handleSaveOrgs = async () => {
    if (!userForOrgs) return;
    setIsSavingOrgs(true);
    try {
      const memberships = Array.from(orgMemberships.entries()).map(([org_id, role]) => ({ org_id, role }));
      await apiClient.put(`/users/${userForOrgs.id}/organizations`, { memberships });
      toast.success("Organizations updated successfully");
      closeOrgsSheet();
    } catch (err) {
      toast.error("Failed to update organizations");
    } finally {
      setIsSavingOrgs(false);
    }
  };

  const filteredOrgs = allOrgs.filter((org) => {
    const searchLower = orgsSearchQuery.toLowerCase();
    return (
      org.name.toLowerCase().includes(searchLower) ||
      (org.description && org.description.toLowerCase().includes(searchLower))
    );
  });

const handleCreateUser = async () => {
 if (!createUsername.trim() || createUsername.length < 3) {
 toast.error("Username must be at least 3 characters");
 return;
 }
 // Validate password
 if (createPassword.length < 8) {
 toast.error("Password must be at least 8 characters");
 return;
 }
 if (!/[A-Z]/.test(createPassword)) {
 toast.error("Password must contain at least 1 uppercase letter");
 return;
 }
 if (!/\d/.test(createPassword)) {
 toast.error("Password must contain at least 1 digit");
 return;
 }
 if (!createPassword.trim()) {
 toast.error("Password cannot be only whitespace");
 return;
 }
 setIsCreatingUser(true);
    try {
      await apiClient.post("/users/", {
        username: createUsername.trim(),
        password: createPassword,
        full_name: createFullName.trim(),
        role: createRole,
      });
      toast.success(`User "${createUsername.trim()}" created successfully`);
      setCreateDialogOpen(false);
      setCreateUsername("");
      setCreateFullName("");
      setCreatePassword("");
      setCreateRole("member");
      fetchUsers();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create user";
      toast.error(message);
    } finally {
      setIsCreatingUser(false);
    }
  };

  const filteredGroups = allGroups.filter((group) => {
    const searchLower = groupsSearchQuery.toLowerCase();
    return (
      group.name.toLowerCase().includes(searchLower) ||
      (group.description && group.description.toLowerCase().includes(searchLower))
    );
  });

  const filteredUsers = (users ?? []).filter(
    (u) =>
      u.username.toLowerCase().includes(searchQuery.toLowerCase()) ||
      u.full_name.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const formatDate = (dateStr: string) => new Date(dateStr).toLocaleDateString();
  const isSuperAdmin = currentUser?.role === "superadmin";
  const canDeleteUser = (user: User) => isSuperAdmin && user.id !== currentUser?.id;
  const canManageUser = (user: User) => user.id !== currentUser?.id;

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="User Management"
          description="Manage system users and their permissions"
        />
        <Button onClick={() => setCreateDialogOpen(true)}>
          <Plus className="mr-2 h-4 w-4" />
          Add User
        </Button>
      </div>
      <div className="space-y-4">
        {/* Search */}
        <div className="relative">
          <Search
            className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            placeholder="Search by username or name..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-10 w-1/2"
            aria-label="Search users"
          />
        </div>

        {/* Users Table */}
        <div className="rounded-sm border">
          <Table>
            <TableCaption className="sr-only">System Users</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead className="text-left p-4">
                  <button type="button" className="flex items-center gap-1 font-medium hover:text-foreground">
                    Username
                  </button>
                </TableHead>
                <TableHead className="text-left p-4">
                  <button type="button" className="flex items-center gap-1 font-medium hover:text-foreground">
                    Full Name
                  </button>
                </TableHead>
                <TableHead className="text-left p-4">Role</TableHead>
                <TableHead className="text-left p-4">Status</TableHead>
                <TableHead className="text-left p-4">
                  <button type="button" className="flex items-center gap-1 font-medium hover:text-foreground">
                    Created
                    <span className="inline-flex flex-col">
                      <ChevronUp className="h-3 w-3 text-muted-foreground/30" aria-hidden="true" />
                      <ChevronDown className="h-3 w-3 -mt-1 text-muted-foreground/30" aria-hidden="true" />
                    </span>
                  </button>
                </TableHead>
                <TableHead className="text-right p-4">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow>
                  <TableCell colSpan={6} className="p-8">
                    <LoadingSpinner label="Loading users…" />
                  </TableCell>
                </TableRow>
              ) : filteredUsers.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="p-8">
                    <EmptyState
                      icon={Users}
                      title={searchQuery ? "No users match your search" : "No users found"}
                      className="py-0"
                    />
                  </TableCell>
                </TableRow>
              ) : (
                filteredUsers.map((user) => (
                  <TableRow key={user.id}>
                    <TableCell className="p-4 font-medium">{user.username}</TableCell>
                    <TableCell className="p-4">{user.full_name}</TableCell>
                    <TableCell className="p-4">
                      <Select
                        value={user.role}
                        onValueChange={(v) => handleRoleChange(user.id, v as UserRole)}
                        disabled={updatingUserId === user.id || user.id === currentUser?.id}
                      >
                        <SelectTrigger aria-label={`Change role for ${user.username}`}>
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
                    </TableCell>
                    <TableCell className="p-4">
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          role="switch"
                          aria-checked={user.is_active}
                          aria-label={`${user.is_active ? "Deactivate" : "Activate"} user ${user.username}`}
                          onClick={() => handleActiveToggle(user.id, !user.is_active)}
                          disabled={updatingUserId === user.id || user.id === currentUser?.id}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
                            user.is_active ? "bg-primary" : "bg-input"
                          }`}
                        >
                          <span
                            className={`inline-block h-5 w-5 rounded-full bg-background shadow transition-transform ${
                              user.is_active ? "translate-x-5" : "translate-x-0.5"
                            }`}
                          />
                        </button>
                        <Badge variant={user.is_active ? "default" : "secondary"}>
                          {user.is_active ? "Active" : "Inactive"}
                        </Badge>
                      </div>
                    </TableCell>
                    <TableCell className="p-4 text-muted-foreground">{formatDate(user.created_at)}</TableCell>
                    <TableCell className="p-4 text-right">
                      <div className="flex items-center justify-end gap-1">
                        {canManageUser(user) && (
                          <>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              onClick={() => openOrgsSheet(user)}
                              aria-label={`Manage organizations for ${user.username}`}
                              title="Manage Organizations"
                            >
                              <Building2 className="w-4 h-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              onClick={() => openGroupsSheet(user)}
                              aria-label={`Manage groups for ${user.username}`}
                              title="Manage Groups"
                            >
                              <Users className="w-4 h-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              onClick={() => openEditDialog(user)}
                              aria-label={`Edit user ${user.username}`}
                              title="Edit User"
                            >
                              <Pencil className="w-4 h-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              onClick={() => openPasswordDialog(user)}
                              aria-label={`Reset password for ${user.username}`}
                              title="Reset Password"
                            >
                              <KeyRound className="w-4 h-4" />
                            </Button>
                          </>
                        )}
                        {canDeleteUser(user) && (
                          <Button
                            variant="destructive"
                            size="icon"
                            className="h-8 w-8"
                            onClick={() => {
                              setUserToDelete(user);
                              setDeleteDialogOpen(true);
                            }}
                            aria-label={`Delete user ${user.username}`}
                          >
                            <Trash2 className="w-4 h-4" />
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      </div>

      {/* Delete User Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent aria-labelledby="delete-title" aria-describedby="delete-desc">
          <DialogHeader>
            <DialogTitle id="delete-title" className="flex items-center gap-2">
              <UserX className="w-5 h-5 text-destructive" />
              Delete User
            </DialogTitle>
            <DialogDescription id="delete-desc">
              Are you sure you want to delete <strong>{userToDelete?.username}</strong>? This action
              cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              Delete User
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit User Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent aria-labelledby="edit-title" aria-describedby="edit-desc">
          <DialogHeader>
            <DialogTitle id="edit-title" className="flex items-center gap-2">
              <Pencil className="w-5 h-5" />
              Edit User
            </DialogTitle>
            <DialogDescription id="edit-desc">
              Update user details for <strong>{userToEdit?.username}</strong>.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="edit-username">Username</Label>
              <Input
                id="edit-username"
                value={userToEdit?.username ?? ""}
                disabled
                aria-readonly="true"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-fullname">Full Name</Label>
              <Input
                id="edit-fullname"
                value={editFullName}
                onChange={(e) => setEditFullName(e.target.value)}
                placeholder="Enter full name"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-role">Role</Label>
              <Select
                value={editRole}
                onValueChange={(v) => setEditRole(v as UserRole)}
              >
                <SelectTrigger id="edit-role">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ROLE_OPTIONS.filter((r) => r.value !== "superadmin" || isSuperAdmin).map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeEditDialog} disabled={isSavingEdit}>
              Cancel
            </Button>
            <Button onClick={handleSaveEdit} disabled={isSavingEdit}>
              {isSavingEdit ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Saving...
                </>
              ) : (
                "Save Changes"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Password Reset Dialog */}
      <Dialog open={passwordDialogOpen} onOpenChange={setPasswordDialogOpen}>
        <DialogContent aria-labelledby="password-title" aria-describedby="password-desc">
          <DialogHeader>
            <DialogTitle id="password-title" className="flex items-center gap-2">
              <KeyRound className="w-5 h-5" />
              Reset Password
            </DialogTitle>
            <DialogDescription id="password-desc">
              Set a new password for <strong>{userToResetPassword?.username}</strong>.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="new-password">New Password</Label>
              <Input
                id="new-password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder="Enter new password"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-password">Confirm Password</Label>
              <Input
                id="confirm-password"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="Confirm new password"
              />
            </div>
            {mustChangePassword && (
              <p className="text-sm text-muted-foreground">
                User will be required to change their password on next login.
              </p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closePasswordDialog} disabled={isResettingPassword}>
              Cancel
            </Button>
            <Button onClick={handleResetPassword} disabled={isResettingPassword}>
              {isResettingPassword ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Resetting...
                </>
              ) : (
                "Reset Password"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Manage Groups Sheet */}
      <Sheet open={groupsSheetOpen} onOpenChange={setGroupsSheetOpen}>
        <SheetContent
          className="sm:max-w-[400px] flex flex-col"
          aria-labelledby="groups-title"
          aria-describedby="groups-desc"
        >
          <SheetHeader>
            <SheetTitle id="groups-title" className="flex items-center gap-2">
              <Users className="h-5 w-5" aria-hidden="true" />
              Manage Groups
            </SheetTitle>
            <SheetDescription id="groups-desc">
              Manage group memberships for <strong>{userForGroups?.username}</strong>. Select groups
              to add or remove from this user.
            </SheetDescription>
          </SheetHeader>

          <div className="flex-1 flex flex-col py-4 min-h-0">
            {/* Search Input */}
            <div className="relative mb-4">
              <Search
                className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                placeholder="Search groups..."
                value={groupsSearchQuery}
                onChange={(e) => setGroupsSearchQuery(e.target.value)}
                className="pl-10"
                aria-label="Search groups"
                disabled={isLoadingGroups}
              />
            </div>

            {/* Groups List */}
            <ScrollArea className="flex-1 -mx-6 px-6">
              {isLoadingGroups ? (
                <div className="space-y-3">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="flex items-center gap-3 p-3 rounded-sm border">
                      <Skeleton className="h-4 w-4" />
                      <div className="flex-1 space-y-2">
                        <Skeleton className="h-4 w-32" />
                        <Skeleton className="h-3 w-24" />
                      </div>
                    </div>
                  ))}
                </div>
              ) : filteredGroups.length === 0 ? (
                <div
                  className="text-center py-8 text-muted-foreground"
                  role="status"
                  aria-live="polite"
                >
                  {groupsSearchQuery ? "No groups match your search" : "No groups available"}
                </div>
              ) : (
                <div className="space-y-2 pr-4">
                  {filteredGroups.map((group) => (
                    <div
                      key={group.id}
                      className="flex items-start space-x-3 rounded-sm border p-3 hover:bg-muted/50 transition-colors"
                    >
                      <Checkbox
                        id={`group-${group.id}`}
                        checked={selectedGroupIds.includes(group.id)}
                        onCheckedChange={() => toggleGroup(group.id)}
                        aria-label={`Select ${group.name}`}
                        disabled={isSavingGroups}
                      />
                      <Label
                        htmlFor={`group-${group.id}`}
                        className="flex-1 cursor-pointer space-y-1"
                      >
                        <div className="font-medium">{group.name}</div>
                        {group.description && (
                          <div className="text-sm text-muted-foreground">{group.description}</div>
                        )}
                      </Label>
                    </div>
                  ))}
                </div>
              )}
            </ScrollArea>

            {/* Selected Count */}
            <div className="mt-4 text-sm text-muted-foreground">
              {selectedGroupIds.length} group{selectedGroupIds.length !== 1 ? "s" : ""} selected
            </div>
          </div>

          <SheetFooter className="flex-col gap-2 sm:flex-row border-t pt-4">
            <Button
              type="button"
              variant="outline"
              onClick={closeGroupsSheet}
              disabled={isSavingGroups}
              className="w-full sm:w-auto"
            >
              Cancel
            </Button>
            <Button
              onClick={handleSaveGroups}
              disabled={isSavingGroups || isLoadingGroups}
              className="w-full sm:w-auto"
              aria-label="Save group changes"
            >
              {isSavingGroups ? (
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

      {/* Manage Organizations Sheet */}
      <Sheet open={orgsSheetOpen} onOpenChange={setOrgsSheetOpen}>
        <SheetContent
          className="sm:max-w-[400px] flex flex-col"
          aria-labelledby="orgs-title"
          aria-describedby="orgs-desc"
        >
          <SheetHeader>
            <SheetTitle id="orgs-title" className="flex items-center gap-2">
              <Building2 className="h-5 w-5" aria-hidden="true" />
              Manage Organizations
            </SheetTitle>
            <SheetDescription id="orgs-desc">
              Manage organization memberships for <strong>{userForOrgs?.username}</strong>. Select organizations
              to add or remove from this user.
            </SheetDescription>
          </SheetHeader>

          <div className="flex-1 flex flex-col py-4 min-h-0">
            <div className="relative mb-4">
              <Search
                className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                placeholder="Search organizations..."
                value={orgsSearchQuery}
                onChange={(e) => setOrgsSearchQuery(e.target.value)}
                className="pl-10"
                aria-label="Search organizations"
                disabled={isLoadingOrgs}
              />
            </div>

            <ScrollArea className="flex-1 -mx-6 px-6">
              {isLoadingOrgs ? (
                <div className="space-y-3">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="flex items-center gap-3 p-3 rounded-sm border">
                      <Skeleton className="h-4 w-4" />
                      <div className="flex-1 space-y-2">
                        <Skeleton className="h-4 w-32" />
                        <Skeleton className="h-3 w-24" />
                      </div>
                    </div>
                  ))}
                </div>
              ) : filteredOrgs.length === 0 ? (
                <div
                  className="text-center py-8 text-muted-foreground"
                  role="status"
                  aria-live="polite"
                >
                  {orgsSearchQuery ? "No organizations match your search" : "No organizations available"}
                </div>
              ) : (
                <div className="space-y-2 pr-4">
                  {filteredOrgs.map((org) => {
                    const isMember = orgMemberships.has(org.id);
                    const role = orgMemberships.get(org.id) ?? "member";
                    return (
                      <div
                        key={org.id}
                        className={`flex items-start space-x-3 rounded-sm border p-3 transition-colors ${isMember ? "border-primary/50 bg-primary/5" : "hover:bg-muted/50"}`}
                      >
                        <Checkbox
                          id={`org-${org.id}`}
                          checked={isMember}
                          onCheckedChange={() => toggleOrg(org.id)}
                          aria-label={`Select ${org.name}`}
                          disabled={isSavingOrgs}
                          className="mt-0.5"
                        />
                        <div className="flex-1 min-w-0">
                          <Label htmlFor={`org-${org.id}`} className="font-medium cursor-pointer block">
                            {org.name}
                          </Label>
                          {org.description && (
                            <div className="text-sm text-muted-foreground line-clamp-1">{org.description}</div>
                          )}
                          <div className="mt-1.5">
                            <Select
                              value={role}
                              onValueChange={(v) => setOrgRole(org.id, v)}
                              disabled={!isMember || isSavingOrgs}
                            >
                              <SelectTrigger className="h-7 w-24 text-xs" aria-label={`Role in ${org.name}`}>
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="member" className="text-xs">Member</SelectItem>
                                <SelectItem value="admin" className="text-xs">Admin</SelectItem>
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
              {orgMemberships.size} organization{orgMemberships.size !== 1 ? "s" : ""} selected
            </div>
          </div>

          <SheetFooter className="flex-col gap-2 sm:flex-row border-t pt-4">
            <Button
              type="button"
              variant="outline"
              onClick={closeOrgsSheet}
              disabled={isSavingOrgs}
              className="w-full sm:w-auto"
            >
              Cancel
            </Button>
            <Button
              onClick={handleSaveOrgs}
              disabled={isSavingOrgs || isLoadingOrgs}
              className="w-full sm:w-auto"
              aria-label="Save organization changes"
            >
              {isSavingOrgs ? (
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

      {/* Create User Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent
          className="sm:max-w-[425px]"
          aria-labelledby="create-title"
          aria-describedby="create-desc"
        >
          <DialogHeader>
            <DialogTitle id="create-title">Create New User</DialogTitle>
            <DialogDescription id="create-desc">
              Add a new user to the system.
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleCreateUser();
            }}
          >
            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label htmlFor="create-username">
                  Username
                  <span className="text-destructive">*</span>
                </Label>
                <Input
                  id="create-username"
                  placeholder="Enter username"
                  value={createUsername}
                  onChange={(e) => setCreateUsername(e.target.value)}
                  disabled={isCreatingUser}
                  required
                  minLength={3}
                  aria-describedby="username-hint"
                />
                <p id="username-hint" className="text-xs text-muted-foreground">
                  Minimum 3 characters
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="create-fullname">Full Name</Label>
                <Input
                  id="create-fullname"
                  placeholder="Full name (optional)"
                  value={createFullName}
                  onChange={(e) => setCreateFullName(e.target.value)}
                  disabled={isCreatingUser}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="create-password">
                  Password
                  <span className="text-destructive">*</span>
                </Label>
                <Input
                  id="create-password"
                  type="password"
                  placeholder="Enter password"
                  value={createPassword}
                  onChange={(e) => setCreatePassword(e.target.value)}
                  disabled={isCreatingUser}
                  required
                  minLength={8}
                  aria-describedby="password-requirements"
                />
                <p id="password-requirements" className="text-xs text-muted-foreground">
                  Min 8 characters, at least 1 digit and 1 uppercase letter
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="create-role">Role</Label>
                <Select
                  value={createRole}
                  onValueChange={(value) => setCreateRole(value as UserRole)}
                  disabled={isCreatingUser}
                >
                  <SelectTrigger id="create-role">
                    <SelectValue placeholder="Select a role" />
                  </SelectTrigger>
                  <SelectContent>
                    {ROLE_OPTIONS.filter((r) => r.value !== "superadmin" || isSuperAdmin).map((r) => (
                      <SelectItem key={r.value} value={r.value}>
                        {r.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <DialogFooter className="flex-col gap-2 sm:flex-row">
              <Button
                type="button"
                variant="outline"
                onClick={() => setCreateDialogOpen(false)}
                disabled={isCreatingUser}
                className="w-full sm:w-auto"
              >
                Cancel
              </Button>
              <Button type="submit" disabled={isCreatingUser} className="w-full sm:w-auto">
                {isCreatingUser ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                    Creating...
                  </>
                ) : (
                  "Create User"
                )}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function AdminUsersPage() {
  return (
    <AdminGuard>
      <AdminUsersPageContent />
    </AdminGuard>
  );
}

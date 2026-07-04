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
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { LoadingSpinner } from "@/components/LoadingSpinner";
import { Pagination } from "@/components/ui/pagination";
import {
  Search,
  Trash2,
  Users,
  Pencil,
  KeyRound,
  Plus,
  Building2,
  ChevronUp,
  ChevronDown,
} from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/EmptyState";

import { DeleteUserDialog } from "./AdminUsersPage/DeleteUserDialog";
import { EditUserDialog } from "./AdminUsersPage/EditUserDialog";
import { ResetPasswordDialog } from "./AdminUsersPage/ResetPasswordDialog";
import { ManageGroupsSheet } from "./AdminUsersPage/ManageGroupsSheet";
import { ManageOrgsSheet } from "./AdminUsersPage/ManageOrgsSheet";
import { CreateUserDialog } from "./AdminUsersPage/CreateUserDialog";
import type { User, UserRole, Group, OrgItem } from "./AdminUsersPage/types";

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
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(20);
  const [totalCount, setTotalCount] = useState(0);

  const currentUser = useAuthStore((state) => state.user);

  // Delete Dialog State
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [userToDelete, setUserToDelete] = useState<User | null>(null);

  // Edit Dialog State
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [userToEdit, setUserToEdit] = useState<User | null>(null);

  // Password Reset Dialog State
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [userToResetPassword, setUserToResetPassword] = useState<User | null>(null);

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
  const [orgMemberships, setOrgMemberships] = useState<Map<number, string>>(new Map());
  const [orgsSearchQuery, setOrgsSearchQuery] = useState("");
  const [isLoadingOrgs, setIsLoadingOrgs] = useState(false);
  const [isSavingOrgs, setIsSavingOrgs] = useState(false);

  // Create User Dialog State
  const [createDialogOpen, setCreateDialogOpen] = useState(false);

  const fetchUsers = useCallback(async () => {
    if (testMode) {
      setUsers(mockAdminUsers);
      setTotalCount(mockAdminUsers.length);
      return;
    }
    setLoading(true);
    try {
      const skip = (page - 1) * limit;
      const response = await apiClient.get<{ users: User[]; total: number }>(
        `/users/?skip=${skip}&limit=${limit}&q=${encodeURIComponent(searchQuery)}`
      );
      setUsers(response.data.users);
      setTotalCount(response.data.total);
    } catch (err) {
      console.error("Failed to fetch users:", err);
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to load users");
    } finally {
      setLoading(false);
    }
  }, [testMode, page, limit, searchQuery]);

  useEffect(() => {
    if (!testMode) fetchUsers();
  }, [fetchUsers, testMode, page, limit]);

  const handleRoleChange = async (userId: number, newRole: UserRole) => {
    setUpdatingUserId(userId);
    try {
      await apiClient.patch(`/users/${userId}/role`, { role: newRole });
      setUsers((prev) => prev.map((u) => (u.id === userId ? { ...u, role: newRole } : u)));
      toast.success("Role updated successfully");
    } catch (err) {
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to update role");
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
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to update user status");
    } finally {
      setUpdatingUserId(null);
    }
  };

  // --- Delete User ---

  const handleDeleteUser = async (user: User): Promise<void> => {
    try {
      await apiClient.delete(`/users/${user.id}`);
      setUsers((prev) => prev.filter((u) => u.id !== user.id));
      toast.success("User deleted successfully");
      setDeleteDialogOpen(false);
      setUserToDelete(null);
    } catch (err) {
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to delete user");
    }
  };

  // --- Edit User ---

  const openEditDialog = (user: User) => {
    setUserToEdit(user);
    setEditDialogOpen(true);
  };

  const closeEditDialog = () => {
    setEditDialogOpen(false);
    setUserToEdit(null);
  };

  const handleSaveEdit = async (user: User, fullName: string, role: UserRole): Promise<void> => {
    try {
      await apiClient.patch(`/users/${user.id}`, {
        full_name: fullName,
        role: role,
      });
      setUsers((prev) =>
        prev.map((u) =>
          u.id === user.id ? { ...u, full_name: fullName, role: role } : u
        )
      );
      toast.success("User updated successfully");
      closeEditDialog();
    } catch (err) {
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to update user");
    }
  };

  // --- Password Reset ---

  const openPasswordDialog = (user: User) => {
    setUserToResetPassword(user);
    setPasswordDialogOpen(true);
  };

  const handleResetPassword = async (user: User, newPassword: string): Promise<void> => {
    try {
      await apiClient.patch(`/users/${user.id}/password`, {
        new_password: newPassword,
      });
      toast.success("Password reset successfully");
      setPasswordDialogOpen(false);
      setUserToResetPassword(null);
    } catch (err) {
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to reset password");
    }
  };

  // --- Manage Groups ---

  const fetchAllGroups = async () => {
    try {
      const response = await apiClient.get<{ groups: Group[] }>("/groups");
      setAllGroups(response.data.groups);
    } catch (err) {
      console.error("Failed to fetch groups:", err);
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to load groups");
    }
  };

  const fetchUserGroups = async (userId: number) => {
    try {
      const response = await apiClient.get<{ groups: Group[] }>(`/users/${userId}/groups`);
      setSelectedGroupIds(response.data.groups.map((g) => g.id));
    } catch (err) {
      console.error("Failed to fetch user groups:", err);
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to load user groups");
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

  const handleSaveGroups = async (): Promise<void> => {
    if (!userForGroups) return;
    setIsSavingGroups(true);
    try {
      await apiClient.put(`/users/${userForGroups.id}/groups`, {
        group_ids: selectedGroupIds,
      });
      toast.success("Groups updated successfully");
      closeGroupsSheet();
    } catch (err) {
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to update groups");
    } finally {
      setIsSavingGroups(false);
    }
  };

  // --- Manage Organizations ---

  const fetchAllOrgs = async () => {
    try {
      const response = await apiClient.get<{ organizations: OrgItem[]; total: number }>("/organizations/");
      setAllOrgs(Array.isArray(response.data) ? response.data : response.data.organizations ?? []);
    } catch (err) {
      console.error("Failed to fetch organizations:", err);
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to load organizations");
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
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to load user organizations");
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

  const handleSaveOrgs = async (): Promise<void> => {
    if (!userForOrgs) return;
    setIsSavingOrgs(true);
    try {
      const memberships = Array.from(orgMemberships.entries()).map(([org_id, role]) => ({ org_id, role }));
      await apiClient.put(`/users/${userForOrgs.id}/organizations`, { memberships });
      toast.success("Organizations updated successfully");
      closeOrgsSheet();
    } catch (err) {
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to update organizations");
    } finally {
      setIsSavingOrgs(false);
    }
  };

  // --- Create User ---

  const handleCreateUser = async (
    username: string,
    fullName: string,
    password: string,
    role: UserRole
  ): Promise<void> => {
    try {
      await apiClient.post("/users/", {
        username,
        password,
        full_name: fullName,
        role,
      });
      toast.success(`User "${username}" created successfully`);
      setCreateDialogOpen(false);
      fetchUsers();
    } catch (err) {
      const detail = (err as any)?.originalError?.response?.data?.detail || (err as any)?.response?.data?.detail;
      toast.error(detail || "Failed to create user");
    }
  };

  const formatDate = (dateStr: string) => new Date(dateStr).toLocaleDateString();
  const isSuperAdmin = currentUser?.role === "superadmin";
  const canDeleteUser = (user: User) => isSuperAdmin && user.id !== currentUser?.id;
  const canManageUser = (user: User) => user.id !== currentUser?.id;

  // Defensive: guard against API returning users: null
  const safeUsers = users ?? [];

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
            onChange={(e) => { setSearchQuery(e.target.value); setPage(1); }}
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
              ) : safeUsers.length === 0 ? (
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
                safeUsers.map((user) => (
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

        {/* Pagination */}
        <Pagination
          page={page}
          limit={limit}
          total={totalCount}
          onPageChange={setPage}
          onLimitChange={(newLimit: number) => {
            setLimit(newLimit);
            setPage(1);
          }}
          isLoading={loading}
        />
      </div>

      {/* Delete User Dialog */}
      <DeleteUserDialog
        open={deleteDialogOpen}
        user={userToDelete}
        onDelete={handleDeleteUser}
        onOpenChange={setDeleteDialogOpen}
        onClose={() => { setDeleteDialogOpen(false); setUserToDelete(null); }}
      />

      {/* Edit User Dialog */}
      <EditUserDialog
        open={editDialogOpen}
        user={userToEdit}
        onSave={handleSaveEdit}
        onClose={closeEditDialog}
        isSuperAdmin={isSuperAdmin}
      />

      {/* Password Reset Dialog */}
      <ResetPasswordDialog
        open={passwordDialogOpen}
        user={userToResetPassword}
        onResetPassword={handleResetPassword}
        onOpenChange={setPasswordDialogOpen}
        onClose={() => { setPasswordDialogOpen(false); setUserToResetPassword(null); }}
      />

      {/* Manage Groups Sheet */}
      <ManageGroupsSheet
        open={groupsSheetOpen}
        user={userForGroups}
        allGroups={allGroups}
        selectedGroupIds={selectedGroupIds}
        isLoading={isLoadingGroups}
        isSaving={isSavingGroups}
        searchQuery={groupsSearchQuery}
        onSearchChange={setGroupsSearchQuery}
        onToggleGroup={toggleGroup}
        onSave={handleSaveGroups}
        onClose={closeGroupsSheet}
      />

      {/* Manage Organizations Sheet */}
      <ManageOrgsSheet
        open={orgsSheetOpen}
        user={userForOrgs}
        allOrgs={allOrgs}
        orgMemberships={orgMemberships}
        isLoading={isLoadingOrgs}
        isSaving={isSavingOrgs}
        searchQuery={orgsSearchQuery}
        onSearchChange={setOrgsSearchQuery}
        onToggleOrg={toggleOrg}
        onSetOrgRole={setOrgRole}
        onSave={handleSaveOrgs}
        onClose={closeOrgsSheet}
      />

      {/* Create User Dialog */}
      <CreateUserDialog
        open={createDialogOpen}
        onCreate={handleCreateUser}
        onOpenChange={setCreateDialogOpen}
        isSuperAdmin={isSuperAdmin}
      />
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

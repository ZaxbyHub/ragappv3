import { useState, useEffect, useCallback, useRef, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { AdminGuard } from "@/components/auth/RoleGuard";
import { useAuthStore } from "@/stores/useAuthStore";
import apiClient from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockAdminUsers } from "@/fixtures/users";
import { useDebounce } from "@/hooks/useDebounce";
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
import { roleOptionsFor } from "./AdminUsersPage/roleOptions";
import type { User, UserRole, Group, OrgItem } from "./AdminUsersPage/types";

import { captureAuthOwner, captureAuthPrincipalGeneration, isCurrentAuthOwner, onAuthOwnerReplacement, subscribeAuthPrincipal, type AuthOwner } from "@/lib/api/auth-lifecycle";

type ReadStatus = "unknown" | "pending" | "success" | "error";
type DialogKind = "create" | "edit" | "delete" | "password";
interface DialogOpening { kind: DialogKind; token: number; user: User | null; mutation: object | null; }
interface MembershipOpening {
  token: number;
  user: User;
  catalogStatus: ReadStatus;
  membershipsStatus: ReadStatus;
  catalogAttempt: object | null;
  membershipsAttempt: object | null;
  catalog: (Group | OrgItem)[];
  draft: Map<number, string>;
  search: string;
  save: object | null;
}
const subscribeAuthOwner = (listener: () => void) => onAuthOwnerReplacement(() => listener());
function errorDetail(error: unknown, fallback: string): string {
  const value = error as { originalError?: { response?: { data?: { detail?: unknown } } }; response?: { data?: { detail?: unknown } } };
  const detail = value?.originalError?.response?.data?.detail || value?.response?.data?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

function snapshotMembershipOpening(value: MembershipOpening): MembershipOpening {
  return { ...value, catalog: [...value.catalog], draft: new Map(value.draft) };
}

function snapshotDialogOpening(value: DialogOpening): DialogOpening {
  return { ...value };
}

// Each callback captures this opening, never whichever opening replaced it.
function useMembershipEditor(kind: "groups" | "organizations", isCurrentSession: () => boolean) {
  const nextToken = useRef(0);
  const openingRef = useRef<MembershipOpening | null>(null);
  const [opening, setOpening] = useState<MembershipOpening | null>(null);
  const current = (value: MembershipOpening | null): value is MembershipOpening =>
    value !== null && isCurrentSession() && openingRef.current === value;
  const publish = (value: MembershipOpening) => {
    setOpening(previous => current(value) ? snapshotMembershipOpening(value) : previous);
  };
  useEffect(() => () => { openingRef.current = null; }, []);
  const read = async (value: MembershipOpening, catalog: boolean): Promise<void> => {
    if (!current(value)) return;
    const attempt = {};
    if (catalog) { value.catalogAttempt = attempt; value.catalogStatus = "pending"; }
    else { value.membershipsAttempt = attempt; value.membershipsStatus = "pending"; }
    const ownsRead = () => current(value) &&
      (catalog ? value.catalogAttempt : value.membershipsAttempt) === attempt;
    publish(value);
    try {
      const response = await apiClient.get(
        catalog ? (kind === "groups" ? "/groups" : "/organizations/") : `/users/${value.user.id}/${kind}`
      );
      if (!ownsRead()) return;
      const data: unknown = response.data;
      const entries = kind === "groups"
        ? (data as { groups?: unknown } | null)?.groups
        : Array.isArray(data) ? data : (data as { organizations?: unknown } | null)?.organizations;
      if (!Array.isArray(entries) || !entries.every(entry =>
        entry && Number.isInteger(entry.id) && (!catalog || typeof entry.name === "string") &&
        (catalog || entry.role === undefined || typeof entry.role === "string")
      )) throw new Error("Invalid membership response");
      if (catalog) { value.catalog = entries; value.catalogStatus = "success"; }
      else {
        value.draft = new Map(entries.map(entry => [entry.id, entry.role || "member"]));
        value.membershipsStatus = "success";
      }
      publish(value);
    } catch (error) {
      if (!ownsRead()) return;
      if (catalog) value.catalogStatus = "error"; else value.membershipsStatus = "error";
      publish(value);
      const label = catalog ? kind : `user ${kind}`;
      console.error(`Failed to fetch ${label}:`, error);
      toast.error(errorDetail(error, `Failed to load ${label}`));
    }
  };
  const open = (user: User) => {
    if (!isCurrentSession()) return;
    const value: MembershipOpening = {
      token: ++nextToken.current, user, catalogStatus: "unknown", membershipsStatus: "unknown",
      catalogAttempt: null, membershipsAttempt: null, catalog: [], draft: new Map(), search: "", save: null,
    };
    openingRef.current = value;
    setOpening(snapshotMembershipOpening(value));
    void read(value, true);
    void read(value, false);
  };
  const close = (value: MembershipOpening | null = openingRef.current) => {
    const active = openingRef.current;
    if (!value || !active || !isCurrentSession() || value.token !== active.token) return;
    openingRef.current = null;
    setOpening(previous => previous?.token === value.token ? null : previous);
  };
  const ready = (value: MembershipOpening | null): value is MembershipOpening => current(value) &&
    value.catalogStatus === "success" && value.membershipsStatus === "success";
  const retry = () => {
    const value = openingRef.current;
    if (!opening || value?.token !== opening.token || !current(value) || value.save) return;
    if (value.catalogStatus === "error" || value.catalogStatus === "unknown") void read(value, true);
    if (value.membershipsStatus === "error" || value.membershipsStatus === "unknown") void read(value, false);
  };
  const save = async (): Promise<void> => {
    const value = openingRef.current;
    if (!opening || value?.token !== opening.token || !ready(value) || value.save) return;
    const attempt = {};
    value.save = attempt;
    const payload = kind === "groups"
      ? { group_ids: Array.from(value.draft.keys()) }
      : { memberships: Array.from(value.draft, ([org_id, role]) => ({ org_id, role })) };
    publish(value);
    try {
      await apiClient.put(`/users/${value.user.id}/${kind}`, payload);
      if (!isCurrentSession()) return;
      toast.success(kind === "groups" ? "Groups updated successfully" : "Organizations updated successfully");
      if (current(value) && value.save === attempt) close(value);
    } catch (error) {
      if (isCurrentSession()) toast.error(errorDetail(error, `Failed to update ${kind}`));
    } finally {
      if (current(value) && value.save === attempt) { value.save = null; publish(value); }
    }
  };
  const toggle = (id: number) => {
    const value = openingRef.current;
    if (!opening || value?.token !== opening.token || !ready(value) || value.save || !value.catalog.some(entry => entry.id === id)) return;
    const draft = new Map(value.draft);
    if (draft.has(id)) draft.delete(id); else draft.set(id, "member");
    value.draft = draft;
    publish(value);
  };
  const setRole = (id: number, role: string) => {
    const value = openingRef.current;
    if (!opening || value?.token !== opening.token || !ready(value) || value.save || !value.draft.has(id) || !["member", "admin"].includes(role)) return;
    value.draft = new Map(value.draft).set(id, role);
    publish(value);
  };
  const search = (query: string) => {
    const value = openingRef.current;
    if (!opening || value?.token !== opening.token || !current(value)) return;
    value.search = query;
    publish(value);
  };
  return { opening, open, close: () => close(opening), retry, save, toggle, setRole, search };
}

function AdminUsersPageContent({ authOwner, principalGeneration }: { authOwner: AuthOwner; principalGeneration: number }) {
  const session = useRef({ authOwner, principalGeneration }).current;
  const mountedRef = useRef(false);
  const isCurrentPrincipal = useCallback(() =>
    isCurrentAuthOwner(session.authOwner) &&
    captureAuthPrincipalGeneration() === session.principalGeneration, [session]);
  const isCurrentSession = useCallback(() => mountedRef.current && isCurrentPrincipal(), [isCurrentPrincipal]);
  const testMode = useTestMode();
  const [users, setUsers] = useState<User[]>(testMode ? mockAdminUsers : []);
  const [loading, setLoading] = useState(!testMode);
  const [usersError, setUsersError] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearchQuery] = useDebounce(searchQuery, 300);
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(20);
  const [totalCount, setTotalCount] = useState(testMode ? mockAdminUsers.length : 0);
  const [queryEpoch, setQueryEpoch] = useState(0);
  const queryIntent = useRef({ page: 1, limit: 20, search: "", epoch: 0 });
  const usersAttempt = useRef<object | null>(null);
  const rowAttempts = useRef(new Map<number, { pending: boolean }>());
  const rowMutationRecords = useRef(new Map<number, object>());
  const [pendingUserIds, setPendingUserIds] = useState(new Set<number>());
  const currentUser = useAuthStore(state => state.user);
  const dialogRef = useRef<DialogOpening | null>(null);
  const dialogToken = useRef(0);
  const [dialog, setDialog] = useState<DialogOpening | null>(null);
  useEffect(() => {
    const attempts = rowAttempts.current;
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      usersAttempt.current = null;
      attempts.clear();
      dialogRef.current = null;
    };
  }, []);

  const queryIsCurrent = useCallback(() => isCurrentSession() &&
    queryIntent.current.epoch === queryEpoch && queryIntent.current.page === page &&
    queryIntent.current.limit === limit && queryIntent.current.search === debouncedSearchQuery,
  [isCurrentSession, queryEpoch, page, limit, debouncedSearchQuery]);
  const fetchUsers = useCallback(async () => {
    if (!queryIsCurrent()) return;
    const attempt = {};
    usersAttempt.current = attempt;
    const ownsRead = () => queryIsCurrent() && usersAttempt.current === attempt;
    setLoading(true);
    setUsersError(false);
    try {
      const response = testMode ? { data: { users: mockAdminUsers, total: mockAdminUsers.length } } :
        await apiClient.get<{ users: User[]; total: number }>(
          `/users/?skip=${(page - 1) * limit}&limit=${limit}&q=${encodeURIComponent(debouncedSearchQuery)}`
        );
      if (!ownsRead()) return;
      if (!Array.isArray(response.data?.users) || !Number.isInteger(response.data?.total) || response.data.total < 0)
        throw new Error("Invalid users response");
      const data = response.data;
      setUsers(previous => ownsRead() ? data.users : previous);
      setTotalCount(previous => ownsRead() ? data.total : previous);
    } catch (error) {
      if (!ownsRead()) return;
      setUsersError(previous => ownsRead() ? true : previous);
      console.error("Failed to fetch users:", error);
      toast.error(errorDetail(error, "Failed to load users"));
    } finally {
      if (ownsRead()) setLoading(previous => ownsRead() ? false : previous);
    }
  }, [queryIsCurrent, testMode, page, limit, debouncedSearchQuery]);
  const latestFetchUsers = useRef(fetchUsers);
  latestFetchUsers.current = fetchUsers;
  useEffect(() => { void fetchUsers(); }, [fetchUsers]);
  const changeQuery = (next: { page: number; limit: number; search: string }) => {
    if (!isCurrentSession() || queryIntent.current.epoch !== queryEpoch) return;
    const epoch = queryIntent.current.epoch + 1;
    queryIntent.current = { ...next, epoch };
    usersAttempt.current = null;
    rowAttempts.current.clear();
    setPendingUserIds(new Set());
    setUsersError(false);
    setLoading(true);
    setPage(next.page); setLimit(next.limit); setSearchQuery(next.search); setQueryEpoch(epoch);
  };
  const changePage = (nextPage: number) => changeQuery({ page: nextPage, limit, search: searchQuery });
  const changeLimit = (nextLimit: number) => changeQuery({ page: 1, limit: nextLimit, search: searchQuery });
  const changeSearch = (search: string) => changeQuery({ page: 1, limit, search });

  const runRowMutation = async (userId: number, work: () => Promise<unknown>,
    apply: (user: User) => User, success: string, failure: string): Promise<void> => {
    if (!queryIsCurrent() || rowAttempts.current.get(userId)?.pending) return;
    const attempt = { pending: true };
    const mutationRecord = {};
    rowAttempts.current.set(userId, attempt);
    rowMutationRecords.current.set(userId, mutationRecord);
    const ownsRowAttempt = () => queryIsCurrent() && rowAttempts.current.get(userId) === attempt;
    const ownsLogicalMutation = () => isCurrentPrincipal() && rowMutationRecords.current.get(userId) === mutationRecord;
    setPendingUserIds(previous => ownsRowAttempt() ? new Set(previous).add(userId) : previous);
    try {
      await work();
      if (ownsRowAttempt()) {
        setUsers(previous => ownsRowAttempt() ? previous.map(user => user.id === userId ? apply(user) : user) : previous);
      }
      if (ownsLogicalMutation()) toast.success(success);
    } catch (error) {
      if (ownsLogicalMutation()) toast.error(errorDetail(error, failure));
    } finally {
      if (rowMutationRecords.current.get(userId) === mutationRecord) {
        rowMutationRecords.current.delete(userId);
      }
      if (ownsRowAttempt()) {
        attempt.pending = false;
        setPendingUserIds(previous => {
          if (!queryIsCurrent() || rowAttempts.current.get(userId) !== attempt) return previous;
          const next = new Set(previous); next.delete(userId); return next;
        });
      }
    }
  };
  const handleRoleChange = (id: number, role: UserRole) => runRowMutation(id,
    () => apiClient.patch(`/users/${id}/role`, { role }), user => ({ ...user, role }),
    "Role updated successfully", "Failed to update role");
  const handleActiveToggle = (id: number, is_active: boolean) => runRowMutation(id,
    () => apiClient.patch(`/users/${id}/active`, { is_active }), user => ({ ...user, is_active }),
    `User ${is_active ? "activated" : "deactivated"} successfully`, "Failed to update user status");

  const openDialog = (kind: DialogKind, user: User | null = null) => {
    if (!isCurrentSession() || queryIntent.current.epoch !== queryEpoch || (kind !== "create" && !user)) return;
    const value: DialogOpening = { kind, user, token: ++dialogToken.current, mutation: null };
    dialogRef.current = value;
    setDialog(snapshotDialogOpening(value));
  };
  const currentDialog = (value: DialogOpening | null): value is DialogOpening =>
    value !== null && isCurrentSession() && dialogRef.current === value;
  const publishDialog = (value: DialogOpening) => {
    setDialog(previous => currentDialog(value) ? snapshotDialogOpening(value) : previous);
  };
  const closeDialog = (value: DialogOpening | null = dialogRef.current) => {
    const active = dialogRef.current;
    if (!value || !active || !isCurrentSession() || value.token !== active.token) return;
    dialogRef.current = null;
    setDialog(previous => previous?.token === value.token ? null : previous);
  };
  const runDialogMutation = async (kind: DialogKind, userId: number | null,
    work: () => Promise<unknown>, apply: ((users: User[]) => User[]) | null,
    success: string, failure: string): Promise<void> => {
    const value = dialogRef.current;
    if (!dialog || value?.token !== dialog.token || !currentDialog(value) || value.kind !== kind || (value.user?.id ?? null) !== userId || value.mutation) return;
    const attempt = {};
    value.mutation = attempt;
    publishDialog(value);
    try {
      await work();
      if (!isCurrentSession()) return;
      if (apply) setUsers(previous => isCurrentSession() ? apply(previous) : previous);
      toast.success(success);
      if (currentDialog(value) && value.mutation === attempt) closeDialog(value);
      if (kind === "create" && isCurrentSession()) void latestFetchUsers.current();
    } catch (error) {
      if (isCurrentSession()) toast.error(errorDetail(error, failure));
    } finally {
      if (currentDialog(value) && value.mutation === attempt) {
        value.mutation = null;
        publishDialog(value);
      }
    }
  };
  const openEditDialog = (user: User) => openDialog("edit", user);
  const closeEditDialog = () => closeDialog(dialog);
  const openPasswordDialog = (user: User) => openDialog("password", user);
  const handleDeleteUser = (user: User) => runDialogMutation("delete", user.id,
    () => apiClient.delete(`/users/${user.id}`), previous => previous.filter(value => value.id !== user.id),
    "User deleted successfully", "Failed to delete user");
  const handleSaveEdit = (user: User, fullName: string, role: UserRole) => runDialogMutation("edit", user.id,
    () => apiClient.patch(`/users/${user.id}`, { full_name: fullName, role }),
    previous => previous.map(value => value.id === user.id ? { ...value, full_name: fullName, role } : value),
    "User updated successfully", "Failed to update user");
  const handleResetPassword = (user: User, newPassword: string) => runDialogMutation("password", user.id,
    () => apiClient.patch(`/users/${user.id}/password`, { new_password: newPassword }), null,
    "Password reset successfully", "Failed to reset password");
  const handleCreateUser = (username: string, fullName: string, password: string, role: UserRole) =>
    runDialogMutation("create", null, () => apiClient.post("/users/", { username, password, full_name: fullName, role }),
      null, `User "${username}" created successfully`, "Failed to create user");
  const deleteDialogOpen = dialog?.kind === "delete", userToDelete = deleteDialogOpen ? dialog.user : null;
  const editDialogOpen = dialog?.kind === "edit", userToEdit = editDialogOpen ? dialog.user : null;
  const passwordDialogOpen = dialog?.kind === "password", userToResetPassword = passwordDialogOpen ? dialog.user : null;
  const createDialogOpen = dialog?.kind === "create";

  const groups = useMembershipEditor("groups", isCurrentSession);
  const orgs = useMembershipEditor("organizations", isCurrentSession);
  const groupOpening = groups.opening, orgOpening = orgs.opening;
  const groupsSheetOpen = groupOpening !== null, userForGroups = groupOpening?.user ?? null;
  const allGroups = (groupOpening?.catalog ?? []) as Group[];
  const selectedGroupIds = Array.from(groupOpening?.draft.keys() ?? []);
  const groupsSearchQuery = groupOpening?.search ?? "";
  const isLoadingGroups = !!groupOpening && [groupOpening.catalogStatus, groupOpening.membershipsStatus].some(value => value === "pending" || value === "unknown");
  const isSavingGroups = !!groupOpening?.save;
  const openGroupsSheet = (user: User) => { if (queryIntent.current.epoch === queryEpoch) groups.open(user); };
  const closeGroupsSheet = groups.close, toggleGroup = groups.toggle, handleSaveGroups = groups.save;
  const orgsSheetOpen = orgOpening !== null, userForOrgs = orgOpening?.user ?? null;
  const allOrgs = (orgOpening?.catalog ?? []) as OrgItem[];
  const orgMemberships = orgOpening?.draft ?? new Map<number, string>();
  const orgsSearchQuery = orgOpening?.search ?? "";
  const isLoadingOrgs = !!orgOpening && [orgOpening.catalogStatus, orgOpening.membershipsStatus].some(value => value === "pending" || value === "unknown");
  const isSavingOrgs = !!orgOpening?.save;
  const openOrgsSheet = (user: User) => { if (queryIntent.current.epoch === queryEpoch) orgs.open(user); };
  const closeOrgsSheet = orgs.close, toggleOrg = orgs.toggle, setOrgRole = orgs.setRole, handleSaveOrgs = orgs.save;

  const formatDate = (dateStr: string) => new Date(dateStr).toLocaleDateString();
  const isSuperAdmin = currentUser?.role === "superadmin";
  const canDeleteUser = (user: User) => isSuperAdmin && user.id !== currentUser?.id;
  const canManageUser = (user: User) => user.id !== currentUser?.id;

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <PageTitleHeader
        title="User Management"
        description="Manage system users and their permissions"
        actions={
          <Button onClick={() => openDialog("create")}>
            <Plus className="mr-2 h-4 w-4" />
            Add User
          </Button>
        }
      />
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
            onChange={(e) => { changeSearch(e.target.value); }}
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
              ) : usersError ? (
                <TableRow><TableCell colSpan={6} className="p-8 text-center">
                  <div role="alert" className="mb-3 text-sm text-destructive">Failed to load users.</div>
                  <Button variant="outline" onClick={() => { void fetchUsers(); }}>Retry users</Button>
                </TableCell></TableRow>
              ) : users.length === 0 ? (
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
                users.map((user) => (
                  <TableRow key={user.id}>
                    <TableCell className="p-4 font-medium">{user.username}</TableCell>
                    <TableCell className="p-4">{user.full_name}</TableCell>
                    <TableCell className="p-4">
                      <Select
                        value={user.role}
                        onValueChange={(v) => handleRoleChange(user.id, v as UserRole)}
                        disabled={pendingUserIds.has(user.id) || user.id === currentUser?.id}
                      >
                        <SelectTrigger aria-label={`Change role for ${user.username}`}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {roleOptionsFor(isSuperAdmin ? "superadmin" : "admin", {
                            mode: "edit",
                            targetRole: user.role,
                          }).map((opt) => (
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
                          disabled={pendingUserIds.has(user.id) || user.id === currentUser?.id}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
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
                              openDialog("delete", user);
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
          onPageChange={changePage}
          onLimitChange={changeLimit}
          isLoading={loading}
        />
      </div>

      {/* Delete User Dialog */}
      <DeleteUserDialog
        key={`delete:${dialog?.kind === "delete" ? dialog.token : 0}`}
        open={deleteDialogOpen}
        user={userToDelete}
        onDelete={handleDeleteUser}
        onOpenChange={(open) => { if (!open && dialog?.kind === "delete") closeDialog(dialog); }}
        onClose={() => closeDialog(dialog)}
      />

      {/* Edit User Dialog */}
      <EditUserDialog
        key={`edit:${dialog?.kind === "edit" ? dialog.token : 0}`}
        open={editDialogOpen}
        user={userToEdit}
        onSave={handleSaveEdit}
        onClose={closeEditDialog}
        isSuperAdmin={isSuperAdmin}
      />

      {/* Password Reset Dialog */}
      <ResetPasswordDialog
        key={`password:${dialog?.kind === "password" ? dialog.token : 0}`}
        open={passwordDialogOpen}
        user={userToResetPassword}
        onResetPassword={handleResetPassword}
        onOpenChange={(open) => { if (!open && dialog?.kind === "password") closeDialog(dialog); }}
        onClose={() => closeDialog(dialog)}
      />

      {/* Manage Groups Sheet */}
      <ManageGroupsSheet
        key={`groups:${groupOpening?.token ?? 0}`}
        catalogStatus={groupOpening?.catalogStatus ?? "unknown"}
        membershipsStatus={groupOpening?.membershipsStatus ?? "unknown"}
        onRetry={groups.retry}
        open={groupsSheetOpen}
        user={userForGroups}
        allGroups={allGroups}
        selectedGroupIds={selectedGroupIds}
        isLoading={isLoadingGroups}
        isSaving={isSavingGroups}
        searchQuery={groupsSearchQuery}
        onSearchChange={groups.search}
        onToggleGroup={toggleGroup}
        onSave={handleSaveGroups}
        onClose={closeGroupsSheet}
      />

      {/* Manage Organizations Sheet */}
      <ManageOrgsSheet
        key={`orgs:${orgOpening?.token ?? 0}`}
        catalogStatus={orgOpening?.catalogStatus ?? "unknown"}
        membershipsStatus={orgOpening?.membershipsStatus ?? "unknown"}
        onRetry={orgs.retry}
        open={orgsSheetOpen}
        user={userForOrgs}
        allOrgs={allOrgs}
        orgMemberships={orgMemberships}
        isLoading={isLoadingOrgs}
        isSaving={isSavingOrgs}
        searchQuery={orgsSearchQuery}
        onSearchChange={orgs.search}
        onToggleOrg={toggleOrg}
        onSetOrgRole={setOrgRole}
        onSave={handleSaveOrgs}
        onClose={closeOrgsSheet}
      />

      {/* Create User Dialog */}
      <CreateUserDialog
        key={`create:${dialog?.kind === "create" ? dialog.token : 0}`}
        open={createDialogOpen}
        onCreate={handleCreateUser}
        onOpenChange={(open) => { if (!open && dialog?.kind === "create") closeDialog(dialog); }}
        isSuperAdmin={isSuperAdmin}
      />
    </div>
  );
}

export default function AdminUsersPage() {
  const authOwner = useSyncExternalStore(subscribeAuthOwner, captureAuthOwner, captureAuthOwner);
  const principalGeneration = useSyncExternalStore(subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration);
  return (
    <AdminGuard>
      <AdminUsersPageContent key={`${authOwner.id}:${principalGeneration}`} authOwner={authOwner} principalGeneration={principalGeneration} />
    </AdminGuard>
  );
}
